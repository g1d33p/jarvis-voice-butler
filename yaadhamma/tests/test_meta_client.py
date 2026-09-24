"""Tests for the Meta Model API client (no key, no network)."""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from livekit import rtc
from livekit.agents import stt as lk_stt

from meta_client import (
    MetaBrainClient,
    MetaConfig,
    MetaConfigError,
    MetaRealtimeSTT,
    ModelTurn,
    _raise_for_stt_error,
    create_async_client,
)


@pytest.fixture(autouse=True)
def _no_proxies(monkeypatch):
    """This sandbox sets malformed proxy env vars that break httpx URL
    parsing; real machines don't. Strip them so client construction works."""
    for var in list(os.environ):
        if "proxy" in var.lower():
            monkeypatch.delenv(var, raising=False)


def test_config_defaults() -> None:
    cfg = MetaConfig(api_key="k")
    assert cfg.base_url == "https://api.meta.ai/v1"
    assert cfg.brain_model == "muse-spark-1.3"
    assert cfg.voice_model == "muse-spark-1.3"
    assert cfg.escalation_model == "muse-spark-1.3"
    assert cfg.stt_model == "muse-voice-transcribe-1.0"
    assert cfg.stt_ws_url == "wss://api.meta.ai/v1/asr/realtime"
    assert cfg.voice_mode == "pipeline"


def test_config_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("YAADHAMMA_MODEL_API_KEY", "env-key")
    monkeypatch.setenv("YAADHAMMA_BRAIN_MODEL", "muse-spark-1.2")
    monkeypatch.setenv("YAADHAMMA_VOICE_MODE", "realtime")
    cfg = MetaConfig.from_env()
    assert cfg.api_key == "env-key"
    assert cfg.brain_model == "muse-spark-1.2"
    assert cfg.voice_mode == "realtime"


def test_missing_key_raises_clear_error() -> None:
    with pytest.raises(MetaConfigError, match="YAADHAMMA_MODEL_API_KEY"):
        create_async_client(MetaConfig(api_key=None))


def test_create_client_points_at_meta() -> None:
    client = create_async_client(MetaConfig(api_key="k"))
    assert str(client.base_url).rstrip("/") == "https://api.meta.ai/v1"


# ---------------------------------------------------------------- brain


def _msg(content="", tool_calls=None, tokens=(10, 5)):
    calls = []
    for i, (name, args) in enumerate(tool_calls or []):
        calls.append(
            SimpleNamespace(
                id=f"call_{i}",
                function=SimpleNamespace(name=name, arguments=json.dumps(args)),
            )
        )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=calls or None)
            )
        ],
        usage=SimpleNamespace(prompt_tokens=tokens[0], completion_tokens=tokens[1]),
    )


class FakeCompletions:
    def __init__(self, *replies):
        self._replies = list(replies)
        self.seen: list[dict] = []

    async def create(self, **kwargs):
        self.seen.append(kwargs)
        return self._replies.pop(0)


class FakeOpenAI:
    def __init__(self, *replies):
        self.chat = SimpleNamespace(completions=FakeCompletions(*replies))


def test_brain_client_tool_call_round_trip() -> None:
    fake = FakeOpenAI(
        _msg(tool_calls=[("search_the_web", {"query": "weather"})]),
        _msg(content="Sunny."),
    )
    brain = MetaBrainClient(client=fake)

    async def run():
        tools = [{"type": "function", "function": {"name": "search_the_web"}}]
        first = await brain.generate("muse-spark-1.3", [{"role": "user"}], tools)
        second = await brain.generate("muse-spark-1.3", [{"role": "user"}], tools)
        return first, second, fake.chat.completions.seen

    first, second, seen = asyncio.run(run())
    assert isinstance(first, ModelTurn)
    assert first.calls == [("search_the_web", {"query": "weather"})]
    assert first.text == ""
    assert (first.tokens_in, first.tokens_out) == (10, 5)
    assert second.calls == []
    assert second.text == "Sunny."
    assert seen[0]["model"] == "muse-spark-1.3"
    assert seen[0]["tools"][0]["function"]["name"] == "search_the_web"


def test_brain_client_sends_reasoning_effort() -> None:
    fake = FakeOpenAI(_msg(content="ok"))
    brain = MetaBrainClient(client=fake)
    asyncio.run(brain.generate("m", [], [], reasoning_effort="high"))
    assert fake.chat.completions.seen[0]["extra_body"] == {"reasoning_effort": "high"}


def test_brain_client_requires_key_when_built_lazily() -> None:
    brain = MetaBrainClient(cfg=MetaConfig(api_key=None))
    with pytest.raises(MetaConfigError, match="YAADHAMMA_MODEL_API_KEY"):
        asyncio.run(brain.generate("m", [], []))


# ---------------------------------------------------------------- STT


class FakeWS:
    """A scripted stand-in for the Meta realtime websocket.

    The test feeds incoming messages explicitly, so there are no races:
    nothing arrives until feed() is called.
    """

    def __init__(self):
        self.sent: list = []
        self._incoming: asyncio.Queue[str] = asyncio.Queue()
        self.closed = False

    async def send(self, data) -> None:
        self.sent.append(data)

    def feed(self, message: str) -> None:
        self._incoming.put_nowait(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await asyncio.wait_for(self._incoming.get(), timeout=10)
        except asyncio.TimeoutError:
            raise StopAsyncIteration from None

    async def close(self) -> None:
        self.closed = True


async def _run_stream(stt, ws, *outgoing: str) -> list:
    """Push one audio frame, feed scripted server messages, return events."""
    stream = stt.stream()
    stream.push_frame(_audio_frame())
    # Wait for the JSON handshake frame and the audio bytes to go out.
    for _ in range(200):
        if len(ws.sent) >= 2:
            break
        await asyncio.sleep(0.01)
    handshake = json.loads(ws.sent[0])
    assert handshake["authorization"] == {"accessToken": "Bearer k"}
    assert handshake["audioEncoding"] == "PCM_24KHZ"
    assert handshake["model"] == "muse-voice-transcribe-1.0"
    assert handshake["mode"] == "ENDPOINTING"
    assert isinstance(ws.sent[1], (bytes, bytearray))

    events: list = []

    async def collect():
        async for ev in stream:
            events.append(ev)

    collector = asyncio.create_task(collect())
    for message in outgoing:
        ws.feed(message)
    await asyncio.sleep(0.3)  # let the events flow through
    await stream.aclose()
    await collector
    return events


def _connect(stt, ws) -> None:
    async def _fake_connect():
        return ws

    stt._ws_connect = _fake_connect  # type: ignore[method-assign]


def _frame(text: str, final: bool) -> str:
    return json.dumps({"type": "transcript", "text": text, "final": final})


def _audio_frame() -> rtc.AudioFrame:
    return rtc.AudioFrame(
        data=b"\x00\x00" * 240,
        sample_rate=24000,
        num_channels=1,
        samples_per_channel=240,
    )


def _audio_frame_16k() -> rtc.AudioFrame:
    return rtc.AudioFrame(
        data=b"\x00\x00" * 160,
        sample_rate=16000,
        num_channels=1,
        samples_per_channel=160,
    )


def test_stt_emits_interim_then_final() -> None:
    stt = MetaRealtimeSTT(MetaConfig(api_key="k"))
    ws = FakeWS()
    _connect(stt, ws)

    async def run():
        return await _run_stream(stt, ws, _frame("hel", False), _frame("hello", True))

    events = asyncio.run(run())
    kinds = [ev.type for ev in events]
    assert lk_stt.SpeechEventType.START_OF_SPEECH in kinds
    assert lk_stt.SpeechEventType.INTERIM_TRANSCRIPT in kinds
    assert lk_stt.SpeechEventType.FINAL_TRANSCRIPT in kinds
    final = next(
        ev for ev in events if ev.type == lk_stt.SpeechEventType.FINAL_TRANSCRIPT
    )
    assert final.alternatives[0].text == "hello"


def test_stt_understands_alternative_shapes() -> None:
    stt = MetaRealtimeSTT(MetaConfig(api_key="k"))
    ws = FakeWS()
    _connect(stt, ws)
    google_style = json.dumps(
        {
            "type": "result",
            "alternatives": [{"transcript": "hi there"}],
            "is_final": True,
        }
    )

    async def run():
        return await _run_stream(stt, ws, google_style)

    events = asyncio.run(run())
    finals = [ev for ev in events if ev.type == lk_stt.SpeechEventType.FINAL_TRANSCRIPT]
    assert finals and finals[0].alternatives[0].text == "hi there"


def test_stt_reports_model_and_provider() -> None:
    stt = MetaRealtimeSTT(MetaConfig(api_key="k"))
    assert stt.model == "muse-voice-transcribe-1.0"
    assert stt.provider == "meta"
    assert stt.capabilities.streaming
    assert stt.capabilities.interim_results


def test_stt_ws_url_carries_fresh_session_id() -> None:
    stt = MetaRealtimeSTT(MetaConfig(api_key="k"))
    url1, url2 = stt._ws_url(), stt._ws_url()
    assert url1.startswith("wss://api.meta.ai/v1/asr/realtime?sessionId=stream-")
    assert url1 != url2  # a fresh session id per connection


def test_stt_error_frame_raises_loudly() -> None:
    with pytest.raises(RuntimeError, match="bad key"):
        _raise_for_stt_error(json.dumps({"type": "error", "message": "bad key"}))
    # Anything else is ignored, never raised.
    _raise_for_stt_error(json.dumps({"type": "transcript", "transcript": "hi"}))
    _raise_for_stt_error("not json at all")
    _raise_for_stt_error(None)


def test_stt_ignores_binary_server_frames() -> None:
    stt = MetaRealtimeSTT(MetaConfig(api_key="k"))
    ws = FakeWS()
    _connect(stt, ws)

    async def run():
        return await _run_stream(stt, ws, b"\x00\x01\x02", _frame("hi", True))

    events = asyncio.run(run())
    finals = [ev for ev in events if ev.type == lk_stt.SpeechEventType.FINAL_TRANSCRIPT]
    assert finals and finals[0].alternatives[0].text == "hi"


def test_stt_resamples_16k_input_to_24k() -> None:
    stt = MetaRealtimeSTT(MetaConfig(api_key="k"))
    ws = FakeWS()
    _connect(stt, ws)

    async def run() -> int:
        stream = stt.stream()
        for _ in range(200):
            stream.push_frame(_audio_frame_16k())
        for _ in range(400):
            total = sum(
                len(s) for s in ws.sent[1:] if isinstance(s, (bytes, bytearray))
            )
            if total >= 80000:
                break
            await asyncio.sleep(0.01)
        total = sum(len(s) for s in ws.sent[1:] if isinstance(s, (bytes, bytearray)))
        await stream.aclose()
        return total

    total = asyncio.run(run())
    # 200 frames x 160 samples @16kHz = 2 s of audio -> ~96000 bytes @24kHz;
    # the resampler may hold a small tail back.
    assert 80000 <= total <= 96000


def test_stt_config_reads_mode_from_env(monkeypatch) -> None:
    monkeypatch.setenv("YAADHAMMA_STT_MODE", "push_to_talk")
    cfg = MetaConfig.from_env()
    assert cfg.stt_mode == "PUSH_TO_TALK"
