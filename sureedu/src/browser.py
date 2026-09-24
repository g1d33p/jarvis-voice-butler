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


DEFAULT_PROFILE_DIR = Path.home() / ".sureedu" / "chrome-profile"

# Detects typed-but-unsent work (drafts, form entries) before a tab is closed.
_UNSAVED_INPUT_SCRIPT = """() => {
  const skipTypes = new Set(['hidden', 'search', 'submit', 'button', 'checkbox',
                             'radio', 'password', 'file', 'reset', 'image']);
  for (const el of document.querySelectorAll('input, textarea')) {
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (el.tagName === 'INPUT' && skipTypes.has(type)) continue;
    if (el.value && el.value.trim() && el.value !== el.defaultValue) return true;
  }
  for (const el of document.querySelectorAll('[contenteditable="true"]')) {
    if (el.innerText && el.innerText.trim()) return true;
  }
  return false;
}"""


# Matches element ids returned by inspect_page, e.g. "#12".
_ELEMENT_ID = re.compile(r"^\s*#(\d+)\s*$")

_INSPECT_SELECTOR = (
    "button, a, input, textarea, select, [role], [contenteditable='true']"
)

# Numbers every visible, meaningful interactive element and stamps it with a
# data-sureedu-id attribute, so it can be clicked later by id even when its
# visible text is split across many child elements (e.g. WhatsApp chat rows).
_INSPECT_SCRIPT = r"""elements => {
  document.querySelectorAll('[data-sureedu-id]')
    .forEach(el => el.removeAttribute('data-sureedu-id'));
  const nativeTags = new Set(['A', 'BUTTON', 'INPUT', 'TEXTAREA', 'SELECT']);
  const layoutRoles = new Set(['presentation', 'none', 'img', 'separator',
    'generic', 'heading', 'group', 'region', 'document', 'application', 'main',
    'navigation', 'banner', 'complementary', 'contentinfo', 'status', 'log',
    'tooltip', 'list', 'grid', 'rowgroup', 'toolbar', 'dialog']);
  const results = [];
  for (const el of elements) {
    if (results.length >= 150) break;
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (role && layoutRoles.has(role) && !nativeTags.has(el.tagName)) continue;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    const name = (el.getAttribute('aria-label') ||
      el.getAttribute('title') ||
      (el.labels && el.labels[0] && el.labels[0].innerText) ||
      el.getAttribute('placeholder') ||
      el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 100);
    const isField = ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) ||
      el.isContentEditable;
    if (!name && !isField) continue;
    const id = results.length + 1;
    el.setAttribute('data-sureedu-id', String(id));
    results.push({
      id: '#' + id,
      tag: el.tagName.toLowerCase(),
      role: role,
      name: name,
      type: el.getAttribute('type') || (el.isContentEditable ? 'editable' : ''),
    });
  }
  return results;
}"""

_ELEMENT_LABEL_SCRIPT = """el => (el.getAttribute('aria-label') || el.getAttribute('title') ||
  el.innerText || el.value || '').trim().replace(/\\s+/g, ' ').slice(0, 100)"""


class BrowserManager:
    """Own one isolated, visible browser for a LiveKit room.

    The browser can hold several tabs. One of them is the "active" tab: every
    page action (read, click, type, ...) applies to it. Tabs are numbered from 1,
    left to right, so the user can say "switch to tab 2".
    """

    def __init__(
        self,
        *,
        headless: bool = False,
        timeout_ms: int = 15_000,
        profile_dir: Path | None = None,
    ) -> None:
        self._headless = headless
        self._timeout_ms = timeout_ms
        self._profile_dir = profile_dir or DEFAULT_PROFILE_DIR
        self._playwright = None
        self._browser = None
        self._context = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._context is not None:
            return

        self._playwright = await async_playwright().start()

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._profile_dir),
            headless=self._headless,
        )

        self._browser = self._context.browser
        # Tabs opened by the page itself (e.g. a link with target="_blank")
        # become the active tab, because that is where the user's attention goes.
        self._context.on("page", self._on_new_page)

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
        """Return readable text plus a numbered inventory of interactive elements.

        Each element gets an id such as "#12" that click and type_text accept.
        Ids are reassigned on every inspection, so they are only valid until the
        page changes.
        """
        page = await self._get_page()

        async with self._lock:
            try:
                text = await page.locator("body").inner_text(timeout=self._timeout_ms)
                elements = await page.locator(_INSPECT_SELECTOR).evaluate_all(
                    _INSPECT_SCRIPT
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

    # ------------------------------------------------------------------
    # Tab management
    # ------------------------------------------------------------------

    async def list_tabs(self) -> dict[str, object]:
        """Return every open tab with its number, title, URL and active flag."""
        await self._get_page()

        async with self._lock:
            tabs = []
            for number, page in enumerate(self._open_pages(), start=1):
                tabs.append(
                    {
                        "number": number,
                        "title": await self._safe_title(page),
                        "url": page.url,
                        "active": page is self._page,
                    }
                )
            return {"tab_count": len(tabs), "tabs": tabs}

    async def switch_tab(self, number: int) -> dict[str, object]:
        """Make tab `number` (1-based) the active tab and bring it to the front."""
        await self._get_page()

        async with self._lock:
            page = self._page_by_number(number)
            self._page = page
            if not self._headless:
                await _bring_page_window_to_front(page)
            return {"number": number, **await self._page_summary(page)}

    async def open_tab(self, url: str | None = None) -> dict[str, object]:
        """Open a new tab, optionally at `url`, and make it the active tab."""
        if url:
            self._validate_url(url)
        await self._get_page()

        async with self._lock:
            assert self._context is not None
            page = await self._context.new_page()
            page.set_default_timeout(self._timeout_ms)
            self._page = page
            if url:
                try:
                    await page.goto(url, wait_until="domcontentloaded")
                except PlaywrightTimeoutError as exc:
                    raise BrowserError("The new tab took too long to load.") from exc
                except Exception as exc:
                    raise BrowserError(f"I could not open that page: {exc}") from exc
            if not self._headless:
                await _bring_page_window_to_front(page)
            number = self._open_pages().index(page) + 1
            return {"number": number, **await self._page_summary(page)}

    async def close_tab(
        self, number: int | None = None, *, confirmed: bool = False
    ) -> dict[str, object]:
        """Close tab `number` (default: the active tab).

        If the tab contains typed-but-unsent text, nothing is closed and the
        result has needs_confirmation=True, so the user can be asked first.
        The last remaining tab is never closed.
        """
        await self._get_page()

        async with self._lock:
            pages = self._open_pages()
            page = self._page if number is None else self._page_by_number(number)
            assert page is not None
            number = pages.index(page) + 1

            if len(pages) == 1:
                raise BrowserError("That is the only open tab, so I will keep it open.")

            if not confirmed and await self._has_unsaved_input(page):
                return {
                    "closed": False,
                    "needs_confirmation": True,
                    "number": number,
                    "title": await self._safe_title(page),
                    "reason": "The tab contains typed text that has not been sent or saved.",
                }

            title = await self._safe_title(page)
            try:
                await page.close()
            except Exception as exc:
                raise BrowserError(f"I could not close that tab: {exc}") from exc

            remaining = self._open_pages()
            if self._page is page or self._page not in remaining:
                # Activate the neighbour to the left, like a normal browser.
                self._page = remaining[max(0, number - 2)]
                if not self._headless:
                    await _bring_page_window_to_front(self._page)

            return {
                "closed": True,
                "closed_title": title,
                "active_tab": remaining.index(self._page) + 1,
                "active_title": await self._safe_title(self._page),
            }

    async def reload(self) -> dict[str, str]:
        """Reload the active tab."""
        page = await self._get_page()

        async with self._lock:
            try:
                await page.reload(wait_until="domcontentloaded")
                return await self._page_summary(page)
            except PlaywrightTimeoutError as exc:
                raise BrowserError("The page took too long to reload.") from exc
            except Exception as exc:
                raise BrowserError(f"I could not reload the page: {exc}") from exc

    def _on_new_page(self, page: Page) -> None:
        page.set_default_timeout(self._timeout_ms)
        self._page = page

    def _open_pages(self) -> list[Page]:
        if self._context is None:
            return []
        return [page for page in self._context.pages if not page.is_closed()]

    def _page_by_number(self, number: int) -> Page:
        pages = self._open_pages()
        if not isinstance(number, int) or not 1 <= number <= len(pages):
            raise BrowserError(
                f"There is no tab {number}. There are {len(pages)} tabs open."
            )
        return pages[number - 1]

    @staticmethod
    async def _safe_title(page: Page) -> str:
        try:
            return await page.title()
        except Exception:
            return ""

    @staticmethod
    async def _has_unsaved_input(page: Page) -> bool:
        try:
            return bool(await page.evaluate(_UNSAVED_INPUT_SCRIPT))
        except Exception:
            # If the page cannot be checked, err on the side of asking.
            return True

    async def _get_page(self) -> Page:
        if self._context is None:
            await self.start()
        if self._page is None or self._page.is_closed():
            # The active tab was closed (by the user or the page): fall back to
            # the right-most open tab, or open a fresh one.
            pages = self._open_pages()
            if pages:
                self._page = pages[-1]
            else:
                assert self._context is not None
                self._page = await self._context.new_page()
                self._page.set_default_timeout(self._timeout_ms)
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

    async def element_label(self, target: str) -> str:
        """Return the visible name of an element id like "#12", else `target`.

        Used so safety checks see "Send" rather than an opaque "#12".
        """
        match = _ELEMENT_ID.match(target)
        if not match:
            return target
        page = await self._get_page()
        locator = page.locator(f'[data-sureedu-id="{match.group(1)}"]')
        try:
            if await locator.count():
                return await locator.first.evaluate(_ELEMENT_LABEL_SCRIPT)
        except Exception:
            pass
        return target

    @staticmethod
    async def _element_by_id(page: Page, target: str) -> Locator | None:
        match = _ELEMENT_ID.match(target)
        if not match:
            return None
        locator = page.locator(f'[data-sureedu-id="{match.group(1)}"]')
        if await locator.count():
            return locator.first
        raise BrowserError(
            f"Element {target.strip()} is no longer on the page. "
            "Inspect the page again to get fresh element ids."
        )

    async def _resolve_target(self, page: Page, target: str) -> Locator:
        by_id = await self._element_by_id(page, target)
        if by_id is not None:
            return by_id

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
        by_id = await self._element_by_id(page, target)
        if by_id is not None:
            return by_id

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
