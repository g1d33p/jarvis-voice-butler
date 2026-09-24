"""Tests for the local personal memory store and tools."""

import json
from pathlib import Path

import pytest
from livekit.agents.llm import ToolError

from audit import AuditLog
from memory_store import KINDS, MemoryStore
from memory_tools import MemoryTools


@pytest.fixture()
def store(tmp_path):
    return MemoryStore(path=tmp_path / "memory.db")


@pytest.fixture()
def tools(store, tmp_path):
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    return MemoryTools(store=store, audit=audit)


def test_remember_and_get(store) -> None:
    memory_id = store.remember(
        "preference", "Jeevan takes his coffee black", provenance="test"
    )
    memory = store.get(memory_id)
    assert memory["kind"] == "preference"
    assert memory["content"] == "Jeevan takes his coffee black"
    assert memory["provenance"] == "test"
    assert memory["created_at"]


def test_remember_rejects_bad_kind(store) -> None:
    with pytest.raises(ValueError, match="kind must be one of"):
        store.remember("secret", "the password is hunter2")


def test_remember_rejects_empty_content(store) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        store.remember("fact", "   ")


def test_recall_finds_best_match_first(store) -> None:
    store.remember("preference", "Jeevan takes his coffee black")
    store.remember("fact", "Jeevan's birthday is in March")
    store.remember("routine", "Gym on Monday mornings")
    results = store.recall("coffee")
    assert [r["content"] for r in results] == ["Jeevan takes his coffee black"]


def test_recall_filters_by_kind(store) -> None:
    store.remember("fact", "Jeevan lives in Austin")
    store.remember("preference", "Jeevan prefers Austin coffee shops")
    results = store.recall("Austin", kind="fact")
    assert len(results) == 1
    assert results[0]["kind"] == "fact"


def test_recall_tolerates_special_characters(store) -> None:
    store.remember("fact", "Jeevan's meeting is at 10:30 (sharp!)")
    # Must not raise an FTS5 syntax error.
    results = store.recall('meeting (sharp!) "quoted"')
    assert results


def test_recall_empty_query_lists_recent(store) -> None:
    store.remember("fact", "first memory")
    store.remember("fact", "second memory")
    results = store.recall("")
    assert len(results) == 2


def test_correct_updates_content_and_keeps_provenance(store) -> None:
    memory_id = store.remember(
        "fact", "Jeevan lives in Dallas", provenance="said in March"
    )
    updated = store.correct(memory_id, "Jeevan lives in Austin")
    assert updated["content"] == "Jeevan lives in Austin"
    assert updated["provenance"] == "said in March"
    assert updated["id"] == memory_id


def test_correct_missing_id_raises(store) -> None:
    with pytest.raises(KeyError):
        store.correct(999, "new content")


def test_forget_removes_memory_and_its_search_index(store) -> None:
    memory_id = store.remember("fact", "temporary tidbit")
    assert store.forget(memory_id) is True
    assert store.get(memory_id) is None
    assert store.recall("tidbit") == []


def test_forget_missing_id_returns_false(store) -> None:
    assert store.forget(999) is False


def test_export_returns_oldest_first(store) -> None:
    store.remember("fact", "first")
    store.remember("fact", "second")
    exported = store.export()
    assert [m["content"] for m in exported] == ["first", "second"]


def test_kinds_cover_plan_categories() -> None:
    assert set(KINDS) == {"fact", "preference", "routine", "person"}


class _Context:
    pass


async def _call(tool_fn, *args, **kwargs):
    return await tool_fn(_Context(), *args, **kwargs)


async def test_tool_remember_stores_with_provenance(tools, store) -> None:
    result = await _call(
        tools.remember, kind="person", content="Jeevan's sister is called Anaya"
    )
    assert result["remembered"] is True
    memory = store.get(result["id"])
    assert "Jeevan said so in conversation" in memory["provenance"]


async def test_tool_remember_rejects_bad_kind(tools) -> None:
    with pytest.raises(ToolError, match="kind must be one of"):
        await _call(tools.remember, kind="secret", content="shhh")


async def test_tool_recall_returns_ids_for_followups(tools) -> None:
    saved = await _call(
        tools.remember, kind="preference", content="Jeevan likes morning walks"
    )
    result = await _call(tools.recall, query="walks")
    assert result["memories"][0]["id"] == saved["id"]


async def test_tool_correct_memory(tools, store) -> None:
    saved = await _call(tools.remember, kind="fact", content="Jeevan lives in Dallas")
    result = await _call(
        tools.correct_memory,
        memory_id=saved["id"],
        new_content="Jeevan lives in Austin",
    )
    assert result["corrected"] is True
    assert store.get(saved["id"])["content"] == "Jeevan lives in Austin"


async def test_tool_correct_missing_memory(tools) -> None:
    with pytest.raises(ToolError, match="no memory with id"):
        await _call(tools.correct_memory, memory_id=999, new_content="whatever")


async def test_tool_forget_memory(tools, store) -> None:
    saved = await _call(tools.remember, kind="fact", content="old phone number")
    result = await _call(tools.forget_memory, memory_id=saved["id"])
    assert result["forgotten"] is True
    assert store.get(saved["id"]) is None


async def test_tool_forget_missing_memory(tools) -> None:
    with pytest.raises(ToolError, match="no memory with id"):
        await _call(tools.forget_memory, memory_id=999)


async def test_tool_export_writes_json_file(tools, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("memory_tools._EXPORT_DIR", tmp_path)
    await _call(tools.remember, kind="fact", content="export me")
    result = await _call(tools.export_memories)
    assert result["count"] == 1
    exported = json.loads(Path(result["exported"]).read_text())
    assert exported[0]["content"] == "export me"


async def test_memory_mutations_are_audited(tools, tmp_path) -> None:
    saved = await _call(tools.remember, kind="fact", content="audited fact")
    await _call(
        tools.correct_memory, memory_id=saved["id"], new_content="audited fact v2"
    )
    await _call(tools.forget_memory, memory_id=saved["id"])
    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    events = [json.loads(line)["event"] for line in lines]
    assert events == ["memory.remember", "memory.correct", "memory.forget"]
