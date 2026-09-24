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
import re
import time
from pathlib import Path

WHATSAPP_URL = "https://web.whatsapp.com/"

_JS_SRC = Path(__file__).with_name("whatsapp_extractors.js").read_text(encoding="utf-8")

_SIGNIN_HINT = (
    "WhatsApp is not paired yet. On the Mac, run "
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

    async def _require_login(self) -> None:
        if await self._login_state() != "logged_in":
            raise WhatsAppNotPairedError(_SIGNIN_HINT)

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

    async def list_all_chats(self, max_rounds: int = 6) -> list[dict]:
        """Every chat, scrolling the (virtualized) list until it stabilizes."""
        await self._require_login()
        seen: dict[str, dict] = {}
        last_tail: str | None = None
        for _ in range(max_rounds):
            chats = (await self._evaluate("waListChats")).get("chats", [])
            for chat in chats:
                seen.setdefault(chat.get("name", ""), chat)
            tail = chats[-1].get("name") if chats else None
            if tail == last_tail:
                break
            last_tail = tail
            await self._evaluate("waScrollChats")
            await asyncio.sleep(1.0)
        return [c for c in seen.values() if c.get("name")]

    async def find_chat(self, name: str, max_rounds: int = 6) -> str:
        """Resolve `name` to the exact chat title, scrolling to find it.

        Raises WhatsAppError naming candidates when ambiguous, or saying
        plainly when nothing matches. Never guesses.
        """
        await self._require_login()
        last_tail: str | None = None
        for _ in range(max_rounds):
            chats = (await self._evaluate("waListChats")).get("chats", [])
            matches = find_chats(name, chats)
            if len(matches) == 1:
                return matches[0]["name"]
            if len(matches) > 1:
                candidates = ", ".join(repr(c["name"]) for c in matches)
                raise WhatsAppError(
                    f"Several WhatsApp chats match {name!r}: {candidates}. "
                    "Which one did you mean?"
                )
            tail = chats[-1].get("name") if chats else None
            if tail == last_tail:
                break
            last_tail = tail
            await self._evaluate("waScrollChats")
            await asyncio.sleep(1.0)
        raise WhatsAppError(
            f"No WhatsApp chat named {name!r} found. "
            "Check the name, or list the chats first."
        )

    async def _open_chat(self, exact_name: str, timeout_s: float = 10) -> None:
        clicked = await self._evaluate("waClickChat", exact_name)
        if not clicked.get("opened"):
            raise WhatsAppError(
                f"The chat {exact_name!r} is not visible right now. "
                "Try listing the chats first."
            )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            result = await self._evaluate("waReadMessages", 1)
            if result.get("error") is None:
                return
            await asyncio.sleep(0.5)
        raise WhatsAppError(f"Opened {exact_name!r} but its messages would not load.")

    async def read_messages(self, chat_name: str, limit: int = 15) -> dict[str, object]:
        """Recent messages from one chat, oldest first.

        Opening the chat marks its messages as read in WhatsApp.
        """
        matched = await self.find_chat(chat_name)
        await self._open_chat(matched)
        result = await self._evaluate("waReadMessages", limit)
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
