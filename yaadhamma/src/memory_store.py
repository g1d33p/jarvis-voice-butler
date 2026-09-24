"""Local personal memory: facts, preferences, routines, and people.

A separate SQLite store from the task database, kept on the Mac
(~/.yaadhamma/memory.db). Raw conversation logs are never stored here —
only durable memories the user asked to keep (or approved), each with
provenance saying where it came from.

Memory is not permission: knowing a preference never silently authorizes a
purchase, message, deletion, or other consequential action.
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path.home() / ".yaadhamma" / "memory.db"

KINDS = ("fact", "preference", "routine", "person")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    """SQLite-backed durable memory with full-text recall."""

    def __init__(self, path: Path | None = None) -> None:
        env = os.environ.get("YAADHAMMA_MEMORY_PATH")
        self.path = Path(env) if env else (path or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    provenance TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(content, content='memories', content_rowid='id')
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS memories_fts_insert
                AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, content)
                    VALUES (new.id, new.content);
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS memories_fts_update
                AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content)
                    VALUES ('delete', old.id, old.content);
                    INSERT INTO memories_fts(rowid, content)
                    VALUES (new.id, new.content);
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS memories_fts_delete
                AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content)
                    VALUES ('delete', old.id, old.content);
                END
                """
            )

    def remember(
        self,
        kind: str,
        content: str,
        provenance: str = "",
        confidence: float = 1.0,
    ) -> int:
        """Store a durable memory; return its id."""
        kind = kind.strip().lower()
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
        content = content.strip()
        if not content:
            raise ValueError("memory content must not be empty")
        now = _utcnow()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO memories (kind, content, provenance, confidence,
                                      created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (kind, content, provenance, confidence, now, now),
            )
            return int(cur.lastrowid)

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "content": row["content"],
            "provenance": row["provenance"],
            "confidence": row["confidence"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get(self, memory_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def recall(self, query: str, kind: str | None = None, limit: int = 5) -> list[dict]:
        """Full-text search over memories, best match first."""
        tokens = [t for t in query.strip().split() if t]
        if not tokens:
            return self.list_memories(kind=kind, limit=limit)
        # Quote each token and OR them: tolerant of FTS5 special characters
        # in the query, and ranks by bm25.
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
        sql = """
            SELECT m.* FROM memories m
            JOIN memories_fts f ON m.id = f.rowid
            WHERE memories_fts MATCH ?
        """
        params: list = [match]
        if kind:
            sql += " AND m.kind = ?"
            params.append(kind.strip().lower())
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def correct(self, memory_id: int, new_content: str) -> dict:
        """Replace a memory's content, keeping its provenance and id."""
        new_content = new_content.strip()
        if not new_content:
            raise ValueError("corrected content must not be empty")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
                (new_content, _utcnow(), memory_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no memory with id {memory_id}")
        result = self.get(memory_id)
        assert result is not None
        return result

    def forget(self, memory_id: int) -> bool:
        """Delete a memory. Returns True if one was deleted."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return cur.rowcount > 0

    def list_memories(self, kind: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM memories"
        params: list = []
        if kind:
            sql += " WHERE kind = ?"
            params.append(kind.strip().lower())
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def export(self) -> list[dict]:
        """Everything, oldest first — for the user's export file."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memories ORDER BY created_at ASC"
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
