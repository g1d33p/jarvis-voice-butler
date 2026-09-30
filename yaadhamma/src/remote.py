"""Phone access over WhatsApp: Jeevan commands her from his phone.

Only his own chats are ever polled (YAADHAMMA_SELF_CHATS, default
19408438446 and 919640520634). A message from any other chat is ignored
entirely: the poller never even opens other chats.

A command is a message HE sent starting with "Yaadhamma" (case-insensitive).
The command text is wrapped as untrusted content and run through the
orchestrator exactly like a voice task, so every approval gate, verification
and audit entry applies unchanged. Replies — results or approval questions —
go back to the same chat. His next outgoing message in a chat with a pending
question is the answer — no prefix needed, any wording ("yes", "no", "the
second one"); unanswered questions expire after 30 minutes.

Each message is handled once (hashes in ~/.yaadhamma/remote-handled.json).
The first poll for a chat only marks the backlog handled — it never runs it.
Messages she sends herself (digests, replies) are recorded as outbound so the
next poll skips them instead of re-ingesting her own "Yaadhamma …" text as a
command.

The poller runs as one resident process (scripts/remote_poller.py, its own
launchd job): a single browser is opened once and reused across polls every
2 minutes, using the digest browser profile and the digest browser lock —
never the voice profile. The lock is held only while a poll runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import config
from browser import DIGEST_PROFILE_DIR, BrowserManager
from remote_health import record_failure, record_success
from whatsapp import WhatsAppClient, WhatsAppNotPairedError
from whatsapp_health import (
    ChatCountLog,
    Check,
    HealthLog,
    HealthReport,
    guarded_chat_list,
)

log = logging.getLogger("yaadhamma.remote")

COMMAND_PREFIX = "yaadhamma"
WINDOW_START_HOUR = 8
WINDOW_END_HOUR = 23
MAX_APPROVAL_ROUNDS = 2  # then the approval is declared expired, honestly

# A pending approval question he never answers within 30 minutes is dropped:
# his late reply must not resolve a question he has forgotten about.
PENDING_TTL_S = 30 * 60


def _default_transcriber(audio: bytes) -> str:
    """Live voice-note transcription (Gemini). Imported lazily so the
    module loads without the transcription library."""
    from voice_transcribe import transcribe_voice_note

    return transcribe_voice_note(audio)


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


def in_window(
    now: datetime, window: tuple = (WINDOW_START_HOUR, WINDOW_END_HOUR)
) -> bool:
    """Whether the poller runs at this time. `window` is (start, end) in 24h
    hours from YAADHAMMA_REMOTE_HOURS; an overnight window wraps (22-6)."""
    start, end = window
    if start <= end:
        return start <= now.hour < end
    return now.hour >= start or now.hour < end


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


def _pending_expired(pending: dict) -> bool:
    """Whether a stored pending question is older than PENDING_TTL_S.

    A record with no timestamp (written by an older version) is treated as
    expired: without proof it is fresh, a late reply must not resolve it.
    """
    asked_at = pending.get("asked_at")
    if not isinstance(asked_at, (int, float)):
        return True
    return time.time() - asked_at > PENDING_TTL_S


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
class PollSummary:
    """What one poll saw and did.

    log_line() renders the single timestamped summary line emitted per poll:
    chats seen, self-chats matched, commands found, actions taken.
    """

    chats_seen: int = 0
    best_chats: int = 0
    self_chats_matched: int = 0
    commands_found: int = 0
    actions_taken: int = 0
    note: str = ""
    outcomes: list = field(default_factory=list)

    def log_line(self, now: datetime) -> str:
        """The one summary line for a poll, timestamped with the poll time."""
        line = (
            f"{now.strftime('%Y-%m-%dT%H:%M:%S')} remote poll:"
            f" chats_seen={self.chats_seen}"
            f" self_chats_matched={self.self_chats_matched}"
            f" commands_found={self.commands_found}"
            f" actions_taken={self.actions_taken}"
        )
        if self.note:
            line += f" note={self.note}"
        return line


def _match_self_chats(chats: list[dict], numbers: list[str]) -> dict[str, dict]:
    """Match each configured number to at most one chat row.

    Each row is claimed at most once: without this, one "(You)" chat would
    match every configured number and be polled several times per cycle.
    Digit evidence wins over the "(You)" marker; numbers are handled in
    configured order, so the result is deterministic. Same strictness as
    resolve_self_chat.
    """

    def digits_of(text: str) -> str:
        return "".join(ch for ch in text if ch.isdigit())

    remaining = list(chats)
    matched: dict[str, dict] = {}
    for number in numbers:
        want = digits_of(number)
        if not want:
            continue
        for chat in remaining:
            if digits_of(chat.get("name", "")).endswith(want[-10:]):
                matched[number] = chat
                remaining.remove(chat)
                break
    for number in numbers:
        if number in matched:
            continue
        for chat in remaining:
            if "(you)" in chat.get("name", "").casefold():
                matched[number] = chat
                remaining.remove(chat)
                break
    return matched


@dataclass
class RemotePoller:
    """Poll his own chats for commands. All I/O is injected for tests."""

    client_factory: Callable[[], Awaitable[object]] | None = None
    orchestrator_factory: Callable[[], object] | None = None
    context_factory: Callable[[str], object] = field(
        default=lambda chat: RemoteContext(chat=chat)
    )
    # Voice-note audio -> transcript. The default is the live Gemini
    # transcriber; tests inject a fake so no model call ever happens here.
    transcriber: Callable[[bytes], str] = field(
        default=lambda audio: _default_transcriber(audio)
    )

    def __post_init__(self) -> None:
        self._handled = JsonStore(handled_path(), _handled_default())
        self._pending = JsonStore(pending_path(), {"chats": {}})
        self._poll_commands = 0
        self._poll_actions = 0

    # ------------------------------------------------------------ polling

    async def poll_once(self, now: datetime) -> PollSummary:
        """One-shot poll: build a client, poll, close it.

        The resident poller (ResidentPoller, scripts/remote_poller.py)
        instead keeps one client across polls via poll_with_client.
        """
        import config

        if self.client_factory is None:
            raise RuntimeError("poll_once needs a client_factory")
        settings = config.remote_settings()
        summary = PollSummary()
        if not settings["enabled"]:
            summary.note = "disabled (YAADHAMMA_REMOTE=off)"
            return summary
        if not in_window(now, settings["hours"]):
            summary.note = "outside polling hours"
            return summary
        client = await self.client_factory()
        try:
            return await self.poll_with_client(client, now, settings=settings)
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()

    async def poll_with_client(
        self, client, now: datetime, settings: dict | None = None
    ) -> PollSummary:
        """Poll using an already-open client: no lock, no close.

        Waits for the chat list to settle, refuses a partial list, then
        reads only his own chats — and opens one only when its row shows
        an unread message. Returns a one-line summary of the run.
        Raises WhatsAppNotPairedError if the profile lost its pairing.
        """
        if self.orchestrator_factory is None:
            raise RuntimeError("polling needs an orchestrator_factory")
        settings = settings or config.remote_settings()
        summary = PollSummary()
        self._poll_commands = 0
        self._poll_actions = 0
        try:
            if not settings["enabled"]:
                summary.note = "disabled (YAADHAMMA_REMOTE=off)"
                return summary
            if not in_window(now, settings["hours"]):
                summary.note = "outside polling hours"
                return summary
            orchestrator = self.orchestrator_factory()
            chats, best, sync_report = await guarded_chat_list(client, ChatCountLog())
            summary.chats_seen = len(chats)
            summary.best_chats = best
            if sync_report:
                # A partial list is worse than no poll: his own chat may simply
                # not have loaded yet, and its commands would go silently
                # unheard. Report loudly, touch nothing.
                summary.note = f"{sync_report} — poll skipped"
                log.warning("remote: %s", summary.note)
                return summary
            outcomes: list[str] = []
            matched = 0
            by_number = _match_self_chats(chats, settings["self_chats"])
            for number in settings["self_chats"]:
                chat = by_number.get(number)
                if chat is None:
                    # Not in the settled list: fall back to a targeted resolve
                    # (WhatsApp search), once. If that fails too, say so loudly —
                    # a missing self chat is exactly how commands used to go
                    # silently unheard — but keep polling the other numbers.
                    try:
                        title = await resolve_self_chat(client, number)
                    except Exception as exc:
                        outcomes.append(f"{number}: could not resolve self chat: {exc}")
                        continue
                    chat = {"name": title, "unread": 1}
                else:
                    matched += 1
                outcomes.extend(await self._poll_self_chat(client, orchestrator, chat))
            summary.self_chats_matched = matched
            summary.commands_found = self._poll_commands
            summary.actions_taken = self._poll_actions
            summary.outcomes = outcomes
            return summary
        finally:
            # Exactly one timestamped summary line per poll, on every path:
            # normal, skipped, disabled, or raising.
            log.info("remote: %s", summary.log_line(now))

    async def _poll_self_chat(self, client, orchestrator, chat: dict) -> list[str]:
        """Poll one of his own chats. Opens it only when needed."""
        name = chat.get("name", "")
        handled = self._handled.read()
        if name not in set(handled.get("primed", [])):
            # First sight of this chat: the backlog is history, not
            # commands. Read once and mark everything handled without
            # executing, so old messages can never run.
            try:
                result = await client.read_messages(name, limit=15)
            except Exception as exc:
                return [f"{name}: read failed: {exc}"]
            messages = result.get("messages", [])
            seen = set(handled.get("hashes", []))
            for message in messages:
                seen.add(message_hash(name, message))
            handled["hashes"] = sorted(seen)[-2000:]
            handled["primed"] = sorted(set(handled.get("primed", [])) | {name})
            self._handled.write(handled)
            return [
                f"{name}: first poll: marked {len(messages)} existing "
                "message(s) as handled"
            ]
        if not chat.get("unread", 0):
            # Cheap poll: nothing new in this chat, don't open it.
            return []
        try:
            result = await client.read_messages(name, limit=15)
        except Exception as exc:
            return [f"{name}: read failed: {exc}"]
        return await self._handle_new_messages(
            client, orchestrator, name, result.get("messages", [])
        )

    async def _handle_new_messages(
        self, client, orchestrator, chat: str, messages: list
    ) -> list[str]:
        outcomes: list[str] = []
        handled = self._handled.read()
        seen = set(handled.get("hashes", []))
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
                # The message became a command, an answer, or a voice note.
                self._poll_commands += 1
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
        if message.get("voice") is not None:
            # A voice note he sent to his own chat: no wake prefix needed,
            # sending it to himself is already deliberate.
            return await self._handle_voice_note(client, orchestrator, chat, message)
        text = str(message.get("text", "")).strip()
        if not text:
            return None
        pending = self._pending.read().get("chats", {}).get(chat)
        if pending and _pending_expired(pending):
            # A question he never answered within 30 minutes is dropped: his
            # late reply must not resolve a question he has forgotten about.
            self._drop_pending(chat)
            log.info("remote: pending question in %s expired unanswered", chat)
            pending = None
        task_text = is_command(text)
        if task_text is None:
            if pending is None:
                return None  # bare "yes" with no pending question does nothing
            # His next outgoing message in this chat is the answer — no
            # Yaadhamma prefix needed. Arbitrary answers accepted: "yes",
            # "no", "the second one".
            return await self._resolve_pending(
                client, orchestrator, chat, pending, text
            )
        if task_text == "":
            await self._reply(client, chat, "Yes — what should I do?")
            return "bare prefix: prompted"
        if pending:
            # A new command replaces the waiting one (newest wins, like the
            # voice loop); the old question is dropped, honestly logged.
            self._drop_pending(chat)
            log.info("remote: new command replaced a pending question in %s", chat)
        return await self._run_command(client, orchestrator, chat, task_text)

    async def _handle_voice_note(
        self, client, orchestrator, chat: str, message: dict
    ) -> str | None:
        """Transcribe a voice note he sent and run it as a command: same
        orchestrator, same tools, same approval gates, same audit as a typed
        command. The transcript is untrusted content, wrapped before use."""
        import config
        from voice_transcribe import log_voice_note_cost

        voice = message.get("voice") or {}
        duration_s = voice.get("duration_s")
        max_s = config.voice_note_settings()["max_s"]
        if duration_s is not None and duration_s > max_s:
            await self._reply(
                client,
                chat,
                f"That voice note is {int(duration_s)} seconds long — I only "
                f"take voice notes up to {int(max_s)} seconds. Send a shorter one?",
            )
            return "voice note skipped: too long"
        try:
            audio = await client.download_voice_note(chat, message)
        except Exception as exc:
            log.warning("remote: voice-note download failed: %s", exc)
            await self._reply(
                client,
                chat,
                "I couldn't download that voice note — the WhatsApp browser "
                "session may need attention. Try again in a bit?",
            )
            return "voice note download failed"
        try:
            transcript = self.transcriber(audio)
        except Exception as exc:
            log.warning("remote: voice-note transcription failed: %s", exc)
            await self._reply(
                client,
                chat,
                "I couldn't make out that voice note — could you send it "
                "again, or type it instead?",
            )
            return "voice note transcription failed"
        if not str(transcript).strip():
            await self._reply(
                client,
                chat,
                "That voice note came back empty — could you send it again?",
            )
            return "voice note transcription empty"
        log_voice_note_cost(duration_s, transcript)
        # A voice note is a new command: it replaces a waiting question
        # (newest wins, like the voice loop).
        if self._pending.read().get("chats", {}).get(chat):
            self._drop_pending(chat)
            log.info("remote: voice note replaced a pending question in %s", chat)
        from untrusted import wrap as _wrap

        goal = (
            f"WhatsApp voice note from Jeevan (his own chat {chat}). "
            f"Reply in this chat when done.\n"
            f"He said:\n{_wrap(transcript, source=f'WhatsApp voice note ({chat})')}"
        )
        task = await orchestrator.start(goal, self.context_factory(chat))
        heard = transcript if len(transcript) <= 280 else transcript[:277] + "..."
        return await self._handle_outcome(client, orchestrator, chat, task, heard=heard)

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
        self, client, orchestrator, chat: str, task, heard: str | None = None
    ) -> str | None:
        """Reply to the outcome. `heard` prefixes the reply for voice notes:
        "Heard: '…'. Done: …"."""
        summary = task.summary()
        status = summary.get("status")
        prefix = f"Heard: '{heard}'. " if heard else ""
        if status == "completed":
            self._drop_pending(chat)
            result = str(summary.get("result", "done"))
            await self._reply(
                client, chat, f"{prefix}Done: {result}" if heard else result
            )
            return "completed" if not heard else "voice note completed"
        if status == "waiting_for_user":
            question = str(summary.get("question_for_user", ""))
            task_id = str(summary.get("task_id", task.id))
            rounds = self._bump_rounds(chat, task_id, question)
            if rounds > MAX_APPROVAL_ROUNDS:
                self._drop_pending(chat)
                await self._reply(
                    client,
                    chat,
                    f"{prefix}That approval expired before your answer arrived — "
                    "approvals only stay open a minute. Send the command "
                    "again if you still want it.",
                )
                return "approval expired"
            self._store_pending(chat, task_id, question, rounds)
            await self._reply(client, chat, f"{prefix}{question}")
            return "asked approval"
        # failed / cancelled: say so briefly and honestly.
        self._drop_pending(chat)
        error = str(summary.get("error", "it did not finish"))
        await self._reply(client, chat, f"{prefix}I couldn't do that: {error}")
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
        # Every reply she sends is an action the poll took.
        self._poll_actions += 1

    # ------------------------------------------------------------ pending

    def _store_pending(
        self, chat: str, task_id: str, question: str, rounds: int
    ) -> None:
        data = self._pending.read()
        data.setdefault("chats", {})[chat] = {
            "task_id": task_id,
            "question": question,
            "rounds": rounds,
            "asked_at": time.time(),
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
        headless=False,
        profile_dir=DIGEST_PROFILE_DIR,
        launch_args=OFF_SCREEN,
        never_raise=True,  # the digest profile never takes his focus
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


async def persistent_client_factory():
    """One browser for the resident poller: opened once, reused across polls.

    Headless first (invisible, no window to steal focus); if headless ever
    proves unusable, YAADHAMMA_POLLER_HEADLESS=off falls back to a visible
    off-screen window. Either way never_raise=True: this profile must never
    ask the OS for focus. No lock handling here — ResidentPoller owns the
    digest lock around each poll.
    """
    headless = config.remote_settings()["poller_headless"]
    browser = BrowserManager(
        headless=headless,
        profile_dir=DIGEST_PROFILE_DIR,
        launch_args=[] if headless else OFF_SCREEN,
        never_raise=True,
    )
    client = WhatsAppClient(browser=browser)

    async def close() -> None:
        await browser.close()

    client.close = close  # type: ignore[attr-defined]
    return client


class DigestLock:
    """One lock for the digest browser profile, shared by poller and digests.

    The resident poller holds it for the whole lifetime of its browser, so
    the profile can never be driven by two Chromiums at once. A scheduled
    digest that needs the profile writes a *request marker* first: the
    poller notices it between polls, parks (closes) its browser and
    releases the lock; the digest runs; afterwards the poller re-acquires
    and reopens.

    The marker carries the requester's pid and a timestamp. A marker from
    a dead process, or one older than 10 minutes, is ignored — a crashed
    digest can never park the poller forever.
    """

    REQUEST_TTL_S = 600.0

    def __init__(self, path: Path = DIGEST_LOCK) -> None:
        self._path = path
        self._file = None

    @property
    def request_path(self) -> Path:
        return self._path.with_name(self._path.name + ".request")

    def request(self) -> None:
        """Ask the resident poller to hand over the profile."""
        self.request_path.parent.mkdir(parents=True, exist_ok=True)
        self.request_path.write_text(
            json.dumps({"at": time.time(), "pid": os.getpid()})
        )

    def requested(self) -> bool:
        """True when a live digest recently asked for the profile."""
        try:
            data = json.loads(self.request_path.read_text())
        except Exception:
            return False
        try:
            if time.time() - float(data["at"]) > self.REQUEST_TTL_S:
                return False
            pid = int(data.get("pid", 0))
        except (TypeError, ValueError):
            return False
        if pid:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False  # the requester died; ignore its marker
            except PermissionError:
                pass  # alive, just not ours
            except OSError:
                return False
        return True

    def clear_request(self) -> None:
        """Remove this process's own request marker, if still present.

        Never removes another live process's marker: two overlapping
        digests must not cancel each other's handoff. (A marker from a
        dead process is ignored by requested() anyway.)
        """
        try:
            data = json.loads(self.request_path.read_text())
            if int(data.get("pid", 0)) != os.getpid():
                return
        except Exception:
            return
        with contextlib.suppress(Exception):
            self.request_path.unlink()

    def acquire(self) -> bool:
        """Non-blocking acquire. True when the lock is now held."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._path, "w")  # noqa: SIM115
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        self._file = handle
        return True

    def release(self) -> None:
        handle, self._file = self._file, None
        if handle is None:
            return
        with contextlib.suppress(Exception):
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()

    @property
    def held(self) -> bool:
        return self._file is not None


@dataclass
class ResidentPoller:
    """The always-on poller: one process, one browser, a poll every 2 min.

    The browser is opened once and reused across polls — never relaunched
    on a schedule. Between polls it just sits on the WhatsApp tab: no
    navigation, no reload, no focus requests.

    The digest lock is held for the whole lifetime of the browser, so the
    profile can never be driven by two Chromiums at once. A scheduled
    digest writes a request marker (DigestLock.request()); the poller
    notices it between polls, parks its browser and releases the lock; the
    digest runs; afterwards the poller re-acquires and reopens. If the
    profile loses its pairing, the browser is parked too — leaving the
    profile free for the pairing script — and retried every 15 minutes.
    """

    poller: RemotePoller
    client_factory: Callable[[], Awaitable[object]] = persistent_client_factory
    lock: DigestLock = field(default_factory=DigestLock)
    interval_s: float = 120.0
    parked_retry_s: float = 30.0
    unpaired_retry_s: float = 900.0
    health_log_path: Path | None = None

    def __post_init__(self) -> None:
        self._client = None

    async def run_forever(self) -> None:
        while True:
            delay = await self.run_one_cycle(datetime.now())
            await self._sleep_between_polls(delay)

    async def run_one_cycle(self, now: datetime) -> float:
        """One cycle. Returns seconds until the next cycle.

        Invariant: the browser exists only while the lock is held, so the
        digest profile is never driven twice at once.
        """
        if self._client is None:
            # The browser may only be opened while holding the lock.
            if not self.lock.acquire():
                log.info("poller: digest is using the profile — waiting")
                return self.parked_retry_s
            try:
                self._client = await self.client_factory()
            except Exception as exc:
                self.lock.release()
                record_failure(exc)
                log.warning("poller: browser failed to open (%s); retrying", exc)
                return self.interval_s
            log.info("poller: browser opened (one browser for all polls)")
        try:
            await self.poller.poll_with_client(self._client, now)
        except WhatsAppNotPairedError as exc:
            await self._park("WhatsApp is not paired")
            self.lock.release()
            self._record_lost_pairing(now, exc)
            log.warning(
                "poller: WhatsApp is NOT PAIRED (%s). The profile is free "
                "now — re-pair it, then polls resume on their own.",
                exc,
            )
            return self.unpaired_retry_s
        except Exception as exc:
            record_failure(exc)
            log.warning("poller: poll failed (%s); browser will relaunch", exc)
            await self._park("poll error")
            self.lock.release()
            return self.interval_s
        # The poll's one timestamped summary line was already emitted by
        # poll_with_client.
        record_success()
        if self.lock.requested():
            # A digest asked for the profile: hand it over now.
            await self._park("digest requested the profile")
            self.lock.release()
            log.info("poller: handed the profile to the digest")
            return self.parked_retry_s
        return self.interval_s

    async def _sleep_between_polls(self, delay: float) -> None:
        """Wait for the next poll, but yield promptly to a digest request."""
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            await asyncio.sleep(min(5.0, max(0.0, deadline - time.monotonic())))
            if self._client is not None and self.lock.requested():
                await self._park("digest requested the profile")
                self.lock.release()
                log.info("poller: handed the profile to the digest")
                return

    async def shutdown(self) -> None:
        """Park the browser and release the lock. Never raises."""
        await self._park("shutting down")
        with contextlib.suppress(Exception):
            self.lock.release()

    async def _park(self, reason: str) -> None:
        """Close the browser so the digest profile is free for others."""
        client, self._client = self._client, None
        if client is None:
            return
        close = getattr(client, "close", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()
        log.info("poller: browser parked (%s)", reason)

    def _record_lost_pairing(self, now: datetime, exc: Exception) -> None:
        """Persist the lost pairing so the morning brief and daemon status
        see it without launching a browser.

        Recorded once per outage, not every retry: if the latest entry
        already says the pairing failed, there is nothing new to say.
        Never raises — a logging failure must not break the poll loop.
        """
        try:
            store = HealthLog(path=self.health_log_path)
            latest = store.latest() or {}
            already = any(
                c.get("name") == "paired" and not c.get("ok")
                for c in latest.get("checks", [])
            )
            if not already:
                store.record(
                    HealthReport(
                        started=now.isoformat(),
                        checks=[Check("paired", False, str(exc)[:200])],
                    )
                )
        except Exception:
            log.exception("poller: could not record the lost pairing")


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
        summary = await poller.poll_once(datetime.now())
    except RemoteError as exc:
        log.info("remote poll skipped: %s", exc)
        return 0
    for outcome in summary.outcomes:
        log.info("remote: %s", outcome)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [remote] %(message)s")
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
