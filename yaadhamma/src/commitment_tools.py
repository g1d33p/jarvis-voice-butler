"""Voice tools for commitments: record them, list them, mark them done.

Completion is explicit only: mark_done completes the one commitment the
user named, and refuses when the name is ambiguous or unknown. Nothing
here ever infers that something is finished.
"""

from __future__ import annotations

from datetime import datetime

from livekit.agents import RunContext, function_tool

from planner import parse_due_date
from task_store import CommitmentStore


class CommitmentTools:
    def __init__(self, store: CommitmentStore | None = None) -> None:
        self._store = store or CommitmentStore()

    @property
    def tools(self) -> list:
        return [self.commit_task, self.list_commitments, self.mark_done]

    @function_tool()
    async def commit_task(self, context: RunContext, text: str) -> dict:
        """Record something Jeevan said he would do.

        Use when he makes a promise or sets himself a task ("I'll call
        Ravi back by 5pm", "remind me to pay the bill tomorrow"). The due
        date is parsed from his words; vague timing ("soon", "when you
        can") stores no due date rather than guessing one.

        Args:
            text: What he committed to, in his own words.
        """
        now = datetime.now()
        due = parse_due_date(text, now)
        task = self._store.committed_task(text, due, context="voice")
        return {
            "recorded": True,
            "id": task["id"],
            "text": task["text"],
            "due_at": task["due_at"],
        }

    @function_tool()
    async def list_commitments(self, context: RunContext) -> dict:
        """His open commitments, with due dates. Use when he asks what he
        owes, what is due, or what is overdue."""
        now = datetime.now()
        return {
            "pending": self._store.pending(),
            "due_today": self._store.due_today(now),
            "overdue": self._store.overdue(now),
        }

    @function_tool()
    async def mark_done(self, context: RunContext, text: str) -> dict:
        """Mark a commitment done — only when he explicitly says so
        ("mark that done", "completed the report").

        Never call this because a task looks finished, a calendar event
        passed, or an email suggests it. If his words do not name exactly
        one open commitment, this fails and completes nothing.

        Args:
            text: Which commitment he said is done, in his words.
        """
        try:
            done = self._store.mark_done(text=text)
        except (LookupError, ValueError) as exc:
            return {"completed": False, "error": str(exc)}
        return {"completed": True, "id": done["id"], "text": done["text"]}
