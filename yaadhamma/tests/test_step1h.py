"""Step 1h (2026-09-27): search to find deep chats, emoji-safe headers."""

import threading

from test_whatsapp import FakeBrowser, _chat

from whatsapp import WhatsAppClient, _norm_chat_name, find_chats


class _SearchBrowser(FakeBrowser):
    """A fake whose chat list only shows the top chats; search finds the rest."""

    def __init__(self, top, everything, **kwargs):
        super().__init__(chats=top, **kwargs)
        self.everything = everything
        self.query = ""
        self.events = []

    async def click_at(self, x, y):
        self.events.append(("click", x, y))

    async def insert_text(self, text):
        self.events.append(("type", text))
        self.query = text

    async def clear_focused_field(self):
        self.events.append(("clear",))
        self.query = ""

    async def evaluate(self, script):
        if "return waSearchBox(document" in script:
            return {"found": True, "hasText": bool(self.query), "x": 50.0, "y": 20.0}
        if "return waListChats(document" in script and self.query:
            return {"chats": find_chats(self.query, self.everything)}
        if "return waScrollChats(document" in script:
            self.events.append(("scroll",))
        return await super().evaluate(script)


async def test_search_finds_a_chat_far_down_the_list() -> None:
    """Live: '+1 (940) 843-8446 (You)' was never reached by scrolling."""
    deep = _chat("+1 (940) 843-8446 (You)")
    browser = _SearchBrowser(
        top=[_chat("SC1-Executives"), _chat("Family")],
        everything=[_chat("SC1-Executives"), _chat("Family"), deep],
    )
    client = WhatsAppClient(browser=browser)

    found = await client.find_chat("9408438446")

    assert found == "+1 (940) 843-8446 (You)"
    assert ("type", "9408438446") in browser.events
    assert ("scroll",) not in browser.events  # no slow scrolling needed


async def test_listing_chats_clears_a_leftover_search_first() -> None:
    browser = _SearchBrowser(top=[_chat("A"), _chat("B")], everything=[])
    browser.query = "old search"
    client = WhatsAppClient(browser=browser)

    chats = await client.list_all_chats()

    assert ("clear",) in browser.events
    assert {c["name"] for c in chats} == {"A", "B"}


def test_header_check_ignores_emoji() -> None:
    """Live: '7k group💵🤑💵' in the list showed as '7k group' in the header."""
    assert _norm_chat_name("7k group💵🤑💵") == _norm_chat_name("7k group")
    assert _norm_chat_name("SC1-Executives") != _norm_chat_name("SC1-Security")


def test_own_chat_matches_by_its_number() -> None:
    chats = [_chat("+1 (940) 843-8446 (You)"), _chat("+1 (940) 364-0413")]
    assert [c["name"] for c in find_chats("8446", chats)] == ["+1 (940) 843-8446 (You)"]


async def test_active_app_check_runs_off_the_voice_loop() -> None:
    from observation import observe

    seen = []

    def slow_app():
        seen.append(threading.current_thread())
        return {"app": "Finder", "window_title": ""}

    class NoBrowser:
        async def snapshot(self):
            return None

    await observe(NoBrowser(), read_app=slow_app)
    assert seen and seen[0] is not threading.main_thread()
