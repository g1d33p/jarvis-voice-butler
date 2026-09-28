"""Planning and suggestions: the "Jarvis" part (Jeevan, 2026-09-28).

Pulls together everything Yaadhamma knows about his day or week and asks the
stronger model for a prioritised plan with reasons:

  calendar       events, free time, clashes
  commitments    promises he made (from overnight learning)
  memory         people, projects and facts she has learned
  digests        what the recent digests flagged as needing him
  email          the last two days of inbox mail (who is waiting on him)
  interviews     upcoming interviews, with a short look at the company's
                 official site and recent news (web search)

"Connections" (two separate pieces of information that matter together,
e.g. a question someone asked in a chat and a meeting with that person
today) are part of the plan, always with where they came from.

The plan is advice only: nothing here sends, adds or changes anything.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, time, timedelta

import config

PLAN_INSTRUCTIONS = """You are Yaadhamma, Jeevan's personal assistant, planning his {horizon}.
You get JSON with his calendar (events, free time today, clashes), his open
commitments, what you have learned about his work and people, what recent
digests flagged, his recent email, and any interview research.

Write a plan he can act on, plain text for WhatsApp, under {limit} characters:

*Top priorities*
1-3 items (up to 5 for a week). Each: what to do, why now, and the source in
brackets, e.g. (promised in SC1-Executives, 27 Sep) or (email from Priya, Acme).

*Suggested schedule*
Fit the priorities into his FREE time slots only, as time blocks, e.g.
"10:00-11:30 Roadmap for SC1". Skip this section for a week plan; give a
short day-by-day outline instead.

*Connections*
Only real links between separate pieces of information that he might miss:
a question someone asked + a meeting with that person, an email that changes
a calendar event, a promise that collides with a full calendar. Each with both
sources. Leave the section out if there are none. Never force one.

*Interview prep* (only if an interview is coming up)
Company and role, what the recruiter asked for, what to revise, 2-3 good
questions to ask. Use the research given; say nothing about the company that
is not in it.

*Heads-up*
Clashes, overdue promises, anything slipping. Leave out if nothing.

Rules: never invent facts, people, times or deadlines; everything must come
from the data. If something is uncertain, say so briefly. Be direct and
practical, like a sharp chief of staff. {focus}"""

WORK_START, WORK_END = time(9, 0), time(19, 0)


def free_slots(events: list[dict], day_start: datetime, now: datetime) -> list[str]:
    """Gaps of 30+ minutes between his timed events, 9am-7pm, from now on."""
    zone = day_start.tzinfo
    start = max(datetime.combine(day_start.date(), WORK_START, tzinfo=zone), now)
    end = datetime.combine(day_start.date(), WORK_END, tzinfo=zone)
    busy = sorted(
        (datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"]))
        for e in events
        if not e["all_day"] and e["busy"] and not e["declined"] and e["start"]
    )
    slots, cursor = [], start
    for b_start, b_end in busy:
        if b_start > cursor and (b_start - cursor) >= timedelta(minutes=30):
            slots.append(f"{cursor:%H:%M}-{min(b_start, end):%H:%M}")
        cursor = max(cursor, b_end)
        if cursor >= end:
            break
    if end - cursor >= timedelta(minutes=30):
        slots.append(f"{cursor:%H:%M}-{end:%H:%M}")
    return slots


def find_interviews(events: list[dict], emails: list[dict]) -> list[dict]:
    """Upcoming interview events, plus interview-related emails."""
    found = [
        {"event": e["title"], "when": e["start"]}
        for e in events
        if "interview" in e["title"].lower() and not e["declined"]
    ]
    found += [
        {"email_from": m["from"], "subject": m["subject"], "snippet": m["snippet"]}
        for m in emails
        if "interview" in (m["subject"] + " " + m["snippet"]).lower()
    ][:5]
    return found


async def research_interviews(interviews: list[dict]) -> str:
    """A short look at each company on the web (official site, recent news).

    Uses Gemini with Google Search. Returns "" if unavailable; planning never
    depends on it.
    """
    if not interviews or not config.PLAN_WEB_RESEARCH:
        return ""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY"))
        prompt = (
            "For each interview below, identify the company and role if you can, then "
            "summarise in 3-4 short lines each: what the company does, anything notable "
            "from its official website, and 1-2 recent news items. Prefer official sources. "
            "If the company is unclear, say so.\n"
            + json.dumps(interviews, ensure_ascii=False)
        )
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=config.ESCALATION_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())]
                ),
            ),
            timeout=45,
        )
        return (response.text or "").strip()[:2500]
    except Exception:
        return ""


async def gather(
    horizon: str, calendar, gmail_clients, memory, digests, now: datetime
) -> dict:
    """Everything the plan needs, collected without any browser or chat access."""
    from digest import collect_email

    days = 7 if horizon == "week" else 1
    context: dict = {"now": f"{now:%A %d %B %Y, %H:%M}", "horizon": horizon}

    events: list[dict] = []
    if calendar is not None:
        from gcal import day_bounds, find_clashes, format_event

        day_start, _ = day_bounds(now.date())
        try:
            events = await asyncio.to_thread(
                calendar.list_events, day_start, day_start + timedelta(days=days)
            )
        except Exception as exc:
            context["calendar_error"] = str(exc)[:150]
        today = [
            e
            for e in events
            if e["start"] and datetime.fromisoformat(e["start"]).date() == now.date()
        ]
        context["calendar"] = [
            {
                "day": f"{datetime.fromisoformat(e['start']):%a %d %b}",
                "event": format_event(e),
            }
            for e in events
            if not e["declined"]
        ]
        context["free_time_today"] = free_slots(today, day_start, now)
        context["clashes"] = [c.describe() for c in find_clashes(events)]
    else:
        context["calendar"] = "not connected"

    context["commitments"] = [
        {"promise": m["content"], "source": m["provenance"]}
        for m in memory.list_memories(kind="commitment", limit=30)
    ]
    context["known"] = [
        {"kind": m["kind"], "content": m["content"]}
        for m in memory.list_memories(limit=60)
        if m["kind"] != "commitment"
    ]
    context["recent_digests"] = digests.recent_summaries(hours=24 if days == 1 else 72)

    email = await collect_email(gmail_clients, now - timedelta(hours=48), limit=20)
    context["email"] = email["emails"][:40]

    interviews = find_interviews(events, context["email"])
    if interviews:
        context["interviews"] = interviews
        research = await research_interviews(interviews)
        if research:
            context["interview_research"] = research
    return context


async def make_plan(
    brain, horizon: str, calendar, gmail_clients, memory, digests, focus: str = ""
) -> str:
    """The plan text for "today" or "week"."""
    from gcal import local_zone

    horizon = "week" if horizon.strip().lower().startswith("week") else "today"
    now = datetime.now(local_zone())
    context = await gather(horizon, calendar, gmail_clients, memory, digests, now)
    instructions = PLAN_INSTRUCTIONS.format(
        horizon="week ahead" if horizon == "week" else "day",
        limit=2200 if horizon == "week" else 1600,
        focus=f"He asked specifically: {focus}" if focus else "",
    )
    turn = await brain.generate(
        config.ESCALATION_MODEL,
        [
            {"role": "system", "content": instructions},
            {
                "role": "user",
                "content": json.dumps(context, ensure_ascii=False, default=str),
            },
        ],
        [],
    )
    return (turn.text or "").strip()


class PlanTools:
    """Voice tool: "what should I do today?", "plan my week"."""

    _UNSET = object()

    def __init__(
        self, brain=None, memory=None, digests=None, calendar=_UNSET, gmail=_UNSET
    ) -> None:
        self._brain = brain
        self._memory = memory
        self._digests = digests
        self._calendar = calendar
        self._gmail = gmail

    @property
    def tools(self) -> list:
        return [self.make_a_plan]

    def _sources(self):
        from digest import DigestStore
        from gcal import CalendarClient
        from gmail import GmailClient, discover_labels
        from memory_store import MemoryStore
        from meta_client import brain_client_from_config

        if self._calendar is PlanTools._UNSET:
            calendar = (
                CalendarClient() if CalendarClient().token_path.exists() else None
            )
        else:
            calendar = self._calendar
        if self._gmail is PlanTools._UNSET:
            gmail = [GmailClient(label=label) for label in discover_labels()]
        else:
            gmail = self._gmail
        return (
            self._brain or brain_client_from_config(),
            calendar,
            gmail,
            self._memory or MemoryStore(),
            self._digests or DigestStore(),
        )

    from livekit.agents import RunContext as _RunContext
    from livekit.agents import function_tool as _function_tool

    @_function_tool()
    async def make_a_plan(
        self, context: _RunContext, horizon: str = "today", focus: str = ""
    ) -> dict[str, object]:
        """Suggest what he should do: a prioritised plan with reasons.

        Use for "what should I focus on today?", "plan my week", "what am I
        forgetting?", "I have two free hours, what should I do?". It combines
        his calendar, promises, chats, email and what you have learned. Read
        the top priorities out briefly; offer to add time blocks to his
        calendar (through run_task, read back first) or to draft replies.

        Args:
            horizon: "today" or "week".
            focus: Optional extra ask in his words, e.g. "I have two free hours".
        """
        brain, calendar, gmail, memory, digests = self._sources()
        plan = await make_plan(brain, horizon, calendar, gmail, memory, digests, focus)
        return {"plan": plan or "I could not put a plan together just now."}
