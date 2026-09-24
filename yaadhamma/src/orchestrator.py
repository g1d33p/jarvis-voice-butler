"""The task orchestrator: Yaadhamma's background brain (Phase 3).

The voice model hands over a goal ("in WhatsApp, send 'running late' to Ravi").
The orchestrator works through it with a text model in a loop:

    ask the model -> run the tools it chose -> show it the results -> repeat

until the model reports the outcome, asks the user a question, or runs out of
steps. Every task is recorded in the task store with its steps and token use.

The brain is Muse Spark through the Meta Model API (see meta_client.py),
called with OpenAI-style chat messages and function tools.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

import config
from actions import ActionRegistry
from meta_client import MetaBrainClient, ModelTurn
from prompts import ORCHESTRATOR_INSTRUCTIONS
from task_manager import Step, Task, TaskStore

__all__ = [
    "VOICE_TOOL_NAMES",
    "ModelTurn",
    "Orchestrator",
    "TaskTools",
    "voice_tools",
]

# Older tool results bigger than this are replaced with a short note, so each
# model call does not re-send every page the task has looked at.
_KEEP_FULL_RESULTS = 2
_SHRINK_ABOVE_CHARS = 1_200
# The orchestrator stops itself at TASK_TIMEOUT_SECONDS; this is only a safety net.
_BACKSTOP_SECONDS = config.TASK_TIMEOUT_SECONDS + 30

_OMITTED = "(older result omitted to save space; call the tool again if needed)"


# ModelTurn is defined in meta_client.py and re-exported through the import
# above, so `from orchestrator import ModelTurn` keeps working.
def _assistant_message(task_id: str, step_index: int, turn: ModelTurn) -> dict:
    """Rebuild the assistant's reply as an OpenAI chat message.

    Tool calls get synthetic ids; the following "tool" messages reference
    them. The model never sees the ids, so they only need to be consistent
    within one task.
    """
    return {
        "role": "assistant",
        "content": turn.text or None,
        "tool_calls": [
            {
                "id": f"call_{task_id}_{step_index}_{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for i, (name, args) in enumerate(turn.calls)
        ],
    }


@dataclass
class Orchestrator:
    registry: ActionRegistry
    store: TaskStore
    client: object = field(default_factory=MetaBrainClient)
    brain_model: str = config.BRAIN_MODEL
    escalation_model: str = config.ESCALATION_MODEL
    max_steps: int = config.MAX_TASK_STEPS
    time_limit: float = config.TASK_TIMEOUT_SECONDS
    now: Callable[[], datetime] = datetime.now
    # Conversation per unfinished task, so a question can be answered and the
    # task continued from where it stopped.
    _conversations: dict[str, list[dict]] = field(default_factory=dict)

    def _tools(self) -> list[dict]:
        return self.registry.openai_tools()

    async def start(self, goal: str, context: object) -> Task:
        task = Task(goal=goal, model=self.brain_model)
        self.store.save(task)
        opening = f"Task: {goal}\nCurrent local time: {self.now():%A %d %B %Y, %H:%M}."
        self._conversations[task.id] = [
            {"role": "system", "content": ORCHESTRATOR_INSTRUCTIONS},
            {"role": "user", "content": opening},
        ]
        return await self._run(task, context)

    async def resume(self, task_id: str, user_reply: str, context: object) -> Task:
        task = self.store.get(task_id)
        messages = self._conversations.get(task_id)
        if task is None or messages is None or task.state != "waiting_for_user":
            raise LookupError(f"Task {task_id} is not waiting for an answer.")
        task.question = ""
        messages.append(
            {
                "role": "user",
                "content": f"The user replied: {user_reply!r}. Continue the task.",
            }
        )
        return await self._run(task, context)

    async def _run(self, task: Task, context: object) -> Task:
        messages = self._conversations[task.id]
        task.set_state("running")
        self.store.save(task)
        model = task.model or self.brain_model
        effort: str | None = None
        failures_in_a_row = 0
        tools = self._tools()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.time_limit

        for _ in range(self.max_steps):
            if loop.time() > deadline:
                return self._finish(
                    task, "failed", error="The task took too long and was stopped."
                )
            try:
                turn = await asyncio.wait_for(
                    self.client.generate(  # type: ignore[attr-defined]
                        model, messages, tools, reasoning_effort=effort
                    ),
                    timeout=config.MODEL_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                return self._finish(
                    task, "failed", error="The background model took too long."
                )
            except Exception as exc:
                return self._finish(
                    task, "failed", error=f"The background model failed: {exc}"
                )

            task.tokens_in += turn.tokens_in
            task.tokens_out += turn.tokens_out
            messages.append(_assistant_message(task.id, len(task.steps), turn))

            if not turn.calls:
                text = turn.text.strip()
                if text.upper().startswith("QUESTION:"):
                    task.question = text.split(":", 1)[1].strip()
                    task.set_state("waiting_for_user")
                    self.store.save(task)
                    return task
                return self._finish(task, "completed", result=text or "Done.")

            for i, (name, args) in enumerate(turn.calls):
                outcome = await self.registry.call(name, args, context)
                task.steps.append(
                    Step(
                        action=name,
                        args=args,
                        ok=outcome["ok"],
                        summary=_summary(outcome),
                    )
                )
                failures_in_a_row = 0 if outcome["ok"] else failures_in_a_row + 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": f"call_{task.id}_{len(task.steps) - 1}_{i}",
                        "content": json.dumps(outcome, default=str),
                    }
                )

            _shrink_old_results(messages)
            self.store.save(task)

            if failures_in_a_row >= 2 and (
                model != self.escalation_model or effort != config.ESCALATION_EFFORT
            ):
                model = self.escalation_model
                effort = config.ESCALATION_EFFORT
                task.model = model

        return self._finish(
            task,
            "failed",
            error=f"Stopped after {self.max_steps} steps without finishing.",
        )

    def _finish(
        self, task: Task, state: str, *, result: str = "", error: str = ""
    ) -> Task:
        task.result = result
        task.error = error
        task.set_state(state)
        self.store.save(task)
        self._conversations.pop(task.id, None)
        return task


def _summary(outcome: dict) -> str:
    text = outcome.get("error") or json.dumps(outcome.get("result"), default=str)
    return text[:200]


def _shrink_old_results(messages: list[dict]) -> None:
    """Replace all but the latest few large tool results with a short note."""
    positions = [
        i for i, message in enumerate(messages) if message.get("role") == "tool"
    ]
    for i in (
        positions[:-_KEEP_FULL_RESULTS] if len(positions) > _KEEP_FULL_RESULTS else []
    ):
        content = messages[i].get("content") or ""
        if len(content) > _SHRINK_ABOVE_CHARS:
            try:
                ok = json.loads(content).get("ok", True)
            except (json.JSONDecodeError, AttributeError):
                ok = True
            messages[i]["content"] = json.dumps({"ok": ok, "result": _OMITTED})


class TaskTools:
    """Voice tools for handing multi-step work to the orchestrator."""

    def __init__(self, orchestrator: Orchestrator) -> None:
        self.orchestrator = orchestrator

    @property
    def tools(self) -> list:
        return [self.run_task, self.continue_task, self.recent_tasks]

    @function_tool()
    async def run_task(self, context: RunContext, goal: str) -> dict[str, object]:
        """Hand a multi-step job to the background assistant and wait for the outcome.

        Use it for anything that needs several steps or reading and clicking
        inside a page: sending a message, reading a chat, searching the web,
        working with files. Give one clear sentence with every detail the user
        gave (names, exact message text, which chat or file).

        Args:
            goal: The complete task in one sentence.
        """
        try:
            task = await asyncio.wait_for(
                self.orchestrator.start(goal, context),
                timeout=_BACKSTOP_SECONDS,
            )
        except TimeoutError as exc:
            raise ToolError("The task took too long and was stopped.") from exc
        return task.summary()

    @function_tool()
    async def continue_task(
        self, context: RunContext, task_id: str, user_reply: str
    ) -> dict[str, object]:
        """Continue a task that asked the user a question.

        Args:
            task_id: The task_id from run_task.
            user_reply: The user's exact reply to the question.
        """
        try:
            task = await asyncio.wait_for(
                self.orchestrator.resume(task_id, user_reply, context),
                timeout=_BACKSTOP_SECONDS,
            )
        except LookupError as exc:
            raise ToolError(str(exc)) from exc
        except TimeoutError as exc:
            raise ToolError("The task took too long and was stopped.") from exc
        return task.summary()

    @function_tool()
    async def recent_tasks(self, context: RunContext) -> list[dict[str, object]]:
        """List the last few tasks and how they went, newest first."""
        return [
            {"goal": task.goal, **task.summary()}
            for task in self.orchestrator.store.recent(5)
        ]


# Quick, single actions the voice model keeps doing itself (instant replies).
# Everything else goes through run_task.
VOICE_TOOL_NAMES = {
    "open_url",
    "open_tab",
    "list_tabs",
    "switch_tab",
    "close_tab",
    "close_browser",
    "observe_state",
    "list_running_apps",
    "open_application",
    "quit_application",
    "capture_screen",
    "read_clipboard",
    "write_clipboard",
    "open_path",
}


def voice_tools(*toolsets: object) -> list:
    """The subset of tools the voice model calls directly in split mode."""
    return [
        tool
        for toolset in toolsets
        for tool in toolset.tools  # type: ignore[attr-defined]
        if tool.id in VOICE_TOOL_NAMES
    ]
