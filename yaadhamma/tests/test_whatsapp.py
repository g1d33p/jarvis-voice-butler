"""Tests for the WhatsApp Web client (browser layer is faked)."""

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
            return {"state": self.login}
        if "return waListChats(document" in script:
            return {"chats": self.chats}
        if "return waScrollChats(document" in script:
            return {"before": len(self.chats)}
        if "return waClickChat(document" in script:
            return {"opened": True, "matched": "x"}
        if "return waReadMessages(document" in script:
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
        if "return waTypeAndSend(document" in script:
            return dict(self.type_results)
        if "return waLastMessage(document" in script:
            self.last_message_calls += 1
            return {"message": self._last_message()}
        raise AssertionError(f"unexpected evaluate script: {script[:60]}")

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
