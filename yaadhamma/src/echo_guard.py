"""Echo guard: stop Yaadhamma from hearing her own voice as Jeevan.

Her TTS plays through the Mac speakers while the mic is live, so her own
spoken words can come back through speech-to-text as if Jeevan said them.
2026-09-24: her own fragments ("Seems the tool got stuck, sir.", "to be your
message about", "It was today.") were transcribed as user speech and
interrupted her own turns.

The guard keeps the last few seconds of her spoken text and drops final
transcripts that are near-verbatim matches/substrings of it. Only final
transcripts are filtered (they are what commit user turns); interims are
left alone. Matching is normalized and time-bounded, and short matches are
ignored, so genuine barge-in still works: Jeevan interrupting with his own
words never looks like an echo.

Wiring (see agent.py): Assistant.stt_node runs every final transcript
through filter_echo_events, and a session "conversation_item_added" listener
feeds her spoken text into EchoGuard.note_assistant_text.
"""

from __future__ import annotations

import logging
import re
import string
import time
from collections import deque
from collections.abc import AsyncIterable
from difflib import SequenceMatcher

from livekit.agents import stt

logger = logging.getLogger("yaadhamma.echo")

_PUNCT_RE = re.compile(f"[{re.escape(string.punctuation)}]")
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = _PUNCT_RE.sub(" ", (text or "").lower())
    return _WS_RE.sub(" ", text).strip()


def _is_near_verbatim(
    transcript: str, spoken: str, min_chars: int, similarity: float
) -> bool:
    """True when the transcript is a near-verbatim echo of spoken text.

    Both inputs must already be normalized. A substring match covers the
    common case (STT returns a fragment of what she said); the similarity
    fallback covers STT mishearing a word or two.
    """
    if not transcript or not spoken:
        return False
    shorter, longer = (
        (transcript, spoken) if len(transcript) <= len(spoken) else (spoken, transcript)
    )
    if len(shorter) < min_chars:
        return False
    if shorter in longer:
        return True
    return SequenceMatcher(None, shorter, longer).ratio() >= similarity


class EchoGuard:
    """Remembers recent assistant utterances; recognizes their echoes."""

    def __init__(
        self,
        window_s: float = 30.0,
        min_match_chars: int = 10,
        similarity: float = 0.9,
        clock=time.monotonic,
    ) -> None:
        self._window_s = window_s
        self._min_match_chars = min_match_chars
        self._similarity = similarity
        self._clock = clock
        self._recent: deque[tuple[float, str]] = deque(maxlen=20)

    def note_assistant_text(self, text: str) -> None:
        """Record text Yaadhamma just said (or is about to say)."""
        normalized = _normalize(text)
        if normalized:
            self._recent.append((self._clock(), normalized))

    def is_echo(self, transcript: str) -> bool:
        """True when the transcript looks like her own just-spoken words."""
        normalized = _normalize(transcript)
        if len(normalized) < self._min_match_chars:
            return False
        now = self._clock()
        while self._recent and now - self._recent[0][0] > self._window_s:
            self._recent.popleft()
        return any(
            _is_near_verbatim(
                normalized, spoken, self._min_match_chars, self._similarity
            )
            for _, spoken in self._recent
        )


async def filter_echo_events(
    events: AsyncIterable[stt.SpeechEvent | str], guard: EchoGuard
):
    """Yield STT events, dropping final transcripts that are self-echo.

    Non-final events and non-SpeechEvent items pass through untouched.
    """
    async for event in events:
        if (
            isinstance(event, stt.SpeechEvent)
            and event.type == stt.SpeechEventType.FINAL_TRANSCRIPT
            and event.alternatives
            and guard.is_echo(event.alternatives[0].text or "")
        ):
            logger.info(
                "echo guard: dropped self-echo transcript %r",
                (event.alternatives[0].text or "")[:80],
            )
            continue
        yield event
