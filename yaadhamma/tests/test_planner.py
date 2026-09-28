"""Planning and suggestions (2026-09-28)."""

import json
from datetime import datetime, timedelta

import pytest

import config
from digest import DigestResult, DigestStore
from gcal import event_summary, local_zone
from memory_store import MemoryStore
from meta_client import ModelTurn
from planner import (
    PlanTools,
    find_interviews,
    free_slots,
    gather,
    make_plan,
    research_interviews,
)

TZ = local_zone()
DAY = datetime(2026, 9, 29, tzinfo=TZ)


def _event(eid, title, start_h, end_h, **extra):
    return event_summary(
        {
            "id": eid,
            "summary": title,
            "start": {"dateTime": (DAY + timedelta(hours=start_h)).isoformat()},
            "end": {"dateTime": (DAY + timedelta(hours=end_h)).isoformat()},
            **extra,
        }
    )


def test_free_time_is_the_gaps_between_events() -> None:
    events = [
        _event("a", "Standup", 10, 11),
        _event("b", "Saayam sync", 14, 15),
        _event(
            "c",
            "Declined thing",
            12,
            13,
            attendees=[{"self": True, "responseStatus": "declined"}],
        ),
    ]
    slots = free_slots(events, DAY, now=DAY + timedelta(hours=8))
    assert slots == ["09:00-10:00", "11:00-14:00", "15:00-19:00"]


def test_free_time_starts_from_now() -> None:
    slots = free_slots(
        [_event("a", "Standup", 10, 11)], DAY, now=DAY + timedelta(hours=10, minutes=30)
    )
    assert slots == ["11:00-19:00"]


def test_interviews_are_spotted_in_calendar_and_email() -> None:
    events = [_event("a", "Interview with Acme", 14, 15), _event("b", "Standup", 9, 10)]
    emails = [
        {
            "from": "Priya <p@acme.com>",
            "subject": "Your interview on Thursday",
            "snippet": "confirm 2pm",
        },
        {"from": "Bank", "subject": "Statement", "snippet": "..."},
    ]
    found = find_interviews(events, emails)
    assert [f.get("event") or f.get("subject") for f in found] == [
        "Interview with Acme",
        "Your interview on Thursday",
    ]


async def test_no_web_research_when_switched_off(monkeypatch) -> None:
    monkeypatch.setattr(config, "PLAN_WEB_RESEARCH", False)
    assert await research_interviews([{"event": "Interview with Acme"}]) == ""


class FakeCalendar:
    def __init__(self, events):
        self.events = events

    def list_events(self, start, end, limit=50):
        return self.events


class FakeGmail:
    label = "p1"

    def search_mail(self, query, limit=10):
        return [
            {
                "id": "1",
                "from": "Priya <p@acme.com>",
                "subject": "Interview slot Thursday?",
                "snippet": "Could you confirm 2pm?",
                "internal_date": 1,
            }
        ]


class FakeBrain:
    def __init__(self):
        self.calls = []

    async def generate(
        self, model, messages, tools, reasoning_effort=None, feature="test"
    ):
        self.calls.append((model, messages))
        return ModelTurn(
            calls=[],
            text="*Top priorities*\n1. Confirm Thursday with Priya (email from Priya, Acme)",
        )


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PLAN_WEB_RESEARCH", False)
    memory = MemoryStore(tmp_path / "memory.db")
    memory.remember(
        "commitment",
        "Share the roadmap with SC1-Executives by Friday.",
        provenance="learned overnight 27 Sep 2026 from SC1-Executives",
    )
    memory.remember("person", "Priya is a recruiter at Acme.")
    digests = DigestStore(tmp_path / "db")
    digests.record(
        datetime.now(),
        DigestResult(
            status="sent", summary="*Needs you*\n- Ravi asked about Cognito (SC1)"
        ),
    )
    return memory, digests


async def test_plan_context_brings_everything_together(stores) -> None:
    memory, digests = stores
    now = DAY + timedelta(hours=8)
    context = await gather(
        "today",
        FakeCalendar([_event("a", "Interview with Acme", 14, 15)]),
        [FakeGmail()],
        memory,
        digests,
        now,
    )

    assert context["commitments"][0]["promise"].startswith("Share the roadmap")
    assert "SC1-Executives" in context["commitments"][0]["source"]
    assert context["known"] == [
        {"kind": "person", "content": "Priya is a recruiter at Acme."}
    ]
    assert "Ravi asked about Cognito" in context["recent_digests"][0]["summary"]
    assert "Interview slot Thursday?" in context["email"][0]["subject"]
    assert "<<UNTRUSTED_CONTENT" in context["email"][0]["subject"]
    assert context["free_time_today"] == ["09:00-14:00", "15:00-19:00"]
    assert context["interviews"][0]["event"] == "Interview with Acme"


async def test_plan_uses_the_stronger_model_and_his_focus(stores) -> None:
    memory, digests = stores
    brain = FakeBrain()
    text = await make_plan(
        brain, "today", None, [], memory, digests, focus="I have two free hours"
    )

    model, messages = brain.calls[0]
    assert model == config.ESCALATION_MODEL
    assert "He asked specifically: I have two free hours" in messages[0]["content"]
    assert "never invent" in messages[0]["content"].lower()
    assert json.loads(messages[1]["content"])["calendar"] == "not connected"
    assert text.startswith("*Top priorities*")


async def test_week_plan_uses_the_week_template(stores) -> None:
    memory, digests = stores
    brain = FakeBrain()
    await make_plan(brain, "Week", None, [], memory, digests)
    assert "week ahead" in brain.calls[0][1][0]["content"]


async def test_voice_tool_returns_the_plan(stores) -> None:
    memory, digests = stores
    # No real calendar or Gmail in tests, even on a Mac that has them linked.
    tools = PlanTools(
        brain=FakeBrain(), memory=memory, digests=digests, calendar=None, gmail=[]
    )
    result = await tools.make_a_plan(None, "today")
    assert "Confirm Thursday with Priya" in result["plan"]


async def test_plan_rules_forbid_padding_and_calendar_echo(stores) -> None:
    """2026-09-28 preview: the day was padded with invented blocks and the week
    plan listed the Daily Scrum every day."""
    memory, digests = stores
    brain = FakeBrain()
    await make_plan(brain, "today", None, [], memory, digests)
    rules = brain.calls[0][1][0]["content"]
    assert "never fill the day with invented work" in rules
    assert (
        "Mention a\nrecurring meeting once" in rules
        or "recurring meeting once" in rules
    )
    assert (
        "Job alerts and recruiter outreach that ask nothing of him are optional"
        in rules
    )


def test_plan_rules_forbid_filler_blocks_and_repeated_meetings() -> None:
    from planner import PLAN_INSTRUCTIONS

    day = PLAN_INSTRUCTIONS.format(horizon="day", limit=1600, focus="")
    assert "never fill the day with invented work" in day
    assert "A meeting on the calendar is not a priority by itself" in day
    assert "Mention a\nrecurring meeting once" in day
    assert "do not list his calendar back to him" in day


def test_interviews_soon_covers_today_and_tomorrow_only() -> None:
    from planner import interviews_soon

    events = [
        _event("a", "Interview with Acme", 14, 15),
        event_summary(
            {
                "id": "b",
                "summary": "Interview with Beta",
                "start": {"dateTime": (DAY + timedelta(days=1, hours=10)).isoformat()},
                "end": {"dateTime": (DAY + timedelta(days=1, hours=11)).isoformat()},
            }
        ),
        event_summary(
            {
                "id": "c",
                "summary": "Interview with Gamma",
                "start": {"dateTime": (DAY + timedelta(days=5)).isoformat()},
                "end": {"dateTime": (DAY + timedelta(days=5, hours=1)).isoformat()},
            }
        ),
    ]
    soon = interviews_soon(events, now=DAY + timedelta(hours=8))
    assert [e["title"] for e in soon] == ["Interview with Acme", "Interview with Beta"]
