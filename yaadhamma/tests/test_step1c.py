"""Step 1c (2026-09-25): Gemini brain, cost-trimmed voice, browser action lock."""

import asyncio
import importlib
import json
import os
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletionMessageToolCall

import meta_client
from browser import ActionLock, browser_action
from meta_client import GEMINI_OPENAI_URL, GeminiBrainClient, MetaConfigError, ModelTurn
from orchestrator import VOICE_TOOL_NAMES, _assistant_message

# ----------------------------------------------------------------- brain


def _gemini_reply_with_signature():
    """A tool call as Google's OpenAI-compatible API returns it, signature included."""
    call = ChatCompletionMessageToolCall.model_validate(
        {
            "id": "gemini-call-7",
            "type": "function",
            "function": {"name": "whatsapp_list_chats", "arguments": "{}"},
            "extra_content": {"google": {"thought_signature": "sig-abc"}},
        }
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[call]))
        ],
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
    )


class _FakeCompletions:
    def __init__(self, reply):
        self.reply = reply
        self.seen = []

    async def create(self, **kwargs):
        self.seen.append(kwargs)
        return self.reply


def test_thought_signature_survives_the_round_trip() -> None:
    """Gemini 3 rejects follow-ups that drop the signature on a tool call."""
    completions = _FakeCompletions(_gemini_reply_with_signature())
    brain = GeminiBrainClient(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions))
    )

    turn = asyncio.run(brain.generate("gemini-3.5-flash-lite", [], []))
    message = _assistant_message("t1", 0, turn)

    sent_back = message["tool_calls"][0]
    assert sent_back["id"] == "gemini-call-7"
    assert sent_back["extra_content"]["google"]["thought_signature"] == "sig-abc"


def test_synthetic_ids_still_used_without_provider_calls() -> None:
    turn = ModelTurn(calls=[("echo", {"text": "hi"})], text="")
    message = _assistant_message("t9", 3, turn)
    assert message["tool_calls"][0]["id"] == "call_t9_3_0"


def test_text_only_reply_has_no_empty_tool_calls() -> None:
    message = _assistant_message("t1", 0, ModelTurn(calls=[], text="Done."))
    assert "tool_calls" not in message


def test_gemini_brain_uses_googles_endpoint_and_key(monkeypatch) -> None:
    for var in list(os.environ):
        if "proxy" in var.lower():
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    client = GeminiBrainClient()._get_client()
    assert str(client.base_url) == GEMINI_OPENAI_URL
    assert client.api_key == "g-key"


def test_gemini_brain_without_key_is_a_clear_error(monkeypatch) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(MetaConfigError, match="GOOGLE_API_KEY"):
        asyncio.run(GeminiBrainClient().generate("m", [], []))


@pytest.fixture()
def fresh_config(monkeypatch):
    import config

    def load(**env):
        for name in (
            "YAADHAMMA_BRAIN_PROVIDER",
            "YAADHAMMA_BRAIN_MODEL",
            "YAADHAMMA_ESCALATION_MODEL",
        ):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return importlib.reload(config)

    yield load
    importlib.reload(config)


def test_brain_defaults_to_gemini(fresh_config) -> None:
    config = fresh_config()
    assert config.BRAIN_PROVIDER == "gemini"
    assert config.BRAIN_MODEL == "gemini-3.5-flash-lite"
    assert config.ESCALATION_MODEL == "gemini-3.8-flash"
    assert isinstance(meta_client.brain_client_from_config(), GeminiBrainClient)


def test_stale_muse_model_name_is_ignored_for_gemini(fresh_config) -> None:
    """An old .env.local line must not send 'muse-spark-1.3' to Google."""
    config = fresh_config(YAADHAMMA_BRAIN_MODEL="muse-spark-1.3")
    assert config.BRAIN_MODEL == "gemini-3.5-flash-lite"


def test_meta_brain_is_still_available_as_rollback(fresh_config) -> None:
    config = fresh_config(YAADHAMMA_BRAIN_PROVIDER="meta")
    assert config.BRAIN_MODEL == "muse-spark-1.3"
    assert type(meta_client.brain_client_from_config()).__name__ == "MetaBrainClient"


# ----------------------------------------------------------------- voice cost


def test_voice_keeps_only_quick_tools() -> None:
    assert len(VOICE_TOOL_NAMES) <= 12
    for moved in (
        "whatsapp_list_chats",
        "whatsapp_send_message",
        "gmail_read_inbox",
        "read_inbox",
        "check_calendar",
        "read_clipboard",
        "forget_memory",
    ):
        assert moved not in VOICE_TOOL_NAMES


def test_moved_rules_now_live_in_the_orchestrator_prompt() -> None:
    from prompts import ORCHESTRATOR_INSTRUCTIONS, VOICE_INSTRUCTIONS

    assert "gmail_read_inbox" in ORCHESTRATOR_INSTRUCTIONS
    assert "whatsapp_where_needed" in ORCHESTRATOR_INSTRUCTIONS
    assert "gmail_read_inbox" not in VOICE_INSTRUCTIONS
    assert "always go through run_task" in VOICE_INSTRUCTIONS


# ----------------------------------------------------------------- browser lock


class _Tools:
    def __init__(self):
        self.browser = SimpleNamespace(action_lock=ActionLock())
        self.log = []

    @browser_action(lambda self: self.browser)
    async def slow(self, name):
        self.log.append(f"{name} start")
        await asyncio.sleep(0.05)
        self.log.append(f"{name} end")

    @browser_action(lambda self: self.browser)
    async def outer(self):
        await self.slow("inner")  # re-entrant: must not deadlock


async def test_two_browser_actions_never_overlap() -> None:
    tools = _Tools()
    await asyncio.gather(tools.slow("a"), tools.slow("b"))
    assert tools.log in (
        ["a start", "a end", "b start", "b end"],
        ["b start", "b end", "a start", "a end"],
    )


async def test_nested_browser_action_does_not_deadlock() -> None:
    tools = _Tools()
    await asyncio.wait_for(tools.outer(), timeout=1)
    assert tools.log == ["inner start", "inner end"]


def test_locked_tools_keep_their_schemas() -> None:
    """The lock decorator must not change what the models see."""
    from livekit.agents import llm

    from browser import BrowserManager
    from tools import BrowserTools

    schemas = llm.ToolContext(
        BrowserTools(BrowserManager(headless=True)).tools
    ).parse_function_tools("openai")
    by_name = {s["function"]["name"]: s for s in schemas}
    assert "url" in json.dumps(by_name["open_url"]["function"]["parameters"])
    assert "context" not in json.dumps(by_name["open_url"]["function"]["parameters"])


async def test_slow_whatsapp_is_not_reported_as_unpaired() -> None:
    from livekit.agents.llm import ToolError

    from whatsapp import WhatsAppNotPairedError
    from whatsapp_tools import WhatsAppTools

    async def slow_page():
        raise WhatsAppNotPairedError("WhatsApp Web is still loading after 8s. ...")

    tools = WhatsAppTools(client=SimpleNamespace())
    with pytest.raises(ToolError, match="still loading") as err:
        await tools._guarded(slow_page)
    assert "whatsapp_signin" not in str(err.value)
