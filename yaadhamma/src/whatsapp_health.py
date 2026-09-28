"""WhatsApp self-check: prove the automation still works, or say what broke.

WhatsApp Web changes without notice, and when it does Yaadhamma can fail
silently or, worse, summarise the wrong thing (2026-09-25: script clicks
stopped opening chats; 2026-09-27: a reply's quoted text was read as the
message). This runs the same steps the digest relies on and reports each
one, so a break is announced instead of guessed at.

Checks, in order (all read-only; nothing is sent or marked read that the
digest would not already open):
  1. paired          WhatsApp Web is loaded and signed in
  2. list chats      the chat list can be read, with names and unread counts
  3. search          WhatsApp's search box finds a chat by its digits
  4. open + read     a watched chat opens and its messages parse
  5. message shape   messages have a sender, a time and text
  6. own chat        his own "(You)" chat can still be found (delivery)
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

import config
from task_manager import DEFAULT_DB
from whatsapp import WhatsAppError, WhatsAppNotPairedError, matches_watchlist


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class HealthReport:
    started: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def summary(self) -> str:
        if self.ok:
            return f"WhatsApp check passed ({len(self.checks)} of {len(self.checks)})."
        first = self.failed[0]
        return (
            f"WhatsApp check FAILED at '{first.name}': {first.detail} "
            f"({len(self.failed)} of {len(self.checks)} checks failed). "
            "Digests may be wrong or empty until this is fixed."
        )


class HealthLog:
    def __init__(self, path=None) -> None:
        self.path = path or DEFAULT_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS whatsapp_health (checked TEXT NOT NULL, "
                "ok INTEGER NOT NULL, report TEXT NOT NULL)"
            )

    def record(self, report: HealthReport) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO whatsapp_health VALUES (?, ?, ?)",
                (
                    report.started,
                    int(report.ok),
                    json.dumps({"checks": [c.__dict__ for c in report.checks]}),
                ),
            )

    def latest(self) -> dict | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT checked, ok, report FROM whatsapp_health "
                "ORDER BY checked DESC, rowid DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        checked, ok, report = row
        return {"time": checked, "ok": bool(ok), **json.loads(report)}


async def run_health_check(client) -> HealthReport:
    """Run every check. Never raises: a failure is a failed check."""
    report = HealthReport(started=datetime.now().isoformat(timespec="seconds"))

    def add(name, ok, detail=""):
        report.checks.append(Check(name=name, ok=ok, detail=detail))

    try:
        chats = await client.list_all_chats()
    except WhatsAppNotPairedError as exc:
        add("paired", False, str(exc)[:200])
        report.checks.append(Check("list chats", False, "skipped: not paired"))
        return report
    except Exception as exc:
        add("paired", True)
        add("list chats", False, f"the chat list could not be read: {exc}"[:200])
        return report
    add("paired", True)

    named = [c for c in chats if (c.get("name") or "").strip()]
    if not named:
        add("list chats", False, "the chat list came back with no names")
        return report
    add("list chats", True, f"{len(named)} chats")

    # Search: find a chat by the digits in its name (unsaved contacts).
    numeric = next(
        (c for c in named if sum(ch.isdigit() for ch in c["name"]) >= 7), None
    )
    if numeric is None:
        add("search", True, "no numbered chat to test with; skipped")
    else:
        digits = "".join(ch for ch in numeric["name"] if ch.isdigit())[-6:]
        try:
            found = await client.find_chat(digits)
            add("search", bool(found), "" if found else "search returned nothing")
        except WhatsAppError as exc:
            add("search", False, f"searching for a chat failed: {exc}"[:200])

    # Open and read a watched chat (the digest's core move). Several are
    # tried: one old, quiet chat that cannot be reached is not the same as
    # WhatsApp breaking (2026-09-28: a dormant group failed this check).
    watched = [
        c for c in named if matches_watchlist(c["name"], config.WHATSAPP_WATCHLIST)
    ]
    messages: list[dict] = []
    target = None
    tried: list[str] = []
    for candidate in [*watched, *named][:3]:
        try:
            read = await client.read_messages(candidate["name"], 5, exact=True)
        except WhatsAppError as exc:
            tried.append(f"{candidate['name']!r}: {exc}"[:150])
            continue
        if read.get("messages"):
            messages, target = read["messages"], candidate
            break
        tried.append(f"{candidate['name']!r}: opened but no messages")
    if target is None:
        add("open and read", False, "; ".join(tried)[:250])
        add("message shape", False, "skipped: no chat could be read")
        add("own chat", False, "skipped")
        return report
    add("open and read", True, f"{len(messages)} messages from {target['name']!r}")

    shaped = [
        m
        for m in messages
        if (m.get("text") or "").strip() and m.get("time") and m.get("sender")
    ]
    add(
        "message shape",
        bool(shaped),
        ""
        if shaped
        else "messages had no sender, time or text: the page layout has changed",
    )

    try:
        own = await client.find_chat(config.SELF_CHAT_NUMBER or "(You)")
        add("own chat", bool(own), "" if own else "his own chat was not found")
    except WhatsAppError as exc:
        add(
            "own chat",
            False,
            f"his own chat (digest delivery) was not found: {exc}"[:200],
        )
    return report
