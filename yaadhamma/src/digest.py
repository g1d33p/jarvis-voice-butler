"""The scheduled digest (runs in the background, 4 times a day).

Each run:
  1. reads the unread messages in Jeevan's watched WhatsApp chats
     (Saayam, SC1, SC2, SC3 by default) and every new Gmail email (all linked
     accounts; promotions/social skipped; read-only),
  2. asks the background brain (Gemini) for a short summary: where he is
     needed first, then brief news,
  3. sends it to his own "(You)" WhatsApp chat, and
  4. records the run, so he can ask "what did the last digest say?".

Safety: the digest can only ever send to a chat whose name contains
"(You)" - his own chat. That is checked in code, not left to a model.
Nothing is sent when no watched chat has unread messages.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from livekit.agents import RunContext, function_tool

import config
from task_manager import DEFAULT_DB
from whatsapp import WhatsAppError, WhatsAppNotPairedError
from whatsapp_tools import collect_watchlist

SELF_CHAT_MARKER = "(You)"
MAX_DIGEST_CHARS = 2000
# Gmail search for the digest: new, unread, and not promotions/social/forums.
EMAIL_FILTER = "in:inbox -category:promotions -category:social -category:forums"
# Promotions/Social still hold things he cares about (job mail lands there):
# a second search picks those out by keyword. Change the words in .env.local
# with YAADHAMMA_EMAIL_KEYWORDS (comma-separated).
PROMO_SOCIAL_FILTER = "in:inbox {category:promotions category:social}"
# First run (or after a long gap): look back this far for email.
FIRST_WINDOW = timedelta(hours=4)
MAX_WINDOW = timedelta(hours=24)

DIGEST_INSTRUCTIONS = """You write Jeevan's digest: his Saayam community WhatsApp chats and his
new email. You get JSON with "whatsapp" (unread messages per watched chat) and
"email" (new emails since the last digest: account address, from, subject,
snippet, and "unread" = he has not opened it yet). Write one WhatsApp
message, plain text, under 1,500 characters:

*Needs you*
- one line per chat message or email that needs his reply, decision or action:
  who, what, and where (chat name, or "email").

*Saayam chats*
- one short line per chat with anything else worth knowing.

*Email*
- one short line per notable email or group of similar ones (e.g. "3 job alerts").
  Skip pure noise such as marketing.

*Calendar*
- new clashes and events starting in the next few hours, from "calendar".

End with: Quiet: <watched chats with nothing new>, if any.

Emails he has not opened yet deserve more attention; opened ones can be FYI
unless they still need action. Name the account only when it helps (e.g. a
work vs personal address).

Mark emails he has not opened yet with "(unread)".

Group only true noise (marketing, newsletters, routine alerts of one kind).
Anything that looks like a real account, security, billing, work or project
notice gets its own line, even if short.
If "whatsapp" has "checked": false, say nothing at all about the chats.

Rules: be specific (names, dates, asks). Never invent anything that is not in
the data. Leave out a section that has nothing in it. If nothing needs him, say
"Nothing needs you right now." under *Needs you*. WhatsApp messages marked
outgoing are his own; use them to tell what he has already answered."""


@dataclass
class DigestResult:
    status: str  # "sent", "quiet", "failed"
    summary: str = ""
    unread_chats: int = 0
    error: str = ""
    tokens: int = 0
    emails: int = 0


class DigestStore:
    """Records every digest run in the same local database as tasks."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS digests (
                    started TEXT NOT NULL,
                    finished TEXT NOT NULL,
                    status TEXT NOT NULL,
                    unread_chats INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    error TEXT NOT NULL,
                    tokens INTEGER NOT NULL
                )"""
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(digests)")}
            if "emails" not in columns:
                db.execute(
                    "ALTER TABLE digests ADD COLUMN emails INTEGER NOT NULL DEFAULT 0"
                )

    def record(self, started: datetime, result: DigestResult) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO digests (started, finished, status, unread_chats, "
                "summary, error, tokens, emails) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    started.isoformat(timespec="seconds"),
                    datetime.now().isoformat(timespec="seconds"),
                    result.status,
                    result.unread_chats,
                    result.summary,
                    result.error,
                    result.tokens,
                    result.emails,
                ),
            )

    def new_clash_keys(self, keys: list[str]) -> list[str]:
        """Which clashes have not been reported before (and remember them)."""
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS reported_clashes "
                "(key TEXT PRIMARY KEY, reported TEXT NOT NULL)"
            )
            fresh = [
                k
                for k in keys
                if not db.execute(
                    "SELECT 1 FROM reported_clashes WHERE key = ?", (k,)
                ).fetchone()
            ]
            now = datetime.now().isoformat(timespec="seconds")
            db.executemany(
                "INSERT OR IGNORE INTO reported_clashes VALUES (?, ?)",
                [(k, now) for k in fresh],
            )
        return fresh

    def last_success(self) -> datetime | None:
        """When the last digest that worked (sent or quiet) started."""
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT started FROM digests WHERE status IN ('sent', 'quiet') "
                "ORDER BY started DESC LIMIT 1"
            ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def latest(self) -> dict | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT started, status, unread_chats, summary, error "
                "FROM digests ORDER BY started DESC, rowid DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        keys = ("time", "status", "unread_chats", "summary", "error")
        return dict(zip(keys, row, strict=True))


def email_since(store: DigestStore, now: datetime) -> datetime:
    """Email window: since the last successful digest, within sensible limits."""
    last = store.last_success()
    if last is None or now - last > MAX_WINDOW:
        return now - (FIRST_WINDOW if last is None else MAX_WINDOW)
    return last


async def _account_name(client) -> str:
    """The account's real address (e.g. name@gmail.com), not its internal
    label, so the digest says which inbox an email is in."""
    import asyncio

    cached = getattr(client, "_digest_address", None)
    if cached:
        return cached
    label = getattr(client, "label", "gmail")
    try:
        address = (await asyncio.to_thread(client.get_profile)).get("email") or label
    except Exception:
        address = label
    with contextlib.suppress(AttributeError):
        client._digest_address = address
    return address


def email_queries(since: datetime) -> list[str]:
    """The Gmail searches for one digest: the main inbox, plus Promotions and
    Social mail that mentions one of his keywords (jobs, interviews, ...)."""
    after = f"after:{int(since.timestamp())}"
    # Read or not: an email he glanced at on his phone still belongs in the
    # digest (2026-09-27). Each digest only covers the time since the last
    # one, so nothing repeats. YAADHAMMA_EMAIL_UNREAD_ONLY=1 restores the
    # unread-only behaviour.
    if config.EMAIL_UNREAD_ONLY:
        after = f"is:unread {after}"
    queries = [f"{EMAIL_FILTER} {after}"]
    words = [w for w in config.EMAIL_KEYWORDS if w]
    if words:
        either = " ".join(f'"{w}"' if " " in w else w for w in words)
        queries.append(f"{PROMO_SOCIAL_FILTER} {{{either}}} {after}")
    return queries


async def collect_email(gmail_clients, since: datetime, limit: int = 25) -> dict:
    """Every new inbox email (read or not) across all linked Gmail accounts.
    Read-only: nothing is marked read, archived or changed."""
    import asyncio

    emails, errors, seen = [], [], set()
    for client in gmail_clients or []:
        label = await _account_name(client)
        for query in email_queries(since):
            try:
                found = await asyncio.to_thread(client.search_mail, query, limit=limit)
            except Exception as exc:  # one broken account must not sink the digest
                errors.append(f"{label}: {exc}")
                break
            for m in found:
                key = (label, m.get("id") or (m.get("subject"), m.get("internal_date")))
                if key in seen:
                    continue
                seen.add(key)
                emails.append(
                    {
                        "account": label,
                        "from": m.get("from", ""),
                        "subject": m.get("subject", ""),
                        "snippet": (m.get("snippet") or "")[:200],
                        "unread": not m.get("is_read", False),
                        "internal_date": m.get("internal_date", 0),
                    }
                )
    emails.sort(key=lambda e: e["internal_date"], reverse=True)
    for e in emails:
        e.pop("internal_date")
    return {"emails": emails, "errors": errors}


async def preview_email(gmail_clients, brain, hours: float) -> dict:
    """Test run for email only: gather the last `hours` of email and write the
    summary, WITHOUT sending anything or touching WhatsApp."""
    since = datetime.now() - timedelta(hours=hours)
    accounts = [await _account_name(c) for c in gmail_clients or []]
    email = await collect_email(gmail_clients, since)
    summary = ""
    if email["emails"]:
        turn = await brain.generate(
            config.BRAIN_MODEL,
            [
                {"role": "system", "content": DIGEST_INSTRUCTIONS},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "whatsapp": {"checked": False},
                            "email": email,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            [],
        )
        summary = (turn.text or "").strip()[:MAX_DIGEST_CHARS]
    duplicates = sorted({a for a in accounts if accounts.count(a) > 1})
    return {
        "found": len(email["emails"]),
        "errors": email["errors"],
        "summary": summary,
        "accounts": accounts,
        "duplicate_accounts": duplicates,
    }


async def calendar_update(calendar, store: DigestStore, now: datetime) -> dict:
    """New clashes in the next 7 days and events starting in the next 4 hours.

    Clashes are reported once; later digests stay quiet about them. A calendar
    problem never sinks the digest: it is returned as an error instead.
    """
    import asyncio

    if calendar is None:
        return {}
    from gcal import find_clashes, format_event, local_zone

    start = (
        now.astimezone(local_zone()) if now.tzinfo else now.replace(tzinfo=local_zone())
    )
    try:
        week = await asyncio.to_thread(
            calendar.list_events, start, start + timedelta(days=7)
        )
    except Exception as exc:
        return {"error": str(exc)[:200]}
    clashes = find_clashes(week)
    fresh = set(store.new_clash_keys([c.key for c in clashes]))
    soon_end = start + timedelta(hours=4)
    soon = [
        format_event(e)
        for e in week
        if not e["all_day"]
        and not e["declined"]
        and start <= datetime.fromisoformat(e["start"]) < soon_end
    ]
    return {
        "new_clashes": [c.describe() for c in clashes if c.key in fresh],
        "starting_soon": soon,
    }


def morning_brief_text(today_events: list[dict], clashes: list, now: datetime) -> str:
    """The 8:45 brief, built directly from the calendar (no model, nothing invented)."""
    from gcal import format_event

    lines = [f"Good morning. {now:%A %d %B}", ""]
    visible = [e for e in today_events if not e["declined"]]
    if visible:
        lines.append("*Today*")
        lines += [f"- {format_event(e)}" for e in visible]
    else:
        lines.append("*Today*: nothing on your calendar.")
    if clashes:
        lines += ["", "*Clashes this week*"]
        lines += [f"- {c.describe()}" for c in clashes]
    return "\n".join(lines)


async def run_morning_brief(client, calendar, store: DigestStore) -> DigestResult:
    """Today's schedule and this week's clashes, sent to his own chat at 8:45."""
    import asyncio

    from gcal import day_bounds, find_clashes, local_zone

    started = datetime.now()
    try:
        now = datetime.now(local_zone())
        day_start, day_end = day_bounds(now.date())
        today = await asyncio.to_thread(calendar.list_events, day_start, day_end)
        week = await asyncio.to_thread(
            calendar.list_events, day_start, day_start + timedelta(days=7)
        )
        clashes = find_clashes(week)
        store.new_clash_keys(
            [c.key for c in clashes]
        )  # the 9am digest won't repeat them
        text = morning_brief_text(today, clashes, now)
        self_chat = await find_self_chat(client)
        await client.send_message(self_chat, text)
        result = DigestResult(status="morning", summary=text)
    except WhatsAppNotPairedError:
        result = DigestResult(
            status="failed",
            error="The digest's WhatsApp is not paired. Run: "
            "uv run scripts/whatsapp_signin.py --digest",
        )
    except Exception as exc:
        result = DigestResult(status="failed", error=str(exc)[:500])
    store.record(started, result)
    return result


async def find_self_chat(client) -> str:
    """Resolve his own chat. Refuses anything else.

    Accepted only if the chat is marked "(You)" or its number is exactly his
    configured full number (10+ digits). 2026-09-27: WhatsApp keeps "(You)"
    outside the chat's name label, so the number is the reliable check.
    """
    own_number = config.SELF_CHAT_NUMBER
    name = await client.find_chat(own_number or SELF_CHAT_MARKER)
    digits = "".join(ch for ch in name if ch.isdigit())
    marked = SELF_CHAT_MARKER.casefold() in name.casefold()
    by_number = len(own_number) >= 10 and digits.endswith(own_number[-10:])
    if not (marked or by_number):
        raise WhatsAppError(
            f"Refusing to send the digest to {name!r}: not his own chat."
        )
    return name


async def send_test_message(client) -> str:
    """Prove the delivery path: send a one-line test to his own chat only."""
    self_chat = await find_self_chat(client)
    await client.send_message(self_chat, "Yaadhamma digest test: delivery works.")
    return self_chat


async def run_digest(
    client, brain, store: DigestStore, gmail_clients=None, calendar=None
) -> DigestResult:
    """One scheduled digest run. Never raises; the outcome is recorded."""
    started = datetime.now()
    try:
        chats = await collect_watchlist(
            client, config.WHATSAPP_WATCHLIST, open_chats=True, max_chats=20
        )
        unread = int(chats.get("unread_watched_chats", 0))
        email = await collect_email(gmail_clients, email_since(store, started))
        new_emails = len(email["emails"])
        cal = await calendar_update(calendar, store, started)
        calendar_news = bool(cal.get("new_clashes") or cal.get("starting_soon"))
        if unread == 0 and new_emails == 0 and not calendar_news:
            result = DigestResult(
                status="quiet", summary="Nothing new in the watched chats or email."
            )
        else:
            turn = await brain.generate(
                config.BRAIN_MODEL,
                [
                    {"role": "system", "content": DIGEST_INSTRUCTIONS},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"whatsapp": chats, "email": email, "calendar": cal},
                            ensure_ascii=False,
                        ),
                    },
                ],
                [],
            )
            summary = (turn.text or "").strip()[:MAX_DIGEST_CHARS]
            if not summary:
                raise WhatsAppError("The summary came back empty.")
            header = f"Yaadhamma digest, {started:%a %I:%M %p}\n\n"
            self_chat = await find_self_chat(client)
            await client.send_message(self_chat, header + summary)
            result = DigestResult(
                status="sent",
                summary=summary,
                unread_chats=unread,
                emails=new_emails,
                tokens=turn.tokens_in + turn.tokens_out,
            )
    except WhatsAppNotPairedError:
        result = DigestResult(
            status="failed",
            error="The digest's WhatsApp is not paired. Run: "
            "uv run scripts/whatsapp_signin.py --digest",
        )
    except Exception as exc:  # recorded, never crashes the scheduler
        result = DigestResult(status="failed", error=str(exc)[:500])
    store.record(started, result)
    return result


class DigestTools:
    """Voice tool: what did the last scheduled digest say?"""

    def __init__(self, store: DigestStore | None = None) -> None:
        self.store = store or DigestStore()

    @property
    def tools(self) -> list:
        return [self.latest_digest]

    @function_tool()
    async def latest_digest(self, context: RunContext) -> dict[str, object]:
        """The most recent scheduled Saayam digest: when it ran and what it said.

        Use when he asks about "the digest", "the last summary" or what the
        background check found.
        """
        latest = self.store.latest()
        return latest or {"status": "none", "note": "No digest has run yet."}
