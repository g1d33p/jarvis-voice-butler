"""Commitments: promises Jeevan made, with due dates and explicit completion.

Separate from task_manager.py, which tracks the agent's own multi-step
work. A commitment is something Jeevan said he would do ("I'll call Ravi
back by 5pm"); it stays pending until he explicitly marks it done — the
assistant never infers completion.

SQLite at ~/.yaadhamma/commitments.db; the path resolves $HOME at call
time so tests can repoint it.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path


def _db_path(path: Path | None = None) -> Path:
    return Path(path) if path else Path.home() / ".yaadhamma" / "commitments.db"


class CommitmentStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = _db_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS commitments (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    due_at TEXT,
                    context TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created TEXT NOT NULL
                )"""
            )

    def committed_task(
        self, plain_text: str, due_at: datetime | None, context: str
    ) -> dict:
        """Record a new commitment. Returns the stored row as a dict."""
        row = {
            "id": uuid.uuid4().hex[:8],
            "text": plain_text.strip(),
            "due_at": due_at.isoformat(timespec="seconds") if due_at else None,
            "context": context,
            "status": "pending",
            "created": datetime.now().isoformat(timespec="seconds"),
        }
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO commitments (id, text, due_at, context, status, created)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row["id"],
                    row["text"],
                    row["due_at"],
                    row["context"],
                    row["status"],
                    row["created"],
                ),
            )
        return row

    def _rows(self, where: str, params: tuple) -> list[dict]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT id, text, due_at, context, status, created"
                f" FROM commitments WHERE {where} ORDER BY due_at, created",
                params,
            ).fetchall()
        return [
            dict(zip(("id", "text", "due_at", "context", "status", "created"), r))
            for r in rows
        ]

    def pending(self) -> list[dict]:
        return self._rows("status = 'pending'", ())

    def due_today(self, now: datetime) -> list[dict]:
        """Pending commitments whose due date is today (not yet overdue)."""
        today = now.date().isoformat()
        return [
            row
            for row in self._rows("status = 'pending' AND due_at IS NOT NULL", ())
            if row["due_at"][:10] == today
            and datetime.fromisoformat(row["due_at"]) >= now
        ]

    def overdue(self, now: datetime) -> list[dict]:
        """Pending commitments whose due date has passed."""
        return [
            row
            for row in self._rows("status = 'pending' AND due_at IS NOT NULL", ())
            if datetime.fromisoformat(row["due_at"]) < now
        ]

    def mark_done(self, task_id: str | None = None, text: str | None = None) -> dict:
        """Complete a commitment, explicitly. Never inferred.

        By id, or by text matching exactly one pending commitment
        (case-insensitive substring). Zero or several matches raise
        LookupError and complete nothing.
        """
        if task_id:
            rows = self._rows("id = ? AND status = 'pending'", (task_id,))
            if not rows:
                raise LookupError(f"No pending commitment with id {task_id!r}.")
            row = rows[0]
        elif text:
            needle = text.strip().lower()
            rows = [r for r in self.pending() if needle in r["text"].lower()]
            if not rows:
                raise LookupError(f"No pending commitment matches {text!r}.")
            if len(rows) > 1:
                names = ", ".join(f"{r['text']!r}" for r in rows)
                raise LookupError(
                    f"{len(rows)} pending commitments match {text!r}: {names}. "
                    "Say which one is done."
                )
            row = rows[0]
        else:
            raise ValueError("mark_done needs a task_id or text.")
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE commitments SET status = 'completed' WHERE id = ?",
                (row["id"],),
            )
        row["status"] = "completed"
        return row


def wake_review_text(now: datetime | None = None) -> str:
    """Overdue + due-today commitments, reviewed on every wake.

    Empty string when nothing needs him; the caller decides what to do
    with the text (session instructions, brief line).
    """
    moment = now or datetime.now()
    store = CommitmentStore()
    try:
        late = store.overdue(moment)
        today = store.due_today(moment)
    except Exception:
        return ""
    lines: list[str] = []
    for row in late:
        lines.append(f"- OVERDUE: {row['text']} (was due {row['due_at']})")
    for row in today:
        lines.append(f"- due today: {row['text']} ({row['due_at'][11:16]})")
    if not lines:
        return ""
    return "Open commitments to mention if relevant, never nag: \n" + "\n".join(lines)


def audit_snapshot() -> str:
    """One-line JSON summary of the commitment store, for the audit log."""
    store = CommitmentStore()
    return json.dumps(
        {"pending": len(store.pending())},
    )
