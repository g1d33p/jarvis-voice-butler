"""Orchestrator tests with a scripted fake model (no API key, no network)."""

import asyncio
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from google.genai import types
from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from actions import ActionRegistry
from browser import BrowserManager
from orchestrator import (
    VOICE_TOOL_NAMES,
    ModelTurn,
    Orchestrator,
    TaskTools,
    _shrink_old_results,
    voice_tools,
)
from task_manager import Task, TaskStore
from tools import BrowserTools


def call(name: str, **args) -> tuple[str, dict]:
    return (name, args)


def turn(*calls, text: str = "", tokens=(10, 5)) -> ModelTurn:
    parts = [
        types.Part(function_call=types.FunctionCall(name=n, args=a)) for n, a in calls
    ]
    if text:
        parts.append(types.Part(text=text))
    return ModelTurn(
        calls=list(calls),
        text=text,
        content=types.Content(role="model", parts=parts),
        tokens_in=tokens[0],
        tokens_out=tokens[1],
    )


class FakeModel:
    """Replays scripted turns and records which model each call used."""

    def __init__(self, *turns) -> None:
        self.turns = list(turns)
        self.models_used: list[str] = []
        self.seen_contents: list[list] = []

    async def generate(self, model, contents, config_):
        self.models_used.append(model)
        self.seen_contents.append(list(contents))
        if not self.turns:
            raise AssertionError("The fake model ran out of scripted turns.")
        next_turn = self.turns.pop(0)
        if isinstance(next_turn, Exception):
            raise next_turn
        return next_turn


class Toys:
    """A tiny toolset: one tool that works and one that always fails."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def tools(self) -> list:
        return [self.echo, self.broken, self.big_page]

    @function_tool()
    async def echo(self, context: RunContext, text: str) -> str:
        """Echo text back.

        Args:
            text: Anything.
        """
        self.calls.append(text)
        return f"echo: {text}"

    @function_tool()
    async def broken(self, context: RunContext) -> str:
        """Always fails."""
        raise ToolError("It broke.")

    @function_tool()
    async def big_page(self, context: RunContext) -> dict:
        """Return a large result."""
        return {"text": "x" * 3000}


def make(model: FakeModel, tmp_path, **kwargs) -> tuple[Orchestrator, Toys]:
    toys = Toys()
    orchestrator = Orchestrator(
        registry=ActionRegistry(toys),
        store=TaskStore(tmp_path / "tasks.db"),
        client=model,
        brain_model="cheap",
        escalation_model="strong",
        **kwargs,
    )
    return orchestrator, toys


# ---------------------------------------------------------------- basics


async def test_runs_tools_then_reports_the_outcome(tmp_path) -> None:
    model = FakeModel(turn(call("echo", text="hi")), turn(text="Echoed hi."))
    orchestrator, toys = make(model, tmp_path)

    task = await orchestrator.start("say hi", context=None)

    assert task.state == "completed"
    assert task.result == "Echoed hi."
    assert toys.calls == ["hi"]
    assert [step.action for step in task.steps] == ["echo"]
    assert (task.tokens_in, task.tokens_out) == (20, 10)
    # The tool result was shown to the model on the second call.
    last = model.seen_contents[1][-1]
    assert last.parts[0].function_response.response["result"] == "echo: hi"


async def test_task_is_saved_and_listed(tmp_path) -> None:
    orchestrator, _ = make(FakeModel(turn(text="Done.")), tmp_path)
    task = await orchestrator.start("nothing much", context=None)

    stored = orchestrator.store.get(task.id)
    assert stored.state == "completed"
    assert orchestrator.store.recent(1)[0].id == task.id


async def test_tool_errors_are_shown_to_the_model_not_raised(tmp_path) -> None:
    model = FakeModel(turn(call("broken")), turn(text="The tool broke, so I stopped."))
    orchestrator, _ = make(model, tmp_path)

    task = await orchestrator.start("try the broken tool", context=None)

    assert task.state == "completed"
    assert task.steps[0].ok is False
    response = model.seen_contents[1][-1].parts[0].function_response.response
    assert response == {"ok": False, "error": "It broke."}


async def test_unknown_tool_is_reported_to_the_model(tmp_path) -> None:
    model = FakeModel(turn(call("launch_rockets")), turn(text="No such tool."))
    orchestrator, _ = make(model, tmp_path)

    task = await orchestrator.start("launch", context=None)

    assert "no tool named" in task.steps[0].summary


async def test_escalates_to_the_stronger_model_after_two_failures(tmp_path) -> None:
    model = FakeModel(
        turn(call("broken")),
        turn(call("broken")),
        turn(call("echo", text="ok")),
        turn(text="Recovered."),
    )
    orchestrator, _ = make(model, tmp_path)

    task = await orchestrator.start("recover", context=None)

    assert model.models_used == ["cheap", "cheap", "strong", "strong"]
    assert task.model == "strong"
    assert task.state == "completed"


async def test_stops_after_the_step_limit(tmp_path) -> None:
    model = FakeModel(*[turn(call("echo", text=str(i))) for i in range(3)])
    orchestrator, _ = make(model, tmp_path, max_steps=3)

    task = await orchestrator.start("loop forever", context=None)

    assert task.state == "failed"
    assert "3 steps" in task.error


async def test_model_failure_fails_the_task_cleanly(tmp_path) -> None:
    orchestrator, _ = make(FakeModel(RuntimeError("quota exceeded")), tmp_path)

    task = await orchestrator.start("anything", context=None)

    assert task.state == "failed"
    assert "quota exceeded" in task.error
    assert orchestrator.store.get(task.id).state == "failed"


async def test_time_limit_stops_the_task(tmp_path) -> None:
    class SlowModel(FakeModel):
        async def generate(self, model, contents, config_):
            await asyncio.sleep(0.2)
            return turn(call("echo", text="again"))

    orchestrator, _ = make(SlowModel(), tmp_path, time_limit=0.3)

    task = await orchestrator.start("slow", context=None)

    assert task.state == "failed"
    assert "too long" in task.error


# ---------------------------------------------------------------- questions


async def test_question_pauses_the_task_and_the_reply_resumes_it(tmp_path) -> None:
    model = FakeModel(
        turn(text="QUESTION: Which chat, Ravi or Priya?"),
        turn(call("echo", text="Ravi")),
        turn(text="Sent to Ravi."),
    )
    orchestrator, toys = make(model, tmp_path)

    task = await orchestrator.start("message someone", context=None)
    assert task.state == "waiting_for_user"
    assert task.question == "Which chat, Ravi or Priya?"
    assert task.summary()["question_for_user"] == "Which chat, Ravi or Priya?"

    task = await orchestrator.resume(task.id, "Ravi", context=None)

    assert task.state == "completed"
    assert toys.calls == ["Ravi"]
    reply_turn = model.seen_contents[1][-1]
    assert "The user replied: 'Ravi'" in reply_turn.parts[0].text


async def test_cannot_resume_a_finished_task(tmp_path) -> None:
    orchestrator, _ = make(FakeModel(turn(text="Done.")), tmp_path)
    task = await orchestrator.start("quick", context=None)

    with pytest.raises(LookupError):
        await orchestrator.resume(task.id, "yes", context=None)


# ---------------------------------------------------------------- cost control


def test_old_large_results_are_shrunk() -> None:
    def response(n: int, size: int) -> types.Content:
        return types.Content(
            role="user",
            parts=[
                types.Part.from_function_response(
                    name="big_page", response={"ok": True, "result": str(n) * size}
                )
            ],
        )

    contents = [
        response(1, 3000),
        response(2, 3000),
        response(3, 3000),
        response(4, 50),
    ]
    _shrink_old_results(contents)

    results = [c.parts[0].function_response.response["result"] for c in contents]
    assert results[0].startswith("(older result omitted")
    assert results[1].startswith("(older result omitted")
    assert results[2] == "3" * 3000  # the latest two stay whole
    assert results[3] == "4" * 50


# ---------------------------------------------------------------- store


def test_unfinished_tasks_are_marked_failed_on_restart(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    task = Task(goal="interrupted")
    task.set_state("running")
    store.save(task)

    restarted = TaskStore(tmp_path / "tasks.db")

    assert restarted.get(task.id).state == "failed"
    assert "restarted" in restarted.get(task.id).error


def test_unknown_state_is_rejected() -> None:
    with pytest.raises(ValueError):
        Task(goal="x").set_state("daydreaming")


# ---------------------------------------------------------------- voice side


def test_voice_keeps_only_quick_tools() -> None:
    browser_tools = BrowserTools(BrowserManager(headless=True))
    names = {tool.id for tool in voice_tools(browser_tools)}

    assert names <= VOICE_TOOL_NAMES
    assert {"open_url", "switch_tab", "close_browser"} <= names
    for multi_step in ("inspect_page", "click", "type_text", "press_key"):
        assert multi_step not in names


def test_registry_describes_every_tool_to_the_model() -> None:
    registry = ActionRegistry(BrowserTools(BrowserManager(headless=True)))
    declared = {declaration.name for declaration in registry.declarations()}

    assert {"inspect_page", "click", "type_text", "confirm_browser_action"} <= declared


async def test_task_tools_return_a_short_summary(tmp_path) -> None:
    orchestrator, _ = make(FakeModel(turn(text="All done.")), tmp_path)

    summary = await TaskTools(orchestrator).run_task(None, "do a thing")

    assert summary["status"] == "completed"
    assert summary["result"] == "All done."


# ---------------------------------------------------------------- real browser


def _history(*user_lines: str) -> SimpleNamespace:
    """A live-updating fake voice session history."""
    import time

    items = [
        SimpleNamespace(
            type="message",
            role="user",
            text_content=t,
            id=f"u{i}",
            created_at=time.time(),
        )
        for i, t in enumerate(user_lines)
    ]
    return SimpleNamespace(
        session=SimpleNamespace(history=SimpleNamespace(items=items))
    )


@pytest.fixture
async def chat_page(tmp_path):
    from test_browser import _ChatPageHandler

    server = ThreadingHTTPServer(("127.0.0.1", 0), _ChatPageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    browser = BrowserManager(headless=True, profile_dir=tmp_path / "profile")
    try:
        await browser.open_url(f"http://127.0.0.1:{server.server_port}/")
        yield browser
    finally:
        await browser.close()
        server.shutdown()
        thread.join(timeout=2)


async def test_end_to_end_dictated_message_is_sent(chat_page, tmp_path) -> None:
    tools = BrowserTools(chat_page)
    model = FakeModel(
        turn(call("inspect_page")),
        turn(call("type_text", target="Type a message", text="Hi")),
        turn(call("press_key", key="Enter")),
        turn(text="Sent 'Hi'."),
    )
    orchestrator = Orchestrator(
        registry=ActionRegistry(tools), store=TaskStore(tmp_path / "t.db"), client=model
    )

    task = await orchestrator.start(
        "send hi in this chat", context=_history("Send hi to the first chat")
    )

    assert task.state == "completed"
    press = task.steps[2]
    assert press.action == "press_key" and press.ok and '"sent": true' in press.summary


async def test_end_to_end_composed_message_asks_then_sends(chat_page, tmp_path) -> None:
    tools = BrowserTools(chat_page)
    context = _history("Send him a friendly note about the meeting")
    model = FakeModel(
        turn(
            call(
                "type_text", target="Type a message", text="Hope the meeting went well"
            )
        ),
        turn(call("press_key", key="Enter")),  # blocked: needs approval
        turn(text="QUESTION: Send 'Hope the meeting went well'?"),
        turn(call("confirm_browser_action", user_reply="yes")),
        turn(text="Sent."),
    )
    orchestrator = Orchestrator(
        registry=ActionRegistry(tools), store=TaskStore(tmp_path / "t.db"), client=model
    )

    task = await orchestrator.start("send a friendly note", context=context)
    assert task.state == "waiting_for_user"
    assert task.steps[1].ok is False  # the gate stopped Enter

    # The user answers out loud; the transcript updates.
    context.session.history.items.append(
        SimpleNamespace(
            type="message", role="user", text_content="Yes.", id="u9", created_at=0
        )
    )
    task = await orchestrator.resume(task.id, "Yes.", context)

    assert task.state == "completed"
    assert task.steps[-1].action == "confirm_browser_action" and task.steps[-1].ok
