"""Meta Model API client: Yaadhamma's brain (Muse Spark) and ears (Voice Transcribe).

Meta exposes OpenAI-compatible surfaces at https://api.meta.ai/v1, so the
brain uses plain Chat Completions through the ``openai`` SDK. Speech-to-text
goes over the Voice Transcribe realtime websocket
(``wss://api.meta.ai/v1/asr/realtime``). Meta offers no TTS, so speech output
stays on LiveKit Inference (see agent.py).

The realtime STT wire protocol follows Meta's official "Transcribe in
realtime" recipe (dev.meta.ai/docs/api-reference/voice/realtime), verified
2026-09-24: the credential travels inside the opening JSON frame
(``authorization.accessToken``), audio goes as raw 16-bit mono PCM @ 24 kHz
binary frames, the stream ends with ``{"type": "endStream"}``, and the server
answers with JSON ``transcript`` / ``error`` events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass

import httpx
from livekit import rtc
from livekit.agents import APIError, stt
from livekit.agents.language import LanguageCode
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    NotGivenOr,
)
from livekit.agents.utils import AudioBuffer
from openai import AsyncOpenAI

logger = logging.getLogger("yaadhamma.meta")

META_BASE_URL = "https://api.meta.ai/v1"
META_STT_WS_URL = "wss://api.meta.ai/v1/asr/realtime"
# The recipe's audio contract: 16-bit little-endian mono PCM at 24 kHz.
_STT_SAMPLE_RATE = 24_000
_STT_AUDIO_ENCODING = "PCM_24KHZ"
# Meta closes the session once received audio falls ~10 s behind its wall
# clock ("ingress audio slower than real-time"), which on a Mac almost always
# means the microphone isn't delivering (permission prompt missed/denied).
# Warn loudly if no audio arrives shortly after the handshake so the cause is
# obvious instead of a bare server error.
_NO_AUDIO_WARN_AFTER_S = 5.0
_NO_AUDIO_WARN_THROTTLE_S = 60.0


class MetaConfigError(RuntimeError):
    """Raised when the Meta Model API cannot be used (usually a missing key)."""


@dataclass
class ModelTurn:
    """One model reply, reduced to what the orchestrator needs."""

    calls: list[tuple[str, dict]]
    text: str
    # Legacy payload kept for the old Gemini loop; the Meta loop ignores it.
    content: object | None = None
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class MetaConfig:
    """Settings for the Meta Model API. Read from YAADHAMMA_* env vars."""

    api_key: str | None = None
    base_url: str = META_BASE_URL
    brain_model: str = "muse-spark-1.3"
    voice_model: str = "muse-spark-1.3"
    escalation_model: str = "muse-spark-1.3"
    escalation_effort: str = "high"
    stt_model: str = "muse-voice-transcribe-1.0"
    stt_ws_url: str = META_STT_WS_URL
    stt_mode: str = "ENDPOINTING"  # PUSH_TO_TALK | ENDPOINTING | DIARIZATION
    voice_mode: str = "pipeline"  # "pipeline" | "realtime" (old Gemini Live path)
    tts_model: str = "fishaudio/s2.1-pro"
    tts_voice: str = "933563129e564b19a115bedd57b7406a"  # Sarah (Jeevan's pick)

    @classmethod
    def from_env(cls) -> MetaConfig:
        """Read YAADHAMMA_* from the environment (fresh on every call)."""
        import config  # local import: config loads .env.local at import time

        get = os.environ.get
        return cls(
            api_key=get("YAADHAMMA_MODEL_API_KEY") or None,
            base_url=get("YAADHAMMA_MODEL_API_URL", config.META_BASE_URL),
            brain_model=get("YAADHAMMA_BRAIN_MODEL", config.BRAIN_MODEL),
            voice_model=get("YAADHAMMA_VOICE_MODEL", config.VOICE_MODEL),
            escalation_model=get("YAADHAMMA_ESCALATION_MODEL", config.ESCALATION_MODEL),
            escalation_effort=get(
                "YAADHAMMA_ESCALATION_EFFORT", config.ESCALATION_EFFORT
            ),
            stt_model=get("YAADHAMMA_STT_MODEL", config.STT_MODEL),
            stt_mode=get("YAADHAMMA_STT_MODE", config.STT_MODE).strip().upper(),
            voice_mode=get("YAADHAMMA_VOICE_MODE", config.VOICE_MODE).strip().lower(),
            tts_model=get("YAADHAMMA_TTS_MODEL", config.TTS_MODEL),
            tts_voice=get("YAADHAMMA_TTS_VOICE", config.TTS_VOICE),
        )


def create_async_client(cfg: MetaConfig | None = None) -> AsyncOpenAI:
    """Build the OpenAI-compatible client for api.meta.ai.

    Raises MetaConfigError if YAADHAMMA_MODEL_API_KEY is missing. Importing
    this module never raises; only building the client does.
    """
    cfg = cfg or MetaConfig.from_env()
    if not cfg.api_key:
        raise MetaConfigError(
            "YAADHAMMA_MODEL_API_KEY is not set. Create a key at "
            "https://dev.meta.ai and put it in .env.local "
            "(never in chat or in code)."
        )
    return AsyncOpenAI(base_url=cfg.base_url, api_key=cfg.api_key)


class MetaBrainClient:
    """Chat-completions brain for the orchestrator.

    The HTTP client is built lazily so importing this module and running the
    test suite never needs an API key.
    """

    def __init__(
        self,
        cfg: MetaConfig | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._cfg = cfg
        self._client = client

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = create_async_client(self._cfg)
        return self._client

    async def generate(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        reasoning_effort: str | None = None,
    ) -> ModelTurn:
        client = self._get_client()
        kwargs: dict = {"model": model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        if reasoning_effort:
            kwargs["extra_body"] = {"reasoning_effort": reasoning_effort}
        response = await client.chat.completions.create(**kwargs)
        message = response.choices[0].message
        calls = []
        for tool_call in message.tool_calls or []:
            try:
                args = json.loads(tool_call.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError, AttributeError):
                args = {}
            calls.append((tool_call.function.name, args))
        usage = response.usage
        return ModelTurn(
            calls=calls,
            text=message.content or "",
            tokens_in=usage.prompt_tokens if usage else 0,
            tokens_out=usage.completion_tokens if usage else 0,
        )


def _language(code: str) -> LanguageCode:
    try:
        return LanguageCode(code)
    except ValueError:
        return LanguageCode("en")


def _parse_message(raw: object) -> tuple[stt.SpeechEventType, str] | str | None:
    """Turn one websocket frame into (event type, text), "start"/"end", or None.

    Meta's live protocol (observed 2026-09-24):
      {"type": "speechStart", ...}                    -> "start"
      {"type": "transcript", "transcript", "final"}    -> interim/final transcript
      {"type": "speechEnd", ...}                      -> "end"
      {"type": "speechComplete", "transcript": ...}    -> FINAL_TRANSCRIPT
    The finished utterance text arrives on "speechComplete", not as
    "final": true, so that frame must map to FINAL_TRANSCRIPT or LiveKit
    never commits the user turn and the agent never replies.
    Unknown frames (e.g. "audioProgress") return None.
    """
    try:
        msg = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else None
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(msg, dict):
        return None
    kind = str(msg.get("type", "")).lower()
    if kind in ("speechstart", "start_of_speech"):
        return "start"
    if kind in ("speechend", "end_of_speech", "speech_end", "endpoint"):
        return "end"

    def _text() -> str | None:
        text = msg.get("text") or msg.get("transcript")
        alternatives = msg.get("alternatives")
        if not text and isinstance(alternatives, list) and alternatives:
            first = alternatives[0] or {}
            text = first.get("transcript", first.get("text"))
        return str(text) if text else None

    if kind in ("speechcomplete", "utterance_end"):
        # Meta's end-of-utterance frame carries the complete text.
        return (stt.SpeechEventType.FINAL_TRANSCRIPT, _text() or "")
    text = _text()
    if not text:
        return None
    final = bool(msg.get("final", msg.get("is_final", False)))
    event = (
        stt.SpeechEventType.FINAL_TRANSCRIPT
        if final
        else stt.SpeechEventType.INTERIM_TRANSCRIPT
    )
    return (event, text)


class MetaRealtimeSTT(stt.STT):
    """Speech-to-text over Meta's Voice Transcribe realtime websocket."""

    def __init__(self, cfg: MetaConfig | None = None) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=True, interim_results=True)
        )
        self._cfg = cfg or MetaConfig.from_env()
        self._last_no_audio_warn = 0.0

    @property
    def model(self) -> str:
        return self._cfg.stt_model

    @property
    def provider(self) -> str:
        return "meta"

    def _require_key(self) -> str:
        if not self._cfg.api_key:
            raise MetaConfigError(
                "YAADHAMMA_MODEL_API_KEY is not set; the Meta STT cannot start."
            )
        return self._cfg.api_key

    def _ws_url(self) -> str:
        """Realtime URL with a fresh session id, per Meta's recipe."""
        return f"{self._cfg.stt_ws_url}?sessionId=stream-{uuid.uuid4()}"

    async def _no_audio_watchdog(self, first_audio: asyncio.Event) -> None:
        """Warn loudly if the mic delivers nothing after the handshake.

        Meta kills the stream ~10 s later ("ingress audio slower than
        real-time"); on a Mac that almost always means the microphone
        permission prompt was missed or denied.
        """
        try:
            await asyncio.wait_for(first_audio.wait(), timeout=_NO_AUDIO_WARN_AFTER_S)
        except TimeoutError:
            now = time.monotonic()
            if now - self._last_no_audio_warn >= _NO_AUDIO_WARN_THROTTLE_S:
                self._last_no_audio_warn = now
                logger.warning(
                    "Meta STT: no microphone audio arrived %ss after connecting. "
                    "If macOS asked for microphone permission, allow it (System "
                    "Settings > Privacy & Security > Microphone); otherwise check "
                    "the input device. The stream will keep retrying.",
                    _NO_AUDIO_WARN_AFTER_S,
                )

    async def _ws_connect(self):
        """Open the realtime websocket. A seam so tests can inject a fake."""
        import websockets

        # The credential travels in the opening JSON frame, not in a header.
        return await websockets.connect(self._ws_url(), open_timeout=30)

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.RecognizeStream:
        self._require_key()
        return MetaRecognizeStream(
            stt=self,
            conn_options=conn_options,
            cfg=self._cfg,
            sample_rate=_STT_SAMPLE_RATE,
        )

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        """One-shot transcription via POST /v1/asr/transcribe."""
        key = self._require_key()
        wav = rtc.combine_audio_frames(buffer).to_wav_bytes()
        async with httpx.AsyncClient() as http:
            response = await http.post(
                f"{self._cfg.base_url}/asr/transcribe",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("audio.wav", wav, "audio/wav")},
                data={"model": self._cfg.stt_model},
                timeout=30.0,
            )
            response.raise_for_status()
            payload = response.json()
        text = payload.get("text") or payload.get("transcript") or ""
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language=_language("en"), text=str(text))],
        )

    async def aclose(self) -> None:
        pass


def _raise_for_stt_error(raw: object) -> None:
    """Surface Meta ``{"type": "error"}`` frames as a retryable APIError.

    LiveKit's STT pump recreates the stream after a short backoff on
    APIError instead of killing the voice session, so a transient failure
    (e.g. the "ingress audio slower than real-time" close) heals itself.
    """
    try:
        msg = json.loads(raw) if isinstance(raw, str) else None
    except (json.JSONDecodeError, ValueError, TypeError):
        return
    if isinstance(msg, dict) and str(msg.get("type", "")).lower() == "error":
        raise APIError(
            f"Meta STT error: {msg.get('message', msg)}",
            body=msg,
            retryable=True,
        )


class _Pcm24kConverter:
    """Convert arbitrary input frames to 16-bit mono PCM @ 24 kHz.

    LiveKit hands the STT stream whatever the audio track delivers; Meta's
    recipe wants PCM_24KHZ. Anything already 24 kHz mono passes through
    untouched so the common path adds no latency.
    """

    def __init__(self) -> None:
        self._resampler: rtc.AudioResampler | None = None
        self._resampler_rate: int | None = None

    def push(self, frame: rtc.AudioFrame) -> list[rtc.AudioFrame]:
        import array

        samples = array.array("h", bytes(frame.data))  # int16 LE on x86/ARM
        channels = frame.num_channels or 1
        frames = len(samples) // channels
        if channels == 1:
            mono = samples
        else:
            mono = array.array(
                "h",
                (
                    sum(samples[i * channels : (i + 1) * channels]) // channels
                    for i in range(frames)
                ),
            )
        pcm = rtc.AudioFrame(
            data=mono.tobytes(),
            sample_rate=frame.sample_rate,
            num_channels=1,
            samples_per_channel=frames,
        )
        if frame.sample_rate == _STT_SAMPLE_RATE:
            return [pcm]
        if self._resampler is None or self._resampler_rate != frame.sample_rate:
            self._resampler = rtc.AudioResampler(
                input_rate=frame.sample_rate, output_rate=_STT_SAMPLE_RATE
            )
            self._resampler_rate = frame.sample_rate
        return list(self._resampler.push(pcm))


class MetaRecognizeStream(stt.RecognizeStream):
    """Streams PCM16 audio to Meta and emits transcript events."""

    def __init__(
        self,
        *,
        stt: MetaRealtimeSTT,
        conn_options: APIConnectOptions,
        cfg: MetaConfig,
        sample_rate: int | None = None,
    ) -> None:
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=sample_rate)
        self._cfg = cfg
        self._speaking = False

    async def _send_audio(self, ws, first_audio: asyncio.Event) -> None:
        convert = _Pcm24kConverter()
        async for data in self._input_ch:
            if isinstance(data, rtc.AudioFrame):
                if not first_audio.is_set():
                    first_audio.set()
                    logger.debug("Meta STT: first audio frame sent to websocket")
                for frame in convert.push(data):
                    await ws.send(bytes(frame.data))
            elif isinstance(data, stt.RecognizeStream._FlushSentinel):
                await ws.send(json.dumps({"type": "endStream"}))

    async def _run(self) -> None:
        meta_stt = self._stt
        assert isinstance(meta_stt, MetaRealtimeSTT)
        key = meta_stt._require_key()
        ws = await meta_stt._ws_connect()
        try:
            # 1. Handshake: the first JSON text frame carries the credential.
            await ws.send(
                json.dumps(
                    {
                        "authorization": {"accessToken": f"Bearer {key}"},
                        "audioEncoding": _STT_AUDIO_ENCODING,
                        "model": self._cfg.stt_model,
                        "mode": self._cfg.stt_mode,
                    }
                )
            )
            first_audio = asyncio.Event()
            send_task = asyncio.create_task(self._send_audio(ws, first_audio))
            watchdog = asyncio.create_task(self._stt._no_audio_watchdog(first_audio))
            try:
                async for raw in ws:
                    if isinstance(raw, (bytes, bytearray)):
                        continue  # binary frames carry no transcript events
                    _raise_for_stt_error(raw)
                    parsed = _parse_message(raw)
                    if parsed is None:
                        continue
                    if parsed == "start":
                        if not self._speaking:
                            self._speaking = True
                            self._event_ch.send_nowait(
                                stt.SpeechEvent(
                                    type=stt.SpeechEventType.START_OF_SPEECH
                                )
                            )
                        continue
                    if parsed == "end":
                        self._event_ch.send_nowait(
                            stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH)
                        )
                        self._speaking = False
                        continue
                    event_type, text = parsed
                    if not self._speaking:
                        self._speaking = True
                        self._event_ch.send_nowait(
                            stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH)
                        )
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(
                            type=event_type,
                            alternatives=[
                                stt.SpeechData(language=_language("en"), text=text)
                            ],
                        )
                    )
            finally:
                send_task.cancel()
                watchdog.cancel()
                await asyncio.gather(send_task, watchdog, return_exceptions=True)
        finally:
            await ws.close()
