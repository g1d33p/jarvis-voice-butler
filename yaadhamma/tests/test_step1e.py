"""Step 1e (2026-09-25): visible step timings, honest task status."""

import json
import sqlite3

from actions import ActionRegistry
from orchestrator import Orchestrator, TaskTools
from task_manager import TaskStore
from test_orchestrator import FakeModel, Toys, call, turn


async def test_each_step_records_how_long_it_took(tmp_path) -> None:
    model = FakeModel(turn(call("echo", text="hi")), turn(text="Done."))
    orchestrator = Orchestrator(
        registry=ActionRegistry(Toys()),
        store=TaskStore(tmp_path / "t.db"),
        client=model,
    )
    task = await orchestrator.start("say hi", context=None)
    assert task.steps[0].seconds >= 0.0
    assert "seconds" in json.dumps(
        [s.__dict__ for s in orchestrator.store.get(task.id).steps]
    )


def test_old_task_records_without_timings_still_load(tmp_path) -> None:
    path = tmp_path / "t.db"
    store = TaskStore(path)
    old = {
        "goal": "old",
        "id": "abc12345",
        "state": "completed",
        "steps": [{"action": "echo", "args": {}, "ok": True, "summary": "x"}],
        "result": "fine",
        "question": "",
        "error": "",
        "model": "m",
        "tokens_in": 0,
        "tokens_out": 0,
        "created": "2026-09-24T10:00:00",
        "updated": "2026-09-24T10:00:00",
    }
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
            (
                "abc12345",
                "old",
                "completed",
                json.dumps(old),
                old["created"],
                old["updated"],
            ),
        )
    assert store.get("abc12345").steps[0].seconds == 0.0


async def test_recent_tasks_reports_a_finished_result(tmp_path) -> None:
    """Live: the answer arrived while he was talking and she said 'still working'."""
    model = FakeModel(turn(text="SC1 needs your review of the budget."))
    orchestrator = Orchestrator(
        registry=ActionRegistry(Toys()),
        store=TaskStore(tmp_path / "t.db"),
        client=model,
    )
    await orchestrator.start("check Saayam", context=None)

    latest = (await TaskTools(orchestrator).recent_tasks(None))[0]
    assert latest["status"] == "completed"
    assert latest["result"] == "SC1 needs your review of the budget."


def test_prompts_keep_quick_look_and_forbid_workarounds() -> None:
    from prompts import ORCHESTRATOR_INSTRUCTIONS, VOICE_INSTRUCTIONS

    assert "quick look, without opening the chats" in VOICE_INSTRUCTIONS
    assert "call recent_tasks" in VOICE_INSTRUCTIONS
    assert "open_chats false" in ORCHESTRATOR_INSTRUCTIONS
    assert "Never work around WhatsApp with web search" in ORCHESTRATOR_INSTRUCTIONS
