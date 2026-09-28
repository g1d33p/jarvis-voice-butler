"""Voice/background tools for Google Calendar (Jeevan's deep.jeevan21 calendar).

Reading is low-risk. Adding an event always goes through the approval gate:
the event (title, day, date, time, clashes) is read back first and only a
clear yes creates it (Jeevan, 2026-09-27: "always read it back first").
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from gcal import (
    CalendarClient,
    GmailAuthError,
    day_bounds,
    find_clashes,
    format_event,
    local_zone,
)
from permissions import ApprovalManager

_SIGNIN_HINT = (
    "Google Calendar is not connected. Run this once on the Mac: "
    "uv run scripts/calendar_signin.py"
)


class CalendarTools:
    def __init__(
        self,
        client: CalendarClient | None = None,
        approvals: ApprovalManager | None = None,
    ) -> None:
        self._client = client or CalendarClient()
        self._approvals = approvals or ApprovalManager()

    @property
    def tools(self) -> list:
        return [self.calendar_agenda, self.calendar_add_event]

    async def _events(self, start: datetime, end: datetime) -> list[dict]:
        try:
            return await asyncio.to_thread(self._client.list_events, start, end)
        except GmailAuthError as exc:
            message = str(exc)
            if "not signed in" in message or "expired" in message:
                raise ToolError(_SIGNIN_HINT) from exc
            raise ToolError(f"Google Calendar did not answer: {message}") from exc

    @function_tool()
    async def calendar_agenda(
        self, context: RunContext, start_date: str = "", days: int = 1
    ) -> dict[str, object]:
        """His Google Calendar for one or more days, with any clashes.

        Use for "what's on today", "am I free Thursday afternoon", "what's my
        week look like".

        Args:
            start_date: First day as YYYY-MM-DD. Empty means today.
            days: How many days to cover (1 = just that day, 7 = a week).
        """
        try:
            first = (
                date.fromisoformat(start_date)
                if start_date
                else datetime.now(local_zone()).date()
            )
        except ValueError as exc:
            raise ToolError(
                "Give the date as YYYY-MM-DD, for example 2026-10-01."
            ) from exc
        days = max(1, min(int(days or 1), 31))
        start, _ = day_bounds(first)
        end = start + timedelta(days=days)
        events = await self._events(start, end)
        by_day: dict[str, list[str]] = {}
        for event in events:
            if event["declined"]:
                continue
            when = datetime.fromisoformat(event["start"]).astimezone(local_zone())
            by_day.setdefault(f"{when:%A %d %B}", []).append(format_event(event))
        return {
            "from": f"{first:%A %d %B %Y}",
            "days": days,
            "events_by_day": by_day or "No events.",
            "clashes": [c.describe() for c in find_clashes(events)],
        }

    @function_tool()
    async def calendar_add_event(
        self,
        context: RunContext,
        title: str,
        start: str,
        duration_minutes: int = 60,
        location: str = "",
    ) -> dict[str, object]:
        """Add an event to his Google Calendar. Always read back first.

        The event is read back to him (title, weekday, date, time, and any
        clash) and is only created after a clear yes.

        Args:
            title: Short event title, e.g. "Interview with Acme".
            start: Start as YYYY-MM-DDTHH:MM in his local time, e.g. 2026-10-01T14:00.
            duration_minutes: Length in minutes (default 60).
            location: Optional place or meeting link.
        """
        title = (title or "").strip()
        if not title:
            raise ToolError("An event needs a title.")
        try:
            begin = datetime.fromisoformat(start).replace(tzinfo=local_zone())
        except ValueError as exc:
            raise ToolError(
                "Give the start as YYYY-MM-DDTHH:MM, e.g. 2026-10-01T14:00."
            ) from exc
        minutes = max(5, min(int(duration_minutes or 60), 24 * 60))
        finish = begin + timedelta(minutes=minutes)
        if begin < datetime.now(local_zone()) - timedelta(minutes=5):
            raise ToolError(
                f"{begin:%A %d %B, %I:%M %p} is in the past. Check the date with him."
            )

        overlapping = [
            e
            for e in await self._events(begin, finish)
            if not e["all_day"] and e["busy"] and not e["declined"]
        ]
        clash_note = (
            " It clashes with: " + ", ".join(e["title"] for e in overlapping) + "."
            if overlapping
            else ""
        )
        spoken = (
            f'add "{title}" to your calendar on {begin:%A %d %B}, '
            f"{begin:%I:%M %p} to {finish:%I:%M %p}"
            + (f" at {location}" if location else "")
        ).replace(" 0", " ")
        description = spoken + "." + clash_note

        async def execute() -> dict[str, object]:
            created = await asyncio.to_thread(
                self._client.create_event, title, begin, finish, location
            )
            try:
                fetched = await asyncio.to_thread(self._client.get_event, created["id"])
            except Exception:
                return {
                    "added": True,
                    "event": format_event(created),
                    "clash_warning": clash_note.strip(),
                    "verified": False,
                    "verification": (
                        "the event was created but re-fetching it failed, so "
                        "the creation is unconfirmed — check the calendar "
                        "before assuming it is there"
                    ),
                }
            if fetched.get("id") != created["id"]:
                return {
                    "added": True,
                    "event": format_event(created),
                    "clash_warning": clash_note.strip(),
                    "verified": False,
                    "verification": (
                        "the event was created but the re-fetch did not match, "
                        "so the creation is unconfirmed — check the calendar "
                        "before assuming it is there"
                    ),
                }
            return {
                "added": True,
                "event": format_event(fetched),
                "clash_warning": clash_note.strip(),
                "verified": True,
                "verification": "re-fetched the created event by id and it matches",
            }

        return await self._approvals.gate(
            tool_name="calendar_add_event",
            description=description,
            context=context,
            execute=execute,
            args={"title": title, "start": begin.isoformat(), "minutes": minutes},
        )
