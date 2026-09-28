"""The 18 realistic scorecard tasks.

Each case drives the REAL tool classes against a fake backend, so the
production paths — approval gates, verification re-fetches, untrusted
wrapping, graceful degradation — are what get scored. Cases are registered
on a Scorecard with register(); tests/scorecard/test_cases.py runs each as
a pytest test and scripts/scorecard.py prints the card.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
from pathlib import Path

from fakes import (
    Context,
    FakeCalendarClient,
    FakeGmailClient,
    FakeWhatsAppClient,
    cal_event,
    gmail_message,
    sim_cost_usd,
    wa_chat,
    wa_msg,
)

from audit import AuditLog
from calendar_tools import CalendarTools
from costs import CostStore
from file_tools import FileTools
from gcal import local_zone
from gmail import GmailAuthError
from gmail_tools import GmailTools
from memory_store import MemoryStore
from memory_tools import MemoryTools
from permissions import ApprovalManager
from scorecard import Scorecard
from whatsapp import WhatsAppError
from whatsapp_tools import WhatsAppTools

_BRAIN = "gemini-3.5-flash-lite"


def _tmp(ctx) -> Path:
    tmp = getattr(ctx, "tmp_path", None)
    return Path(tmp) if tmp else Path("/tmp/yaadhamma-scorecard")


def _approvals(ctx) -> ApprovalManager:
    audit = AuditLog(path=_tmp(ctx) / "audit.jsonl")
    return ApprovalManager(audit=audit)


def _gmail_tools(ctx, *clients) -> GmailTools:
    return GmailTools(clients=list(clients), approvals=_approvals(ctx))


def _wa_tools(ctx, client) -> WhatsAppTools:
    return WhatsAppTools(client=client, approvals=_approvals(ctx))


def _cal_tools(ctx, client) -> CalendarTools:
    return CalendarTools(client=client, approvals=_approvals(ctx))


def _memory_tools(ctx) -> MemoryTools:
    store = MemoryStore(path=_tmp(ctx) / "memory.db")
    audit = AuditLog(path=_tmp(ctx) / "audit.jsonl")
    return MemoryTools(store=store, audit=audit)


# ---------------------------------------------------------------- Gmail


def register(card: Scorecard) -> None:
    @card.case("gmail: summarise unread inbox", category="gmail")
    async def _gmail_inbox(ctx):
        personal = FakeGmailClient(
            "personal",
            [
                gmail_message(
                    "p1", "boss@work.example", "Q3 planning", "see attached", 300
                ),
                gmail_message(
                    "p2", "mom@example.com", "Sunday lunch", "come over?", 200
                ),
            ],
        )
        work = FakeGmailClient(
            "work",
            [
                gmail_message(
                    "w1", "hr@example.com", "Benefits enrolment", "deadline Friday", 250
                )
            ],
        )
        for client in (personal, work):
            ctx.watch(client)
        tools = _gmail_tools(ctx, personal, work)
        result = await tools.gmail_read_inbox(Context(), limit=5)
        messages = result["messages"]
        assert len(messages) == 3, f"expected 3, got {len(messages)}"
        dates = [m["internal_date"] for m in messages]
        assert dates == sorted(dates, reverse=True), "not newest-first"
        assert result["account_errors"] == []
        ctx.note_cost(sim_cost_usd(_BRAIN, 1200, 300), "summarise inbox")
        return "3 unread across 2 accounts, newest first"

    @card.case("gmail: search then read full mail", category="gmail")
    async def _gmail_search_read(ctx):
        client = FakeGmailClient(
            "personal",
            [
                gmail_message(
                    "p9",
                    "billing@example.com",
                    "Invoice #1042 due",
                    "Your invoice is attached",
                    100,
                    body_text="Invoice #1042 for $120 is due Friday. Pay at example.com/pay.",
                ),
                gmail_message(
                    "p8", "news@example.com", "Daily digest", "headlines", 90
                ),
            ],
        )
        ctx.watch(client)
        tools = _gmail_tools(ctx, client)
        found = await tools.gmail_search_email(Context(), "invoice", limit=5)
        assert len(found["messages"]) == 1, found["messages"]
        full = await tools.gmail_read_email(Context(), found["messages"][0]["id"])
        assert "Invoice #1042" in full["body_text"]
        # Stage 0: outside-world content stays inside the envelope.
        assert "<<UNTRUSTED_CONTENT" in full["body_text"]
        ctx.note_cost(sim_cost_usd(_BRAIN, 800, 200), "read one mail")
        return "invoice found and read, body kept in the untrusted envelope"

    @card.case("gmail: dictated email sends verified", category="gmail")
    async def _gmail_send_dictated(ctx):
        client = FakeGmailClient("personal")
        ctx.watch(client)
        tools = _gmail_tools(ctx, client)
        said = "send an email to ravi@example.com saying running late"
        result = await tools.gmail_send_email(
            Context([said]),
            to="ravi@example.com",
            subject="running late",
            body="running late",
        )
        assert result["sent"] is True
        assert result["verified"] is True, result.get("verification")
        assert len(client.sent) == 1
        ctx.note_cost(sim_cost_usd(_BRAIN, 400, 100), "send email turn")
        return "dictated email sent, SENT label re-fetched and confirmed"

    @card.case("gmail: one account down, the other still serves", category="gmail")
    async def _gmail_degraded(ctx):
        personal = FakeGmailClient("personal")
        personal.fail_next(GmailAuthError("401 expired token"))
        work = FakeGmailClient(
            "work", [gmail_message("w1", "hr@example.com", "Benefits", "deadline", 250)]
        )
        for client in (personal, work):
            ctx.watch(client)
        tools = _gmail_tools(ctx, personal, work)
        result = await tools.gmail_read_inbox(Context(), limit=5)
        assert len(result["messages"]) == 1, "work mail should still come through"
        assert any("personal" in e for e in result["account_errors"])
        return "personal account auth failed; work inbox still served, error reported"

    # ---------------------------------------------------------------- WhatsApp

    @card.case("whatsapp: triage shows where needed", category="whatsapp")
    async def _wa_triage(ctx):
        client = FakeWhatsAppClient(
            [
                wa_chat(
                    "SC1-Confidants",
                    unread=3,
                    preview="meeting at 5?",
                    messages=[wa_msg("Asha", "Can we move the meeting to 5?")],
                ),
                wa_chat(
                    "Ravi",
                    unread=1,
                    preview="call me",
                    messages=[wa_msg("Ravi", "call me when free")],
                ),
                wa_chat("Family", unread=0, preview="ok"),
            ]
        )
        ctx.watch(client)
        tools = _wa_tools(ctx, client)
        result = await tools.whatsapp_where_needed(Context())
        names = [c["chat"] for c in result["chats_needing_attention"]]
        assert "SC1-Confidants" in names and "Ravi" in names
        assert "Family" not in names
        # Stage 0: chat text stays inside the envelope.
        texts = result["chats_needing_attention"][0]["messages"]
        assert "<<UNTRUSTED_CONTENT" in texts[0]["text"]
        ctx.note_cost(sim_cost_usd(_BRAIN, 1500, 400), "triage summary")
        return "2 chats need attention; message text kept in the untrusted envelope"

    @card.case("whatsapp: dictated message sends at once", category="whatsapp")
    async def _wa_send_dictated(ctx):
        client = FakeWhatsAppClient([wa_chat("Ravi")])
        ctx.watch(client)
        tools = _wa_tools(ctx, client)
        result = await tools.whatsapp_send_message(
            Context(["send a whatsapp to Ravi saying running late"]),
            chat_name="Ravi",
            message="running late",
        )
        assert result["sent"] is True
        assert client.sent == [{"chat": "Ravi", "text": "running late"}]
        return "dictated message sent without a second prompt"

    @card.case("whatsapp: composed message waits for approval", category="whatsapp")
    async def _wa_send_composed_asks(ctx):
        client = FakeWhatsAppClient([wa_chat("Ravi")])
        ctx.watch(client)
        tools = _wa_tools(ctx, client)
        try:
            await tools.whatsapp_send_message(
                Context(), chat_name="Ravi", message="Are you free on Friday evening?"
            )
        except Exception as exc:
            assert "approval" in str(exc).casefold(), str(exc)
        else:
            raise AssertionError("composed message must not send without approval")
        assert client.sent == [], "nothing may be sent before approval"
        return "composed message stopped at the approval gate; nothing sent"

    @card.case("whatsapp: a clear yes sends the approved draft", category="whatsapp")
    async def _wa_send_approved(ctx):
        client = FakeWhatsAppClient([wa_chat("Ravi")])
        ctx.watch(client)
        tools = _wa_tools(ctx, client)
        # The approval heuristic needs the full draft in the question.
        turns = [
            ("assistant", "To Ravi on WhatsApp: running late. Shall I send it?"),
            ("user", "yes"),
        ]
        result = await tools.whatsapp_send_message(
            Context(turns), chat_name="Ravi", message="running late"
        )
        assert result["sent"] is True
        assert len(client.sent) == 1
        return "spoken yes approved the exact draft; sent once"

    @card.case(
        "whatsapp: unknown chat fails gracefully",
        category="whatsapp",
        expect_failure=True,
    )
    async def _wa_unknown_chat(ctx):
        client = FakeWhatsAppClient([wa_chat("Ravi")])
        ctx.watch(client)
        tools = _wa_tools(ctx, client)
        await tools.whatsapp_read_chat(Context(), chat_name="Nobody Here")
        return "should not reach here"

    @card.case(
        "whatsapp: send retry after a transient failure is safe", category="whatsapp"
    )
    async def _wa_send_retry(ctx):
        client = FakeWhatsAppClient([wa_chat("Ravi")])
        client.fail_next(WhatsAppError("send timed out"))
        ctx.watch(client)
        tools = _wa_tools(ctx, client)
        said = "send a whatsapp to Ravi saying running late"
        # The transient failure; the tool wraps it, nothing is sent.
        with contextlib.suppress(Exception):
            await tools.whatsapp_send_message(
                Context([said]), chat_name="Ravi", message="running late"
            )
        assert client.sent == [], "a failed send must not send anything"
        # A retry is a new utterance, so it gets a fresh utterance id.
        retry = [
            (
                "user",
                "try again and send that whatsapp to Ravi saying running late",
                "id-retry-1",
            )
        ]
        result = await tools.whatsapp_send_message(
            Context(retry), chat_name="Ravi", message="running late"
        )
        assert result["sent"] is True
        assert len(client.sent) == 1, f"must not double-send: {client.sent}"
        return "transient send failure recovered with a retry; exactly one message sent"

    # ---------------------------------------------------------------- Calendar

    @card.case("calendar: today's agenda spots a clash", category="calendar")
    async def _cal_agenda(ctx):
        zone = local_zone()
        today = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        events = [
            cal_event("e1", "Standup", today.replace(hour=9), 30),
            cal_event("e2", "Dentist", today.replace(hour=9, minute=15), 45),
            cal_event("e3", "Lunch with Priya", today.replace(hour=12), 60),
        ]
        client = FakeCalendarClient(events)
        ctx.watch(client)
        tools = _cal_tools(ctx, client)
        result = await tools.calendar_agenda(
            Context(), start_date=today.date().isoformat(), days=1
        )
        day = result["events_by_day"]
        assert day != "No events."
        assert result["clashes"], "the 9:00/9:15 overlap must be flagged"
        ctx.note_cost(sim_cost_usd(_BRAIN, 600, 200), "agenda turn")
        return f"3 events listed; clash flagged: {result['clashes'][0]}"

    @card.case("calendar: added event is verified on read-back", category="calendar")
    async def _cal_add_verified(ctx):
        client = FakeCalendarClient()
        ctx.watch(client)
        tools = _cal_tools(ctx, client)
        begin = datetime.now(local_zone()) + timedelta(days=1)
        begin = begin.replace(hour=10, minute=0, second=0, microsecond=0)
        finish = begin + timedelta(minutes=30)
        # Mirror the description the tool builds, without quote marks (the
        # approval heuristic treats a partial quote as a different draft).
        spoken = (
            (
                f'add "Dentist" to your calendar on {begin:%A %d %B}, '
                f"{begin:%I:%M %p} to {finish:%I:%M %p}"
            )
            .replace(" 0", " ")
            .replace('"', "")
        )
        turns = [
            ("assistant", f"Shall I {spoken}?"),
            ("user", "yes"),
        ]
        result = await tools.calendar_add_event(
            Context(turns),
            title="Dentist",
            start=begin.isoformat(),
            duration_minutes=30,
        )
        assert result["added"] is True
        assert result["verified"] is True, result.get("verification")
        assert len(client.created) == 1
        return "event created and re-fetched by id; read-back matches"

    # ---------------------------------------------------------------- Memory

    @card.case("memory: remember then recall", category="memory")
    async def _memory_recall(ctx):
        tools = _memory_tools(ctx)
        saved = await tools.remember(
            Context(), kind="preference", content="Jeevan takes his coffee black."
        )
        assert saved.get("remembered") and saved.get("id"), saved
        found = await tools.recall(Context(), query="coffee")
        texts = " ".join(str(m) for m in found.get("memories", []))
        assert "black" in texts.casefold(), f"recall missed it: {found}"
        return "preference saved and recalled"

    @card.case("memory: forget really deletes", category="memory")
    async def _memory_forget(ctx):
        tools = _memory_tools(ctx)
        saved = await tools.remember(
            Context(), kind="fact", content="Jeevan once owned a red bicycle."
        )
        memory_id = saved.get("id")
        assert memory_id, f"no id in {saved}"
        await tools.forget_memory(Context(), memory_id=int(memory_id))
        found = await tools.recall(Context(), query="bicycle")
        assert not found.get("memories"), f"still there: {found}"
        return "memory deleted and no longer recalled"

    # ---------------------------------------------------------------- Files

    @card.case("files: create and move a file", category="files")
    async def _files_create_move(ctx):
        tools = FileTools(approvals=_approvals(ctx))
        base = _tmp(ctx) / "notes"
        await tools.create_file(
            Context(), path=str(base / "idea.txt"), content="Buy milk.\n"
        )
        await tools.create_folder(Context(), path=str(base / "done"))
        moved = await tools.move_path(
            Context(),
            source=str(base / "idea.txt"),
            destination=str(base / "done" / "idea.txt"),
        )
        assert moved["moved"] is True
        assert moved["verified"] is True, moved.get("verification")
        assert Path(moved["destination"]).read_text() == "Buy milk.\n"
        assert not (base / "idea.txt").exists()
        return "file created then moved; content intact, move verified"

    @card.case("files: trash needs approval", category="files")
    async def _files_trash_asks(ctx):
        from unittest.mock import patch

        import file_tools

        tools = FileTools(approvals=_approvals(ctx))
        target = _tmp(ctx) / "keep.txt"
        target.write_text("important")
        # Stub the home-folder check so the APPROVAL gate is what's tested.
        with patch.object(file_tools, "check_trashable", return_value=target):
            try:
                await tools.move_to_trash(Context(), path=str(target))
            except Exception as exc:
                assert "approval" in str(exc).casefold(), str(exc)
            else:
                raise AssertionError("trash must not happen without approval")
        assert target.exists(), "the file must survive a refused approval"
        return "trash stopped at the approval gate; file untouched"

    # ---------------------------------------------------------------- Recovery

    @card.case("recovery: unverified gmail send is reported honestly", category="gmail")
    async def _gmail_send_unverified(ctx):
        client = FakeGmailClient("personal", verify_sends=False)
        ctx.watch(client)
        tools = _gmail_tools(ctx, client)
        said = "send an email to ravi@example.com saying running late"
        result = await tools.gmail_send_email(
            Context([said]),
            to="ravi@example.com",
            subject="running late",
            body="running late",
        )
        assert result["sent"] is True
        assert result["verified"] is False, "must not claim verification it lacks"
        assert "unconfirm" in result["verification"].casefold()
        return "send accepted by the API but honestly reported as unconfirmed"

    # ---------------------------------------------------------------- Cost

    @card.case("cost: brain and voice spend are accounted", category="cost")
    async def _cost_accounting(ctx):
        store = CostStore(path=_tmp(ctx) / "costs.db")
        store.record("briefing", _BRAIN, 2000, 500)
        store.record("voice", "gemini-live", 0, 0, cost_usd=0.042)
        report = store.summary_7d()
        total = report.get("usd", 0)
        assert total > 0, report
        assert "voice" in report.get("by_feature", {}), report
        return f"7-day spend ${total:.4f}; voice row present"
