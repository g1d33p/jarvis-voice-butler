"""Tasks: the durable record of what Sureedu was asked to do and how it went.

Each spoken request that needs several steps becomes a Task. Tasks are saved in
a small SQLite database (~/.sureedu/sureedu.db), so they can be listed later
and, from Phase 12, resumed after a restart.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path.home() / ".sureedu" / "sureedu.db"

# planned -> running -> completed | failed | cancelled
#                    -> waiting_for_user -> running ...
STATES = {"planned", "running", "waiting_for_user", "completed", "failed", "cancelled"}
FINISHED = {"completed", "failed", "cancelled"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Step:
    action: str
    args: dict
    ok: bool
    summary: str


@dataclass
class Task:
    goal: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    state: str = "planned"
    steps: list[Step] = field(default_factory=list)
    result: str = ""
    question: str = ""
    error: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    created: str = field(default_factory=_now)
    updated: str = field(default_factory=_now)

    def set_state(self, state: str) -> None:
        if state not in STATES:
            raise ValueError(f"Unknown task state {state!r}")
        self.state = state
        self.updated = _now()

    def summary(self) -> dict[str, object]:
        """What the voice agent needs to know, kept short."""
        data: dict[str, object] = {
            "task_id": self.id,
            "status": self.state,
            "steps_taken": len(self.steps),
        }
        if self.result:
            data["result"] = self.result
        if self.question:
            data["question_for_user"] = self.question
        if self.error:
            data["error"] = self.error
        return data


class TaskStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    state TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created TEXT NOT NULL,
                    updated TEXT NOT NULL
                )"""
            )
            # Anything unfinished belongs to a session that has ended.
            rows = db.execute(
                "SELECT data FROM tasks WHERE state IN "
                "('running', 'planned', 'waiting_for_user')"
            ).fetchall()
        for (data,) in rows:
            task = _from_json(data)
            task.error = task.error or "Interrupted: Sureedu was restarted."
            task.set_state("failed")
            self.save(task)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def save(self, task: Task) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO tasks (id, goal, state, data, created, updated) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task.id,
                    task.goal,
                    task.state,
                    json.dumps(asdict(task)),
                    task.created,
                    task.updated,
                ),
            )

    def get(self, task_id: str) -> Task | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT data FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _from_json(row[0]) if row else None

    def recent(self, limit: int = 5) -> list[Task]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT data FROM tasks ORDER BY updated DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_from_json(row[0]) for row in rows]


def _from_json(text: str) -> Task:
    data = json.loads(text)
    data["steps"] = [Step(**step) for step in data.get("steps", [])]
    return Task(**data)
