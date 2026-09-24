"""Sureedu's "eyes": one compact picture of what is happening on the Mac.

An Observation records the frontmost app and window, and the state of
Sureedu's browser (tabs, active page). Comparing two observations tells
Sureedu what changed, which the verification layer (Phase 4) builds on.

Everything here is read-only and cheap: it never starts the browser, never
takes screenshots, and returns short text rather than page contents.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from browser import BrowserManager
from mac_tools import read_active_app


@dataclass
class Observation:
    at: str
    app: str
    window_title: str
    browser: dict | None
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "time": self.at,
            "front_app": self.app or "unknown",
            "window_title": self.window_title,
        }
        if self.browser is None:
            data["sureedu_browser"] = "not open"
        else:
            active = self.browser.get("active_tab") or {}
            data["sureedu_browser"] = {
                "tab_count": self.browser.get("tab_count", 0),
                "active_tab": active.get("title", ""),
                "active_url": active.get("url", ""),
                "tabs": [
                    f"{tab['number']}: {tab['title'] or tab['url']}"
                    for tab in self.browser.get("tabs", [])
                ],
            }
        if self.note:
            data["note"] = self.note
        return data


async def observe(
    browser: BrowserManager,
    read_app: Callable[[], dict[str, object]] | None = None,
) -> Observation:
    """Take one observation of the Mac and Sureedu's browser."""
    note = ""
    try:
        app_info = (read_app or read_active_app)()
    except ToolError as exc:
        app_info = {"app": "", "window_title": ""}
        note = str(exc)
    return Observation(
        at=datetime.now().strftime("%H:%M:%S"),
        app=str(app_info.get("app", "")),
        window_title=str(app_info.get("window_title", "")),
        browser=await browser.snapshot(),
        note=note or str(app_info.get("note", "")),
    )


def _tabs_by_url(browser: dict | None) -> dict[str, str]:
    if not browser:
        return {}
    return {tab["url"]: tab["title"] or tab["url"] for tab in browser.get("tabs", [])}


def diff(before: Observation, after: Observation) -> list[str]:
    """Describe, in short sentences, what changed between two observations."""
    changes: list[str] = []

    if before.app != after.app:
        changes.append(
            f"Front app changed from {before.app or 'unknown'} to {after.app or 'unknown'}."
        )
    elif before.window_title != after.window_title and after.window_title:
        changes.append(f"Window changed to {after.window_title!r}.")

    if before.browser is None and after.browser is not None:
        changes.append("Sureedu's browser was opened.")
    elif before.browser is not None and after.browser is None:
        changes.append("Sureedu's browser was closed.")
    elif before.browser is not None and after.browser is not None:
        old_tabs, new_tabs = _tabs_by_url(before.browser), _tabs_by_url(after.browser)
        for url in new_tabs.keys() - old_tabs.keys():
            changes.append(f"Page opened: {new_tabs[url]}.")
        for url in old_tabs.keys() - new_tabs.keys():
            changes.append(f"Page no longer open: {old_tabs[url]}.")
        old_active = (before.browser.get("active_tab") or {}).get("url")
        new_active = after.browser.get("active_tab") or {}
        if old_active != new_active.get("url") and new_active:
            changes.append(
                f"Active tab is now {new_active.get('title') or new_active.get('url')}."
            )

    return changes


class ObservationTools:
    """Voice tool for looking at the current state of the Mac."""

    def __init__(self, browser: BrowserManager) -> None:
        self.browser = browser
        self._last: Observation | None = None

    @property
    def tools(self) -> list:
        return [self.observe_state]

    @function_tool()
    async def observe_state(self, context: RunContext) -> dict[str, object]:
        """Look at what is happening on the Mac right now.

        Returns the app in front and its window, Sureedu's browser tabs, and
        what changed since you last looked. Use this when the user asks what
        they are looking at or refers to "this app" or "this window", and
        whenever you are unsure what state things are in before acting.
        It does not read page contents; use read_page or inspect_page for that.
        """
        current = await observe(self.browser)
        result = current.to_dict()
        if self._last is None:
            result["changes_since_last_look"] = "This is the first look this session."
        else:
            result["changes_since_last_look"] = (
                diff(self._last, current) or "Nothing changed."
            )
        self._last = current
        return result
