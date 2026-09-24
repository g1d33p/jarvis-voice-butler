"""Tests for src/failover_llm.py — runtime LLM failover.

TDD for the 2026-09-24 Meta outage: when Muse Spark hangs mid-conversation,
the voice agent must fail over to a Gemini text model instead of retrying the
dead endpoint and going silent. No network in these tests — both backends are
scripted fakes implementing the LiveKit LLM interface.
"""

import asyncio
from pathlib import Path

import pytest
from livekit.agents import function_tool
from livekit.agents._exceptions import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
)
from livekit.agents.llm import (
    LLM,
    ChatChunk,
    ChatContext,
    ChoiceDelta,
    FunctionToolCall,
    LLMStream,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

import failover_llm
from failover_llm import (
    BACKUP_CONTEXT_NOTE,
    FailoverLLM,
    maybe_wrap_with_failover,
)


def _text_chunk(text: str) -> ChatChunk:
    return ChatChunk(
        id="chunk-1",
        delta=ChoiceDelta(role="assistant", content=text),
    )


def _tool_chunk(name: str) -> ChatChunk:
    return ChatChunk(
        id="chunk-1",
        delta=ChoiceDelta(
            role="assistant",
            tool_calls=[
                FunctionToolCall(name=name, arguments='{"x": 1}', call_id="call-1")
            ],
        ),
    )


class ScriptedStream(LLMStream):
    """LLMStream that replays one scripted behavior per chat() call."""

    def __init__(self, llm, *, chat_ctx, tools, conn_options, behavior):
        super().__init__(llm, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._behavior = behavior

    async def _run(self) -> None:
        if isinstance(self._behavior, BaseException):
            raise self._behavior
        for chunk in self._behavior:
            self._event_ch.send_nowait(chunk)

    async def _metrics_monitor_task(self, event_aiter) -> None:
        return


class ScriptedLLM(LLM):
    """Fake LLM. Each chat() pops the next behavior: an Exception to raise
    or a list of ChatChunks to stream. An exhausted script streams nothing
    (a successful empty generation)."""

    def __init__(self, name: str, behaviors):
        super().__init__()
        self._name = name
        self._behaviors = list(behaviors)
        self.chat_calls = 0
        self.last_tools = None
        self.last_chat_ctx = None

    @property
    def model(self) -> str:
        return self._name

    def chat(
        self,
        *,
        chat_ctx,
        tools=None,
        conn_options=DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls=None,
        tool_choice=None,
        extra_kwargs=None,
    ) -> LLMStream:
        self.chat_calls += 1
        self.last_tools = tools
        self.last_chat_ctx = chat_ctx
        behavior = self._behaviors.pop(0) if self._behaviors else []
        return ScriptedStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
            behavior=behavior,
        )


async def _collect(llm: LLM, tools=None) -> list[ChatChunk]:
    chunks: list[ChatChunk] = []
    async with llm.chat(chat_ctx=ChatContext(), tools=tools or []) as stream:
        async for chunk in stream:
            chunks.append(chunk)
    return chunks


def _text(chunks: list[ChatChunk]) -> str:
    return "".join(c.delta.content or "" for c in chunks if c.delta)


async def test_primary_timeout_falls_back_to_backup():
    primary = ScriptedLLM("primary", [APITimeoutError("timed out")])
    backup = ScriptedLLM("backup", [[_text_chunk("backup answer")]])
    llm = FailoverLLM(primary, backup=backup)
    chunks = await _collect(llm)
    assert _text(chunks) == "backup answer"
    assert primary.chat_calls == 1
    assert backup.chat_calls == 1


async def test_connection_error_falls_back_to_backup():
    primary = ScriptedLLM("primary", [APIConnectionError("conn reset")])
    backup = ScriptedLLM("backup", [[_text_chunk("backup answer")]])
    llm = FailoverLLM(primary, backup=backup)
    chunks = await _collect(llm)
    assert _text(chunks) == "backup answer"


async def test_non_retryable_error_does_not_fail_over():
    auth_error = APIStatusError("bad key", status_code=401)
    assert auth_error.retryable is False
    primary = ScriptedLLM("primary", [auth_error])
    backup = ScriptedLLM("backup", [[_text_chunk("backup answer")]])
    llm = FailoverLLM(primary, backup=backup)
    with pytest.raises(APIStatusError) as exc_info:
        await _collect(llm)
    assert exc_info.value.status_code == 401
    assert backup.chat_calls == 0, "must not touch backup on auth errors"
    # The circuit must not open on a config error either.
    assert llm.primary_available is True


async def test_circuit_stays_closed_until_k_consecutive_failures():
    primary = ScriptedLLM(
        "primary",
        [
            APITimeoutError("t1"),
            [_text_chunk("primary fine")],
        ],
    )
    backup = ScriptedLLM("backup", [[_text_chunk("b1")]])
    llm = FailoverLLM(primary, backup=backup, failover_threshold=2)

    assert _text(await _collect(llm)) == "b1"
    assert llm.primary_available is True  # 1 failure < K: still closed
    # Next turn still tries the primary first.
    assert _text(await _collect(llm)) == "primary fine"
    assert primary.chat_calls == 2


async def test_circuit_opens_after_k_failures_and_skips_primary():
    from unittest import mock

    from failover_llm import _FailoverLLMStream

    primary = ScriptedLLM(
        "primary",
        [
            APITimeoutError("t1"),
            APITimeoutError("t2"),
        ],
    )
    backup = ScriptedLLM("backup", [[_text_chunk(f"b{i}")] for i in range(1, 5)])
    llm = FailoverLLM(primary, backup=backup, failover_threshold=2)

    # Background recovery probes are covered by the failback test; mute them
    # here so primary.chat calls can only come from real turns.
    with mock.patch.object(_FailoverLLMStream, "_try_recovery", lambda self, llm: None):
        assert _text(await _collect(llm)) == "b1"
        assert llm.primary_available is True
        assert _text(await _collect(llm)) == "b2"
        assert llm.primary_available is False  # K=2 consecutive: circuit open

        # Turn 3 must not attempt the primary at all: straight to backup.
        assert _text(await _collect(llm)) == "b3"
        assert primary.chat_calls == 2
        assert backup.chat_calls == 3


def _backup_note_present(ctx: ChatContext) -> bool:
    """Whether the invisible backup-turn note is in the given context."""
    for item in ctx.items:
        if getattr(item, "type", "") != "message":
            continue
        for content in item.content:
            text = content if isinstance(content, str) else getattr(content, "text", "")
            if BACKUP_CONTEXT_NOTE in str(text or ""):
                return True
    return False


async def test_circuit_fails_back_silently_after_recovery():
    primary = ScriptedLLM(
        "primary",
        [
            APITimeoutError("t1"),
            APITimeoutError("t2"),
            # Recovery probe spawned after turn 2 fails: primary still down.
            APITimeoutError("probe-fails"),
            # Recovery probe spawned during turn 3 succeeds.
            [_text_chunk("probe-ok")],
            [_text_chunk("primary back")],
        ],
    )
    backup = ScriptedLLM("backup", [[_text_chunk(f"b{i}")] for i in range(1, 4)])
    llm = FailoverLLM(primary, backup=backup, failover_threshold=2)

    assert _text(await _collect(llm)) == "b1"
    assert _text(await _collect(llm)) == "b2"
    # The first recovery probe already ran (and failed) during turn 2, so the
    # circuit is still open here.
    assert llm.primary_available is False
    # Every backup-served turn carries the invisible context note; it is
    # never spoken and never added to the session history.
    assert _backup_note_present(backup.last_chat_ctx)

    # Turn 3 goes to the backup; its recovery probe succeeds, so the circuit
    # closes silently afterwards.
    assert _text(await _collect(llm)) == "b3"
    await asyncio.sleep(0.3)
    assert llm.primary_available is True

    # Next turn goes back to the primary with the untouched context: no
    # note, no announcement.
    assert _text(await _collect(llm)) == "primary back"
    assert not _backup_note_present(primary.last_chat_ctx)


async def test_tools_flow_through_backup_path():
    primary = ScriptedLLM("primary", [APITimeoutError("down")])
    backup = ScriptedLLM("backup", [[_tool_chunk("gmail_read_inbox")]])
    llm = FailoverLLM(primary, backup=backup)

    @function_tool
    async def gmail_read_inbox() -> str:
        """Read the inbox."""
        return "ok"

    chunks = await _collect(llm, tools=[gmail_read_inbox])
    tool_calls = [tc for c in chunks if c.delta for tc in c.delta.tool_calls]
    assert [tc.name for tc in tool_calls] == ["gmail_read_inbox"]
    # The backup backend received the tool definitions.
    assert backup.last_tools is not None
    assert [t.id for t in backup.last_tools] == ["gmail_read_inbox"]


async def test_no_google_key_returns_primary_unchanged(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    primary = ScriptedLLM("primary", [])
    assert maybe_wrap_with_failover(primary) is primary


async def test_google_key_wraps_primary(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    primary = ScriptedLLM("primary", [])
    wrapped = maybe_wrap_with_failover(primary, backup=ScriptedLLM("b", []))
    assert isinstance(wrapped, FailoverLLM)


async def test_force_failover_routes_next_turn_to_backup(monkeypatch):
    monkeypatch.setenv("YAADHAMMA_FORCE_FAILOVER", "1")
    primary = ScriptedLLM("primary", [[_text_chunk("primary")]])
    backup = ScriptedLLM("backup", [[_text_chunk("backup")]])
    llm = FailoverLLM(primary, backup=backup)

    # Forced turn: backup answers without the primary being tried, silently.
    assert _text(await _collect(llm)) == "backup"
    assert primary.chat_calls == 0
    assert _backup_note_present(backup.last_chat_ctx)

    # One-shot: the following turn goes back to the primary with the
    # untouched context.
    assert _text(await _collect(llm)) == "primary"
    assert primary.chat_calls == 1
    assert not _backup_note_present(primary.last_chat_ctx)


# ---------------------------------------------------------------------------
# Silence: failover must never be announced out loud (2026-09-24).
# ---------------------------------------------------------------------------


def test_no_failover_notice_mechanism():
    assert not hasattr(failover_llm, "FAILOVER_NOTICE_EVENT"), (
        "failover must be silent: the spoken-notice event was removed"
    )


def test_failover_module_contains_no_speech_calls():
    src = Path(__file__).resolve().parent.parent / "src" / "failover_llm.py"
    assert ".say(" not in src.read_text()


def test_agent_does_not_wire_a_failover_announcement():
    agent_src = Path(__file__).resolve().parent.parent / "src" / "agent.py"
    text = agent_src.read_text()
    assert "FAILOVER_NOTICE_EVENT" not in text
    assert "session.say" not in text


# ---------------------------------------------------------------------------
# Backup model: gemini-2.5-flash was retired for new keys on 2026-09-24.
# ---------------------------------------------------------------------------


def _capture_google_llm(monkeypatch):
    captured = {}

    def fake_llm(**kwargs):
        captured.update(kwargs)
        return ScriptedLLM("fake-backup", [])

    monkeypatch.setattr(failover_llm.google, "LLM", fake_llm)
    return captured


def test_default_backup_model_is_current(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.delenv("YAADHAMMA_FAILOVER_MODEL", raising=False)
    captured = _capture_google_llm(monkeypatch)
    wrapped = maybe_wrap_with_failover(ScriptedLLM("primary", []))
    assert isinstance(wrapped, FailoverLLM)
    assert captured.get("model") == "gemini-3.8-flash"
    assert failover_llm.DEFAULT_BACKUP_MODEL == "gemini-3.8-flash"


def test_failover_model_env_override_still_works(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("YAADHAMMA_FAILOVER_MODEL", "gemini-x-custom")
    captured = _capture_google_llm(monkeypatch)
    maybe_wrap_with_failover(ScriptedLLM("primary", []))
    assert captured.get("model") == "gemini-x-custom"


# ---------------------------------------------------------------------------
# Backup deadline: Google rejects generate_content deadlines under 10s.
# On 2026-09-24 the 5s attempt timeout leaked into the backup request and
# every failover turn died with 400 "Manually set deadline 5s is too short".
# ---------------------------------------------------------------------------


def test_backup_llm_gets_deadline_above_googles_minimum(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.delenv("YAADHAMMA_FAILOVER_MODEL", raising=False)
    monkeypatch.delenv("YAADHAMMA_FAILOVER_BACKUP_TIMEOUT", raising=False)
    captured = _capture_google_llm(monkeypatch)
    maybe_wrap_with_failover(ScriptedLLM("primary", []))
    timeout_ms = captured["http_options"].timeout
    assert timeout_ms is not None and timeout_ms >= 10_000, (
        f"backup deadline {timeout_ms}ms would be rejected by Google (< 10s)"
    )


def test_backup_timeout_knob_is_floored_at_google_minimum(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("YAADHAMMA_FAILOVER_BACKUP_TIMEOUT", "2")
    captured = _capture_google_llm(monkeypatch)
    maybe_wrap_with_failover(ScriptedLLM("primary", []))
    assert captured["http_options"].timeout >= 10_000


def test_backup_timeout_knob_accepts_larger_values(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("YAADHAMMA_FAILOVER_BACKUP_TIMEOUT", "45")
    captured = _capture_google_llm(monkeypatch)
    maybe_wrap_with_failover(ScriptedLLM("primary", []))
    assert captured["http_options"].timeout == 45_000
