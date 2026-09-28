"""Yaadhamma's "eyes": one compact picture of what is happening on the Mac.

An Observation records the frontmost app and window, and the state of
Yaadhamma's browser (tabs, active page). Comparing two observations tells
Yaadhamma what changed, which the verification layer (Phase 4) builds on.

Everything here is read-only and cheap: it never starts the browser, never
takes screenshots, and returns short text rather than page contents.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from browser import BrowserManager
from mac_tools import read_active_app
from untrusted import wrap as _wrap_untrusted


@dataclass
class Observation:
    at: str
    app: str
    window_title: str
    browser: dict | None
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        # Window and page titles come from the outside world (any app, any
        # web page). They are data for the model, never instructions.
        data: dict[str, object] = {
            "time": self.at,
            "front_app": self.app or "unknown",
            "window_title": _wrap_untrusted(self.window_title, "Mac window title"),
        }
        if self.browser is None:
            data["yaadhamma_browser"] = "not open"
        else:
            active = self.browser.get("active_tab") or {}
            data["yaadhamma_browser"] = {
                "tab_count": self.browser.get("tab_count", 0),
                "active_tab": _wrap_untrusted(
                    str(active.get("title", "")), "browser tab title"
                ),
                "active_url": active.get("url", ""),
                "tabs": [
                    f"{tab['number']}: "
                    f"{_wrap_untrusted(str(tab['title'] or ''), 'browser tab title') or tab['url']}"
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
    """Take one observation of the Mac and Yaadhamma's browser."""
    note = ""
    try:
        # osascript can take up to a second; keep it off the voice's loop.
        app_info = await asyncio.to_thread(read_app or read_active_app)
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
    """Describe, in short sentences, what changed between two observations.

    Titles in these sentences come from the outside world, so they are
    wrapped as untrusted content just like in Observation.to_dict().
    """
    changes: list[str] = []

    if before.app != after.app:
        changes.append(
            f"Front app changed from {before.app or 'unknown'} to {after.app or 'unknown'}."
        )
    elif before.window_title != after.window_title and after.window_title:
        changes.append(
            "Window changed to "
            f"{_wrap_untrusted(after.window_title, 'Mac window title')!r}."
        )

    if before.browser is None and after.browser is not None:
        changes.append("Yaadhamma's browser was opened.")
    elif before.browser is not None and after.browser is None:
        changes.append("Yaadhamma's browser was closed.")
    elif before.browser is not None and after.browser is not None:
        old_tabs, new_tabs = _tabs_by_url(before.browser), _tabs_by_url(after.browser)
        for url in new_tabs.keys() - old_tabs.keys():
            changes.append(
                f"Page opened: {_wrap_untrusted(new_tabs[url], 'browser tab title')}."
            )
        for url in old_tabs.keys() - new_tabs.keys():
            changes.append(
                "Page no longer open: "
                f"{_wrap_untrusted(old_tabs[url], 'browser tab title')}."
            )
        old_active = (before.browser.get("active_tab") or {}).get("url")
        new_active = after.browser.get("active_tab") or {}
        if old_active != new_active.get("url") and new_active:
            changes.append(
                "Active tab is now "
                f"{_wrap_untrusted(str(new_active.get('title') or new_active.get('url')), 'browser tab title')}."
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

        Returns the app in front and its window, Yaadhamma's browser tabs, and
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
