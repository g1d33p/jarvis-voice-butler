"""The task orchestrator: Sureedu's background brain (Phase 3).

The voice model hands over a goal ("in WhatsApp, send 'running late' to Ravi").
The orchestrator works through it with a cheaper text model in a loop:

    ask the model -> run the tools it chose -> show it the results -> repeat

until the model reports the outcome, asks the user a question, or runs out of
steps. Every task is recorded in the task store with its steps and token use.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from google import genai
from google.genai import types
from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

import config
from actions import ActionRegistry
from prompts import ORCHESTRATOR_INSTRUCTIONS
from task_manager import Step, Task, TaskStore

# Older tool results bigger than this are replaced with a short note, so each
# model call does not re-send every page the task has looked at.
_KEEP_FULL_RESULTS = 2
_SHRINK_ABOVE_CHARS = 1_200
# The orchestrator stops itself at TASK_TIMEOUT_SECONDS; this is only a safety net.
_BACKSTOP_SECONDS = config.TASK_TIMEOUT_SECONDS + 30

_OMITTED = "(older result omitted to save space; call the tool again if needed)"


@dataclass
class ModelTurn:
    """One model reply, reduced to what the orchestrator needs."""

    calls: list[tuple[str, dict]]
    text: str
    content: types.Content
    tokens_in: int = 0
    tokens_out: int = 0


class GeminiClient:
    """Calls the Gemini API. Created lazily so tests never need a key."""

    def __init__(self) -> None:
        self._client: genai.Client | None = None

    async def generate(
        self, model: str, contents: list, config_: types.GenerateContentConfig
    ) -> ModelTurn:
        if self._client is None:
            self._client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY"))
        response = await self._client.aio.models.generate_content(
            model=model, contents=contents, config=config_
        )
        candidate = response.candidates[0] if response.candidates else None
        content = candidate.content if candidate and candidate.content else None
        if content is None:
            content = types.Content(role="model", parts=[types.Part(text="")])
        text = "".join(
            part.text
            for part in content.parts or []
            if part.text and not getattr(part, "thought", False)
        )
        calls = [
            (call.name, dict(call.args or {})) for call in response.function_calls or []
        ]
        usage = response.usage_metadata
        return ModelTurn(
            calls=calls,
            text=text,
            content=content,
            tokens_in=(usage.prompt_token_count or 0) if usage else 0,
            tokens_out=(
                (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
            )
            if usage
            else 0,
        )


@dataclass
class Orchestrator:
    registry: ActionRegistry
    store: TaskStore
    client: object = field(default_factory=GeminiClient)
    brain_model: str = config.BRAIN_MODEL
    escalation_model: str = config.ESCALATION_MODEL
    max_steps: int = config.MAX_TASK_STEPS
    time_limit: float = config.TASK_TIMEOUT_SECONDS
    now: Callable[[], datetime] = datetime.now
    # Conversation per unfinished task, so a question can be answered and the
    # task continued from where it stopped.
    _conversations: dict[str, list] = field(default_factory=dict)

    def _config(self) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=ORCHESTRATOR_INSTRUCTIONS,
            tools=[types.Tool(function_declarations=self.registry.declarations())],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )

    async def start(self, goal: str, context: object) -> Task:
        task = Task(goal=goal, model=self.brain_model)
        self.store.save(task)
        opening = f"Task: {goal}\nCurrent local time: {self.now():%A %d %B %Y, %H:%M}."
        self._conversations[task.id] = [
            types.Content(role="user", parts=[types.Part(text=opening)])
        ]
        return await self._run(task, context)

    async def resume(self, task_id: str, user_reply: str, context: object) -> Task:
        task = self.store.get(task_id)
        contents = self._conversations.get(task_id)
        if task is None or contents is None or task.state != "waiting_for_user":
            raise LookupError(f"Task {task_id} is not waiting for an answer.")
        task.question = ""
        contents.append(
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        text=f"The user replied: {user_reply!r}. Continue the task."
                    )
                ],
            )
        )
        return await self._run(task, context)

    async def _run(self, task: Task, context: object) -> Task:
        contents = self._conversations[task.id]
        task.set_state("running")
        self.store.save(task)
        model = task.model or self.brain_model
        failures_in_a_row = 0
        cfg = self._config()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.time_limit

        for _ in range(self.max_steps):
            if loop.time() > deadline:
                return self._finish(
                    task, "failed", error="The task took too long and was stopped."
                )
            try:
                turn = await asyncio.wait_for(
                    self.client.generate(model, contents, cfg),
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
            contents.append(turn.content)

            if not turn.calls:
                text = turn.text.strip()
                if text.upper().startswith("QUESTION:"):
                    task.question = text.split(":", 1)[1].strip()
                    task.set_state("waiting_for_user")
                    self.store.save(task)
                    return task
                return self._finish(task, "completed", result=text or "Done.")

            responses = []
            for name, args in turn.calls:
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
                responses.append(
                    types.Part.from_function_response(name=name, response=outcome)
                )

            _shrink_old_results(contents)
            contents.append(types.Content(role="user", parts=responses))
            self.store.save(task)

            if failures_in_a_row >= 2 and model != self.escalation_model:
                model = self.escalation_model
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


def _shrink_old_results(contents: list) -> None:
    """Replace all but the latest few large tool results with a short note."""
    positions = [
        (ci, pi)
        for ci, content in enumerate(contents)
        if content.role == "user"
        for pi, part in enumerate(content.parts or [])
        if part.function_response is not None
    ]
    for ci, pi in (
        positions[:-_KEEP_FULL_RESULTS] if len(positions) > _KEEP_FULL_RESULTS else []
    ):
        part = contents[ci].parts[pi]
        response = part.function_response.response or {}
        if len(json.dumps(response, default=str)) > _SHRINK_ABOVE_CHARS:
            contents[ci].parts[pi] = types.Part.from_function_response(
                name=part.function_response.name,
                response={"ok": response.get("ok", True), "result": _OMITTED},
            )


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
