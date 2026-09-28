"""WhatsApp self-check (2026-09-28): announce breakage instead of failing quietly."""

import pytest

import config
from whatsapp import WhatsAppError, WhatsAppNotPairedError
from whatsapp_health import HealthLog, run_health_check

CHATS = [
    {"name": "SC1-Executives", "unread": 2},
    {"name": "+1 (940) 364-0413", "unread": 0},
    {"name": "+1 (940) 843-8446 (You)", "unread": 0},
]
GOOD = [
    {
        "sender": "Ravi",
        "time": "10:30, 28/09/2026",
        "text": "Roadmap ready?",
        "outgoing": False,
    }
]


class FakeClient:
    def __init__(self, chats=CHATS, messages=None, fail=None):
        self.chats = chats
        self.messages = GOOD if messages is None else messages
        self.fail = fail or {}

    async def list_all_chats(self):
        if "list" in self.fail:
            raise self.fail["list"]
        return self.chats

    async def find_chat(self, query):
        if "find" in self.fail:
            raise self.fail["find"]
        return self.chats[0]["name"]

    async def read_messages(self, name, limit, exact=False):
        if "read" in self.fail:
            raise self.fail["read"]
        return {"chat": name, "messages": self.messages}


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(config, "WHATSAPP_WATCHLIST", ["Saayam", "SC1"])
    monkeypatch.setattr(config, "SELF_CHAT_NUMBER", "19408438446")


async def test_a_working_setup_passes_every_check() -> None:
    report = await run_health_check(FakeClient())
    assert report.ok
    assert [c.name for c in report.checks] == [
        "paired",
        "list chats",
        "search",
        "open and read",
        "message shape",
        "own chat",
    ]
    assert "passed" in report.summary()


async def test_unpaired_is_reported_not_raised() -> None:
    report = await run_health_check(
        FakeClient(fail={"list": WhatsAppNotPairedError("not paired")})
    )
    assert not report.ok
    assert report.failed[0].name == "paired"
    assert "FAILED at 'paired'" in report.summary()


async def test_chats_that_will_not_open_are_caught() -> None:
    """The 2026-09-25 break: clicking stopped opening chats."""
    report = await run_health_check(
        FakeClient(
            fail={
                "read": WhatsAppError(
                    "Opened 'SC1-Executives' but its messages would not load."
                )
            }
        )
    )
    assert not report.ok
    assert report.failed[0].name == "open and read"
    assert "would not load" in report.failed[0].detail
    assert "Digests may be wrong" in report.summary()


async def test_unreadable_message_layout_is_caught() -> None:
    """A page change that leaves messages without a sender, time or text."""
    report = await run_health_check(
        FakeClient(messages=[{"text": "", "time": "", "sender": ""}])
    )
    assert not report.ok
    assert report.failed[0].name in {"open and read", "message shape"}


async def test_a_broken_search_is_caught() -> None:
    report = await run_health_check(
        FakeClient(fail={"find": WhatsAppError("no chat found")})
    )
    assert [c.name for c in report.failed] == ["search", "own chat"]


async def test_the_result_is_recorded_and_warns_in_the_digest(
    tmp_path, monkeypatch
) -> None:
    import digest
    from whatsapp_health import HealthLog as RealLog

    log = RealLog(tmp_path / "db")
    log.record(
        await run_health_check(
            FakeClient(fail={"read": WhatsAppError("would not load")})
        )
    )
    monkeypatch.setattr("whatsapp_health.HealthLog", lambda *a, **k: log)

    assert log.latest()["ok"] is False
    warning = digest._health_warning()
    assert "*WhatsApp check failed*" in warning
    assert "open and read" in warning


async def test_no_warning_when_healthy(tmp_path, monkeypatch) -> None:
    import digest

    log = HealthLog(tmp_path / "db")
    log.record(await run_health_check(FakeClient()))
    monkeypatch.setattr("whatsapp_health.HealthLog", lambda *a, **k: log)

    assert digest._health_warning() == ""


class PickyClient(FakeClient):
    """Some chats refuse to open (dormant, far down the list); others work."""

    def __init__(self, broken_names):
        super().__init__()
        self.broken = set(broken_names)
        self.attempts = []

    async def read_messages(self, name, limit, exact=False):
        self.attempts.append(name)
        if name in self.broken:
            raise WhatsAppError(f"No WhatsApp chat named {name!r} found.")
        return {"chat": name, "messages": GOOD}


async def test_one_unreachable_chat_does_not_fail_the_check(monkeypatch) -> None:
    """2026-09-28: a dormant 'Saayam-Marketing' group failed the whole check."""
    monkeypatch.setattr(config, "WHATSAPP_WATCHLIST", ["Saayam", "SC1"])
    chats = [
        {"name": "Saayam-Marketing", "unread": 0},
        {"name": "SC1-Executives", "unread": 2},
        {"name": "+1 (940) 843-8446 (You)", "unread": 0},
    ]
    client = PickyClient(broken_names={"Saayam-Marketing"})
    client.chats = chats

    report = await run_health_check(client)

    assert report.ok
    assert client.attempts[:2] == ["Saayam-Marketing", "SC1-Executives"]
    read_check = next(c for c in report.checks if c.name == "open and read")
    assert "SC1-Executives" in read_check.detail


async def test_every_chat_failing_is_still_a_failure(monkeypatch) -> None:
    monkeypatch.setattr(config, "WHATSAPP_WATCHLIST", ["Saayam", "SC1"])
    client = PickyClient(broken_names={c["name"] for c in CHATS})
    report = await run_health_check(client)

    assert not report.ok
    assert report.failed[0].name == "open and read"
    assert [c.name for c in report.failed] == [
        "open and read",
        "message shape",
        "own chat",
    ]
