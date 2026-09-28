"""Google Calendar: reading, clashes, read-back-first adding (2026-09-27)."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from livekit.agents.llm import ToolError

from audit import AuditLog
from calendar_tools import CalendarTools
from digest import DigestStore, morning_brief_text, run_digest, run_morning_brief
from gcal import CalendarClient, event_summary, find_clashes, format_event, local_zone
from gmail import GmailClient
from permissions import ApprovalManager

TZ = local_zone()


def _event(eid, title, start, end, **extra):
    return event_summary(
        {
            "id": eid,
            "summary": title,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
            **extra,
        }
    )


def at(day_offset, hour, minute=0):
    base = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return base + timedelta(days=day_offset, hours=hour, minutes=minute)


# ----------------------------------------------------------------- basics


def test_calendar_uses_its_own_token_scope_and_api() -> None:
    client = CalendarClient()
    assert client.token_path.name == "gcal-token-calendar.json"
    assert "calendar.events" in client.SCOPES
    assert client.API.startswith("https://www.googleapis.com/calendar")
    # Gmail is unchanged by the shared sign-in code.
    gmail = GmailClient(label="personal1")
    assert gmail.token_path.name == "gmail-token-personal1.json"
    assert "gmail.readonly" in gmail.SCOPES


def test_overlapping_events_clash_but_back_to_back_do_not() -> None:
    events = [
        _event("a", "Standup", at(1, 9), at(1, 10)),
        _event("b", "Interview with Acme", at(1, 9, 30), at(1, 10, 30)),
        _event("c", "Lunch", at(1, 10, 30), at(1, 11, 30)),  # starts as b ends
    ]
    clashes = find_clashes(events)
    assert [(c.first["title"], c.second["title"]) for c in clashes] == [
        ("Standup", "Interview with Acme")
    ]


def test_declined_free_and_all_day_events_never_clash() -> None:
    events = [
        _event("a", "Standup", at(1, 9), at(1, 10)),
        _event(
            "b",
            "Declined",
            at(1, 9),
            at(1, 10),
            attendees=[{"self": True, "responseStatus": "declined"}],
        ),
        _event("c", "Focus (free)", at(1, 9), at(1, 10), transparency="transparent"),
        event_summary(
            {
                "id": "d",
                "summary": "Holiday",
                "start": {"date": "2026-10-01"},
                "end": {"date": "2026-10-02"},
            }
        ),
    ]
    assert find_clashes(events) == []


def test_format_event_reads_naturally() -> None:
    event = _event("a", "Interview", at(1, 14), at(1, 15), location="Zoom")
    assert format_event(event) == "2:00 PM-03:00 PM: Interview (Zoom)"


# ----------------------------------------------------------------- tools


class FakeCalendar:
    def __init__(self, events=()):
        self.events = list(events)
        self.created = []

    def list_events(self, start, end, limit=50):
        return [
            e
            for e in self.events
            if start <= datetime.fromisoformat(e["start"]) < end
            or (
                datetime.fromisoformat(e["start"]) < end
                and datetime.fromisoformat(e["end"]) > start
            )
        ]

    def create_event(self, title, start, end, location="", notes=""):
        event = _event(f"new{len(self.created)}", title, start, end, location=location)
        self.created.append(event)
        return event


def _tools(calendar, tmp_path):
    return CalendarTools(
        client=calendar,
        approvals=ApprovalManager(audit=AuditLog(path=tmp_path / "a.jsonl")),
    )


def _said(text):
    item = SimpleNamespace(
        type="message", role="user", text_content=text, id="u1", created_at=0
    )
    return SimpleNamespace(
        session=SimpleNamespace(history=SimpleNamespace(items=[item]))
    )


async def test_agenda_lists_the_day_and_its_clashes(tmp_path) -> None:
    calendar = FakeCalendar(
        [
            _event("a", "Standup", at(0, 9), at(0, 10)),
            _event("b", "Interview", at(0, 9, 30), at(0, 10, 30)),
        ]
    )
    result = await _tools(calendar, tmp_path).calendar_agenda(None)
    [day] = result["events_by_day"].values()
    assert day[0].endswith("Standup") and day[1].endswith("Interview")
    assert len(result["clashes"]) == 1


async def test_adding_an_event_always_reads_it_back_first(tmp_path) -> None:
    calendar = FakeCalendar([_event("a", "Standup", at(2, 14), at(2, 14, 30))])
    tools = _tools(calendar, tmp_path)
    start = at(2, 14).strftime("%Y-%m-%dT%H:%M")

    with pytest.raises(ToolError, match="needs the user's approval") as asked:
        await tools.calendar_add_event(
            _said("add interview with acme"), "Interview with Acme", start
        )
    question = str(asked.value)
    assert "Interview with Acme" in question
    assert at(2, 14).strftime("%A") in question  # the weekday is read back
    assert "clashes with: Standup" in question
    assert calendar.created == []  # nothing added before the yes

    from permissions import ApprovalTools

    await ApprovalTools(approvals=tools._approvals).approve_pending_action(
        _said("Yes."), "Yes."
    )
    assert [e["title"] for e in calendar.created] == ["Interview with Acme"]


async def test_past_dates_are_refused(tmp_path) -> None:
    with pytest.raises(ToolError, match="in the past"):
        await _tools(FakeCalendar(), tmp_path).calendar_add_event(
            None,
            "Old",
            (datetime.now(TZ) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"),
        )


# ----------------------------------------------------------------- digest + brief


class FakeWhatsApp:
    def __init__(self):
        self.sent = []

    async def list_all_chats(self):
        return [{"name": "+1 (940) 843-8446 (You)", "unread": 0}]

    async def find_chat(self, query):
        return "+1 (940) 843-8446 (You)"

    async def send_message(self, name, text):
        self.sent.append(text)
        return {"sent": True}


class FakeBrain:
    def __init__(self):
        self.calls = []

    async def generate(self, model, messages, tools, reasoning_effort=None):
        from meta_client import ModelTurn

        self.calls.append(messages)
        return ModelTurn(
            calls=[], text="*Calendar*\n- Clash on Thursday.", tokens_in=1, tokens_out=1
        )


def test_morning_brief_lists_today_and_clashes() -> None:
    today = [_event("a", "Standup", at(0, 9), at(0, 10))]
    clashes = find_clashes(
        [
            _event("b", "X", at(2, 9), at(2, 10)),
            _event("c", "Y", at(2, 9, 30), at(2, 10)),
        ]
    )
    text = morning_brief_text(today, clashes, datetime.now(TZ))
    assert "*Today*" in text and "Standup" in text
    assert "*Clashes this week*" in text and "X overlaps Y" in text


async def test_morning_brief_is_sent_to_his_own_chat(tmp_path, monkeypatch) -> None:
    import config

    monkeypatch.setattr(config, "SELF_CHAT_NUMBER", "19408438446")
    whatsapp = FakeWhatsApp()
    calendar = FakeCalendar([_event("a", "Standup", at(0, 11), at(0, 12))])

    result = await run_morning_brief(whatsapp, calendar, DigestStore(tmp_path / "db"))

    assert result.status == "morning"
    assert "Standup" in whatsapp.sent[0]


async def test_a_new_clash_triggers_a_digest_once(tmp_path, monkeypatch) -> None:
    import config

    monkeypatch.setattr(config, "SELF_CHAT_NUMBER", "19408438446")
    monkeypatch.setattr(config, "WHATSAPP_WATCHLIST", ["SC1"])
    calendar = FakeCalendar(
        [
            _event("a", "Standup", at(3, 9), at(3, 10)),
            _event("b", "Interview", at(3, 9, 30), at(3, 10)),
        ]
    )
    store = DigestStore(tmp_path / "db")

    first = await run_digest(FakeWhatsApp(), FakeBrain(), store, calendar=calendar)
    second = await run_digest(FakeWhatsApp(), FakeBrain(), store, calendar=calendar)

    assert first.status == "sent"  # chats and email quiet, but a new clash
    assert second.status == "quiet"  # the same clash is not reported again
