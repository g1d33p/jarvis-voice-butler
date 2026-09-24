import re
import time
from urllib.parse import urlencode

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from browser import BrowserError, BrowserManager
from policy import can_send_without_asking

# How long a user's approval stays valid before the click must happen.
APPROVAL_TTL_SECONDS = 60

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
_AFFIRMATIVE_PHRASES = ("go ahead", "do it", "send it", "please do", "go for it")
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


def is_clear_approval(reply: str) -> bool:
    """True only for an unambiguous yes; anything unclear counts as no."""
    words = re.findall(r"[a-z']+", reply.casefold())
    if not words or _NEGATIVE.intersection(words):
        return False
    text = " ".join(words)
    return bool(_AFFIRMATIVE.intersection(words)) or any(
        phrase in text for phrase in _AFFIRMATIVE_PHRASES
    )


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
        # One pending approval: (target, label, expiry time). It is used by
        # exactly one click, and revoked by anything that changes the page.
        self._approval: tuple[str, str, float] | None = None
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
        self._revoke_approval()
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
        self._revoke_approval()
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
        self._revoke_approval()
        try:
            return await self.browser.inspect_page()
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def go_back(self, context: RunContext) -> dict[str, str]:
        """Go back to the previous page in the agent-controlled browser."""
        self._revoke_approval()
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

        Args:
            target: An element id such as "#12", or the control's visible name.
        """
        label = await self.browser.element_label(target)
        needs_approval = self._requires_confirmation(
            target
        ) or self._requires_confirmation(label)
        if not needs_approval:
            # Any other click (e.g. opening a different chat) could change what
            # an earlier approval referred to, so it cancels that approval.
            self._revoke_approval()
        elif not self._consume_approval(target, label) and not (
            label.casefold().strip() == "send"
            and await self._may_send_without_asking(context)
        ):
            raise ToolError(
                f"This action may be consequential and is not approved. Ask the "
                f"user to confirm clicking {label!r}, stating exactly what will "
                f"happen, then call confirm_browser_action with their reply."
            )

        try:
            return await self.browser.click(target)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def confirm_browser_action(
        self, context: RunContext, target: str, user_reply: str
    ) -> str:
        """Record the user's approval for one consequential click, such as Send.

        Call this only after asking the user and hearing their answer. This does
        NOT perform the click: call click with the same target right afterwards,
        and only report success once click returns.

        Args:
            target: The exact target you will pass to click, e.g. "#12" or "Send".
            user_reply: The user's exact words in reply to your confirmation question.
        """
        # Judge the user's real words (the speech transcript) when available,
        # rather than the model's retelling of them.
        heard = _latest_user_text(context) or user_reply
        if not is_clear_approval(heard):
            self._approval = None
            raise ToolError(
                "That reply is not a clear yes. Nothing was approved. Ask the user "
                "again, and do not click."
            )
        label = await self.browser.element_label(target)
        self._approval = (
            target.casefold(),
            label.casefold(),
            time.monotonic() + APPROVAL_TTL_SECONDS,
        )
        return (
            f"Approved for one click on {label!r} within {APPROVAL_TTL_SECONDS} "
            f"seconds. The action has NOT happened yet: call click with target "
            f"{target!r} now."
        )

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
        self._revoke_approval()
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
        it, so Enter then needs the user's approval exactly like clicking Send:
        ask, call confirm_browser_action with target "Enter", then press Enter.

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
            if (
                consequential
                and not self._consume_approval("Enter", "enter")
                and not (
                    consequential == "send"
                    and await self._may_send_without_asking(
                        context, str(effect.get("text") or "")
                    )
                )
            ):
                raise ToolError(
                    "Pressing Enter here would send or submit it, and it is not "
                    "approved. Tell the user exactly what will be sent or "
                    "submitted, ask for confirmation, then call "
                    'confirm_browser_action with target "Enter" and their reply.'
                )
            if not consequential:
                self._revoke_approval()
        else:
            self._revoke_approval()

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
        self._revoke_approval()
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
        self._revoke_approval()
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
        self._revoke_approval()
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
        self._revoke_approval()
        try:
            return await self.browser.close_browser(confirmed=user_confirmed)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def reload_page(self, context: RunContext) -> dict[str, str]:
        """Reload the active browser tab."""
        self._revoke_approval()
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

    def _revoke_approval(self) -> None:
        self._approval = None

    def _consume_approval(self, target: str, label: str) -> bool:
        approval, self._approval = self._approval, None
        if approval is None:
            return False
        approved_target, approved_label, expires_at = approval
        if time.monotonic() > expires_at:
            return False
        return approved_target == target.casefold() or (
            approved_label == label.casefold() and approved_label != ""
        )

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
