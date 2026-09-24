import re
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from browser import BrowserError, BrowserManager
from policy import can_send_without_asking

# How long a blocked action waits for the user's yes.
APPROVAL_TTL_SECONDS = 60
# How long an approved draft message stays valid while it is typed and sent.
DRAFT_TTL_SECONDS = 120

_AFFIRMATIVE = {
    "yes",
    "yeah",
    "yep",
    "yup",
    "sure",
    "ok",
    "okay",
    "confirm",
    "confirmed",
    "proceed",
    "absolutely",
    "definitely",
    "correct",
    "affirmative",
    # Telugu / Hindi
    "avunu",
    "sare",
    "sari",
    "haan",
    "cheyyi",
    "pampu",
}
_AFFIRMATIVE_PHRASES = (
    "go ahead",
    "do it",
    "send it",
    "please do",
    "go for it",
    "that works",
    "sounds good",
    "looks good",
)
# While a message is waiting to be sent, a plain instruction to send it
# ("send the message", "what are you waiting for, send it") is also a yes.
_SEND_COMMANDS = {"send", "pampu", "bhejo"}
_NEGATIVE = {
    "no",
    "nope",
    "not",
    "don't",
    "dont",
    "wait",
    "stop",
    "cancel",
    "hold",
    "never",
    "vaddu",
    "ledu",
    "nahi",
}


def is_clear_approval(reply: str, *, sending: bool = False) -> bool:
    """True only for an unambiguous yes; anything unclear counts as no.

    With sending=True, a direct instruction to send also counts as a yes.
    """
    words = re.findall(r"[a-z']+", reply.casefold())
    if not words or _NEGATIVE.intersection(words):
        return False
    text = " ".join(words)
    if sending and _SEND_COMMANDS.intersection(words):
        return True
    return bool(_AFFIRMATIVE.intersection(words)) or any(
        phrase in text for phrase in _AFFIRMATIVE_PHRASES
    )


def _normalize_message(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(".!").casefold()


@dataclass
class PendingAction:
    """A consequential action that was stopped to ask the user first."""

    kind: str  # "click" or "enter"
    target: str
    label: str
    message: str  # the text that would be sent, if any
    expires_at: float


def _latest_user_message(context: object) -> tuple[str, str] | None:
    """Return (id, text) of the most recent thing the user actually said."""
    try:
        for item in reversed(context.session.history.items):  # type: ignore[attr-defined]
            if getattr(item, "type", "") == "message" and item.role == "user":
                return str(getattr(item, "id", "")), item.text_content or ""
    except Exception:
        return None
    return None


def _latest_user_text(context: object) -> str | None:
    """Return the most recent thing the user actually said, if available."""
    latest = _latest_user_message(context)
    return latest[1] if latest else None


def duckduckgo_search_url(query: str) -> str:
    query = query.strip()
    if not query:
        raise ValueError("The search query cannot be empty.")
    return f"https://duckduckgo.com/?{urlencode({'q': query})}"


class BrowserTools:
    def __init__(self, browser: BrowserManager) -> None:
        self.browser = browser
        # The consequential action waiting for the user's yes. confirming it
        # performs it; anything that changes the page cancels it.
        self._pending: PendingAction | None = None
        # A drafted message the user approved before it was typed:
        # (normalized text, expiry time).
        self._approved_draft: tuple[str, float] | None = None
        # The user utterance that already sent a message without asking, so one
        # instruction can never send more than one message.
        self._auto_sent_for: str | None = None

    @property
    def tools(self) -> list:
        return [
            self.open_url,
            self.search_the_web,
            self.read_page,
            self.inspect_page,
            self.go_back,
            self.take_screenshot,
            self.click,
            self.confirm_browser_action,
            self.approve_draft,
            self.type_text,
            self.scroll,
            self.press_key,
            self.list_tabs,
            self.switch_tab,
            self.open_tab,
            self.close_tab,
            self.reload_page,
            self.close_browser,
        ]

    @function_tool()
    async def search_the_web(
        self,
        context: RunContext,
        query: str,
    ) -> dict[str, str]:
        """Open fallback DuckDuckGo results in the agent-controlled browser.

        Use this only when the user needs a general internet search and did not name a
        website, service, or domain. If the user names a destination, open its official
        URL directly with open_url instead. Results open in a new tab. Read or
        inspect the results before answering the user.

        Args:
            query: A concise DuckDuckGo search query containing all relevant context.
        """
        self._page_changed()
        try:
            # Search in a new tab so the page the user was on stays intact.
            return await self.browser.open_tab(duckduckgo_search_url(query))
        except (BrowserError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def open_url(self, context: RunContext, url: str) -> dict[str, str]:
        """Open a public webpage directly in the agent-controlled browser.

        Prefer this over DuckDuckGo whenever the user names a website, service, domain,
        or specific destination. Use the destination's official URL.

        Args:
            url: A complete http or https URL to open.
        """
        self._page_changed()
        try:
            return await self.browser.open_url(url)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def read_page(self, context: RunContext) -> dict[str, str | bool]:
        """Read the visible text from the current browser page."""
        try:
            return await self.browser.read_page()
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def inspect_page(self, context: RunContext) -> dict[str, object]:
        """Inspect the current page: readable text plus numbered interactive elements.

        Use this before clicking or typing. Each element has an id such as "#12";
        pass that id to click or type_text. Ids change whenever the page changes,
        so inspect again after navigating or if an id is reported missing.
        """
        self._pending = None
        try:
            return await self.browser.inspect_page()
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def go_back(self, context: RunContext) -> dict[str, str]:
        """Go back to the previous page in the agent-controlled browser."""
        self._page_changed()
        try:
            return await self.browser.go_back()
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def take_screenshot(self, context: RunContext) -> dict[str, str | int | bool]:
        """Capture the current browser page for diagnostics."""
        try:
            return await self.browser.take_screenshot()
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def click(self, context: RunContext, target: str) -> dict[str, str]:
        """Click a visible control.

        Prefer the element id from inspect_page, written like "#12". A visible or
        accessible name also works for simple pages. Never pass a CSS selector.
        The result's clicked field names what was actually clicked, such as the
        chat that opened; use it when telling the user what happened.

        Args:
            target: An element id such as "#12", or the control's visible name.
        """
        label = await self.browser.element_label(target)
        if self._requires_confirmation(target) or self._requires_confirmation(label):
            message = ""
            if label.casefold().strip() == "send":
                message = await self.browser.pending_message_text()
            await self._gate(context, "click", target, label, message)
        else:
            # Any other click (e.g. opening a different chat) could change what
            # an approval referred to, so it cancels it.
            self._page_changed()

        try:
            result = await self.browser.click(target)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc
        return {"clicked": label, **result}

    @function_tool()
    async def confirm_browser_action(
        self, context: RunContext, user_reply: str
    ) -> dict[str, object]:
        """Carry out the action that was stopped for approval, now that the user agreed.

        Call this right after the user answers your confirmation question. It
        performs the waiting click or Enter itself, so do not click Send or press
        Enter again afterwards. Only report success if this returns done.

        Args:
            user_reply: The user's exact words in reply to your question.
        """
        pending, self._pending = self._pending, None
        if pending is None or time.monotonic() > pending.expires_at:
            raise ToolError(
                "Nothing is waiting for approval any more. Try the action again; "
                "it will say whether approval is needed."
            )
        heard = _latest_user_text(context) or user_reply
        if not is_clear_approval(heard, sending=bool(pending.message)):
            raise ToolError(
                "That reply is not a clear yes, so nothing was done. Ask the user "
                "again."
            )
        if pending.message:
            current = await self.browser.pending_message_text()
            if _normalize_message(current) != _normalize_message(pending.message):
                raise ToolError(
                    "The message changed after the user was asked, so it was not "
                    "sent. Ask again with the new text."
                )

        try:
            if pending.kind == "enter":
                result = await self.browser.press_key("Enter")
            else:
                result = await self.browser.click(pending.target)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "done": True,
            "action": pending.label,
            "sent": pending.message,
            **result,
        }

    @function_tool()
    async def approve_draft(
        self, context: RunContext, message: str, user_reply: str
    ) -> str:
        """Record that the user approved a message you drafted for them.

        Use this when you proposed the wording and the user agreed ("that works",
        "yes, send that"). Then type exactly that text and send it; it will go
        through without asking a second time. Any different text still needs
        approval.

        Args:
            message: The exact message text the user approved.
            user_reply: The user's exact words agreeing to it.
        """
        heard = _latest_user_text(context) or user_reply
        if not is_clear_approval(heard, sending=True):
            raise ToolError("That reply is not a clear yes. Nothing was approved.")
        self._approved_draft = (
            _normalize_message(message),
            time.monotonic() + DRAFT_TTL_SECONDS,
        )
        return "Approved. Type exactly this text, then send it."

    @function_tool()
    async def type_text(
        self,
        context: RunContext,
        target: str,
        text: str,
    ) -> dict[str, str]:
        """Type into a visible text field without sending or submitting anything.

        Prefer the element id from inspect_page, written like "#7". A label,
        placeholder, or accessible name also works. Never pass a CSS selector.
        The result's typed_into field says which field was actually filled;
        check it before telling the user where the text went.

        Args:
            target: An element id such as "#7", or the field's label or placeholder.
            text: The text to enter.
        """
        self._pending = None
        try:
            return await self.browser.type_text(target, text)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def scroll(self, context: RunContext, direction: str) -> dict[str, str]:
        """Scroll the current browser page up or down.

        Args:
            direction: Either 'up' or 'down'.
        """
        try:
            return await self.browser.scroll(direction)  # type: ignore[arg-type]
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def press_key(self, context: RunContext, key: str) -> dict[str, str]:
        """Press a safe navigation key in the current browser page.

        Pressing Enter in a message box sends the message, and in a form submits
        it, so it follows the same approval rules as clicking Send.

        Args:
            key: One of Enter, Escape, Tab, an arrow key, or Backspace.
        """
        if key == "Enter":
            effect = await self.browser.enter_effect()
            label = str(effect.get("label") or "")
            consequential = effect.get("consequential")
            if consequential == "button":
                # Enter on a focused button is a click on that button.
                consequential = self._requires_confirmation(label)
            if consequential:
                message = str(effect.get("text") or "")
                await self._gate(
                    context, "enter", "Enter", "Send" if message else label, message
                )
            else:
                self._page_changed()
        else:
            self._page_changed()

        try:
            return await self.browser.press_key(key)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def list_tabs(self, context: RunContext) -> dict[str, object]:
        """List the open browser tabs with their numbers, titles and URLs.

        Use this when the user asks what tabs are open, or before switching or
        closing a tab when you are not sure which number it has.
        """
        try:
            return await self.browser.list_tabs()
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def switch_tab(
        self, context: RunContext, tab_number: int
    ) -> dict[str, object]:
        """Switch to an open browser tab so later page actions apply to it.

        Args:
            tab_number: The tab's number from list_tabs, counting from 1 on the left.
        """
        self._page_changed()
        try:
            return await self.browser.switch_tab(tab_number)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def open_tab(self, context: RunContext, url: str = "") -> dict[str, object]:
        """Open a new browser tab and make it the active tab.

        Use this when the user asks for a new tab, or wants a site opened without
        leaving the current page. To replace the current page instead, use open_url.

        Args:
            url: Optional complete http or https URL to load in the new tab.
        """
        self._page_changed()
        try:
            return await self.browser.open_tab(url or None)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def close_tab(
        self,
        context: RunContext,
        tab_number: int = 0,
        user_confirmed: bool = False,
    ) -> dict[str, object]:
        """Close a browser tab.

        If the result says needs_confirmation, the tab holds unsent or unsaved text.
        Tell the user, ask whether to close it anyway, and only call again with
        user_confirmed set to true after they clearly agree.

        Args:
            tab_number: The tab's number from list_tabs. Use 0 for the active tab.
            user_confirmed: True only after the user explicitly approved losing unsaved text.
        """
        self._page_changed()
        try:
            return await self.browser.close_tab(
                tab_number or None, confirmed=user_confirmed
            )
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def close_browser(
        self, context: RunContext, user_confirmed: bool = False
    ) -> dict[str, object]:
        """Close Sureedu's entire browser window, including all of its tabs.

        Use this when the user says "close the window" or "close the browser".
        To close only one page, use close_tab. Logins are kept, and the browser
        reopens automatically when next needed. If the result says
        needs_confirmation, tell the user which tabs have unsent text and only
        retry with user_confirmed set to true after they clearly agree.

        Args:
            user_confirmed: True only after the user approved losing unsent text.
        """
        self._page_changed()
        try:
            return await self.browser.close_browser(confirmed=user_confirmed)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def reload_page(self, context: RunContext) -> dict[str, str]:
        """Reload the active browser tab."""
        self._page_changed()
        try:
            return await self.browser.reload()
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    async def _may_send_without_asking(
        self, context: object, message: str | None = None
    ) -> bool:
        """True if policy allows sending the pending message without a question."""
        latest = _latest_user_message(context)
        if latest is None:
            return False
        utterance_id, user_words = latest
        if message is None:
            message = await self.browser.pending_message_text()
        allowed, _reason = can_send_without_asking(message, user_words)
        key = utterance_id or user_words
        if not allowed or key == self._auto_sent_for:
            return False
        self._auto_sent_for = key
        return True

    async def _gate(
        self, context: object, kind: str, target: str, label: str, message: str
    ) -> None:
        """Allow a consequential action, or stop it and ask the user first."""
        if message:
            draft, self._approved_draft = self._approved_draft, None
            if (
                draft is not None
                and time.monotonic() <= draft[1]
                and _normalize_message(message) == draft[0]
            ):
                return
            if await self._may_send_without_asking(context, message):
                return

        self._pending = PendingAction(
            kind=kind,
            target=target,
            label=label,
            message=message,
            expires_at=time.monotonic() + APPROVAL_TTL_SECONDS,
        )
        what = f"send {message!r}" if message else f"click {label!r}"
        raise ToolError(
            f"Not done yet: this needs the user's approval. Ask once, naturally, "
            f"whether to {what}, naming the recipient if there is one. When they "
            f"agree, call confirm_browser_action with their reply; that performs "
            f"it. Do not click or press Enter again yourself."
        )

    def _page_changed(self) -> None:
        """Cancel anything approved or waiting, because the context changed."""
        self._pending = None
        self._approved_draft = None

    @staticmethod
    def _requires_confirmation(target: str) -> bool:
        risky_words = {
            "buy",
            "confirm",
            "delete",
            "purchase",
            "remove",
            "send",
            "submit",
        }
        return bool(risky_words.intersection(target.casefold().split()))
