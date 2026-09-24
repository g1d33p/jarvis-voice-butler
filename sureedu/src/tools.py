from urllib.parse import urlencode

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from browser import BrowserError, BrowserManager


def duckduckgo_search_url(query: str) -> str:
    query = query.strip()
    if not query:
        raise ValueError("The search query cannot be empty.")
    return f"https://duckduckgo.com/?{urlencode({'q': query})}"


class BrowserTools:
    def __init__(self, browser: BrowserManager) -> None:
        self.browser = browser
        self._confirmed_target: str | None = None

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
        URL directly with open_url instead. Read or inspect the resulting page before
        answering the user.

        Args:
            query: A concise DuckDuckGo search query containing all relevant context.
        """
        try:
            return await self.browser.open_url(duckduckgo_search_url(query))
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
        """Inspect the current page, including readable text and interactive element names.

        Use this before clicking or typing so you can choose a visible control by its
        returned name or role.
        """
        try:
            return await self.browser.inspect_page()
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def go_back(self, context: RunContext) -> dict[str, str]:
        """Go back to the previous page in the agent-controlled browser."""
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
        """Click a visible control by its accessible name.

        Args:
            target: The visible or accessible name of the control to click.
        """
        if self._requires_confirmation(target):
            if self._confirmed_target != target.casefold():
                raise ToolError(
                    f"This action may be consequential. Ask the user to confirm clicking {target!r} before retrying."
                )
            self._confirmed_target = None

        try:
            return await self.browser.click(target)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def confirm_browser_action(self, context: RunContext, target: str) -> str:
        """Authorize one previously discussed consequential browser click.

        Call this only after the user explicitly confirms the exact action.

        Args:
            target: The exact accessible name of the control the user approved.
        """
        self._confirmed_target = target.casefold()
        return f"The user confirmed clicking {target!r}."

    @function_tool()
    async def type_text(
        self,
        context: RunContext,
        target: str,
        text: str,
    ) -> dict[str, str]:
        """Fill a visible text field by its label, placeholder, or accessible name.

        Args:
            target: The label, placeholder, or accessible name of the text field.
            text: The text to enter.
        """
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

        Args:
            key: One of Enter, Escape, Tab, an arrow key, or Backspace.
        """
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
        try:
            return await self.browser.close_tab(
                tab_number or None, confirmed=user_confirmed
            )
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def reload_page(self, context: RunContext) -> dict[str, str]:
        """Reload the active browser tab."""
        try:
            return await self.browser.reload()
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

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
