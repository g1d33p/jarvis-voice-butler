"""2026-09-25: WhatsApp Web only opens a chat on a real (trusted) mouse click."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_whatsapp import FakeBrowser, _chat

from browser import BrowserManager
from whatsapp import WhatsAppClient


class _MouseBrowser(FakeBrowser):
    """FakeBrowser whose rows report a position, and which records mouse clicks."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.mouse_clicks = []

    async def evaluate(self, script):
        result = await super().evaluate(script)
        if "return waClickChat(document" in script and result.get("opened"):
            result = {**result, "x": 120.0, "y": 340.0}
        return result

    async def click_at(self, x, y):
        self.mouse_clicks.append((x, y))


async def test_opening_a_chat_uses_the_real_mouse() -> None:
    browser = _MouseBrowser(chats=[_chat("SC1-Executives", unread=2)])
    client = WhatsAppClient(browser=browser)

    await client.read_messages("SC1-Executives", 5)

    assert browser.mouse_clicks == [(120.0, 340.0)]


_PAGE = b"""<html><body>
<div id=row style="width:300px;height:60px;background:#eee">SC1</div>
<div id=status>closed</div>
<script>
document.getElementById('row').addEventListener('click', e => {
  // Like WhatsApp Web now: ignore clicks that did not come from a real mouse.
  if (e.isTrusted) document.getElementById('status').textContent = 'open';
});
</script></body></html>"""


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(_PAGE)

    def log_message(self, *args):
        return


@pytest.fixture
async def trusted_page(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    browser = BrowserManager(headless=True, profile_dir=tmp_path / "profile")
    try:
        await browser.open_url(f"http://127.0.0.1:{server.server_port}/")
        yield browser
    finally:
        await browser.close()
        server.shutdown()
        thread.join(timeout=2)


async def test_script_click_is_ignored_but_click_at_opens(trusted_page) -> None:
    browser = trusted_page
    await browser.evaluate("() => document.getElementById('row').click()")
    assert (
        await browser.evaluate("() => document.getElementById('status').textContent")
        == "closed"
    )

    box = await browser.evaluate(
        "() => { const r = document.getElementById('row').getBoundingClientRect();"
        " return {x: r.left + r.width / 2, y: r.top + r.height / 2}; }"
    )
    await browser.click_at(box["x"], box["y"])
    assert (
        await browser.evaluate("() => document.getElementById('status').textContent")
        == "open"
    )
