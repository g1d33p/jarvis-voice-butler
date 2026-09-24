"""Meta Model API client: Yaadhamma's brain (Muse Spark) and ears (Voice Transcribe).

Meta exposes OpenAI-compatible surfaces at https://api.meta.ai/v1, so the
brain uses plain Chat Completions through the ``openai`` SDK. Speech-to-text
goes over the Voice Transcribe realtime websocket
(``wss://api.meta.ai/v1/asr/realtime``). Meta offers no TTS, so speech output
stays on LiveKit Inference (see agent.py).

The realtime STT wire protocol below follows the endpoint published on
dev.meta.ai (Bearer auth, audio frames one way, JSON transcript events back).
The exact JSON frame shapes are isolated in ``_parse_message`` — confirm them
against the Voice Transcribe realtime guide before first live use.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass

import httpx
from livekit import rtc
from livekit.agents import stt
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
_STT_SAMPLE_RATE = 16_000


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
    voice_mode: str = "pipeline"  # "pipeline" | "realtime" (old Gemini Live path)
    tts_model: str = "fishaudio/s2.1-pro"
    tts_voice: str = "fa4c9eb3dccc4806b382b40d61c6b10a"

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
    """Turn one websocket frame into (event type, text), "end", or None.

    Accepts Meta's {"type": "transcript", "text", "final"} shape plus a couple
    of common provider shapes, defensively — unknown frames return None.
    """
    try:
        msg = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else None
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(msg, dict):
        return None
    kind = str(msg.get("type", "")).lower()
    if kind in ("end_of_speech", "speech_end", "endpoint"):
        return "end"
    text = msg.get("text") or msg.get("transcript")
    final = bool(msg.get("final", msg.get("is_final", False)))
    alternatives = msg.get("alternatives")
    if not text and isinstance(alternatives, list) and alternatives:
        first = alternatives[0] or {}
        text = first.get("transcript", first.get("text"))
    if not text:
        return None
    event = (
        stt.SpeechEventType.FINAL_TRANSCRIPT
        if final
        else stt.SpeechEventType.INTERIM_TRANSCRIPT
    )
    return (event, str(text))


class MetaRealtimeSTT(stt.STT):
    """Speech-to-text over Meta's Voice Transcribe realtime websocket."""

    def __init__(self, cfg: MetaConfig | None = None) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=True, interim_results=True)
        )
        self._cfg = cfg or MetaConfig.from_env()

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

    async def _ws_connect(self):
        """Open the realtime websocket. A seam so tests can inject a fake."""
        import websockets

        return await websockets.connect(
            self._cfg.stt_ws_url,
            additional_headers={"Authorization": f"Bearer {self._require_key()}"},
        )

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

    async def _send_audio(self, ws) -> None:
        async for data in self._input_ch:
            if isinstance(data, rtc.AudioFrame):
                await ws.send(bytes(data.data))
            elif isinstance(data, stt.RecognizeStream._FlushSentinel):
                await ws.send(json.dumps({"type": "flush"}))

    async def _run(self) -> None:
        meta_stt = self._stt
        assert isinstance(meta_stt, MetaRealtimeSTT)
        ws = await meta_stt._ws_connect()
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "start",
                        "model": self._cfg.stt_model,
                        "sample_rate": _STT_SAMPLE_RATE,
                        "format": "pcm_s16le",
                        "channels": 1,
                        "language": "en",
                        "interim_results": True,
                    }
                )
            )
            send_task = asyncio.create_task(self._send_audio(ws))
            try:
                async for raw in ws:
                    parsed = _parse_message(raw)
                    if parsed is None:
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
                await asyncio.gather(send_task, return_exceptions=True)
        finally:
            await ws.close()
