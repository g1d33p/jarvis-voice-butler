"""Overnight learning (2026-09-27)."""

import json
from datetime import datetime, timedelta

import pytest
from test_whatsapp import _chat

import config
from learning import LearningLog, apply_memories, collect_chats, run_learning
from memory_store import MemoryStore
from meta_client import ModelTurn
from untrusted import is_wrapped

NOW = datetime(2026, 9, 28, 2, 0)


def _msg(text, sender="Ravi", hours_ago=2, outgoing=False):
    when = NOW - timedelta(hours=hours_ago)
    return {
        "sender": sender,
        "time": f"{when:%H:%M, %d/%m/%Y}",
        "text": text,
        "outgoing": outgoing,
    }


class FakeWhatsApp:
    def __init__(self, chats, messages):
        self.chats = chats
        self.messages = messages
        self.opened = []

    async def list_all_chats(self):
        return self.chats

    async def read_messages(self, name, limit, exact=False):
        self.opened.append(name)
        return {"chat": name, "messages": self.messages.get(name, [])}


class FakeBrain:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def generate(self, model, messages, tools, reasoning_effort=None):
        self.calls.append(messages)
        return ModelTurn(
            calls=[], text=json.dumps(self.reply), tokens_in=1, tokens_out=1
        )


@pytest.fixture(autouse=True)
def _watchlist(monkeypatch):
    monkeypatch.setattr(config, "WHATSAPP_WATCHLIST", ["Saayam", "SC1", "SC2", "SC3"])


CHATS = [
    {**_chat("SC1-Executives", unread=2), "time": "11:40 PM"},
    {**_chat("Amma", unread=0), "time": "9:15 PM"},
    {**_chat("Ravi", unread=3), "time": "10:02 PM"},  # unread personal: must be skipped
    {**_chat("Old friend", unread=0), "time": "9/12/2026"},  # not recent
]
MESSAGES = {
    "SC1-Executives": [
        _msg("Jeevan, can you share the roadmap by Friday?"),
        _msg("Sure, will do", "Jeevan", outgoing=True),
    ],
    "Amma": [_msg("Call me on Sunday", "Amma"), _msg("ancient", "Amma", hours_ago=100)],
}


async def test_personal_chats_with_unread_messages_are_never_opened(tmp_path) -> None:
    whatsapp = FakeWhatsApp(CHATS, MESSAGES)
    chats, skipped, _ = await collect_chats(whatsapp, LearningLog(tmp_path / "db"), NOW)

    assert "Ravi" not in whatsapp.opened
    assert "Old friend" not in whatsapp.opened
    assert skipped == 1
    names = {c["chat"]: c["kind"] for c in chats}
    assert names == {"SC1-Executives": "Saayam community", "Amma": "personal"}
    amma = next(c for c in chats if c["chat"] == "Amma")
    # Message text is untrusted: enveloped, content preserved. (100h-old dropped.)
    assert len(amma["messages"]) == 1
    assert is_wrapped(amma["messages"][0]["text"])
    assert "Call me on Sunday" in amma["messages"][0]["text"]
    sc1 = next(c for c in chats if c["chat"] == "SC1-Executives")
    assert sc1["messages"][1]["from"] == "Jeevan"


def test_sensitive_or_malformed_memories_are_dropped(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    reply = {
        "add": [
            {
                "kind": "commitment",
                "content": "Share the roadmap with SC1-Executives by Friday 2 Oct.",
                "source": "SC1-Executives, 27 Sep",
            },
            {"kind": "fact", "content": "His OTP is 482913"},
            {
                "kind": "fact",
                "content": "Card number ends 4417 and account number is 12345678",
            },
            {"kind": "gossip", "content": "not a real kind"},
            {"kind": "person", "content": ""},
        ]
    }
    added, _ = apply_memories(reply, store, "27 Sep 2026")

    assert added == [
        "commitment: Share the roadmap with SC1-Executives by Friday 2 Oct."
    ]
    [saved] = store.list_memories()
    assert (
        saved["provenance"]
        == "learned overnight 27 Sep 2026 from SC1-Executives, 27 Sep"
    )


def test_at_most_fifteen_new_memories_a_night(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    reply = {
        "add": [
            {"kind": "fact", "content": f"Fact number {chr(65 + i)}"} for i in range(30)
        ]
    }
    added, _ = apply_memories(reply, store, "27 Sep 2026")
    assert len(added) == 15


def test_changes_update_the_existing_memory(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    memory_id = store.remember("fact", "Cognito migration is in planning.")
    _, updated = apply_memories(
        {"update": [{"id": memory_id, "content": "Cognito migration is in testing."}]},
        store,
        "27 Sep 2026",
    )
    assert updated == ["Cognito migration is in testing."]
    assert store.get(memory_id)["content"] == "Cognito migration is in testing."


async def test_full_run_saves_and_never_relearns_the_same_messages(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    log = LearningLog(tmp_path / "db")
    brain = FakeBrain(
        {
            "add": [
                {
                    "kind": "commitment",
                    "content": "Share the roadmap by Friday.",
                    "source": "SC1",
                }
            ]
        }
    )
    whatsapp = FakeWhatsApp(CHATS, MESSAGES)

    first = await run_learning(whatsapp, brain, store, log)
    assert first.status == "learned"
    assert first.added == ["commitment: Share the roadmap by Friday."]
    payload = json.loads(brain.calls[0][1]["content"])
    assert {c["chat"] for c in payload["whatsapp"]} == {"SC1-Executives", "Amma"}

    second = await run_learning(
        FakeWhatsApp(CHATS, MESSAGES), FakeBrain({}), store, log
    )
    assert second.status == "nothing"  # the same messages are not learned twice
    assert log.latest()["status"] == "nothing"


def test_schedule_learns_at_two_am() -> None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import digest_schedule

    jobs = {label: (times, extra) for label, times, extra, _ in digest_schedule.JOBS}
    assert jobs["com.yaadhamma.learn"] == ([(2, 0)], ["--learn"])
