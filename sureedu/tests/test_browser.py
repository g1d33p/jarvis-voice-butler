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
        assert any(line.endswith(": Search") for line in elements)
        assert any(line.endswith(": Go") for line in elements)

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
    """Find an inspected element line such as "#12 button: Send"."""
    line = next(e for e in elements if name_part in e.split(": ", 1)[1])
    element_id, rest = line.split(" ", 1)
    return {"id": element_id, "name": rest.split(": ", 1)[1], "line": line}


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


def test_close_browser_tool_is_registered() -> None:
    ids = [tool.id for tool in BrowserTools(BrowserManager(headless=True)).tools]
    assert "close_browser" in ids


# ----------------------------------------------------------------------
# Enter key: the live bug where "type Hi, press Enter" sent without approval
# ----------------------------------------------------------------------

_ENTER_PAGE = b"""
<html><head><title>Enter test</title></head><body>
  <input type="search" aria-label="Search or start a new chat">
  <div contenteditable="true" aria-label="Type a message"></div>
  <button aria-label="Send">&gt;</button>
  <button aria-label="Emoji">:)</button>
</body></html>
"""


class _EnterPageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(_ENTER_PAGE)))
        self.end_headers()
        self.wfile.write(_ENTER_PAGE)

    def log_message(self, message_format: str, *args: object) -> None:
        return


@pytest.fixture
async def enter_browser(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EnterPageHandler)
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


def _yes_context():
    from types import SimpleNamespace

    message = SimpleNamespace(type="message", role="user", text_content="Yes, send it")
    return SimpleNamespace(
        session=SimpleNamespace(history=SimpleNamespace(items=[message]))
    )


@pytest.mark.asyncio
async def test_enter_in_search_box_is_free(enter_browser) -> None:
    tools = BrowserTools(enter_browser)
    search = _find((await enter_browser.inspect_page())["elements"], "Search")
    await tools.type_text(None, search["id"], "Priya")

    await tools.press_key(None, "Enter")  # no approval needed


@pytest.mark.asyncio
async def test_enter_on_focused_send_button_needs_approval(enter_browser) -> None:
    from livekit.agents.llm import ToolError

    tools = BrowserTools(enter_browser)
    page = await enter_browser._get_page()
    await page.focus("button[aria-label='Send']")

    with pytest.raises(ToolError, match="needs the user's approval"):
        await tools.press_key(None, "Enter")


@pytest.mark.asyncio
async def test_enter_on_harmless_button_is_free(enter_browser) -> None:
    tools = BrowserTools(enter_browser)
    page = await enter_browser._get_page()
    await page.focus("button[aria-label='Emoji']")

    await tools.press_key(None, "Enter")


# ----------------------------------------------------------------------
# Context-aware sending: simple dictated messages go straight out
# ----------------------------------------------------------------------


def _said(text: str, item_id: str = "u1"):
    from types import SimpleNamespace

    message = SimpleNamespace(
        type="message", role="user", text_content=text, id=item_id
    )
    return SimpleNamespace(
        session=SimpleNamespace(history=SimpleNamespace(items=[message]))
    )


@pytest.mark.asyncio
async def test_dictated_hi_is_sent_without_asking(chat_browser) -> None:
    tools = BrowserTools(chat_browser)
    elements = (await chat_browser.inspect_page())["elements"]
    composer = _find(elements, "Type a message")
    send = _find(elements, "Send")
    context = _said("Send hi to the guy in the first chat")

    await tools.type_text(context, composer["id"], "Hi")
    await tools.click(context, send["id"])  # no approval question needed


@pytest.mark.asyncio
async def test_one_instruction_sends_at_most_one_message(chat_browser) -> None:
    from livekit.agents.llm import ToolError

    tools = BrowserTools(chat_browser)
    elements = (await chat_browser.inspect_page())["elements"]
    composer = _find(elements, "Type a message")
    send = _find(elements, "Send")
    context = _said("send hi")

    await tools.type_text(context, composer["id"], "Hi")
    await tools.click(context, send["id"])
    await tools.type_text(context, composer["id"], "Hi")
    with pytest.raises(ToolError, match="needs the user's approval"):
        await tools.click(context, send["id"])


@pytest.mark.asyncio
async def test_composed_message_still_needs_approval(chat_browser) -> None:
    from livekit.agents.llm import ToolError

    tools = BrowserTools(chat_browser)
    elements = (await chat_browser.inspect_page())["elements"]
    composer = _find(elements, "Type a message")
    send = _find(elements, "Send")
    context = _said("send him a friendly greeting")

    await tools.type_text(context, composer["id"], "Hey! Hope you're doing well")
    with pytest.raises(ToolError, match="needs the user's approval"):
        await tools.click(context, send["id"])


@pytest.mark.asyncio
async def test_enter_follows_the_same_policy(chat_browser) -> None:
    from livekit.agents.llm import ToolError

    tools = BrowserTools(chat_browser)
    composer = _find((await chat_browser.inspect_page())["elements"], "Type a message")

    # Sensitive text: Enter is blocked.
    await tools.type_text(
        _said("send code 482913", "u1"), composer["id"], "code 482913"
    )
    with pytest.raises(ToolError, match="needs the user's approval"):
        await tools.press_key(_said("send code 482913", "u1"), "Enter")

    # Simple dictated text: Enter goes through.
    await tools.type_text(_said("send hello", "u2"), composer["id"], "hello")
    await tools.press_key(_said("send hello", "u2"), "Enter")


# ----------------------------------------------------------------------
# Approval flow: one question, and the yes performs the action
# (replays of the live test on 24 Sep 2026)
# ----------------------------------------------------------------------


async def _typed(tools, browser, text, context=None):
    composer = _find((await browser.inspect_page())["elements"], "Type a message")
    await tools.type_text(context, composer["id"], text)
    return composer


async def _send_id(browser):
    page = await browser._get_page()
    return await page.evaluate(
        "() => document.querySelector('[aria-label=\"Send\"]')"
        ".getAttribute('data-sureedu-id')"
    )


async def test_yes_performs_the_waiting_send(chat_browser) -> None:
    from livekit.agents.llm import ToolError

    tools = BrowserTools(chat_browser)
    await _typed(tools, chat_browser, "Hope the meeting went well")
    send = "#" + await _send_id(chat_browser)

    with pytest.raises(ToolError, match="needs the user's approval"):
        await tools.click(_said("send it to him", "u1"), send)

    result = await tools.confirm_browser_action(_said("Yes.", "u2"), "yes")

    assert result["done"] is True
    assert result["sent"] == "Hope the meeting went well"
    # Nothing is left approved afterwards.
    with pytest.raises(ToolError, match="Nothing is waiting"):
        await tools.confirm_browser_action(_said("yes", "u3"), "yes")


async def test_send_command_counts_as_yes_while_a_message_waits(chat_browser) -> None:
    """Live run: 'What are you waiting for? Send a message.' was rejected."""
    from livekit.agents.llm import ToolError

    tools = BrowserTools(chat_browser)
    await _typed(tools, chat_browser, "Good morning! Hope you have a brilliant day.")
    with pytest.raises(ToolError):
        await tools.press_key(_said("type it", "u1"), "Enter")

    result = await tools.confirm_browser_action(
        _said("What are you waiting for? Send a message.", "u2"), "send it already"
    )
    assert result["done"] is True


async def test_garbled_or_negative_replies_do_not_send(chat_browser) -> None:
    from livekit.agents.llm import ToolError

    tools = BrowserTools(chat_browser)
    await _typed(tools, chat_browser, "Call me later")
    with pytest.raises(ToolError):
        await tools.press_key(_said("type call me later", "u1"), "Enter")

    for reply in ("Est-ce que", "ready", "don't send that"):
        with pytest.raises(ToolError):
            await tools.confirm_browser_action(_said(reply, "u2"), "yes")


async def test_message_edited_after_the_question_is_not_sent(chat_browser) -> None:
    from livekit.agents.llm import ToolError

    tools = BrowserTools(chat_browser)
    await _typed(tools, chat_browser, "See you at six")
    with pytest.raises(ToolError):
        await tools.press_key(_said("type see you at six", "u1"), "Enter")

    # The text changes behind the user's back (typing clears the pending send).
    await _typed(tools, chat_browser, "See you at seven")
    with pytest.raises(ToolError, match="Nothing is waiting"):
        await tools.confirm_browser_action(_said("yes", "u2"), "yes")


async def test_switching_chats_cancels_a_waiting_send(chat_browser) -> None:
    from livekit.agents.llm import ToolError

    tools = BrowserTools(chat_browser)
    await _typed(tools, chat_browser, "Running late, sorry")
    with pytest.raises(ToolError):
        await tools.press_key(_said("type running late", "u1"), "Enter")

    other_chat = _find((await chat_browser.inspect_page())["elements"], "Team Group")
    await tools.click(None, other_chat["id"])

    with pytest.raises(ToolError, match="Nothing is waiting"):
        await tools.confirm_browser_action(_said("yes", "u2"), "yes")


async def test_waiting_send_expires(chat_browser, monkeypatch) -> None:
    from livekit.agents.llm import ToolError

    import tools as tools_module

    tools = BrowserTools(chat_browser)
    await _typed(tools, chat_browser, "Call me")
    with pytest.raises(ToolError):
        await tools.press_key(_said("type call me", "u1"), "Enter")

    real = tools_module.time.monotonic
    monkeypatch.setattr(
        tools_module.time,
        "monotonic",
        lambda: real() + tools_module.APPROVAL_TTL_SECONDS + 1,
    )
    with pytest.raises(ToolError, match="Nothing is waiting"):
        await tools.confirm_browser_action(_said("yes", "u2"), "yes")


async def test_approval_judges_the_real_transcript(chat_browser) -> None:
    """The user said 'research'; the model claims they said 'yes'."""
    from livekit.agents.llm import ToolError

    tools = BrowserTools(chat_browser)
    await _typed(tools, chat_browser, "Meeting moved")
    with pytest.raises(ToolError):
        await tools.press_key(_said("type meeting moved", "u1"), "Enter")

    with pytest.raises(ToolError, match="not a clear yes"):
        await tools.confirm_browser_action(_said("research", "u2"), "yes")


async def test_click_reports_what_was_clicked(chat_browser) -> None:
    tools = BrowserTools(chat_browser)
    team = _find((await chat_browser.inspect_page())["elements"], "Team Group")

    result = await tools.click(None, team["id"])

    assert "Team Group" in result["clicked"]


@pytest.mark.parametrize(
    ("current", "new", "replace"),
    [
        ("about:blank", "https://web.whatsapp.com/", True),
        ("https://web.whatsapp.com/", "https://www.google.com/search?q=pizza", False),
        ("https://duckduckgo.com/?q=weather", "https://www.youtube.com/", True),
        ("https://www.youtube.com/watch?v=1", "https://youtube.com/", True),
        ("https://outlook.live.com/mail/", "https://www.amazon.com/", False),
    ],
)
def test_pages_in_use_are_not_replaced(current: str, new: str, replace: bool) -> None:
    assert browser_module._may_replace(current, new) is replace


def test_elements_are_compact_lines_without_duplicates() -> None:
    raw = [
        {"id": "#1", "tag": "button", "role": "", "name": "Send", "type": ""},
        {
            "id": "#2",
            "tag": "div",
            "role": "textbox",
            "name": "Type a message",
            "type": "editable",
        },
        {"id": "#3", "tag": "button", "role": "", "name": "Send", "type": ""},
        {"id": "#4", "tag": "a", "role": "", "name": "", "type": ""},
    ]

    assert browser_module.compact_elements(raw) == [
        "#1 button: Send",
        "#2 textbox, editable: Type a message",
        "#4 a: (unnamed)",
    ]


async def test_snapshot_never_starts_the_browser() -> None:
    manager = BrowserManager(headless=True)

    assert await manager.snapshot() is None
    assert manager._context is None


async def test_snapshot_describes_tabs(tab_browser) -> None:
    manager, base = tab_browser
    await manager.open_url(f"{base}/one")
    await manager.open_tab(f"{base}/two")

    snap = await manager.snapshot()

    assert snap["tab_count"] == 2
    assert snap["active_tab"]["title"] == "Page Two"


# ----------------------------------------------------------------------
# Replays of the Phase 2 live test (24 Sep 2026, 02:58)
# ----------------------------------------------------------------------


def _conversation(*turns, age_seconds: float = 0):
    """Build a fake session history from (role, text) pairs, oldest first."""
    import time
    from types import SimpleNamespace

    items = [
        SimpleNamespace(
            type="message",
            role=role,
            text_content=text,
            id=f"m{i}",
            created_at=time.time() - age_seconds,
        )
        for i, (role, text) in enumerate(turns)
    ]
    return SimpleNamespace(
        session=SimpleNamespace(history=SimpleNamespace(items=items))
    )


async def test_two_tools_at_once_launch_the_browser_only_once(tmp_path) -> None:
    """Live bug: open_url and open_tab together crashed ("profile already in use")."""
    import asyncio

    server = ThreadingHTTPServer(("127.0.0.1", 0), _TabPageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manager = BrowserManager(headless=True, profile_dir=tmp_path / "profile")
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        await asyncio.gather(
            manager.open_url(f"{base}/one"), manager.open_tab(f"{base}/two")
        )
        assert (await manager.list_tabs())["tab_count"] >= 2
    finally:
        await manager.close()
        server.shutdown()
        thread.join(timeout=2)


async def test_proposed_draft_plus_that_works_sends_without_asking(
    chat_browser,
) -> None:
    """Live: 'How about: "Rise and shine! ..."' -> 'Oh, that works, indeed.'"""
    draft = "Rise and shine! Wishing you a peaceful and productive day."
    context = _conversation(
        ("user", "Suggest a soothing good morning message."),
        ("assistant", f'How about: "{draft}" Too cliche?'),
        ("user", "Oh, that works, indeed."),
    )
    tools = BrowserTools(chat_browser)
    await _typed(tools, chat_browser, draft)

    result = await tools.press_key(context, "Enter")

    assert result["sent"] is True


async def test_draft_approval_does_not_cover_different_text(chat_browser) -> None:
    from livekit.agents.llm import ToolError

    context = _conversation(
        ("assistant", 'How about: "Good morning!"?'),
        ("user", "Yes."),
    )
    tools = BrowserTools(chat_browser)
    await _typed(tools, chat_browser, "Good morning! Also, I quit.")

    with pytest.raises(ToolError, match="needs the user's approval"):
        await tools.press_key(context, "Enter")


async def test_old_draft_approval_expires(chat_browser) -> None:
    from livekit.agents.llm import ToolError

    context = _conversation(
        ("assistant", 'How about: "Morning all"?'),
        ("user", "Sure."),
        ("user", "Open the other chat."),
        age_seconds=600,
    )
    tools = BrowserTools(chat_browser)
    await _typed(tools, chat_browser, "Morning all")

    with pytest.raises(ToolError, match="needs the user's approval"):
        await tools.press_key(context, "Enter")


async def test_yes_to_a_send_question_counts_once(chat_browser) -> None:
    """Live: 'Shall I try sending it again?' -> 'Yes, please.' then Enter."""
    from livekit.agents.llm import ToolError

    context = _conversation(
        ("assistant", "It still looks like a draft. Shall I try sending it again?"),
        ("user", "Yes, please."),
    )
    tools = BrowserTools(chat_browser)
    await _typed(tools, chat_browser, "Call you later")

    assert (await tools.press_key(context, "Enter"))["sent"] is True

    await _typed(tools, chat_browser, "Call you later")
    with pytest.raises(ToolError, match="needs the user's approval"):
        await tools.press_key(context, "Enter")  # the same yes is used up


async def test_yes_to_a_question_about_a_different_message_does_not_count(
    chat_browser,
) -> None:
    from livekit.agents.llm import ToolError

    context = _conversation(
        ("assistant", "Shall I send 'See you at six'?"),
        ("user", "Yes."),
    )
    tools = BrowserTools(chat_browser)
    await _typed(tools, chat_browser, "See you at seven")

    with pytest.raises(ToolError, match="needs the user's approval"):
        await tools.press_key(context, "Enter")


async def test_unclear_reply_keeps_the_send_waiting(chat_browser) -> None:
    """Live: echo "Very well." wiped the pending send, forcing a third question."""
    from livekit.agents.llm import ToolError

    tools = BrowserTools(chat_browser)
    await _typed(tools, chat_browser, "Running late")
    with pytest.raises(ToolError):
        await tools.press_key(_said("type running late", "u1"), "Enter")

    with pytest.raises(ToolError, match="still waiting"):
        await tools.confirm_browser_action(_said("Very well.", "u2"), "very well")

    result = await tools.confirm_browser_action(_said("Yes.", "u3"), "yes")
    assert result["done"] is True


async def test_no_cancels_the_waiting_send(chat_browser) -> None:
    from livekit.agents.llm import ToolError

    tools = BrowserTools(chat_browser)
    await _typed(tools, chat_browser, "Running late")
    with pytest.raises(ToolError):
        await tools.press_key(_said("type running late", "u1"), "Enter")

    with pytest.raises(ToolError, match="said no"):
        await tools.confirm_browser_action(_said("No, don't.", "u2"), "no")
    with pytest.raises(ToolError, match="Nothing is waiting"):
        await tools.confirm_browser_action(_said("yes", "u3"), "yes")


async def test_enter_in_search_box_says_nothing_was_sent(chat_browser) -> None:
    """Live: Enter in the search box was reported as 'message sent'."""
    tools = BrowserTools(chat_browser)
    page = await chat_browser._get_page()
    await page.set_content(
        "<input aria-label='Search or start a new chat'>"
        "<div contenteditable='true' aria-label='Type a message'></div>"
    )
    search = _find((await chat_browser.inspect_page())["elements"], "Search")

    typed = await tools.type_text(None, search["id"], "Rise and shine")
    assert "search box" in typed["warning"]

    result = await tools.press_key(None, "Enter")
    assert result["sent"] is False
    assert "Nothing was sent" in result["effect"]


def test_page_screenshot_tool_is_gone() -> None:
    ids = [tool.id for tool in BrowserTools(BrowserManager(headless=True)).tools]
    assert "take_screenshot" not in ids  # it never saved a file; capture_screen does
    assert "approve_draft" not in ids
