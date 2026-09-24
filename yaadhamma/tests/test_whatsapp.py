"""Tests for the WhatsApp Web client (browser layer is faked)."""

import re

import pytest

from whatsapp import (
    WhatsAppClient,
    WhatsAppError,
    WhatsAppNotPairedError,
    find_chats,
    parse_message_meta,
)


def _chat(name, unread=0, preview="", time=""):
    return {"name": name, "unread": unread, "preview": preview, "time": time}


_CLICK_ARG = re.compile(r"waClickChat\(document,\s*\"((?:[^\"\\]|\\.)*)\"\)")


def _script_arg(script):
    """The chat-name argument passed to waClickChat in an evaluate script."""
    m = _CLICK_ARG.search(script)
    return m.group(1) if m else ""


def _script_clicked_name(browser):
    for script in reversed(browser.scripts):
        if "return waClickChat(document" in script:
            return _script_arg(script)
    return ""


class FakeBrowser:
    """Stands in for BrowserManager: canned evaluate() results per extractor."""

    def __init__(
        self, *, login="logged_in", chats=(), tab_url="https://web.whatsapp.com/"
    ):
        self.login = login
        self.chats = list(chats)
        self.tabs = [
            {
                "number": 1,
                "title": "WhatsApp",
                "url": tab_url,
                "active": True,
            }
        ]
        self.scripts = []
        self.pressed = []
        self.last_message_calls = 0
        self.type_results = {"typed": True, "clickedSend": True}
        self.sent_texts = []
        self.click_calls = 0
        # Conversation header: None means the header follows whatever chat
        # was clicked (the healthy case); a test can pin it to simulate a
        # stale pane showing the wrong chat.
        self.conversation_title = None
        # waReadMessages canned rounds: a list of result dicts consumed one
        # per call, so a test can simulate slow-loading message panes.
        self.read_rounds = None
        # waScrollMessagesUp message pages: a list of message lists; each
        # scroll advances to the next page, simulating older history loading.
        self.message_pages = None
        self._scrolls = 0

    async def list_tabs(self):
        return {"tab_count": len(self.tabs), "tabs": self.tabs}

    async def switch_tab(self, number):
        for t in self.tabs:
            t["active"] = t["number"] == number
        return {"number": number}

    async def open_tab(self, url):
        number = len(self.tabs) + 1
        self.tabs.append({"number": number, "title": "new", "url": url, "active": True})
        return {"number": number, "url": url}

    async def press_key(self, key):
        self.pressed.append(key)
        return {"key": key, "url": ""}

    async def evaluate(self, script):
        self.scripts.append(script)
        if "return waLoginState(document" in script:
            # A list of states simulates a page that is still loading and
            # then resolves (the last state sticks).
            if isinstance(self.login, list):
                state = self.login[0]
                if len(self.login) > 1:
                    self.login.pop(0)
                return {"state": state}
            return {"state": self.login}
        if "return waListChats(document" in script:
            return {"chats": self.chats}
        if "return waScrollTop(document" in script:
            return {"ok": True}
        if "return waScrollChats(document" in script:
            return {"before": len(self.chats)}
        if "return waClickChat(document" in script:
            self.click_calls += 1
            name = _script_arg(script)
            return {"opened": True, "matched": name or "x"}
        if "return waConversationTitle(document" in script:
            return {"title": self.conversation_title or _script_clicked_name(self)}
        if "return waReadMessages(document" in script:
            if self.message_pages is not None:
                idx = min(self._scrolls, len(self.message_pages) - 1)
                return {"messages": self.message_pages[idx], "empty": False}
            if self.read_rounds is not None:
                idx = min(self._read_calls(), len(self.read_rounds) - 1)
                return self.read_rounds[idx]
            return {
                "messages": [
                    {
                        "meta": "[10:30, 24/09/2026] Ravi Kumar: ",
                        "text": "Are we still on for 6?",
                        "outgoing": False,
                    },
                    {
                        "meta": "[10:32, 24/09/2026] Jeevan: ",
                        "text": "Yes, see you then",
                        "outgoing": True,
                    },
                ]
            }
        if "return waScrollMessagesUp(document" in script:
            if self.message_pages is not None:
                self._scrolls += 1
                advanced = self._scrolls < len(self.message_pages)
                return {"ok": True, "advanced": advanced, "atTop": not advanced}
            return {"ok": True, "advanced": False, "atTop": True}
        if "return waTypeAndSend(document" in script:
            return dict(self.type_results)
        if "return waLastMessage(document" in script:
            self.last_message_calls += 1
            return {"message": self._last_message()}
        raise AssertionError(f"unexpected evaluate script: {script[:60]}")

    def _read_calls(self):
        return sum(1 for s in self.scripts if "return waReadMessages(document" in s)

    def _last_message(self):
        # Overridden per-test via last_message_sequence.
        seq = getattr(self, "last_message_sequence", None)
        if seq is not None:
            idx = min(self.last_message_calls - 1, len(seq) - 1)
            return seq[idx]
        return None


def _client(**kwargs):
    return WhatsAppClient(browser=FakeBrowser(**kwargs))


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_parse_message_meta_extracts_sender_and_time() -> None:
    time, sender = parse_message_meta("[10:30, 24/09/2026] Ravi Kumar: ")
    assert time == "10:30, 24/09/2026"
    assert sender == "Ravi Kumar"


def test_parse_message_meta_without_sender() -> None:
    _time, sender = parse_message_meta("garbage")
    assert sender is None


def test_find_chats_prefers_exact_over_substring() -> None:
    chats = [_chat("Ravi"), _chat("Ravi Kumar")]
    assert [c["name"] for c in find_chats("ravi", chats)] == ["Ravi"]
    assert [c["name"] for c in find_chats("kumar", chats)] == ["Ravi Kumar"]


def test_find_chats_empty_for_no_match() -> None:
    assert find_chats("nobody", [_chat("Ravi")]) == []
    assert find_chats("  ", [_chat("Ravi")]) == []


# ---------------------------------------------------------------------------
# Tab + login
# ---------------------------------------------------------------------------


async def test_ensure_tab_reuses_existing_whatsapp_tab() -> None:
    browser = FakeBrowser()
    client = WhatsAppClient(browser=browser)
    await client.list_chats()
    assert len(browser.tabs) == 1  # no new tab opened
    assert any("return waListChats(document" in s for s in browser.scripts)


async def test_ensure_tab_opens_whatsapp_when_missing() -> None:
    browser = FakeBrowser(tab_url="https://example.com/")
    client = WhatsAppClient(browser=browser)
    await client.list_chats()
    assert any(t["url"] == "https://web.whatsapp.com/" for t in browser.tabs)


async def test_operations_require_login_qr_gives_pairing_error() -> None:
    client = _client(login="qr")
    with pytest.raises(WhatsAppNotPairedError, match="not paired"):
        await client.list_chats()


async def test_require_login_waits_while_page_is_loading() -> None:
    # 2026-09-24: the old one-shot check fired during WhatsApp Web's loading
    # screen and reported "not paired"; ~20s later the tab read chats fine.
    browser = FakeBrowser(login=["loading", "loading", "logged_in"])
    client = WhatsAppClient(browser=browser)
    await client._require_login(timeout_s=5, poll_s=0.01)  # must not raise


async def test_require_login_tolerates_transient_qr_flash() -> None:
    # 2026-09-24 (second session): the extractor reported "qr" once while
    # WhatsApp Web was still restoring the session, and the old check raised
    # "not paired" immediately. A single "qr" reading is not definitive.
    browser = FakeBrowser(login=["qr", "loading", "logged_in"])
    client = WhatsAppClient(browser=browser)
    await client._require_login(timeout_s=5, poll_s=0.01)  # must not raise


async def test_require_login_raises_when_qr_is_stable() -> None:
    # A QR code that persists for qr_confirm_s really is "not paired".
    client = _client(login="qr")
    with pytest.raises(WhatsAppNotPairedError, match="not paired"):
        await client._require_login(timeout_s=5, poll_s=0.01, qr_confirm_s=0.05)


async def test_require_login_qr_timeout_reports_not_paired() -> None:
    # Stable QR on a tight deadline still reports not-paired (not "loading").
    client = _client(login="qr")
    with pytest.raises(WhatsAppNotPairedError, match="not paired"):
        await client._require_login(timeout_s=0.05, poll_s=0.01, qr_confirm_s=0.01)


async def test_not_paired_hint_clarifies_dedicated_browser() -> None:
    # Jeevan saw WhatsApp open in his own Chrome and was confused: the agent
    # never uses his Chrome, so the hint must say so.
    client = _client(login="qr")
    with pytest.raises(WhatsAppNotPairedError) as exc_info:
        await client._require_login(timeout_s=0.05, poll_s=0.01, qr_confirm_s=0.01)
    assert "regular Chrome" in str(exc_info.value)


async def test_require_login_loading_timeout_mentions_pairing() -> None:
    client = _client(login="loading")
    with pytest.raises(WhatsAppNotPairedError, match="not paired"):
        await client._require_login(timeout_s=0.05, poll_s=0.01)


async def test_wait_for_login_returns_once_paired() -> None:
    browser = FakeBrowser(login="qr")
    client = WhatsAppClient(browser=browser)
    browser.login = "logged_in"  # user scans the QR mid-wait
    await client.wait_for_login(timeout_s=30, poll_s=0.01)


async def test_wait_for_login_times_out_with_help() -> None:
    client = _client(login="qr")
    with pytest.raises(WhatsAppNotPairedError, match="Timed out"):
        await client.wait_for_login(timeout_s=0.05, poll_s=0.01)


# ---------------------------------------------------------------------------
# Chat listing
# ---------------------------------------------------------------------------


async def test_list_chats_returns_parsed_chats() -> None:
    client = _client(chats=[_chat("Ravi", unread=2, preview="hi", time="10:30")])
    chats = await client.list_chats()
    assert chats == [{"name": "Ravi", "unread": 2, "preview": "hi", "time": "10:30"}]


async def test_list_all_chats_scrolls_until_stable() -> None:
    browser = FakeBrowser(chats=[_chat("A"), _chat("B")])
    scrolls = 0

    orig_evaluate = browser.evaluate

    async def evaluate(script):
        nonlocal scrolls
        if "return waScrollChats(document" in script:
            scrolls += 1
            if scrolls == 1:
                browser.chats.append(_chat("C"))
        return await orig_evaluate(script)

    browser.evaluate = evaluate
    client = WhatsAppClient(browser=browser)
    chats = await client.list_all_chats(max_rounds=5)
    assert [c["name"] for c in chats] == ["A", "B", "C"]
    assert scrolls >= 2  # kept going until the bottom stopped changing


async def test_list_all_chats_dedupes_by_name() -> None:
    browser = FakeBrowser(chats=[_chat("A"), _chat("B")])
    client = WhatsAppClient(browser=browser)
    chats = await client.list_all_chats(max_rounds=3)
    assert [c["name"] for c in chats] == ["A", "B"]


async def test_chat_windows_reset_to_top_before_scrolling_down() -> None:
    # 2026-09-24: traversal started at the bottom and reported "no unread"
    # while 3-4 unread chats sat at the top (list is newest-first).
    browser = FakeBrowser(chats=[_chat("A")])
    client = WhatsAppClient(browser=browser)
    windows = [w async for w in client._chat_windows(max_rounds=3)]
    top_idx = next(
        i for i, s in enumerate(browser.scripts) if "return waScrollTop(document" in s
    )
    list_idx = next(
        i for i, s in enumerate(browser.scripts) if "return waListChats(document" in s
    )
    scroll_idx = next(
        i for i, s in enumerate(browser.scripts) if "return waScrollChats(document" in s
    )
    assert top_idx < list_idx < scroll_idx
    assert windows and windows[0] == [_chat("A")]


# ---------------------------------------------------------------------------
# Finding + reading chats
# ---------------------------------------------------------------------------


async def test_find_chat_resolves_exact_name() -> None:
    client = _client(chats=[_chat("Ravi Kumar"), _chat("Family")])
    assert await client.find_chat("ravi kumar") == "Ravi Kumar"


async def test_find_chat_ambiguous_lists_candidates() -> None:
    client = _client(chats=[_chat("Ravi"), _chat("Ravi Kumar")])
    # No exact match: "rav" is a substring of both, so it must ask.
    with pytest.raises(WhatsAppError, match=r"Ravi.*Ravi Kumar"):
        await client.find_chat("rav")


async def test_find_chat_missing_names_the_problem() -> None:
    client = _client(chats=[_chat("Ravi")])
    with pytest.raises(WhatsAppError, match="No WhatsApp chat named 'Nobody'"):
        await client.find_chat("Nobody")


async def test_read_messages_parses_sender_time_direction() -> None:
    client = _client(chats=[_chat("Ravi Kumar")])
    result = await client.read_messages("Ravi Kumar", limit=10)
    assert result["chat"] == "Ravi Kumar"
    first, second = result["messages"]
    assert (first["sender"], first["time"], first["outgoing"]) == (
        "Ravi Kumar",
        "10:30, 24/09/2026",
        False,
    )
    assert (second["sender"], second["outgoing"]) == ("Jeevan", True)
    assert first["text"] == "Are we still on for 6?"


# ---------------------------------------------------------------------------
# Wrong-chat protection (2026-09-24: read_chat("SC1-Confidants") silently
# returned SC1-Executives' messages twice because the pane stayed stale)
# ---------------------------------------------------------------------------


async def test_read_messages_raises_when_header_shows_wrong_chat() -> None:
    browser = FakeBrowser(chats=[_chat("SC1-Executives"), _chat("SC1-Confidants")])
    browser.conversation_title = "SC1-Executives"  # stale pane after the click
    client = WhatsAppClient(browser=browser)
    with pytest.raises(
        WhatsAppError, match=r"SC1-Confidants.*SC1-Executives|wrong chat"
    ):
        await client.read_messages("SC1-Confidants")
    # The header check runs inside _open_chat, so no messages are returned.


async def test_read_messages_succeeds_when_header_matches_clicked_chat() -> None:
    browser = FakeBrowser(chats=[_chat("SC1-Confidants")])
    client = WhatsAppClient(browser=browser)
    result = await client.read_messages("SC1-Confidants")
    assert result["chat"] == "SC1-Confidants"
    assert len(result["messages"]) == 2


async def test_header_match_ignores_case_and_extra_whitespace() -> None:
    browser = FakeBrowser(chats=[_chat("SC1-Confidants")])
    browser.conversation_title = "  sc1-confidants "
    client = WhatsAppClient(browser=browser)
    result = await client.read_messages("sc1-confidants")
    assert result["chat"] == "SC1-Confidants"


# ---------------------------------------------------------------------------
# Resilient message loading (2026-09-24: "its messages would not load"
# aborted the whole where_needed triage; the same chat worked on retry)
# ---------------------------------------------------------------------------


async def test_open_chat_waits_for_slow_loading_messages() -> None:
    browser = FakeBrowser(chats=[_chat("Ravi")])
    browser.read_rounds = [
        {"messages": []},  # pane exists but messages not rendered yet
        {"messages": []},
        {
            "messages": [
                {"meta": "[10:30] Ravi: ", "text": "hi", "outgoing": False},
            ]
        },
    ]
    client = WhatsAppClient(browser=browser)
    result = await client.read_messages("Ravi", limit=5)
    assert len(result["messages"]) == 1
    assert browser.click_calls == 1  # no retry needed: polling waited it out


async def test_open_chat_retries_once_then_raises_when_unloadable() -> None:
    browser = FakeBrowser(chats=[_chat("SC1-Organization12")])
    browser.read_rounds = [{"messages": []}]  # never renders
    client = WhatsAppClient(browser=browser)
    with pytest.raises(WhatsAppError, match="would not load"):
        await client._open_chat("SC1-Organization12", timeout_s=0.05, poll_s=0.01)
    assert browser.click_calls == 2  # one retry, then give up


async def test_open_chat_treats_empty_chat_placeholder_as_loaded() -> None:
    browser = FakeBrowser(chats=[_chat("Quiet")])
    browser.read_rounds = [{"messages": [], "empty": True}]
    client = WhatsAppClient(browser=browser)
    result = await client.read_messages("Quiet", limit=5)
    assert result["messages"] == []


# ---------------------------------------------------------------------------
# Message-pane scroll-up pagination (2026-09-24: limit=100 only returned the
# initially rendered handful; older history was never reached)
# ---------------------------------------------------------------------------


def _paged_msg(i):
    return {"meta": f"[10:{i:02d}] Ravi: ", "text": f"message {i}", "outgoing": False}


async def test_read_messages_scrolls_up_for_older_history() -> None:
    browser = FakeBrowser(chats=[_chat("Ravi")])
    browser.message_pages = [
        [_paged_msg(1), _paged_msg(2)],
        [_paged_msg(i) for i in range(1, 6)],
    ]
    client = WhatsAppClient(browser=browser)
    result = await client.read_messages("Ravi", limit=5, scroll_pause_s=0.01)
    texts = [m["text"] for m in result["messages"]]
    assert texts == [f"message {i}" for i in range(1, 6)]
    assert any("return waScrollMessagesUp(document" in s for s in browser.scripts)


async def test_read_messages_stops_scrolling_when_history_exhausted() -> None:
    browser = FakeBrowser(chats=[_chat("Ravi")])
    # Only 3 messages exist in total: scrolling cannot grow the list.
    browser.message_pages = [[_paged_msg(i) for i in range(1, 4)]]
    client = WhatsAppClient(browser=browser)
    result = await client.read_messages("Ravi", limit=100, scroll_pause_s=0.01)
    assert len(result["messages"]) == 3


# ---------------------------------------------------------------------------
# Sending (verification, no blind retries)
# ---------------------------------------------------------------------------


def _send_browser():
    browser = FakeBrowser(chats=[_chat("Ravi")])
    browser.last_message_sequence = [
        {"meta": "[10:30] Ravi: ", "text": "old", "outgoing": False},
        {"meta": "[10:31] Ravi: ", "text": "old", "outgoing": False},
        {"meta": "[10:35] Jeevan: ", "text": "running late", "outgoing": True},
    ]
    return browser


async def test_send_message_verifies_new_outgoing_message() -> None:
    browser = _send_browser()
    client = WhatsAppClient(browser=browser)
    result = await client.send_message("Ravi", "running late")
    assert result == {"sent": True, "chat": "Ravi"}
    assert browser.last_message_calls >= 2


async def test_send_message_never_retries_an_unconfirmed_send() -> None:
    browser = _send_browser()
    # The sent message never appears: verification must fail, not retry.
    browser.last_message_sequence = [
        {"meta": "[10:30] Ravi: ", "text": "old", "outgoing": False}
    ] * 20
    client = WhatsAppClient(browser=browser)
    with pytest.raises(WhatsAppError, match="may not have been sent"):
        await client.send_message("Ravi", "running late")
    type_calls = sum("return waTypeAndSend(document" in s for s in browser.scripts)
    assert type_calls == 1


async def test_send_message_falls_back_to_enter() -> None:
    browser = _send_browser()
    browser.type_results = {"typed": True, "clickedSend": False}
    client = WhatsAppClient(browser=browser)
    result = await client.send_message("Ravi", "running late")
    assert result["sent"] is True
    assert "Enter" in browser.pressed


async def test_send_message_type_failure_raises() -> None:
    browser = _send_browser()
    browser.type_results = {"typed": False, "reason": "no-message-box"}
    client = WhatsAppClient(browser=browser)
    with pytest.raises(WhatsAppError, match="Could not type"):
        await client.send_message("Ravi", "hi")


async def test_evaluate_scripts_call_the_named_extractor() -> None:
    browser = FakeBrowser(chats=[_chat("Ravi")])
    client = WhatsAppClient(browser=browser)
    await client.list_chats()
    assert any("waListChats(document)" in s for s in browser.scripts)
