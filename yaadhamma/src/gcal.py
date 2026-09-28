"""Google Calendar for Yaadhamma: read the schedule, spot clashes, add events.

Uses the same Google Cloud sign-in client as Gmail (YAADHAMMA_GOOGLE_CLIENT_ID
/ _SECRET) with its own token (~/.yaadhamma/gcal-token-<label>.json) and the
calendar.events permission: read events and create them. Nothing here
deletes or edits existing events.

One-time setup (Jeevan, 2026-09-27: deep.jeevan21's calendar):
  1. Google Cloud Console > APIs & Services > Library > "Google Calendar API"
     > Enable (same project as the Gmail sign-in client).
  2. uv run scripts/calendar_signin.py
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from gmail import GmailAuthError, GmailClient

CALENDAR_API = "https://www.googleapis.com/calendar/v3"
CALENDAR_SCOPES = "https://www.googleapis.com/auth/calendar.events"
DEFAULT_LABEL = "calendar"


def local_zone() -> ZoneInfo:
    return ZoneInfo(os.environ.get("YAADHAMMA_TIMEZONE", "America/Chicago"))


class CalendarClient(GmailClient):
    SCOPES = CALENDAR_SCOPES
    API = CALENDAR_API
    TOKEN_PREFIX = "gcal-token-"
    SERVICE = "Google Calendar"
    SIGNIN_COMMAND = "uv run scripts/calendar_signin.py"

    def __init__(self, label: str = DEFAULT_LABEL, **kwargs) -> None:
        super().__init__(label=label, **kwargs)

    def get_profile(self) -> dict:
        """The calendar's owner address (the primary calendar's id)."""
        cal = self._get("/calendars/primary")
        return {"email": cal.get("id", "")}

    def list_events(
        self, start: datetime, end: datetime, limit: int = 50
    ) -> list[dict]:
        resp = self._get(
            "/calendars/primary/events",
            {
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": max(1, min(limit, 250)),
            },
        )
        return [event_summary(e) for e in resp.get("items", [])]

    def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        location: str = "",
        notes: str = "",
    ) -> dict:
        zone = str(local_zone())
        body = {
            "summary": title,
            "start": {"dateTime": start.isoformat(), "timeZone": zone},
            "end": {"dateTime": end.isoformat(), "timeZone": zone},
        }
        if location:
            body["location"] = location
        if notes:
            body["description"] = notes
        created = self._post("/calendars/primary/events", body)
        return event_summary(created)


def _parse_when(value: dict) -> tuple[datetime | None, bool]:
    """Google gives dateTime for timed events and date for all-day ones."""
    if value.get("dateTime"):
        return datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00")), False
    if value.get("date"):
        day = date.fromisoformat(value["date"])
        return datetime.combine(day, time.min, tzinfo=local_zone()), True
    return None, False


def event_summary(event: dict) -> dict:
    start, all_day = _parse_when(event.get("start", {}))
    end, _ = _parse_when(event.get("end", {}))
    mine = next((a for a in event.get("attendees", []) if a.get("self")), {})
    return {
        "id": event.get("id", ""),
        "title": event.get("summary", "(no title)"),
        "start": start.isoformat() if start else "",
        "end": end.isoformat() if end else "",
        "all_day": all_day,
        "location": event.get("location", ""),
        # Events he declined, or marked "free", can't clash.
        "declined": mine.get("responseStatus") == "declined",
        "busy": event.get("transparency", "opaque") != "transparent",
    }


@dataclass
class Clash:
    first: dict
    second: dict

    def describe(self) -> str:
        start = datetime.fromisoformat(self.second["start"]).astimezone(local_zone())
        return (
            f"{self.first['title']} overlaps {self.second['title']} "
            f"({start:%a %d %b, %I:%M %p})"
        )

    @property
    def key(self) -> str:
        return "|".join(sorted([self.first["id"], self.second["id"]]))


def find_clashes(events: list[dict]) -> list[Clash]:
    """Timed, busy, not-declined events that overlap in time."""
    timed = [
        e
        for e in events
        if not e["all_day"] and e["busy"] and not e["declined"] and e["start"]
    ]
    timed.sort(key=lambda e: e["start"])
    clashes = []
    for i, a in enumerate(timed):
        a_end = datetime.fromisoformat(a["end"])
        for b in timed[i + 1 :]:
            if datetime.fromisoformat(b["start"]) >= a_end:
                break
            clashes.append(Clash(a, b))
    return clashes


def day_bounds(day: date) -> tuple[datetime, datetime]:
    zone = local_zone()
    start = datetime.combine(day, time.min, tzinfo=zone)
    return start, start + timedelta(days=1)


def format_event(event: dict) -> str:
    if event["all_day"]:
        return f"All day: {event['title']}"
    start = datetime.fromisoformat(event["start"]).astimezone(local_zone())
    end = datetime.fromisoformat(event["end"]).astimezone(local_zone())
    where = f" ({event['location']})" if event["location"] else ""
    return f"{start:%I:%M %p}-{end:%I:%M %p}: {event['title']}{where}".lstrip("0")


__all__ = [
    "CalendarClient",
    "Clash",
    "GmailAuthError",
    "day_bounds",
    "find_clashes",
    "format_event",
    "local_zone",
]
