"""Voice tools for Yaadhamma's local personal memory.

Facts, preferences, routines, and people the user asks her to keep, stored
on the Mac in ~/.yaadhamma/memory.db. Raw conversation is never stored here.

Memory is not permission: knowing a preference never silently authorizes a
purchase, message, deletion, or other consequential action. Storing,
correcting, and forgetting memories are local, reversible, and always
audited; acting on a memory still goes through the normal approval gates.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from audit import AuditLog
from memory_store import MemoryStore

_EXPORT_DIR = Path.home() / ".yaadhamma"


class MemoryTools:
    """Tools for remembering, recalling, correcting, forgetting, exporting."""

    def __init__(
        self,
        store: MemoryStore | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self._store = store or MemoryStore()
        self._audit = audit

    @property
    def tools(self) -> list:
        return [
            self.remember,
            self.recall,
            self.correct_memory,
            self.forget_memory,
            self.export_memories,
        ]

    def _provenance(self) -> str:
        today = datetime.now(timezone.utc).date().isoformat()
        return f"Jeevan said so in conversation on {today}"

    def _record(self, event: str, **fields) -> None:
        if self._audit is not None:
            self._audit.record(event, **fields)

    @function_tool()
    async def remember(
        self,
        context: RunContext,
        kind: str,
        content: str,
    ) -> dict[str, str | int]:
        """Save something about the user for the long term.

        Use when the user states a durable fact, preference, routine, or
        person detail and expects it kept ("remember that...", "my wife's
        name is...", "I always..."). Do not store one-off remarks, secrets
        like passwords, or anything the user did not mean to keep.

        Args:
            kind: One of "fact", "preference", "routine", "person".
            content: The memory itself, phrased to make sense months later.
        """
        try:
            memory_id = self._store.remember(
                kind, content, provenance=self._provenance()
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        self._record("memory.remember", id=memory_id, kind=kind.strip().lower())
        return {"remembered": True, "id": memory_id}

    @function_tool()
    async def recall(
        self,
        context: RunContext,
        query: str,
        kind: str = "",
    ) -> dict[str, object]:
        """Search what is remembered about the user.

        Use when the user asks what you remember ("what do you remember
        about...", "do you know my..."). Returns the best matches with
        their ids, so a follow-up correction or deletion can name one.

        Args:
            query: What to look for, in the user's own words.
            kind: Optional filter: "fact", "preference", "routine", "person".
        """
        memories = self._store.recall(query, kind=kind or None)
        return {
            "memories": [
                {
                    "id": m["id"],
                    "kind": m["kind"],
                    "content": m["content"],
                    "updated_at": m["updated_at"],
                }
                for m in memories
            ]
        }

    @function_tool()
    async def correct_memory(
        self,
        context: RunContext,
        memory_id: int,
        new_content: str,
    ) -> dict[str, object]:
        """Fix a remembered detail the user says is wrong or outdated.

        Recall first so the user confirms which memory to change; never
        guess the id.

        Args:
            memory_id: The id from a recall result.
            new_content: The corrected memory, phrased to last.
        """
        try:
            updated = self._store.correct(memory_id, new_content)
        except (KeyError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        self._record("memory.correct", id=memory_id)
        return {"corrected": True, "id": updated["id"], "content": updated["content"]}

    @function_tool()
    async def forget_memory(
        self,
        context: RunContext,
        memory_id: int,
    ) -> dict[str, object]:
        """Delete a memory the user asks to forget.

        Recall first so the user confirms which memory goes; never guess
        the id. The user's explicit instruction is the approval.

        Args:
            memory_id: The id from a recall result.
        """
        if not self._store.forget(memory_id):
            raise ToolError(f"no memory with id {memory_id}")
        self._record("memory.forget", id=memory_id)
        return {"forgotten": True, "id": memory_id}

    @function_tool()
    async def export_memories(
        self,
        context: RunContext,
    ) -> dict[str, str | int]:
        """Write every memory to a JSON file the user can read or keep.

        Use when the user asks what you remember in full, or wants a copy
        of their data.
        """
        memories = self._store.export()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = _EXPORT_DIR / f"memory-export-{stamp}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(memories, indent=2, ensure_ascii=False))
        self._record("memory.export", path=str(path), count=len(memories))
        return {"exported": str(path), "count": len(memories)}
