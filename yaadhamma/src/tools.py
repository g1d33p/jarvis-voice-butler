"""Browser tools: what Yaadhamma can do on the web.

Consequential actions (sending, buying, submitting) go through the shared
ApprovalManager from permissions.py: the tool stops, the model asks, and
approve_pending_action carries the action out after a clear yes.
"""

from urllib.parse import urlencode

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from browser import BrowserError, BrowserManager
from permissions import (
    ApprovalManager,
    is_clear_approval,  # noqa: F401  (re-exported: tests import it from here)
    is_risky_click_target,
    latest_user_message,
    normalize_message,
)
from policy import can_send_without_asking


def duckduckgo_search_url(query: str) -> str:
    query = query.strip()
    if not query:
        raise ValueError("The search query cannot be empty.")
    return f"https://duckduckgo.com/?{urlencode({'q': query})}"


class BrowserTools:
    def __init__(
        self, browser: BrowserManager, approvals: ApprovalManager | None = None
    ) -> None:
        self.browser = browser
        # Shared across toolsets: the voice agent and the orchestrator use one
        # manager, so an approval asked in one path can be answered in either.
        self._approvals = approvals or ApprovalManager()
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
            self.click,
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
        self._approvals.cancel_pending()
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
        self._approvals.cancel_pending()
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
        self._approvals.cancel_pending()
        try:
            return await self.browser.inspect_page()
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def go_back(self, context: RunContext) -> dict[str, str]:
        """Go back to the previous page in the agent-controlled browser."""
        self._approvals.cancel_pending()
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
            description = f"send {message!r}" if message else f"click {label!r}"
            pre_approved = (
                await self._policy_approval(context, message) if message else None
            )

            async def execute() -> dict[str, str]:
                try:
                    result = await self.browser.click(target)
                except BrowserError as exc:
                    raise ToolError(str(exc)) from exc
                outcome: dict[str, str] = {"clicked": label}
                if message:
                    outcome["sent"] = message
                return {**outcome, **result}

            async def verify() -> None:
                current = await self.browser.pending_message_text()
                if normalize_message(current) != normalize_message(message):
                    raise ToolError(
                        "The message changed after the user was asked, so it was "
                        "not sent. Ask again with the new text."
                    )

            return await self._approvals.gate(
                tool_name="click",
                description=description,
                context=context,
                execute=execute,
                verify=verify if message else None,
                args={"target": target, "label": label},
                pre_approved=pre_approved,
                quoted=message,
            )
        # Any other click (e.g. opening a different chat) could change what an
        # approval referred to, so it cancels it.
        self._approvals.cancel_pending()

        try:
            result = await self.browser.click(target)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc
        return {"clicked": label, **result}

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
        self._approvals.cancel_pending()
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
                description = (
                    f"send {message!r}"
                    if message
                    else f"submit with Enter ({label or 'the form'})"
                )
                pre_approved = (
                    await self._policy_approval(context, message) if message else None
                )

                async def execute() -> dict[str, str]:
                    try:
                        sent_result = await self.browser.press_key("Enter")
                    except BrowserError as exc:
                        raise ToolError(str(exc)) from exc
                    sent_result["effect"] = (
                        f"Enter sent or submitted it ({label or 'form'})."
                    )
                    sent_result["sent"] = True
                    return sent_result

                async def verify() -> None:
                    current = await self.browser.pending_message_text()
                    if normalize_message(current) != normalize_message(message):
                        raise ToolError(
                            "The message changed after the user was asked, so it "
                            "was not sent. Ask again with the new text."
                        )

                return await self._approvals.gate(
                    tool_name="press_key",
                    description=description,
                    context=context,
                    execute=execute,
                    verify=verify if message else None,
                    args={"key": "Enter", "consequential": True},
                    pre_approved=pre_approved,
                    quoted=message,
                )
            self._approvals.cancel_pending()
        else:
            self._approvals.cancel_pending()

        try:
            result = await self.browser.press_key(key)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc
        if key == "Enter":
            if consequential:
                result["effect"] = f"Enter sent or submitted it ({label or 'form'})."
                result["sent"] = True
            else:
                where = label or "the page"
                result["effect"] = f"Enter pressed in {where}. Nothing was sent."
                result["sent"] = False
        return result

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
        self._approvals.cancel_pending()
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
        self._approvals.cancel_pending()
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
        self._approvals.cancel_pending()
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
        """Close Yaadhamma's entire browser window, including all of its tabs.

        Use this when the user says "close the window" or "close the browser".
        To close only one page, use close_tab. Logins are kept, and the browser
        reopens automatically when next needed. If the result says
        needs_confirmation, tell the user which tabs have unsent text and only
        retry with user_confirmed set to true after they clearly agree.

        Args:
            user_confirmed: True only after the user approved losing unsent text.
        """
        self._approvals.cancel_pending()
        try:
            return await self.browser.close_browser(confirmed=user_confirmed)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def reload_page(self, context: RunContext) -> dict[str, str]:
        """Reload the active browser tab."""
        self._approvals.cancel_pending()
        try:
            return await self.browser.reload()
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    async def _policy_approval(self, context: object, message: str) -> str | None:
        """Reason the dictated-message policy allows sending, else None.

        Short messages the user dictated word for word can go without asking
        (see policy.can_send_without_asking). One utterance can only send one
        message this way.
        """
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

    @staticmethod
    def _requires_confirmation(target: str) -> bool:
        return is_risky_click_target(target)
