"""One-shot voice-note transcription with Gemini (v2 Stage 3).

WhatsApp voice notes arrive as audio bytes downloaded through the existing
browser session; this module turns them into text with config.BRAIN_MODEL.
Live-only: it needs GOOGLE_API_KEY and makes a paid call, so tests inject
a fake transcriber into RemotePoller and never touch this.
"""

from __future__ import annotations

import logging

log = logging.getLogger("yaadhamma.voice_transcribe")


class TranscriptionError(Exception):
    """The audio could not be turned into text."""


def transcribe_voice_note(
    audio: bytes, *, mime_type: str = "audio/ogg; codecs=opus"
) -> str:
    """Transcribe one voice note. Raises TranscriptionError on any failure.

    Never returns an empty string: silence or an unintelligible note is an
    honest failure, not an empty command.
    """
    if not audio:
        raise TranscriptionError("no audio to transcribe")
    import config

    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise TranscriptionError(f"transcription library missing: {exc}") from exc

    model = config.BRAIN_MODEL
    try:
        client = genai.Client()
        response = client.models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=audio, mime_type=mime_type),
                "Transcribe this voice note word for word. Reply with only "
                "the transcription, no commentary.",
            ],
        )
        text = (response.text or "").strip()
    except Exception as exc:
        raise TranscriptionError(f"transcription failed: {exc}") from exc
    if not text:
        raise TranscriptionError("transcription came back empty")
    return text


def log_voice_note_cost(duration_s: float | None, transcript: str) -> None:
    """Record the estimated transcription cost under feature 'voice_note'.

    Audio tokens are estimated at ~32 per second of audio (Gemini audio
    tokenisation); the text estimate is rough and the provider's billing
    page is the truth. Never raises.
    """
    try:
        import config
        from costs import CostStore

        tokens_in = int((duration_s or 0.0) * 32)
        tokens_out = max(1, len(transcript) // 4)
        CostStore().record(
            feature="voice_note",
            model=config.BRAIN_MODEL,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
    except Exception:
        log.warning("could not log voice-note cost", exc_info=True)
