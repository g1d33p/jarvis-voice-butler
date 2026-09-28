"""Step 1d (2026-09-25): reliable background tasks and the Saayam watchlist."""

import asyncio

import pytest
from livekit.agents import RunContext, function_tool
from test_orchestrator import FakeModel, call, turn
from test_whatsapp_tools import FakeClient, _chat, _Context

from actions import ActionRegistry
from audit import AuditLog
from orchestrator import Orchestrator, TaskTools
from permissions import ApprovalManager
from task_manager import TaskStore
from untrusted import is_wrapped
from whatsapp import find_chats, matches_watchlist
from whatsapp_tools import WhatsAppTools

# ----------------------------------------------------------------- WhatsApp


def test_digits_find_the_unsaved_contact_by_number_ending() -> None:
    chats = [
        {"name": "+1 (940) 843-8446"},
        {"name": "+1 (945) 265-8990"},
        {"name": "SC1-Executives"},
    ]
    assert [c["name"] for c in find_chats("8990", chats)] == ["+1 (945) 265-8990"]
    assert find_chats("7777", chats) == []
    # Names with letters are still matched as text.
    assert [c["name"] for c in find_chats("sc1", chats)] == ["SC1-Executives"]


@pytest.mark.parametrize(
    ("name", "watched"),
    [
        ("Saayam For All - Volunteers", True),
        ("SC1-Executives", True),
        ("sc3 confidants", True),
        ("Family", False),
        ("+1 (945) 265-8990", False),
    ],
)
def test_watchlist_matching(name: str, watched: bool) -> None:
    assert matches_watchlist(name, ["Saayam", "SC1", "SC2", "SC3"]) is watched


@pytest.fixture()
def watch_tools(tmp_path):
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    client = FakeClient(
        chats=[
            _chat("SC1-Executives", unread=3, preview="Jeevan, can you review?"),
            _chat("SC2-Leads", unread=0, preview="thanks all"),
            _chat("Saayam For All - Volunteers", unread=1, preview="meeting moved"),
            _chat("Family", unread=5, preview="dinner?"),
        ]
    )
    return WhatsAppTools(client=client, approvals=ApprovalManager(audit=audit)), client


async def test_watchlist_digest_reads_only_watched_unread_chats(watch_tools) -> None:
    tools, client = watch_tools
    result = await tools.whatsapp_watchlist_digest(_Context())

    assert result["watched_chats"] == 3
    assert result["unread_watched_chats"] == 2
    assert {c["chat"] for c in result["unread"]} == {
        "SC1-Executives",
        "Saayam For All - Volunteers",
    }
    assert result["quiet_chats"] == ["SC2-Leads"]
    assert "Family" not in client.read_calls  # never opens chats off the watchlist


async def test_watchlist_quick_look_opens_nothing(watch_tools) -> None:
    tools, client = watch_tools
    result = await tools.whatsapp_watchlist_digest(_Context(), open_chats=False)

    assert client.read_calls == []
    previews = {c["chat"]: c["preview"] for c in result["unread"]}
    # Previews are untrusted message text: enveloped, content preserved.
    assert is_wrapped(previews["SC1-Executives"])
    assert "Jeevan, can you review?" in previews["SC1-Executives"]


async def test_watchlist_digest_is_available_to_the_background_brain() -> None:
    tools = WhatsAppTools(client=FakeClient())
    assert "whatsapp_watchlist_digest" in ActionRegistry(tools).names


# ----------------------------------------------------------------- tasks


class _Hanging:
    @property
    def tools(self) -> list:
        return [self.hang]

    @function_tool()
    async def hang(self, context: RunContext) -> str:
        """Never finishes."""
        await asyncio.sleep(3600)
        return "unreachable"


async def test_a_hung_tool_is_stopped_and_reported() -> None:
    registry = ActionRegistry(_Hanging())
    outcome = await registry.call("hang", {}, None, timeout=0.05)
    assert outcome["ok"] is False
    assert "took longer than" in outcome["error"]


class _SlowModel(FakeModel):
    async def generate(
        self, model, messages, tools, reasoning_effort=None, feature="test"
    ):
        await asyncio.sleep(0.3)
        return await super().generate(model, messages, tools, reasoning_effort)


async def test_newer_request_replaces_a_running_task(tmp_path) -> None:
    """Live: three overlapping WhatsApp tasks all timed out behind each other."""
    model = _SlowModel(turn(text="First done."), turn(text="Second done."))
    orchestrator = Orchestrator(
        registry=ActionRegistry(_Hanging()),
        store=TaskStore(tmp_path / "t.db"),
        client=model,
    )
    tools = TaskTools(orchestrator)

    first = asyncio.ensure_future(tools.run_task(None, "check WhatsApp"))
    await asyncio.sleep(0.05)
    second = await tools.run_task(None, "check the Saayam chats instead")
    first_result = await first

    assert first_result["status"] == "cancelled"
    assert second["status"] == "completed"
    recorded = {t.goal: t.state for t in orchestrator.store.recent(5)}
    assert recorded["check WhatsApp"] == "cancelled"  # not left "running"


async def test_task_time_limit_bounds_a_stuck_tool(tmp_path) -> None:
    model = FakeModel(turn(call("hang")), turn(text="Gave up."))
    orchestrator = Orchestrator(
        registry=ActionRegistry(_Hanging()),
        store=TaskStore(tmp_path / "t.db"),
        client=model,
        time_limit=0.2,
    )
    task = await asyncio.wait_for(orchestrator.start("hang", context=None), timeout=15)
    assert task.steps[0].ok is False
    assert "took longer than" in task.steps[0].summary


# ----------------------------------------------------------------- Gmail


async def test_gmail_calls_run_off_the_voice_loop(monkeypatch) -> None:
    """Live: synchronous Gmail HTTP froze the voice for up to 1.4 s."""
    import threading

    import gmail_tools

    seen_threads = []

    class Client:
        label = "personal1"

        def list_messages(self, limit):
            seen_threads.append(threading.current_thread())
            return []

    tools = gmail_tools.GmailTools.__new__(gmail_tools.GmailTools)
    tools._clients = [Client()]
    monkeypatch.setattr(tools, "_merge", lambda per_account, limit: {}, raising=False)

    await tools.gmail_read_inbox(_Context())

    assert seen_threads and seen_threads[0] is not threading.main_thread()


# ----------------------------------------------------------------- prompts


def test_prompts_know_saayam_and_the_watchlist() -> None:
    from prompts import ORCHESTRATOR_INSTRUCTIONS, VOICE_INSTRUCTIONS

    assert "Saiyam" in VOICE_INSTRUCTIONS and "Saayam" in VOICE_INSTRUCTIONS
    assert "whatsapp_watchlist_digest" in ORCHESTRATOR_INSTRUCTIONS
    assert 'chat_name "8990"' in ORCHESTRATOR_INSTRUCTIONS
