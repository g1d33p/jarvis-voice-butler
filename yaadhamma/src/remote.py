"""Phone access over WhatsApp: Jeevan commands her from his phone.

Only his own chats are ever polled (YAADHAMMA_SELF_CHATS, default
19408438446 and 919640520634). A message from any other chat is ignored
entirely: the poller never even opens other chats.

A command is a message HE sent starting with "Yaadhamma" (case-insensitive).
The command text is wrapped as untrusted content and run through the
orchestrator exactly like a voice task, so every approval gate, verification
and audit entry applies unchanged. Replies — results or approval questions —
go back to the same chat. A "yes"/"no" reply to a pending question resolves
it via the orchestrator.

Each message is handled once (hashes in ~/.yaadhamma/remote-handled.json).
The first poll for a chat only marks the backlog handled — it never runs it.
Messages she sends herself (digests, replies) are recorded as outbound so the
next poll skips them instead of re-ingesting her own "Yaadhamma …" text as a
command.
Polls run every 2 minutes, 08:00-23:00 local, as their own launchd job
(scripts/remote_schedule.py), using the digest browser profile and the
digest browser lock — never the voice profile.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger("yaadhamma.remote")

COMMAND_PREFIX = "yaadhamma"
WINDOW_START_HOUR = 8
WINDOW_END_HOUR = 23
MAX_APPROVAL_ROUNDS = 2  # then the approval is declared expired, honestly

AFFIRMATIVE = {"yes", "yeah", "yep", "sure", "do it", "go ahead", "ok"}
NEGATIVE = {"no", "nope", "don't", "dont", "cancel", "stop", "never mind"}


class RemoteError(Exception):
    """A self chat could not be resolved safely."""


def _home() -> Path:
    return Path.home()


def handled_path() -> Path:
    return _home() / ".yaadhamma" / "remote-handled.json"


def pending_path() -> Path:
    return _home() / ".yaadhamma" / "remote-pending.json"


def is_command(text: str) -> str | None:
    """The task after the Yaadhamma prefix, or None when not a command.

    Returns "" for a bare prefix (the caller prompts gently instead of
    running the orchestrator on nothing).
    """
    stripped = text.strip()
    if stripped.lower() != COMMAND_PREFIX and not stripped.lower().startswith(
        COMMAND_PREFIX
    ):
        return None
    rest = stripped[len(COMMAND_PREFIX) :].lstrip(" ,:;-")
    return rest


def message_hash(chat: str, message: dict) -> str:
    key = "|".join(
        (
            chat,
            str(message.get("sender", "")),
            str(message.get("time", "")),
            str(message.get("text", "")),
        )
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _outbound_key(chat: str, text: str) -> str:
    """Key for a message she sent herself.

    The sender/time WhatsApp stamps on the message cannot be predicted at
    send time, so the key covers chat + text only. WhatsApp may strip
    *bold* / _italic_ markers when it displays the message, so the text is
    flattened the same way send-verification compares messages.
    """
    flat = re.sub(r"[\s*_~`]", "", text).casefold()
    return hashlib.sha256(f"outbound|{chat}|{flat}".encode()).hexdigest()[:32]


def record_outbound(chat: str, text: str) -> None:
    """Remember a message she just sent to one of his chats.

    The next poll skips it instead of re-ingesting her own digest or reply
    as a command — digests begin "Yaadhamma digest, …", which is_command
    would otherwise treat as a command. Best-effort: never raises. Each key
    is consumed by the first poll that sees the matching message.
    """
    store = JsonStore(handled_path(), _handled_default())
    data = store.read()
    keys = data.setdefault("outbound", [])
    key = _outbound_key(chat, text)
    if key not in keys:
        keys.append(key)
        data["outbound"] = keys[-200:]
        store.write(data)


def _handled_default() -> dict:
    return {"hashes": [], "outbound": [], "primed": []}


def in_window(now: datetime) -> bool:
    """Polls run 08:00-23:00 local."""
    return WINDOW_START_HOUR <= now.hour < WINDOW_END_HOUR


async def resolve_self_chat(client, number: str) -> str:
    """Resolve one of his own chats. Refuses anything that is not his.

    Accepted only if the chat is marked "(You)" or its digits end with the
    number's last 10 digits — the same strictness as the digest's
    find_self_chat.
    """
    digits = "".join(ch for ch in number if ch.isdigit())
    if len(digits) < 10:
        raise RemoteError(f"Refusing {number!r}: not a full phone number.")
    name = await client.find_chat(number)
    name_digits = "".join(ch for ch in name if ch.isdigit())
    marked = "(you)" in name.casefold()
    by_number = name_digits.endswith(digits[-10:])
    if not (marked or by_number):
        raise RemoteError(f"Refusing to poll {name!r}: not his own chat.")
    return name


class JsonStore:
    """A tiny JSON file store. Never raises on read; writes best-effort."""

    def __init__(self, path: Path, default):
        self._path = path
        self._default = default

    def read(self):
        try:
            return json.loads(self._path.read_text())
        except Exception:
            # json.loads needs a str, not the exception type: copy the default
            # so callers can mutate it freely.
            return json.loads(json.dumps(self._default))

    def write(self, data) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data))
        except Exception:
            pass


@dataclass
class RemoteContext:
    """What the orchestrator's tools see: a WhatsApp command, not voice."""

    source: str = "whatsapp-remote"
    chat: str = ""


@dataclass
class RemotePoller:
    """Poll his own chats for commands. All I/O is injected for tests."""

    client_factory: Callable[[], Awaitable[object]]
    orchestrator_factory: Callable[[], object]
    context_factory: Callable[[str], object] = field(
        default=lambda chat: RemoteContext(chat=chat)
    )

    def __post_init__(self) -> None:
        self._handled = JsonStore(handled_path(), _handled_default())
        self._pending = JsonStore(pending_path(), {"chats": {}})

    # ------------------------------------------------------------ polling

    async def poll_once(self, now: datetime) -> list[str]:
        """One poll. Returns short outcome lines for the log."""
        import config

        settings = config.remote_settings()
        if not settings["enabled"]:
            return []
        if not in_window(now):
            return []
        outcomes: list[str] = []
        client = await self.client_factory()
        try:
            orchestrator = self.orchestrator_factory()
            for number in settings["self_chats"]:
                try:
                    chat = await resolve_self_chat(client, number)
                except RemoteError as exc:
                    outcomes.append(f"{number}: {exc}")
                    continue
                outcomes.extend(await self._poll_chat(client, orchestrator, chat))
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()
        return outcomes

    async def _poll_chat(self, client, orchestrator, chat: str) -> list[str]:
        outcomes: list[str] = []
        try:
            result = await client.read_messages(chat, limit=15)
        except Exception as exc:
            return [f"{chat}: read failed: {exc}"]
        messages = result.get("messages", [])
        handled = self._handled.read()
        seen = set(handled.get("hashes", []))
        if chat not in set(handled.get("primed", [])):
            # First poll for this chat: the backlog is history, not commands.
            # Mark everything handled without executing, so old messages —
            # including her own earlier digests — can never run.
            for message in messages:
                seen.add(message_hash(chat, message))
            handled["hashes"] = sorted(seen)[-2000:]
            handled["primed"] = sorted(set(handled.get("primed", [])) | {chat})
            self._handled.write(handled)
            return [
                f"{chat}: first poll: marked {len(messages)} existing "
                "message(s) as handled"
            ]
        outbound = set(handled.get("outbound", []))
        for message in messages:
            digest = message_hash(chat, message)
            if digest in seen:
                continue
            key = _outbound_key(chat, str(message.get("text", "")))
            if key in outbound:
                # She sent this (digest, reply, approval question): it is
                # never a command. Consume the key and mark it handled.
                outbound.discard(key)
                seen.add(digest)
                continue
            seen.add(digest)
            outcome = await self._handle_message(client, orchestrator, chat, message)
            if outcome:
                outcomes.append(f"{chat}: {outcome}")
        # Re-read before writing: _reply records outbound keys mid-poll, and
        # this write must merge them, never clobber them.
        fresh = self._handled.read()
        seen |= set(fresh.get("hashes", []))
        outbound |= set(fresh.get("outbound", []))
        handled["hashes"] = sorted(seen)[-2000:]
        handled["outbound"] = sorted(outbound)[-200:]
        handled["primed"] = fresh.get("primed", handled.get("primed", []))
        self._handled.write(handled)
        return outcomes

    # ------------------------------------------------------------ handling

    async def _handle_message(
        self, client, orchestrator, chat: str, message: dict
    ) -> str | None:
        if not message.get("outgoing"):
            return None  # not his message: never a command
        text = str(message.get("text", "")).strip()
        if not text:
            return None
        pending = self._pending.read().get("chats", {}).get(chat)
        lowered = text.lower()
        if pending and (lowered in AFFIRMATIVE or lowered in NEGATIVE):
            return await self._resolve_pending(
                client, orchestrator, chat, pending, text
            )
        task_text = is_command(text)
        if task_text is None:
            return None  # chat that is not a command and not an answer
        if task_text == "":
            await self._reply(client, chat, "Yes — what should I do?")
            return "bare prefix: prompted"
        if pending:
            # A new command replaces the waiting one (newest wins, like the
            # voice loop); the old question is dropped, honestly logged.
            self._drop_pending(chat)
            log.info("remote: new command replaced a pending question in %s", chat)
        return await self._run_command(client, orchestrator, chat, task_text)

    async def _run_command(
        self, client, orchestrator, chat: str, task_text: str
    ) -> str:
        from untrusted import wrap as _wrap

        goal = (
            f"WhatsApp command from Jeevan (his own chat {chat}). "
            f"Reply in this chat when done.\n"
            f"Command:\n{_wrap(task_text, source=f'WhatsApp command ({chat})')}"
        )
        task = await orchestrator.start(goal, self.context_factory(chat))
        return await self._handle_outcome(client, orchestrator, chat, task)

    async def _resolve_pending(
        self, client, orchestrator, chat: str, pending: dict, reply: str
    ) -> str:
        task = await orchestrator.resume(
            pending["task_id"], reply, self.context_factory(chat)
        )
        return await self._handle_outcome(client, orchestrator, chat, task)

    async def _handle_outcome(
        self, client, orchestrator, chat: str, task
    ) -> str | None:
        summary = task.summary()
        status = summary.get("status")
        if status == "completed":
            self._drop_pending(chat)
            result = str(summary.get("result", "done"))
            await self._reply(client, chat, result)
            return "completed"
        if status == "waiting_for_user":
            question = str(summary.get("question_for_user", ""))
            task_id = str(summary.get("task_id", task.id))
            rounds = self._bump_rounds(chat, task_id, question)
            if rounds > MAX_APPROVAL_ROUNDS:
                self._drop_pending(chat)
                await self._reply(
                    client,
                    chat,
                    "That approval expired before your answer arrived — "
                    "approvals only stay open a minute. Send the command "
                    "again if you still want it.",
                )
                return "approval expired"
            self._store_pending(chat, task_id, question, rounds)
            await self._reply(client, chat, question)
            return "asked approval"
        # failed / cancelled: say so briefly and honestly.
        self._drop_pending(chat)
        error = str(summary.get("error", "it did not finish"))
        await self._reply(client, chat, f"I couldn't do that: {error}")
        return f"{status}: {error[:80]}"

    # ------------------------------------------------------------ replies

    async def _reply(self, client, chat: str, text: str) -> None:
        # His own chat, resolved and verified above: the reply mechanism
        # itself needs no approval (like the digest delivery). What the
        # command DOES went through the orchestrator's gates.
        await client.send_message(chat, text[:1500])
        # Record her own message (only after a successful send) so the next
        # poll skips it instead of re-ingesting it as a command.
        record_outbound(chat, text[:1500])

    # ------------------------------------------------------------ pending

    def _store_pending(
        self, chat: str, task_id: str, question: str, rounds: int
    ) -> None:
        data = self._pending.read()
        data.setdefault("chats", {})[chat] = {
            "task_id": task_id,
            "question": question,
            "rounds": rounds,
        }
        self._pending.write(data)

    def _drop_pending(self, chat: str) -> None:
        data = self._pending.read()
        data.get("chats", {}).pop(chat, None)
        self._pending.write(data)

    def _bump_rounds(self, chat: str, task_id: str, question: str) -> int:
        data = self._pending.read()
        old = data.get("chats", {}).get(chat)
        if old and old.get("task_id") == task_id and old.get("question") == question:
            return int(old.get("rounds", 1)) + 1
        return 1


# ------------------------------------------------------------ real wiring

OFF_SCREEN = ["--window-position=-3000,-3000", "--window-size=1280,900"]
DIGEST_LOCK = Path.home() / ".yaadhamma" / "digest.lock"


async def real_client_factory():
    """The digest browser profile and lock — never the voice profile."""
    import fcntl

    from browser import DIGEST_PROFILE_DIR, BrowserManager
    from whatsapp import WhatsAppClient

    DIGEST_LOCK.parent.mkdir(parents=True, exist_ok=True)
    # The lock file must stay open (and the flock held) for the whole browser
    # session, so it cannot be a `with` block here; close() releases it.
    lock = open(DIGEST_LOCK, "w")  # noqa: SIM115
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as err:
        lock.close()
        raise RemoteError(
            "another digest-profile browser is running; skipping"
        ) from err

    browser = BrowserManager(
        headless=False, profile_dir=DIGEST_PROFILE_DIR, launch_args=OFF_SCREEN
    )
    client = WhatsAppClient(browser=browser)

    async def close() -> None:
        try:
            await browser.close()
        finally:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
            finally:
                lock.close()

    client.close = close  # type: ignore[attr-defined]
    return client


def real_orchestrator_factory():
    """The same orchestrator the voice uses: every tool, gate and audit entry.

    2026-09-29: this called Orchestrator() with no arguments, so every phone
    command failed with "missing 2 required positional arguments".
    """
    from actions import ActionRegistry
    from agent import build_toolsets
    from orchestrator import Orchestrator
    from task_manager import TaskStore

    toolsets, _shared = build_toolsets()
    return Orchestrator(registry=ActionRegistry(*toolsets), store=TaskStore())


async def main_async() -> int:
    poller = RemotePoller(
        client_factory=real_client_factory,
        orchestrator_factory=real_orchestrator_factory,
    )
    try:
        outcomes = await poller.poll_once(datetime.now())
    except RemoteError as exc:
        log.info("remote poll skipped: %s", exc)
        return 0
    for outcome in outcomes:
        log.info("remote: %s", outcome)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [remote] %(message)s")
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
