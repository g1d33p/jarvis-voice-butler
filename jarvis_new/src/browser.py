from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from playwright.async_api import (
    Locator,
    Page,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)


class BrowserError(Exception):
    """A user-facing browser operation failure."""


async def _bring_page_window_to_front(page: Page) -> None:
    """Best-effort: bring Sureedu's browser tab to the front.

    Focus handling must never block browser use, so any failure is ignored.
    """
    with contextlib.suppress(Exception):
        await page.bring_to_front()


class BrowserManager:
    """Own one isolated, visible browser for a LiveKit room."""

    def __init__(self, *, headless: bool = False, timeout_ms: int = 15_000) -> None:
        self._headless = headless
        self._timeout_ms = timeout_ms
        self._playwright = None
        self._browser = None
        self._context = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._page is not None:
            return

        self._playwright = await async_playwright().start()

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path.home() / ".sureedu" / "chrome-profile"),
            headless=self._headless,
        )

        self._browser = self._context.browser

        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()
        self._page.set_default_timeout(self._timeout_ms)
        if not self._headless:
            await _bring_page_window_to_front(self._page)

    async def close(self) -> None:
        async with self._lock:
            # On Ctrl+C the Playwright driver can exit before this runs,
            # so shutdown is best-effort and must never raise.
            if self._context is not None:
                with contextlib.suppress(Exception):
                    await self._context.close()
            if self._playwright is not None:
                with contextlib.suppress(Exception):
                    await self._playwright.stop()

            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None

    async def open_url(self, url: str) -> dict[str, str]:
        self._validate_url(url)
        page = await self._get_page()

        async with self._lock:
            try:
                await page.goto(url, wait_until="domcontentloaded")
                return await self._page_summary(page)
            except PlaywrightTimeoutError as exc:
                raise BrowserError("The page took too long to load.") from exc
            except Exception as exc:
                raise BrowserError(f"I could not open that page: {exc}") from exc

    async def read_page(self, *, max_chars: int = 12_000) -> dict[str, str | bool]:
        page = await self._get_page()

        async with self._lock:
            try:
                text = await page.locator("body").inner_text(timeout=self._timeout_ms)
            except PlaywrightTimeoutError as exc:
                raise BrowserError(
                    "The page content was not available in time."
                ) from exc

            text = re.sub(r"\s+", " ", text).strip()
            truncated = len(text) > max_chars
            return {
                "url": page.url,
                "title": await page.title(),
                "text": text[:max_chars],
                "truncated": truncated,
            }

    async def inspect_page(self, *, max_chars: int = 8_000) -> dict[str, object]:
        """Return readable text plus a compact inventory of interactive elements."""
        page = await self._get_page()

        async with self._lock:
            try:
                text = await page.locator("body").inner_text(timeout=self._timeout_ms)
                elements = await page.locator(
                    "button, a, input, textarea, select, [role]"
                ).evaluate_all(
                    """elements => elements
                      .filter(element => {
                        const style = window.getComputedStyle(element);
                        return style.display !== 'none' && style.visibility !== 'hidden';
                      })
                      .slice(0, 80)
                      .map((element, index) => ({
                        index,
                        tag: element.tagName.toLowerCase(),
                        role: element.getAttribute('role') || '',
                        name: (element.getAttribute('aria-label') ||
                          element.getAttribute('title') ||
                          (element.labels && element.labels[0] && element.labels[0].innerText) ||
                          element.getAttribute('placeholder') ||
                          element.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 120),
                        type: element.getAttribute('type') || '',
                      }))"""
                )
            except PlaywrightTimeoutError as exc:
                raise BrowserError("The page could not be inspected in time.") from exc

            text = re.sub(r"\s+", " ", text).strip()
            return {
                "url": page.url,
                "title": await page.title(),
                "text": text[:max_chars],
                "elements": elements,
            }

    async def go_back(self) -> dict[str, str]:
        page = await self._get_page()

        async with self._lock:
            try:
                await page.go_back(wait_until="domcontentloaded")
                return await self._page_summary(page)
            except PlaywrightTimeoutError as exc:
                raise BrowserError("The previous page took too long to load.") from exc
            except Exception as exc:
                raise BrowserError(f"I could not go back: {exc}") from exc

    async def take_screenshot(self) -> dict[str, str | int | bool]:
        page = await self._get_page()

        async with self._lock:
            try:
                image = await page.screenshot(type="png")
                return {
                    "captured": True,
                    "url": page.url,
                    "bytes": len(image),
                }
            except Exception as exc:
                raise BrowserError(f"I could not capture the page: {exc}") from exc

    async def click(self, target: str) -> dict[str, str]:
        page = await self._get_page()

        async with self._lock:
            locator = await self._resolve_target(page, target)
            try:
                await locator.click()
            except PlaywrightTimeoutError as exc:
                raise BrowserError(
                    f"The control {target!r} did not become clickable in time."
                ) from exc
            except Exception as exc:
                raise BrowserError(f"I could not click {target!r}: {exc}") from exc

            with contextlib.suppress(PlaywrightTimeoutError):
                await page.wait_for_load_state("domcontentloaded", timeout=5_000)

            return await self._page_summary(page)

    async def type_text(self, target: str, text: str) -> dict[str, str]:
        page = await self._get_page()

        async with self._lock:
            locator = await self._resolve_textbox(page, target)
            try:
                await locator.fill(text)
            except Exception as exc:
                raise BrowserError(f"I could not type into {target!r}: {exc}") from exc

            return {"target": target, "url": page.url}

    async def scroll(self, direction: Literal["up", "down"]) -> dict[str, str]:
        if direction not in {"up", "down"}:
            raise BrowserError("Scroll direction must be 'up' or 'down'.")

        page = await self._get_page()
        amount = -650 if direction == "up" else 650

        async with self._lock:
            await page.mouse.wheel(0, amount)
            return {"direction": direction, "url": page.url}

    async def press_key(self, key: str) -> dict[str, str]:
        allowed_keys = {
            "Enter",
            "Escape",
            "Tab",
            "ArrowDown",
            "ArrowLeft",
            "ArrowRight",
            "ArrowUp",
            "Backspace",
        }
        if key not in allowed_keys:
            raise BrowserError("That keyboard key is not allowed.")

        page = await self._get_page()
        async with self._lock:
            await page.keyboard.press(key)
            return {"key": key, "url": page.url}

    async def _get_page(self) -> Page:
        if self._page is None:
            await self.start()
        assert self._page is not None
        return self._page

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise BrowserError("Only complete http or https URLs can be opened.")
        if parsed.username or parsed.password:
            raise BrowserError("URLs containing embedded credentials are not allowed.")

    @staticmethod
    async def _page_summary(page: Page) -> dict[str, str]:
        return {"url": page.url, "title": await page.title()}

    async def _resolve_target(self, page: Page, target: str) -> Locator:
        for role in ("button", "link", "tab", "menuitem", "checkbox", "radio"):
            locator = page.get_by_role(role, name=target, exact=True)
            if await locator.count():
                return locator.first

            locator = page.get_by_role(role, name=target, exact=False)
            if await locator.count():
                return locator.first

        locator = page.get_by_text(target, exact=True)
        if await locator.count():
            return locator.first

        locator = page.get_by_text(target, exact=False)
        if await locator.count():
            return locator.first

        raise BrowserError(f"I could not find a visible control named {target!r}.")

    async def _resolve_textbox(self, page: Page, target: str) -> Locator:
        for locator in (
            page.get_by_role("textbox", name=target, exact=True),
            page.get_by_role("textbox", name=target, exact=False),
            page.get_by_label(target, exact=True),
            page.get_by_label(target, exact=False),
            page.get_by_placeholder(target, exact=True),
            page.get_by_placeholder(target, exact=False),
        ):
            if await locator.count():
                return locator.first

        normalized_target = target.casefold()
        if any(
            word in normalized_target for word in ("search", "query", "input", "text")
        ):
            for selector in (
                "input[type='search']:visible",
                "input[aria-label*='search' i]:visible",
                "input[placeholder*='search' i]:visible",
                "textarea:visible",
                "input:visible",
            ):
                locator = page.locator(selector)
                if await locator.count():
                    return locator.first

        raise BrowserError(f"I could not find a text field named {target!r}.")
