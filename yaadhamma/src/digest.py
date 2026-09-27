"""The scheduled digest (runs in the background, 4 times a day).

Each run:
  1. reads the unread messages in Jeevan's watched WhatsApp chats
     (Saayam, SC1, SC2, SC3 by default) and his new unread Gmail (all linked
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
EMAIL_FILTER = (
    "in:inbox is:unread -category:promotions -category:social -category:forums"
)
# First run (or after a long gap): look back this far for email.
FIRST_WINDOW = timedelta(hours=4)
MAX_WINDOW = timedelta(hours=24)

DIGEST_INSTRUCTIONS = """You write Jeevan's digest: his Saayam community WhatsApp chats and his
new email. You get JSON with "whatsapp" (unread messages per watched chat) and
"email" (new unread emails: account, from, subject, snippet). Write one WhatsApp
message, plain text, under 1,500 characters:

*Needs you*
- one line per chat message or email that needs his reply, decision or action:
  who, what, and where (chat name, or "email").

*Saayam chats*
- one short line per chat with anything else worth knowing.

*Email*
- one short line per notable email or group of similar ones (e.g. "3 job alerts").
  Skip pure noise such as marketing.

End with: Quiet: <watched chats with nothing new>, if any.

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


async def collect_email(gmail_clients, since: datetime, limit: int = 25) -> dict:
    """New unread inbox email across all linked Gmail accounts. Read-only:
    nothing is marked read, archived or changed."""
    import asyncio

    query = f"{EMAIL_FILTER} after:{int(since.timestamp())}"
    emails, errors = [], []
    for client in gmail_clients or []:
        try:
            found = await asyncio.to_thread(client.search_mail, query, limit=limit)
        except Exception as exc:  # one broken account must not sink the digest
            errors.append(f"{getattr(client, 'label', 'gmail')}: {exc}")
            continue
        for m in found:
            emails.append(
                {
                    "account": getattr(client, "label", ""),
                    "from": m.get("from", ""),
                    "subject": m.get("subject", ""),
                    "snippet": (m.get("snippet") or "")[:200],
                    "internal_date": m.get("internal_date", 0),
                }
            )
    emails.sort(key=lambda e: e["internal_date"], reverse=True)
    for e in emails:
        e.pop("internal_date")
    return {"emails": emails, "errors": errors}


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
    client, brain, store: DigestStore, gmail_clients=None
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
        if unread == 0 and new_emails == 0:
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
                            {"whatsapp": chats, "email": email}, ensure_ascii=False
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
