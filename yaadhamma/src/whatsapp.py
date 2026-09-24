"""WhatsApp skill: triage and messaging through WhatsApp Web.

Yaadhamma drives WhatsApp Web in its own dedicated browser tab (a persistent
Chromium profile at ~/.yaadhamma/chrome-profile, shared with the rest of the
browser skill). It never touches Jeevan's own Chrome. All WhatsApp-DOM
selectors live in whatsapp_extractors.js; this file holds the driving logic.

Duplicate-send protection is layered:
  1. The voice tool routes every send through the shared ApprovalManager:
     one approval authorizes exactly one send attempt.
  2. The tool layer refuses to auto-send the same dictated message twice for
     one utterance (mirrors the email tools).
  3. The client snapshots the chat's last message before typing, and after
     clicking Send it polls until a NEW outgoing message with the exact text
     appears. If that confirmation never comes, it raises instead of
     retrying — retrying a send blindly is how duplicates happen.

Known side effect, stated plainly: opening a chat marks its messages as read
in WhatsApp, exactly as if Jeevan had opened it himself. Triage therefore
consumes unread state; the voice prompts say so.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger("yaadhamma.whatsapp")

WHATSAPP_URL = "https://web.whatsapp.com/"

_JS_SRC = Path(__file__).with_name("whatsapp_extractors.js").read_text(encoding="utf-8")

_SIGNIN_HINT = (
    "WhatsApp is not paired in Yaadhamma's own dedicated browser window. "
    "WhatsApp being open in your regular Chrome does not count — Yaadhamma "
    "never uses your Chrome, it drives its own separate browser with its "
    "own saved login. If you already paired: close any other Yaadhamma "
    "window that might be holding the browser profile (for example the "
    "WhatsApp sign-in browser), then try again. To pair, on the Mac run "
    "`uv run scripts/whatsapp_signin.py` in the yaadhamma folder and scan "
    "the QR code with the phone (WhatsApp > Settings > Linked devices > "
    "Link a device), then try again."
)


class WhatsAppError(Exception):
    """A user-facing WhatsApp operation failure."""


class WhatsAppNotPairedError(WhatsAppError):
    """WhatsApp Web is showing the QR code: run the pairing script."""


_META_RE = re.compile(r"^\[(?P<time>[^\]]+)\]\s*(?P<sender>.*?):\s*$")


def parse_message_meta(meta: str) -> tuple[str | None, str | None]:
    """Split "[10:30, 24/09/2026] Ravi Kumar: " into (time, sender)."""
    match = _META_RE.match(meta or "")
    if not match:
        return None, None
    return match.group("time"), match.group("sender")


def find_chats(name: str, chats: list[dict]) -> list[dict]:
    """Exact name matches if any, else case-insensitive substring matches."""
    target = (name or "").strip().casefold()
    if not target:
        return []
    exact = [c for c in chats if c.get("name", "").casefold() == target]
    if exact:
        return exact
    return [c for c in chats if target in c.get("name", "").casefold()]


def _norm_chat_name(name: str) -> str:
    """Normalized for comparing a resolved chat name with the on-screen header."""
    return " ".join(str(name).casefold().split())


def _js_call(name: str, *args: object) -> str:
    """Wrap an extractor so page.evaluate runs it against the document."""
    encoded = ", ".join(json.dumps(a) for a in args)
    call = f"{name}(document{(', ' + encoded) if encoded else ''})"
    return f"() => {{{_JS_SRC}\nreturn {call};}}"


def _is_whatsapp_url(url: str) -> bool:
    return "web.whatsapp.com" in (url or "")


def _message_signature(message: dict | None) -> tuple | None:
    if not message:
        return None
    return (message.get("meta"), message.get("text"), message.get("outgoing"))


class WhatsAppClient:
    """Drive WhatsApp Web through the shared BrowserManager.

    `browser` must provide list_tabs/switch_tab/open_tab/evaluate/press_key
    (BrowserManager does). The browser starts lazily on first use.
    """

    def __init__(self, browser=None) -> None:
        self._browser = browser
        self._tab_number: int | None = None

    # ------------------------------------------------------------------
    # Browser plumbing
    # ------------------------------------------------------------------

    def _browser_or_default(self):
        if self._browser is None:
            from browser import BrowserManager

            self._browser = BrowserManager(headless=True)
        return self._browser

    async def _evaluate(self, name: str, *args: object):
        from browser import BrowserError

        browser = self._browser_or_default()
        await self.ensure_tab(browser)
        try:
            return await browser.evaluate(_js_call(name, *args))
        except BrowserError as exc:
            raise WhatsAppError(f"WhatsApp Web did not respond: {exc}") from exc

    async def ensure_tab(self, browser=None) -> None:
        """Make a WhatsApp Web tab the active tab, opening one if needed."""
        browser = browser or self._browser_or_default()
        if self._tab_number is not None:
            tabs = (await browser.list_tabs())["tabs"]
            for tab in tabs:
                if tab["number"] == self._tab_number and _is_whatsapp_url(
                    tab.get("url", "")
                ):
                    if not tab.get("active"):
                        await browser.switch_tab(self._tab_number)
                    return
            self._tab_number = None
        for tab in (await browser.list_tabs())["tabs"]:
            if _is_whatsapp_url(tab.get("url", "")):
                self._tab_number = tab["number"]
                if not tab.get("active"):
                    await browser.switch_tab(tab["number"])
                return
        opened = await browser.open_tab(WHATSAPP_URL)
        self._tab_number = opened["number"]

    async def _login_state(self) -> str:
        result = await self._evaluate("waLoginState")
        return result.get("state", "loading")

    async def _require_login(
        self, timeout_s: float = 8.0, poll_s: float = 1.0, qr_confirm_s: float = 4.0
    ) -> None:
        """Require a paired session, tolerating WhatsApp Web's loading screen.

        On 2026-09-24 the old one-shot check fired while the page was still
        loading and reported "not paired"; ~20 seconds later the same tab
        read chats fine. So a "loading" state now polls for a few seconds
        before we declare the session unpaired.

        The 17:58 session the same day showed the mirror image: the page
        flashed the QR screen once while restoring the session, and the
        check raised "not paired" on that single reading. A "qr" state is
        therefore only trusted once it has persisted for `qr_confirm_s`
        seconds — a transient flash is not a diagnosis.
        """
        deadline = time.monotonic() + timeout_s
        qr_since: float | None = None
        while True:
            state = await self._login_state()
            now = time.monotonic()
            if state == "logged_in":
                return
            if state == "qr":
                if qr_since is None:
                    qr_since = now
                elif now - qr_since >= qr_confirm_s:
                    raise WhatsAppNotPairedError(_SIGNIN_HINT)
            else:
                qr_since = None
            if now >= deadline:
                raise WhatsAppNotPairedError(
                    f"WhatsApp Web is still loading after {timeout_s:.0f}s. "
                    + _SIGNIN_HINT
                )
            await asyncio.sleep(poll_s)

    # ------------------------------------------------------------------
    # Pairing
    # ------------------------------------------------------------------

    async def wait_for_login(self, timeout_s: float = 300, poll_s: float = 2.0) -> None:
        """Wait until the QR code is scanned (used by the pairing script)."""
        browser = self._browser_or_default()
        await self.ensure_tab(browser)
        deadline = time.monotonic() + timeout_s
        last = "loading"
        while True:
            last = await self._login_state()
            if last == "logged_in":
                return
            if time.monotonic() >= deadline:
                raise WhatsAppNotPairedError(
                    "Timed out waiting for the QR code scan. "
                    "Run the script again when the phone is handy."
                )
            await asyncio.sleep(poll_s)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    async def list_chats(self, limit: int = 30) -> list[dict]:
        """Chats currently rendered in the chat list (no scrolling)."""
        await self._require_login()
        result = await self._evaluate("waListChats")
        return result.get("chats", [])[:limit]

    async def _chat_windows(self, max_rounds: int):
        """Yield the rendered chat window, top first, walking downward.

        The chat list is virtualized and ordered newest-first. Traversal
        resets to the top first (so unread badges on the newest chats are
        captured before anything else), then scrolls down one viewport per
        round until the tail stops changing or the bottom is reached.
        """
        await self._evaluate("waScrollTop")
        await asyncio.sleep(0.5)
        last_tail: str | None = None
        for _ in range(max_rounds):
            chats = (await self._evaluate("waListChats")).get("chats", [])
            yield chats
            tail = chats[-1].get("name") if chats else None
            if tail == last_tail:
                return
            last_tail = tail
            scrolled = await self._evaluate("waScrollChats")
            if not scrolled.get("advanced", True):
                return
            await asyncio.sleep(1.0)

    async def list_all_chats(self, max_rounds: int = 8) -> list[dict]:
        """Every chat, scrolling the (virtualized) list top-down until stable."""
        await self._require_login()
        seen: dict[str, dict] = {}
        async for chats in self._chat_windows(max_rounds):
            for chat in chats:
                seen.setdefault(chat.get("name", ""), chat)
        return [c for c in seen.values() if c.get("name")]

    async def find_chat(self, name: str, max_rounds: int = 8) -> str:
        """Resolve `name` to the exact chat title, scrolling to find it.

        Raises WhatsAppError naming candidates when ambiguous, or saying
        plainly when nothing matches. Never guesses.
        """
        await self._require_login()
        async for chats in self._chat_windows(max_rounds):
            matches = find_chats(name, chats)
            if len(matches) == 1:
                return matches[0]["name"]
            if len(matches) > 1:
                candidates = ", ".join(repr(c["name"]) for c in matches)
                raise WhatsAppError(
                    f"Several WhatsApp chats match {name!r}: {candidates}. "
                    "Which one did you mean?"
                )
        raise WhatsAppError(
            f"No WhatsApp chat named {name!r} found. "
            "Check the name, or list the chats first."
        )

    async def _open_chat(
        self,
        exact_name: str,
        timeout_s: float = 10,
        poll_s: float = 0.5,
        attempts: int = 2,
    ) -> None:
        """Click a chat row and wait until its messages actually render.

        A chat whose pane stays empty is retried once (a fresh click); after
        that it is declared unloadable. The conversation header is then
        verified, so a stale pane can never silently return another chat's
        messages.
        """
        last_error: WhatsAppError | None = None
        for _ in range(max(1, attempts)):
            clicked = await self._evaluate("waClickChat", exact_name)
            if not clicked.get("opened"):
                last_error = WhatsAppError(
                    f"The chat {exact_name!r} is not visible right now. "
                    "Try listing the chats first."
                )
                continue
            deadline = time.monotonic() + timeout_s
            loaded = False
            while time.monotonic() < deadline:
                result = await self._evaluate("waReadMessages", 1)
                # An empty pane with no messages yet is still loading; a
                # genuinely empty chat shows WhatsApp's "No messages here
                # yet" placeholder, reported as `empty`, which counts as
                # loaded rather than flaky.
                if result.get("error") is None and (
                    result.get("messages") or result.get("empty")
                ):
                    loaded = True
                    break
                await asyncio.sleep(poll_s)
            if not loaded:
                last_error = WhatsAppError(
                    f"Opened {exact_name!r} but its messages would not load."
                )
                continue
            try:
                await self._verify_conversation_header(exact_name)
            except WhatsAppError as exc:
                last_error = exc
                continue
            return
        raise last_error or WhatsAppError(
            f"The chat {exact_name!r} is not visible right now."
        )

    async def _verify_conversation_header(self, exact_name: str) -> None:
        """Raise if the open conversation header is not the requested chat.

        2026-09-24: read_chat("SC1-Confidants") twice returned SC1-Executives'
        messages because the pane stayed on the previous chat. A positive
        mismatch is never tolerated; an unreadable header (layout drift)
        skips the check rather than breaking everything.
        """
        title = (await self._evaluate("waConversationTitle")).get("title", "")
        if title and _norm_chat_name(title) != _norm_chat_name(exact_name):
            logger.warning(
                "WhatsApp header mismatch: requested %r but pane shows %r",
                exact_name,
                title,
            )
            raise WhatsAppError(
                f"Opened {exact_name!r} but WhatsApp is still showing the wrong chat "
                f"({title!r}); those messages would not be trustworthy. "
                "Please try again."
            )

    @staticmethod
    def _parse_messages(result: dict) -> list[dict]:
        messages = []
        for raw in result.get("messages", []):
            timestamp, sender = parse_message_meta(raw.get("meta", ""))
            messages.append(
                {
                    "sender": sender,
                    "time": timestamp,
                    "text": raw.get("text", ""),
                    "outgoing": bool(raw.get("outgoing")),
                }
            )
        return messages

    async def read_messages(
        self,
        chat_name: str,
        limit: int = 15,
        scroll_pause_s: float = 0.8,
        max_scrolls: int = 10,
    ) -> dict[str, object]:
        """Recent messages from one chat, oldest first.

        When `limit` exceeds the initially rendered handful, the message pane
        is scrolled upward (bounded) so older history loads; it stops when
        the limit is reached or no more history appears. Opening the chat
        marks its messages as read in WhatsApp.
        """
        matched = await self.find_chat(chat_name)
        await self._open_chat(matched)
        result = await self._evaluate("waReadMessages", limit)
        messages = self._parse_messages(result)
        scrolls = 0
        while len(messages) < limit and scrolls < max_scrolls:
            scrolled = await self._evaluate("waScrollMessagesUp")
            if not scrolled.get("advanced"):
                break
            await asyncio.sleep(scroll_pause_s)
            result = await self._evaluate("waReadMessages", limit)
            grown = self._parse_messages(result)
            if len(grown) <= len(messages):
                break  # history exhausted: scrolling loaded nothing older
            messages = grown
            scrolls += 1
        return {"chat": matched, "messages": messages}

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send_message(self, chat_name: str, text: str) -> dict[str, object]:
        """Type `text` into the chat and send it, verifying delivery to chat.

        Verification: a NEW outgoing message with the exact text must appear.
        On any doubt it raises without retrying — see the module docstring.
        """
        matched = await self.find_chat(chat_name)
        await self._open_chat(matched)
        before = _message_signature(
            (await self._evaluate("waLastMessage")).get("message")
        )
        typed = await self._evaluate("waTypeAndSend", text)
        if not typed.get("typed"):
            raise WhatsAppError(
                "Could not type into the WhatsApp message box "
                f"({typed.get('reason', 'unknown reason')}). Nothing was sent."
            )
        if not typed.get("clickedSend"):
            # The Send button was not found; Enter sends in WhatsApp Web.
            browser = self._browser_or_default()
            await browser.press_key("Enter")

        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            current = (await self._evaluate("waLastMessage")).get("message")
            if (
                current
                and _message_signature(current) != before
                and current.get("outgoing")
                and (current.get("text") or "") == text
            ):
                return {"sent": True, "chat": matched}
        raise WhatsAppError(
            "The message may not have been sent: I typed it but could not "
            "confirm it appeared in the chat. Please check WhatsApp before "
            "trying again — sending it again blindly could deliver it twice."
        )
