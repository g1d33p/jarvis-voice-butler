"""Step 1g (2026-09-27): who sent what, spoken chat names, trusted sending."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_whatsapp import FakeBrowser, _chat

from browser import BrowserManager
from whatsapp import WhatsAppClient, _js_call, find_chats

# ----------------------------------------------------------------- names


def test_spoken_chat_names_match_the_written_ones() -> None:
    chats = [
        {"name": "SC1-Confidants"},
        {"name": "SC1-Organization1"},
        {"name": "SC1-Executives"},
    ]
    for spoken in ("SC1 organization one", "sc1 organization 1", "SC1-Organization1"):
        assert [c["name"] for c in find_chats(spoken, chats)] == ["SC1-Organization1"]
    # A bare "SC1" is still ambiguous and must not guess.
    assert len(find_chats("SC1", chats)) == 3


# ----------------------------------------------------------------- direction

_CHAT = b"""<html><body style="margin:0">
<div id="main" style="width:800px">
 <div data-testid="conversation-panel-messages" style="width:800px">
  <div style="display:flex;justify-content:flex-start"><div style="width:300px"
    data-pre-plain-text="[10:30, 27/09/2026] +1 940: "><span class="copyable-text">Nammochu antav</span></div></div>
  <div style="display:flex;justify-content:flex-end"><div style="width:300px"
    data-pre-plain-text="[10:31, 27/09/2026] Jeevan: "><span class="copyable-text">yeahhhhhhh</span></div></div>
 </div>
</div></body></html>"""


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(_CHAT)

    def log_message(self, *args):
        return


@pytest.fixture
async def chat_page(tmp_path):
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


async def test_direction_follows_bubble_position(chat_page) -> None:
    """Live 2026-09-27: the other person's message was read as Jeevan's."""
    result = await chat_page.evaluate(_js_call("waReadMessages", 10))
    by_text = {m["text"]: m["outgoing"] for m in result["messages"]}
    assert by_text == {"Nammochu antav": False, "yeahhhhhhh": True}


# ----------------------------------------------------------------- sending


class _KeyboardBrowser(FakeBrowser):
    """A fake with a real mouse and keyboard, recording what happened."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.events = []
        self._sent = None

    async def click_at(self, x, y):
        self.events.append(("click", x, y))

    async def insert_text(self, text):
        self.events.append(("type", text))
        self._sent = text

    async def press_key(self, key):
        self.events.append(("key", key))
        return {"key": key}

    async def evaluate(self, script):
        if "return waComposerBox(document" in script:
            return {"found": True, "empty": True, "x": 400.0, "y": 700.0}
        if "return waLastMessage(document" in script:
            if self._sent and ("key", "Enter") in self.events:
                return {
                    "message": {
                        "meta": "[m] Jeevan: ",
                        "text": self._sent,
                        "outgoing": True,
                    }
                }
            return {"message": {"meta": "[m] Ravi: ", "text": "old", "outgoing": False}}
        return await super().evaluate(script)


async def test_sending_uses_real_mouse_keyboard_and_enter() -> None:
    browser = _KeyboardBrowser(chats=[_chat("Me (You)")])
    client = WhatsAppClient(browser=browser)

    result = await client.send_message("Me (You)", "test one")

    assert result["sent"] is True
    kinds = [e[0] for e in browser.events]
    assert kinds[-3:] == ["click", "type", "key"]  # focus box, type, Enter
    assert ("type", "test one") in browser.events


async def test_existing_draft_is_never_appended_to() -> None:
    from whatsapp import WhatsAppError

    class _Drafty(_KeyboardBrowser):
        async def evaluate(self, script):
            if "return waComposerBox(document" in script:
                return {"found": True, "empty": False, "x": 1.0, "y": 1.0}
            return await super().evaluate(script)

    browser = _Drafty(chats=[_chat("Ravi")])
    with pytest.raises(WhatsAppError, match="already contains a draft"):
        await WhatsAppClient(browser=browser).send_message("Ravi", "hi")
    assert not any(e[0] == "type" for e in browser.events)


def test_prompts_keep_full_chat_names() -> None:
    from prompts import ORCHESTRATOR_INSTRUCTIONS, VOICE_INSTRUCTIONS

    assert '"SC1 organization one" stays' in VOICE_INSTRUCTIONS
    assert "never for answering which chat" in ORCHESTRATOR_INSTRUCTIONS
