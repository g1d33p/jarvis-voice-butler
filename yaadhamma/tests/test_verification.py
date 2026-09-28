"""Stage 2: every consequential action verifies its effect and reports honestly.

A "done" that was never checked is a lie waiting to happen. These tests prove:
- file moves/copies/renames/creates confirm the end state, and raise (not
  claim success) when the confirmation fails;
- memory writes read back what was stored;
- calendar creation re-fetches the created event;
- tidy apply confirms every move it counts;
- the audit log records the verification outcome of each action.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from livekit.agents.llm import ToolError

from audit import AuditLog
from calendar_tools import CalendarTools
from file_tools import FileTools
from memory_store import MemoryStore
from memory_tools import MemoryTools
from permissions import ApprovalManager
from tidy import Move, TidyLog, TidyPlan, apply_plan


class _Context:
    pass


@pytest.fixture()
def files():
    return FileTools()


@pytest.fixture()
def memories(tmp_path):
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    return MemoryTools(store=MemoryStore(path=tmp_path / "memory.db"), audit=audit)


# --- file tools -----------------------------------------------------------


async def test_move_path_verifies_destination_and_source_gone(files, tmp_path) -> None:
    source = tmp_path / "a.txt"
    source.write_text("x")
    destination = tmp_path / "b.txt"
    result = await files.move_path(_Context(), str(source), str(destination))
    assert result["moved"] is True
    assert result["verified"] is True
    assert "destination exists" in result["verification"]
    assert "source is gone" in result["verification"]


async def test_move_path_reports_failure_instead_of_claiming_success(
    files, tmp_path, monkeypatch
) -> None:
    source = tmp_path / "a.txt"
    source.write_text("x")
    # Simulate a move that reports no error but changes nothing.
    monkeypatch.setattr(shutil, "move", lambda *args, **kwargs: None)
    with pytest.raises(ToolError, match="could not be confirmed"):
        await files.move_path(_Context(), str(source), str(tmp_path / "b.txt"))


async def test_copy_path_verifies_the_copy_exists(files, tmp_path) -> None:
    source = tmp_path / "a.txt"
    source.write_text("x")
    destination = tmp_path / "copy.txt"
    result = await files.copy_path(_Context(), str(source), str(destination))
    assert result["copied"] is True
    assert result["verified"] is True
    assert source.exists()  # a copy leaves the source alone


async def test_copy_path_reports_unconfirmed_copy(files, tmp_path, monkeypatch) -> None:
    source = tmp_path / "a.txt"
    source.write_text("x")
    monkeypatch.setattr(shutil, "copy2", lambda *args, **kwargs: None)
    with pytest.raises(ToolError, match="not at the destination"):
        await files.copy_path(_Context(), str(source), str(tmp_path / "c.txt"))


async def test_rename_path_verifies_new_name(files, tmp_path) -> None:
    source = tmp_path / "old.txt"
    source.write_text("x")
    result = await files.rename_path(_Context(), str(source), "new.txt")
    assert result["renamed"] is True
    assert result["verified"] is True
    assert (tmp_path / "new.txt").exists()
    assert not source.exists()


async def test_create_file_verifies_the_file(files, tmp_path) -> None:
    target = tmp_path / "note.txt"
    result = await files.create_file(_Context(), str(target), "hello")
    assert result["created"] is True
    assert result["verified"] is True
    assert target.read_text() == "hello"


async def test_create_folder_verifies_the_folder(files, tmp_path) -> None:
    target = tmp_path / "newdir" / "sub"
    result = await files.create_folder(_Context(), str(target))
    assert result["created"] is True
    assert result["verified"] is True
    assert target.is_dir()


# --- memory tools ---------------------------------------------------------


async def test_remember_reads_back_what_was_stored(memories) -> None:
    result = await memories.remember(
        _Context(), kind="fact", content="Jeevan drinks filter coffee"
    )
    assert result["remembered"] is True
    assert result["verified"] is True
    assert "read the memory back" in result["verification"]


async def test_remember_reports_mismatch_instead_of_claiming_success(
    memories, monkeypatch
) -> None:
    tools = memories
    real_get = tools._store.get
    monkeypatch.setattr(
        tools._store, "get", lambda memory_id: {"content": "something else"}
    )
    with pytest.raises(ToolError, match="did not match"):
        await tools.remember(_Context(), kind="fact", content="the real fact")
    monkeypatch.setattr(tools._store, "get", real_get)


async def test_correct_memory_verifies_new_content(memories) -> None:
    saved = await memories.remember(
        _Context(), kind="fact", content="Jeevan lives in Dallas"
    )
    result = await memories.correct_memory(
        _Context(), memory_id=saved["id"], new_content="Jeevan lives in Austin"
    )
    assert result["corrected"] is True
    assert result["verified"] is True


async def test_forget_memory_verifies_it_is_gone(memories) -> None:
    saved = await memories.remember(_Context(), kind="fact", content="old phone number")
    result = await memories.forget_memory(_Context(), memory_id=saved["id"])
    assert result["forgotten"] is True
    assert result["verified"] is True
    assert "gone" in result["verification"]


# --- calendar -------------------------------------------------------------


class _FakeCalendarClient:
    def __init__(self, fail_fetch: bool = False) -> None:
        self.fail_fetch = fail_fetch

    def list_events(self, start, end, limit=50):
        return []

    def create_event(self, title, start, end, location=""):
        return {
            "id": "evt-1",
            "title": title,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "all_day": False,
            "location": location,
            "declined": False,
            "busy": True,
        }

    def get_event(self, event_id):
        if self.fail_fetch:
            raise RuntimeError("network blip")
        return {
            "id": event_id,
            "title": "Dentist",
            "start": "2026-10-02T10:00:00",
            "end": "2026-10-02T11:00:00",
            "all_day": False,
            "location": "",
            "declined": False,
            "busy": True,
        }


async def _add_event(client) -> dict:
    tools = CalendarTools(client=client, approvals=ApprovalManager())
    start = (datetime.now() + timedelta(days=2)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    with pytest.raises(ToolError, match="needs the user's approval"):
        await tools.calendar_add_event(
            _Context(), title="Dentist", start=start.isoformat()
        )
    from permissions import ApprovalTools

    confirm = ApprovalTools(approvals=tools._approvals)
    return await confirm.approve_pending_action(_Context(), "yes")


async def test_calendar_add_event_refetches_the_created_event() -> None:
    result = await _add_event(_FakeCalendarClient())
    assert result["added"] is True
    assert result["verified"] is True
    assert "re-fetched" in result["verification"]


async def test_calendar_add_event_is_honest_when_refetch_fails() -> None:
    result = await _add_event(_FakeCalendarClient(fail_fetch=True))
    assert result["added"] is True
    assert result["verified"] is False
    assert "unconfirmed" in result["verification"]


# --- tidy -----------------------------------------------------------------


def _plan(tmp_path: Path) -> TidyPlan:
    source = tmp_path / "loose.txt"
    source.write_text("x")
    return TidyPlan(
        created="2026-09-28T00:00:00",
        moves=[
            Move(
                source=str(source),
                destination=str(tmp_path / "sorted" / "loose.txt"),
                group="Documents",
            )
        ],
    )


def test_apply_plan_verifies_every_move_it_counts(tmp_path) -> None:
    log = TidyLog(path=tmp_path / "tidy.jsonl")
    result = apply_plan(_plan(tmp_path), log)
    assert result["moved"] == 1
    assert result["verified"] is True
    assert result["failed"] == []


def test_apply_plan_does_not_count_unconfirmed_moves(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(shutil, "move", lambda *args, **kwargs: None)
    log = TidyLog(path=tmp_path / "tidy.jsonl")
    result = apply_plan(_plan(tmp_path), log)
    assert result["moved"] == 0
    assert result["verified"] is False
    assert len(result["failed"]) == 1
    assert "could not be confirmed" in result["failed"][0]


# --- audit log ------------------------------------------------------------


async def test_audit_log_records_verification_outcome(tmp_path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLog(path=audit_path)
    approvals = ApprovalManager(audit=audit)

    async def execute():
        return {"done": True, "verified": True}

    await approvals.gate(
        tool_name="list_directory",
        description="list a directory",
        context=_Context(),
        execute=execute,
    )
    entries = audit.read_recent(limit=5)
    done = [e for e in entries if e["event"] == "action_done"]
    assert done and done[0]["verified"] is True


async def test_audit_log_marks_results_without_verification(tmp_path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLog(path=audit_path)
    approvals = ApprovalManager(audit=audit)

    async def execute():
        return "just a string"

    await approvals.gate(
        tool_name="list_directory",
        description="list a directory",
        context=_Context(),
        execute=execute,
    )
    entries = audit.read_recent(limit=5)
    done = [e for e in entries if e["event"] == "action_done"]
    assert done and done[0]["verified"] == "not-reported"
