"""Custom pronunciations for Yaadhamma's spoken output.

The Fish Audio model behind LiveKit Inference has no pronunciation lexicon,
so words it mispronounces are respelled just before synthesis. Transcripts
keep the original spelling; only the audio changes.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

from livekit.agents import inference, tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

# Spoken respellings, keyed by the written word.
PRONUNCIATIONS = {
    "Yaadhamma": "Yaah-dh-um-ah",
}

_LOOKUP = {k.lower(): v for k, v in PRONUNCIATIONS.items()}
_MAX_KEY_LEN = max(len(k) for k in PRONUNCIATIONS)

_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in PRONUNCIATIONS) + r")\b",
    re.IGNORECASE,
)


def apply_pronunciations(text: str) -> str:
    """Replace mispronounced words with their spoken respellings."""

    def _replace(match: re.Match[str]) -> str:
        return _LOOKUP[match.group(1).lower()]

    return _PATTERN.sub(_replace, text)


class _PronunciationStream:
    """Wraps a SynthesizeStream, respelling words before they are spoken.

    Tokens are buffered with a short holdback so a word split across two
    push_text calls is still respelled as one word.
    """

    def __init__(self, inner: tts.SynthesizeStream) -> None:
        self._inner = inner
        self._buf = ""

    def push_text(self, token: str) -> None:
        self._buf += token
        # Hold back enough trailing chars that a word split across the
        # emit/remainder boundary is always whole in the remainder.
        holdback = _MAX_KEY_LEN
        if len(self._buf) > holdback:
            emit = self._buf[: len(self._buf) - holdback]
            self._buf = self._buf[len(emit) :]
            self._inner.push_text(apply_pronunciations(emit))

    def flush(self) -> None:
        if self._buf:
            self._inner.push_text(apply_pronunciations(self._buf))
            self._buf = ""
        self._inner.flush()

    def end_input(self) -> None:
        self.flush()
        self._inner.end_input()

    async def aclose(self) -> None:
        await self._inner.aclose()

    def __aiter__(self) -> AsyncIterator[tts.SynthesizedAudio]:
        return self._inner.__aiter__()

    async def __anext__(self) -> tts.SynthesizedAudio:
        return await self._inner.__anext__()

    async def __aenter__(self) -> _PronunciationStream:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


class PronunciationTTS(inference.TTS):
    """LiveKit Inference TTS that applies PRONUNCIATIONS before synthesis."""

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> tts.ChunkedStream:
        return super().synthesize(apply_pronunciations(text), conn_options=conn_options)

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> _PronunciationStream:
        return _PronunciationStream(super().stream(conn_options=conn_options))
