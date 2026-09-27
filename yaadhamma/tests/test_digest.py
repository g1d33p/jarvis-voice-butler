"""The scheduled Saayam digest (2026-09-27)."""

import plistlib
import sys
from pathlib import Path

import pytest
from test_whatsapp import _chat

import config
from digest import DigestStore, DigestTools, find_self_chat, run_digest
from meta_client import ModelTurn
from whatsapp import WhatsAppError, WhatsAppNotPairedError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import digest_schedule


class FakeClient:
    def __init__(self, chats, paired=True):
        self.chats = chats
        self.paired = paired
        self.sent = []
        self.read = []

    async def list_all_chats(self):
        if not self.paired:
            raise WhatsAppNotPairedError("not paired")
        return self.chats

    async def read_messages(self, name, limit, exact=False):
        self.read.append(name)
        return {
            "chat": name,
            "messages": [{"sender": "Ravi", "text": f"news in {name}"}],
        }

    async def find_chat(self, query):
        digits = "".join(ch for ch in query if ch.isdigit())
        for chat in self.chats:
            name = chat["name"]
            if (
                digits and "".join(c for c in name if c.isdigit()).endswith(digits)
            ) or (not digits and query.casefold() in name.casefold()):
                return name
        raise WhatsAppError(f"No chat {query!r}")

    async def send_message(self, name, text):
        self.sent.append((name, text))
        return {"sent": True}


class FakeBrain:
    def __init__(self, text="*Needs you*\n- Ravi needs the budget review (SC1)."):
        self.text = text
        self.calls = []

    async def generate(self, model, messages, tools, reasoning_effort=None):
        self.calls.append(messages)
        return ModelTurn(calls=[], text=self.text, tokens_in=900, tokens_out=80)


CHATS = [
    _chat("SC1-Executives", unread=3),
    _chat("SC2-Leads", unread=0),
    _chat("Family", unread=4),
    _chat("+1 (940) 843-8446 (You)"),
]


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(config, "WHATSAPP_WATCHLIST", ["Saayam", "SC1", "SC2", "SC3"])
    monkeypatch.setattr(config, "SELF_CHAT_NUMBER", "9408438446")


async def test_digest_summarises_watched_chats_and_sends_to_himself(tmp_path) -> None:
    client, brain = FakeClient(CHATS), FakeBrain()
    store = DigestStore(tmp_path / "db.sqlite")

    result = await run_digest(client, brain, store)

    assert result.status == "sent"
    assert client.read == ["SC1-Executives"]  # Family is not on the watchlist
    [(to, text)] = client.sent
    assert to == "+1 (940) 843-8446 (You)"
    assert "Ravi needs the budget review" in text
    assert store.latest()["status"] == "sent"


async def test_quiet_run_sends_nothing(tmp_path) -> None:
    client = FakeClient(
        [_chat("SC1-Executives", unread=0), _chat("+1 (940) 843-8446 (You)")]
    )
    store = DigestStore(tmp_path / "db.sqlite")

    result = await run_digest(client, FakeBrain(), store)

    assert result.status == "quiet"
    assert client.sent == []
    assert store.latest()["status"] == "quiet"


async def test_digest_never_sends_to_anyone_but_himself(tmp_path, monkeypatch) -> None:
    """Even if the number setting pointed at someone else, nothing is sent."""
    monkeypatch.setattr(config, "SELF_CHAT_NUMBER", "0413")
    client = FakeClient([*CHATS, _chat("+1 (940) 364-0413")])

    result = await run_digest(client, FakeBrain(), DigestStore(tmp_path / "db.sqlite"))

    assert result.status == "failed"
    assert "not his own chat" in result.error
    assert client.sent == []


async def test_unpaired_digest_says_how_to_pair(tmp_path) -> None:
    result = await run_digest(
        FakeClient(CHATS, paired=False),
        FakeBrain(),
        DigestStore(tmp_path / "db.sqlite"),
    )
    assert result.status == "failed"
    assert "whatsapp_signin.py --digest" in result.error


async def test_self_chat_found_by_marker_when_no_number_set(monkeypatch) -> None:
    monkeypatch.setattr(config, "SELF_CHAT_NUMBER", "")
    assert await find_self_chat(FakeClient(CHATS)) == "+1 (940) 843-8446 (You)"


async def test_voice_can_ask_for_the_latest_digest(tmp_path) -> None:
    store = DigestStore(tmp_path / "db.sqlite")
    tools = DigestTools(store)
    assert (await tools.latest_digest(None))["status"] == "none"

    await run_digest(FakeClient(CHATS), FakeBrain(), store)
    latest = await tools.latest_digest(None)
    assert latest["status"] == "sent"
    assert "budget review" in latest["summary"]


def test_schedule_runs_four_times_a_day_in_local_time(tmp_path) -> None:
    plist = digest_schedule.build_plist("/usr/local/bin/uv", project=tmp_path)
    times = [(e["Hour"], e["Minute"]) for e in plist["StartCalendarInterval"]]
    assert times == [(9, 0), (13, 0), (17, 0), (21, 0)]
    assert plist["ProgramArguments"] == [
        "/usr/local/bin/uv",
        "run",
        "scripts/digest_run.py",
    ]
    assert plist["WorkingDirectory"] == str(tmp_path)
    plistlib.dumps(plist)  # valid for macOS
