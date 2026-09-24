import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import browser as browser_module
from browser import BrowserError, BrowserManager
from tools import BrowserTools


def test_validate_url_accepts_http_urls() -> None:
    BrowserManager._validate_url("https://example.com/path")
    BrowserManager._validate_url("http://localhost:3000")


@pytest.mark.parametrize(
    "url",
    [
        "example.com",
        "file:///tmp/page.html",
        "javascript:alert(1)",
        "https://user:password@example.com",
    ],
)
def test_validate_url_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(BrowserError):
        BrowserManager._validate_url(url)


def test_consequential_controls_require_confirmation() -> None:
    assert BrowserTools._requires_confirmation("Submit order")
    assert BrowserTools._requires_confirmation("Delete account")
    assert not BrowserTools._requires_confirmation("Search")


def test_direct_navigation_is_prioritized_over_fallback_search() -> None:
    tools = BrowserTools(BrowserManager(headless=True)).tools

    assert [tool.id for tool in tools[:2]] == ["open_url", "search_the_web"]


@pytest.mark.asyncio
async def test_visible_browser_window_is_brought_to_front() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.was_brought_to_front = False

        async def bring_to_front(self) -> None:
            self.was_brought_to_front = True

    page = FakePage()

    await browser_module._bring_page_window_to_front(page)

    assert page.was_brought_to_front


@pytest.mark.asyncio
async def test_focus_failure_does_not_break_browser_use() -> None:
    class BrokenPage:
        async def bring_to_front(self) -> None:
            raise RuntimeError("window unavailable")

    # Must not raise: focusing is best-effort.
    await browser_module._bring_page_window_to_front(BrokenPage())


class _TestPageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"""
        <html>
          <body>
            <label for="search">Search</label>
            <input id="search" type="search" placeholder="Search the site">
            <button>Go</button>
          </body>
        </html>
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, message_format: str, *args: object) -> None:
        return


@pytest.mark.asyncio
async def test_inspect_and_interact_with_local_page(tmp_path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TestPageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # A throwaway profile, so tests never touch the real Sureedu browser profile.
    manager = BrowserManager(headless=True, profile_dir=tmp_path / "profile")

    try:
        await manager.open_url(f"http://127.0.0.1:{server.server_port}")
        inspected = await manager.inspect_page()
        elements = inspected["elements"]

        assert isinstance(elements, list)
        assert any(element["name"] == "Search" for element in elements)
        assert any(element["name"] == "Go" for element in elements)

        await manager.type_text("Search", "LiveKit")
        await manager.click("Go")
    finally:
        await manager.close()
        server.shutdown()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_close_survives_dead_playwright_driver() -> None:
    class DeadContext:
        async def close(self) -> None:
            raise RuntimeError("Connection closed while reading from the driver")

    class DeadPlaywright:
        async def stop(self) -> None:
            raise RuntimeError("driver already exited")

    manager = browser_module.BrowserManager()
    manager._context = DeadContext()
    manager._playwright = DeadPlaywright()

    await manager.close()  # must not raise

    assert manager._context is None
    assert manager._playwright is None


# ----------------------------------------------------------------------
# Tab management
# ----------------------------------------------------------------------

_TAB_PAGES = {
    "/one": b"<html><head><title>Page One</title></head><body>One</body></html>",
    "/two": b"<html><head><title>Page Two</title></head><body>Two</body></html>",
    "/draft": (
        b"<html><head><title>Draft Page</title></head>"
        b"<body><textarea aria-label='Message'></textarea></body></html>"
    ),
    "/links": (
        b"<html><head><title>Links</title></head>"
        b"<body><a href='/two' target='_blank'>Open two</a></body></html>"
    ),
}


class _TabPageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = _TAB_PAGES.get(self.path, b"<html><title>Missing</title></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, message_format: str, *args: object) -> None:
        return


@pytest.fixture
async def tab_browser(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TabPageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manager = BrowserManager(headless=True, profile_dir=tmp_path / "profile")
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield manager, base
    finally:
        await manager.close()
        server.shutdown()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_open_list_and_switch_tabs(tab_browser) -> None:
    manager, base = tab_browser

    await manager.open_url(f"{base}/one")
    opened = await manager.open_tab(f"{base}/two")
    assert opened["number"] == 2
    assert opened["title"] == "Page Two"

    listing = await manager.list_tabs()
    assert listing["tab_count"] == 2
    assert [tab["title"] for tab in listing["tabs"]] == ["Page One", "Page Two"]
    assert [tab["active"] for tab in listing["tabs"]] == [False, True]

    switched = await manager.switch_tab(1)
    assert switched["title"] == "Page One"
    # Page actions now apply to tab 1.
    read = await manager.read_page()
    assert read["title"] == "Page One"


@pytest.mark.asyncio
async def test_switch_to_missing_tab_is_a_clear_error(tab_browser) -> None:
    manager, base = tab_browser
    await manager.open_url(f"{base}/one")

    with pytest.raises(BrowserError, match="There is no tab 5"):
        await manager.switch_tab(5)


@pytest.mark.asyncio
async def test_close_tab_activates_left_neighbour(tab_browser) -> None:
    manager, base = tab_browser
    await manager.open_url(f"{base}/one")
    await manager.open_tab(f"{base}/two")

    result = await manager.close_tab()

    assert result["closed"] is True
    assert result["closed_title"] == "Page Two"
    assert result["active_tab"] == 1
    assert (await manager.list_tabs())["tab_count"] == 1


@pytest.mark.asyncio
async def test_last_tab_is_never_closed(tab_browser) -> None:
    manager, base = tab_browser
    await manager.open_url(f"{base}/one")

    with pytest.raises(BrowserError, match="only open tab"):
        await manager.close_tab()


@pytest.mark.asyncio
async def test_tab_with_unsent_text_needs_confirmation(tab_browser) -> None:
    manager, base = tab_browser
    await manager.open_url(f"{base}/one")
    await manager.open_tab(f"{base}/draft")
    await manager.type_text("Message", "Half-written reply")

    first = await manager.close_tab()
    assert first["closed"] is False
    assert first["needs_confirmation"] is True
    assert (await manager.list_tabs())["tab_count"] == 2

    second = await manager.close_tab(confirmed=True)
    assert second["closed"] is True
    assert (await manager.list_tabs())["tab_count"] == 1


@pytest.mark.asyncio
async def test_link_opened_in_new_tab_becomes_active(tab_browser) -> None:
    manager, base = tab_browser
    await manager.open_url(f"{base}/links")

    await manager.click("Open two")
    # Give the browser a moment to create and load the new tab.
    for _ in range(20):
        listing = await manager.list_tabs()
        if listing["tab_count"] == 2:
            break
        await asyncio.sleep(0.1)

    assert listing["tab_count"] == 2
    assert listing["tabs"][1]["active"] is True


@pytest.mark.asyncio
async def test_reload_keeps_the_active_tab(tab_browser) -> None:
    manager, base = tab_browser
    await manager.open_url(f"{base}/one")

    result = await manager.reload()

    assert result["title"] == "Page One"


def test_tab_tools_are_registered() -> None:
    ids = [tool.id for tool in BrowserTools(BrowserManager(headless=True)).tools]
    for name in ("list_tabs", "switch_tab", "open_tab", "close_tab", "reload_page"):
        assert name in ids


# ----------------------------------------------------------------------
# Element ids (clicking rows whose text is split across child elements)
# ----------------------------------------------------------------------

_CHAT_PAGE = b"""
<html><head><title>Chats</title></head><body>
  <div role="list">
    <div role="listitem" onclick="document.title='Opened Alice'">
      <span>Alice</span> <span>Yesterday</span> <span>Voice call</span>
    </div>
    <div role="listitem" onclick="document.title='Opened Team'">
      <span>Team Group</span> <span>1 unread message</span>
    </div>
  </div>
  <div contenteditable="true" aria-label="Type a message"></div>
  <button aria-label="Send">&gt;</button>
</body></html>
"""


class _ChatPageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(_CHAT_PAGE)))
        self.end_headers()
        self.wfile.write(_CHAT_PAGE)

    def log_message(self, message_format: str, *args: object) -> None:
        return


@pytest.fixture
async def chat_browser(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ChatPageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manager = BrowserManager(headless=True, profile_dir=tmp_path / "profile")
    try:
        await manager.open_url(f"http://127.0.0.1:{server.server_port}/")
        yield manager
    finally:
        await manager.close()
        server.shutdown()
        thread.join(timeout=2)


def _find(elements: list, name_part: str) -> dict:
    return next(e for e in elements if name_part in e["name"])


@pytest.mark.asyncio
async def test_row_with_split_text_is_clickable_by_id(chat_browser) -> None:
    manager = chat_browser
    elements = (await manager.inspect_page())["elements"]
    alice = _find(elements, "Alice")
    assert alice["id"].startswith("#")

    result = await manager.click(alice["id"])
    assert result["title"] == "Opened Alice"


@pytest.mark.asyncio
async def test_type_into_editable_by_id_then_close_asks_first(chat_browser) -> None:
    manager = chat_browser
    await manager.open_tab()
    await manager.switch_tab(1)
    elements = (await manager.inspect_page())["elements"]
    composer = _find(elements, "Type a message")

    await manager.type_text(composer["id"], "Hello, not sending this")
    result = await manager.close_tab()

    assert result["closed"] is False
    assert result["needs_confirmation"] is True


@pytest.mark.asyncio
async def test_stale_element_id_gives_clear_error(chat_browser) -> None:
    manager = chat_browser
    await manager.inspect_page()

    with pytest.raises(BrowserError, match="Inspect the page again"):
        await manager.click("#999")


@pytest.mark.asyncio
async def test_plain_numbers_are_treated_as_text_not_ids(chat_browser) -> None:
    manager = chat_browser
    await manager.inspect_page()

    # "1" is not an id (ids need "#"); it is matched as visible text instead.
    result = await manager.click("1 unread message")
    assert result["title"] == "Opened Team"


@pytest.mark.asyncio
async def test_send_button_clicked_by_id_still_requires_confirmation(
    chat_browser,
) -> None:
    from livekit.agents.llm import ToolError

    manager = chat_browser
    tools = BrowserTools(manager)
    send = _find((await manager.inspect_page())["elements"], "Send")

    assert await manager.element_label(send["id"]) == "Send"
    with pytest.raises(ToolError, match="confirm clicking 'Send'"):
        await tools.click(None, send["id"])

    await tools.confirm_browser_action(None, send["id"])
    await tools.click(None, send["id"])  # now allowed, exactly once
