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
async def test_closing_last_tab_closes_browser_and_it_reopens(tab_browser) -> None:
    manager, base = tab_browser
    await manager.open_url(f"{base}/one")

    result = await manager.close_tab()
    assert result["closed"] is True
    assert result["browser_closed"] is True

    # The next request simply starts the browser again.
    reopened = await manager.open_url(f"{base}/two")
    assert reopened["title"] == "Page Two"


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

    await tools.confirm_browser_action(None, send["id"], "Yes, send it")
    await tools.click(None, send["id"])  # now allowed, exactly once
    with pytest.raises(ToolError):
        await tools.click(None, send["id"])  # approval was used up


# ----------------------------------------------------------------------
# Approval hardening (from the live WhatsApp test)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    ["yes", "Yeah, send it", "go ahead", "okay", "Avunu", "haan, pampu"],
)
def test_clear_approvals(reply: str) -> None:
    from tools import is_clear_approval

    assert is_clear_approval(reply)


@pytest.mark.parametrize(
    "reply",
    ["Est-ce que", "", "the step", "no", "wait", "don't send it", "not sure", "hmm"],
)
def test_unclear_or_negative_replies_are_not_approval(reply: str) -> None:
    from tools import is_clear_approval

    assert not is_clear_approval(reply)


@pytest.mark.asyncio
async def test_garbled_reply_does_not_approve(chat_browser) -> None:
    from livekit.agents.llm import ToolError

    tools = BrowserTools(chat_browser)
    send = _find((await chat_browser.inspect_page())["elements"], "Send")

    with pytest.raises(ToolError, match="not a clear yes"):
        await tools.confirm_browser_action(None, send["id"], "Est-ce que")
    with pytest.raises(ToolError):
        await tools.click(None, send["id"])


@pytest.mark.asyncio
async def test_unused_approval_does_not_carry_over_to_a_new_message(
    chat_browser,
) -> None:
    """The live bug: approval for message 1 was silently reused for message 2."""
    from livekit.agents.llm import ToolError

    tools = BrowserTools(chat_browser)
    elements = (await chat_browser.inspect_page())["elements"]
    send = _find(elements, "Send")
    composer = _find(elements, "Type a message")

    await tools.confirm_browser_action(None, send["id"], "yes")
    # Approval never used; then a different message is typed.
    await tools.type_text(None, composer["id"], "A different message")

    with pytest.raises(ToolError, match="not approved"):
        await tools.click(None, send["id"])


@pytest.mark.asyncio
async def test_approval_expires(chat_browser, monkeypatch) -> None:
    from livekit.agents.llm import ToolError

    import tools as tools_module

    tools = BrowserTools(chat_browser)
    send = _find((await chat_browser.inspect_page())["elements"], "Send")
    await tools.confirm_browser_action(None, send["id"], "yes")

    real_monotonic = tools_module.time.monotonic
    monkeypatch.setattr(
        tools_module.time,
        "monotonic",
        lambda: real_monotonic() + tools_module.APPROVAL_TTL_SECONDS + 1,
    )
    with pytest.raises(ToolError):
        await tools.click(None, send["id"])


@pytest.mark.asyncio
async def test_opening_a_url_never_discards_an_unsent_draft(tab_browser) -> None:
    manager, base = tab_browser
    await manager.open_url(f"{base}/draft")
    await manager.type_text("Message", "Unsent draft")

    result = await manager.open_url(f"{base}/one")

    assert result["opened_in_new_tab"] is True
    tabs = (await manager.list_tabs())["tabs"]
    assert [tab["title"] for tab in tabs] == ["Draft Page", "Page One"]


@pytest.mark.asyncio
async def test_web_search_opens_in_a_new_tab(monkeypatch) -> None:
    manager = BrowserManager(headless=True)
    calls: list[str] = []

    async def fake_open_tab(url=None):
        calls.append(url)
        return {"number": 2, "url": url, "title": "Results"}

    async def fail_open_url(url):
        raise AssertionError("search must not replace the current page")

    monkeypatch.setattr(manager, "open_tab", fake_open_tab)
    monkeypatch.setattr(manager, "open_url", fail_open_url)

    await BrowserTools(manager).search_the_web(None, "weather")
    assert calls and "duckduckgo.com" in calls[0]


@pytest.mark.asyncio
async def test_switching_chats_cancels_an_earlier_approval(chat_browser) -> None:
    from livekit.agents.llm import ToolError

    tools = BrowserTools(chat_browser)
    elements = (await chat_browser.inspect_page())["elements"]
    other_chat = _find(elements, "Team Group")

    await tools.confirm_browser_action(None, "Send", "yes")
    await tools.click(None, other_chat["id"])  # user's approval was for another chat

    with pytest.raises(ToolError, match="not approved"):
        await tools.click(None, "Send")


# ----------------------------------------------------------------------
# Closing the whole window; guarding against invented selectors
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_browser_closes_every_tab(tab_browser) -> None:
    manager, base = tab_browser
    await manager.open_url(f"{base}/one")
    await manager.open_tab(f"{base}/two")

    result = await manager.close_browser()

    assert result == {"closed": True, "tabs_closed": 2}
    assert manager._context is None
    # Reopens on demand.
    assert (await manager.open_url(f"{base}/one"))["title"] == "Page One"


@pytest.mark.asyncio
async def test_close_browser_asks_first_when_a_tab_has_a_draft(tab_browser) -> None:
    manager, base = tab_browser
    await manager.open_url(f"{base}/draft")
    await manager.type_text("Message", "Unsent")
    await manager.open_tab(f"{base}/one")

    first = await manager.close_browser()
    assert first["closed"] is False
    assert first["tabs_with_unsent_text"] == ["Draft Page"]

    second = await manager.close_browser(confirmed=True)
    assert second["closed"] is True


@pytest.mark.asyncio
async def test_css_selectors_are_rejected_with_guidance(chat_browser) -> None:
    await chat_browser.inspect_page()

    with pytest.raises(BrowserError, match="CSS selectors are not supported"):
        await chat_browser.click('button[aria-label="Send"]')
    with pytest.raises(BrowserError, match="CSS selectors are not supported"):
        await chat_browser.type_text('input[role="textbox"]', "hello")


@pytest.mark.asyncio
async def test_type_text_reports_which_field_was_filled(chat_browser) -> None:
    elements = (await chat_browser.inspect_page())["elements"]
    composer = _find(elements, "Type a message")

    result = await chat_browser.type_text(composer["id"], "hi")

    assert result["typed_into"] == "Type a message"


@pytest.mark.asyncio
async def test_approval_uses_the_real_transcript_not_the_models_retelling(
    chat_browser,
) -> None:
    from types import SimpleNamespace

    from livekit.agents.llm import ToolError

    def context_with_last_user_words(text: str):
        message = SimpleNamespace(type="message", role="user", text_content=text)
        history = SimpleNamespace(items=[message])
        return SimpleNamespace(session=SimpleNamespace(history=history))

    tools = BrowserTools(chat_browser)
    send = _find((await chat_browser.inspect_page())["elements"], "Send")

    # The user actually said "research"; the model claims they said "yes".
    with pytest.raises(ToolError, match="not a clear yes"):
        await tools.confirm_browser_action(
            context_with_last_user_words("research"), send["id"], "yes"
        )

    # The user actually said "Yes, send it."
    await tools.confirm_browser_action(
        context_with_last_user_words("Yes, send it."), send["id"], "yes and then"
    )
    await tools.click(None, send["id"])


def test_close_browser_tool_is_registered() -> None:
    ids = [tool.id for tool in BrowserTools(BrowserManager(headless=True)).tools]
    assert "close_browser" in ids
