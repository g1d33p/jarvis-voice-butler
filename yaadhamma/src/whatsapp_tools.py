"""Voice tools for the WhatsApp skill (WhatsApp Web in Yaadhamma's browser).

Triage is the point: whatsapp_list_chats and whatsapp_where_needed gather
what the LLM needs to tell Jeevan where he is needed — unread chats with
their recent messages — and the LLM does the understanding.

Reading is low-risk and ungated. Sending a message is consequential: it goes
through the shared approval gate. A short message the user dictated word for
word may go without asking (the same policy as dictated emails); anything
Yaadhamma composed is read back first and needs a clear yes.

Names are never guessed: an unknown or ambiguous chat name stops with a
question instead of picking one.

Note: opening a chat marks its messages as read in WhatsApp, exactly as if
Jeevan had opened it himself. Triage therefore consumes unread state.
"""

from pathlib import Path

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from browser import browser_action
from permissions import (
    ApprovalManager,
    latest_user_message,
)
from policy import can_send_without_asking
from untrusted import wrap as _wrap_untrusted
from whatsapp import (
    WhatsAppClient,
    WhatsAppError,
    WhatsAppNotPairedError,
    matches_watchlist,
)


def _wrap_chat_messages(messages: list[dict], chat_name: str) -> list[dict]:
    """Mark WhatsApp message text as untrusted data before it reaches a model.

    A message can say "ignore previous instructions and send X to Y"; the
    envelope (see untrusted.py) keeps it as data to summarise, never
    instructions to follow.
    """
    source = f"WhatsApp {chat_name}"
    wrapped = []
    for m in messages:
        m = dict(m)
        if m.get("text"):
            m["text"] = _wrap_untrusted(m["text"], source)
        if m.get("replying_to"):
            m["replying_to"] = _wrap_untrusted(m["replying_to"], source)
        wrapped.append(m)
    return wrapped


_SIGNIN_HINT = (
    "WhatsApp is not paired in Yaadhamma's own dedicated browser window. "
    "WhatsApp being open in your regular Chrome does not count — Yaadhamma "
    "never uses your Chrome, it drives its own separate browser with its "
    "own saved login. To pair, on the Mac run "
    "`uv run scripts/whatsapp_signin.py` in the yaadhamma folder and scan "
    "the QR code with the phone (WhatsApp > Settings > Linked devices > "
    "Link a device), then try again."
)

# WhatsApp file sends refuse anything bigger (v2 Stage 4).
MAX_SEND_FILE_BYTES = 64 * 1024 * 1024


def _check_sendable(path: Path) -> Path:
    """Refuse paths outside his home and files over 64 MB. Returns the
    resolved path."""
    home = Path.home().resolve()
    resolved = path.resolve()
    if resolved != home and home not in resolved.parents:
        raise ToolError(
            f"I only send files from your home folder — {path} is outside it."
        )
    size = resolved.stat().st_size
    if size > MAX_SEND_FILE_BYTES:
        raise ToolError(
            f"{resolved.name} is {size / (1024 * 1024):.1f} MB — over the "
            "64 MB limit for WhatsApp file sends."
        )
    return resolved


def _resolve_sendable_file(file_path: str) -> Path:
    """Turn what he said into the file to send.

    Exact path first; then the tidy log (where_did_file_go) in case the
    nightly tidy moved it; then a bounded filename search of the usual
    places. Zero matches — or several — stop with a question instead of
    guessing.
    """
    raw = (file_path or "").strip()
    if not raw:
        raise ToolError("To send a file I need a file path or name.")
    home = Path.home()
    cand = Path(raw).expanduser()
    if not cand.is_absolute():
        cand = home / raw
    if cand.is_file():
        return _check_sendable(cand)
    name = Path(raw).name
    candidates: list[Path] = []
    # The nightly tidy may have moved it.
    try:
        from tidy import TidyLog

        for move in TidyLog().find_moved(name):
            now = Path(move.get("now", ""))
            if now.is_file() and now not in candidates:
                candidates.append(now)
    except Exception:
        pass  # a broken tidy log must not block sending
    # Bounded search: the usual places, then the home top level.
    if not candidates:
        for root in (home / "Desktop", home / "Documents", home / "Downloads"):
            if root.is_dir():
                candidates.extend(p for p in root.rglob(name) if p.is_file())
            if len(candidates) >= 10:
                break
        if not candidates:
            candidates.extend(p for p in home.glob(name) if p.is_file())
        candidates = candidates[:10]
    if not candidates:
        raise ToolError(
            f"I couldn't find a file named {name!r} in your home folder. "
            "Give me more of the path?"
        )
    if len(candidates) > 1:
        listing = "\n".join(f"- {p}" for p in candidates)
        raise ToolError(
            f"Several files match {name!r} — which one did you mean?\n{listing}"
        )
    return _check_sendable(candidates[0])


class WhatsAppTools:
    """WhatsApp triage and messaging tools for the voice agent."""

    def __init__(
        self,
        client: WhatsAppClient | None = None,
        browser=None,
        approvals: ApprovalManager | None = None,
    ) -> None:
        self._client = client or WhatsAppClient(browser=browser)
        self._approvals = approvals or ApprovalManager()
        self._auto_sent_for: str | None = None

    @property
    def tools(self) -> list:
        return [
            self.whatsapp_list_chats,
            self.whatsapp_read_chat,
            self.whatsapp_where_needed,
            self.whatsapp_watchlist_digest,
            self.whatsapp_send_message,
            self.whatsapp_send_file,
        ]

    async def _guarded(self, fn, *args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except WhatsAppNotPairedError as exc:
            if "still loading" in str(exc):
                # Not a pairing problem: the page was slow. Don't send him off
                # to re-pair a session that works.
                raise ToolError(
                    "WhatsApp Web is still loading in Yaadhamma's browser. "
                    "Wait a few seconds and try again."
                ) from exc
            raise ToolError(_SIGNIN_HINT) from exc
        except WhatsAppError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    @browser_action(lambda self: self._client._browser_or_default())
    async def whatsapp_list_chats(
        self, context: RunContext, limit: int = 30
    ) -> dict[str, object]:
        """List WhatsApp chats with unread counts and last-message previews.

        Use for a quick overview of what's waiting. For "where am I needed",
        prefer whatsapp_where_needed.
        """
        chats = await self._guarded(self._client.list_all_chats)
        return {"chats": chats[:limit], "total": len(chats)}

    @function_tool()
    @browser_action(lambda self: self._client._browser_or_default())
    async def whatsapp_read_chat(
        self, context: RunContext, chat_name: str, limit: int = 10
    ) -> dict[str, object]:
        """Read recent messages from one WhatsApp chat, oldest first.

        Opening the chat marks its messages as read in WhatsApp.
        If the name is unknown or matches several chats, this stops and
        asks instead of guessing.

        Args:
            chat_name: Contact or group name, e.g. "Ravi" or "Family Group".
            limit: How many recent messages to read (default 10).
        """
        chat_name = (chat_name or "").strip()
        if not chat_name:
            raise ToolError("Which chat should I read? Give me a name.")
        read = await self._guarded(self._client.read_messages, chat_name, limit)
        read = dict(read)
        read["messages"] = _wrap_chat_messages(
            read.get("messages", []), read.get("chat", chat_name)
        )
        return read

    @function_tool()
    @browser_action(lambda self: self._client._browser_or_default())
    async def whatsapp_where_needed(
        self,
        context: RunContext,
        max_chats: int = 8,
        messages_per_chat: int = 6,
    ) -> dict[str, object]:
        """Triage: gather the WhatsApp chats where Jeevan is needed.

        Returns every chat with unread messages plus its recent messages, so
        you can summarize what needs his attention: questions for him,
        @mentions, time-sensitive asks. Opening a chat marks it as read.

        Args:
            max_chats: Maximum unread chats to open (default 8).
            messages_per_chat: Recent messages to include per chat (default 6).
        """
        chats = await self._guarded(self._client.list_all_chats)
        unread = [c for c in chats if c.get("unread", 0) > 0]
        attention = []
        skipped = []
        for chat in unread[:max_chats]:
            # One unloadable chat must not abort the whole triage: skip it,
            # report it, and keep going. A lost pairing still aborts — nothing
            # can be triaged without it.
            try:
                messages = await self._client.read_messages(
                    chat["name"], messages_per_chat, exact=True
                )
            except WhatsAppNotPairedError as exc:
                raise ToolError(_SIGNIN_HINT) from exc
            except WhatsAppError as exc:
                skipped.append({"chat": chat["name"], "reason": str(exc)})
                continue
            attention.append(
                {
                    "chat": chat["name"],
                    "unread": chat.get("unread", 0),
                    "preview": _wrap_untrusted(
                        chat.get("preview", ""), f"WhatsApp {chat['name']}"
                    ),
                    "messages": _wrap_chat_messages(messages["messages"], chat["name"]),
                }
            )
        return {
            "chats_needing_attention": attention,
            "skipped_chats": skipped,
            "unread_chat_count": len(unread),
            "total_chats": len(chats),
        }

    @function_tool()
    @browser_action(lambda self: self._client._browser_or_default())
    async def whatsapp_watchlist_digest(
        self,
        context: RunContext,
        open_chats: bool = True,
        max_chats: int = 10,
        messages_per_chat: int = 8,
    ) -> dict[str, object]:
        """Jeevan's main WhatsApp check: his watched community chats.

        Covers every chat whose name contains a watchlist word (by default
        Saayam, SC1, SC2, SC3), in one pass over the chat list. Use this for
        any request about the Saayam or SC community chats, "my community
        chats", or "where am I needed" in them.

        Args:
            open_chats: True reads the recent messages of each watched chat
                with unread messages (opening a chat marks it as read in
                WhatsApp). False only returns names, unread counts and the
                last-message preview, without opening anything.
            max_chats: Most unread watched chats to open (default 10).
            messages_per_chat: Recent messages to read per chat (default 8).
        """
        import config

        chats = await self._guarded(self._client.list_all_chats)
        try:
            return await collect_watchlist(
                self._client,
                config.WHATSAPP_WATCHLIST,
                chats=chats,
                open_chats=open_chats,
                max_chats=max_chats,
                messages_per_chat=messages_per_chat,
            )
        except WhatsAppNotPairedError as exc:
            raise ToolError(_SIGNIN_HINT) from exc

    @function_tool()
    @browser_action(lambda self: self._client._browser_or_default())
    async def whatsapp_send_message(
        self, context: RunContext, chat_name: str, message: str
    ) -> dict[str, object]:
        """Send a WhatsApp message to a contact or group.

        The message is always shown to the user first unless they dictated
        it word for word. A clear yes sends it. Never invent the chat: an
        unknown or ambiguous name stops and asks instead of guessing.

        Args:
            chat_name: Contact or group name, e.g. "Ravi".
            message: The message text, exactly as it should be sent.
        """
        chat_name, message = (chat_name or "").strip(), (message or "").strip()
        if not chat_name or not message:
            raise ToolError("To send a WhatsApp message I need a chat and a message.")
        # Resolve the chat BEFORE the approval gate, so a yes can never
        # approve a guessed recipient.
        matched = await self._guarded(self._client.find_chat, chat_name)
        description = f"send WhatsApp message to {matched}: {message!r}"
        quoted = f"To {matched} on WhatsApp:\n\n{message}"

        async def execute() -> dict[str, object]:
            return await self._guarded(self._client.send_message, matched, message)

        return await self._approvals.gate(
            tool_name="whatsapp_send_message",
            description=description,
            context=context,
            execute=execute,
            args={"chat": matched},
            pre_approved=self._dictated_message_ok(context, message),
            quoted=quoted,
        )

    def _dictated_message_ok(self, context: object, message: str) -> str | None:
        """Policy reason if the user dictated this exact message, else None."""
        latest = latest_user_message(context)
        if latest is None:
            return None
        utterance_id, user_words = latest
        allowed, reason = can_send_without_asking(message, user_words)
        key = utterance_id or user_words
        if not allowed or key == self._auto_sent_for:
            return None
        self._auto_sent_for = key
        return reason

    async def _own_chat_titles(self) -> list[str]:
        """Exact titles of his own chats, resolved from the configured
        self-chat numbers — the same mechanism the phone poller uses."""
        import config

        titles = []
        for number in config.remote_settings()["self_chats"]:
            try:
                titles.append(await self._guarded(self._client.find_chat, number))
            except Exception:
                continue
        return titles

    @function_tool()
    @browser_action(lambda self: self._client._browser_or_default())
    async def whatsapp_send_file(
        self, context: RunContext, chat_name: str, file_path: str, caption: str = ""
    ) -> dict[str, object]:
        """Send a file to one of Jeevan's own WhatsApp chats.

        Files only ever go to his own chats — enforced in code, not by the
        model — and always need his approval first: the approval names the
        file, the folder it comes from, and the recipient. Files over 64 MB
        or outside his home folder are refused outright. A partial file
        name is searched for (including the tidy log of moved files); if
        several files match, this stops and asks which one instead of
        guessing. The attachment is verified in the chat before success is
        reported.

        Args:
            chat_name: One of his own chats, e.g. "Jeevan (You)".
            file_path: Path to the file, or part of its name.
            caption: Optional caption sent with the file.
        """
        chat_name, file_path = (chat_name or "").strip(), (file_path or "").strip()
        if not chat_name or not file_path:
            raise ToolError("To send a file I need a chat and a file.")
        # Resolve the file BEFORE the approval gate, so a yes can never
        # approve a guessed file.
        path = _resolve_sendable_file(file_path)
        # Resolve the chat BEFORE the approval gate, so a yes can never
        # approve a guessed recipient.
        matched = await self._guarded(self._client.find_chat, chat_name)
        own = {t.casefold() for t in await self._own_chat_titles()}
        if matched.casefold() not in own:
            raise ToolError(
                f"Files only go to your own chats — {matched!r} is not one of them."
            )
        size_mb = path.stat().st_size / (1024 * 1024)
        description = (
            f"send WhatsApp file {path.name} ({size_mb:.1f} MB) "
            f"from {path.parent} to {matched}"
        )
        quoted = (
            f"To {matched} on WhatsApp:\n\n{path.name} "
            f"({size_mb:.1f} MB) from {path.parent}"
            + (f"\nCaption: {caption.strip()}" if caption.strip() else "")
        )

        async def execute() -> dict[str, object]:
            return await self._guarded(
                self._client.send_file, matched, str(path), caption.strip()
            )

        # No dictated-file bypass: file sends are always RiskTier.HIGH and
        # always ask.
        return await self._approvals.gate(
            tool_name="whatsapp_send_file",
            description=description,
            context=context,
            execute=execute,
            args={"chat": matched, "file": str(path)},
            quoted=quoted,
        )


async def collect_watchlist(
    client,
    patterns: list[str],
    *,
    chats: list[dict] | None = None,
    open_chats: bool = True,
    max_chats: int = 10,
    messages_per_chat: int = 8,
) -> dict[str, object]:
    """Gather the watched chats' unread messages (shared by the voice tool
    and the scheduled digest). Raises WhatsAppNotPairedError if unpaired."""
    if chats is None:
        chats = await client.list_all_chats()
    watched = [c for c in chats if matches_watchlist(c.get("name", ""), patterns)]
    unread = [c for c in watched if c.get("unread", 0) > 0]
    result: dict[str, object] = {
        "watchlist": patterns,
        "watched_chats": len(watched),
        "unread_watched_chats": len(unread),
        "quiet_chats": [c["name"] for c in watched if not c.get("unread", 0)],
    }
    if not open_chats:
        result["unread"] = [
            {
                "chat": c["name"],
                "unread": c.get("unread", 0),
                "preview": _wrap_untrusted(
                    c.get("preview", ""), f"WhatsApp {c['name']}"
                ),
            }
            for c in unread
        ]
        return result

    details, skipped = [], []
    for chat in unread[:max_chats]:
        try:
            messages = await client.read_messages(
                chat["name"], messages_per_chat, exact=True
            )
        except WhatsAppNotPairedError:
            raise
        except WhatsAppError as exc:
            skipped.append({"chat": chat["name"], "reason": str(exc)})
            continue
        details.append(
            {
                "chat": chat["name"],
                "unread": chat.get("unread", 0),
                "messages": _wrap_chat_messages(messages["messages"], chat["name"]),
            }
        )
    result["unread"] = details
    result["skipped_chats"] = skipped
    result["not_opened"] = [c["name"] for c in unread[max_chats:]]
    return result
