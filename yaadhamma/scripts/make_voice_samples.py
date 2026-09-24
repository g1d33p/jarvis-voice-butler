#!/usr/bin/env python3
"""Generate one audio sample per candidate TTS voice so Jeevan can listen and choose.

Uses the exact same LiveKit Inference TTS path as the agent
(``fishaudio/s2.1-pro``), so what you hear in the samples is what Yaadhamma
will sound like with that voice.

Reads LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET from ``.env.local``
(they never leave your machine).

Usage:  uv run python scripts/make_voice_samples.py
Output: voice-samples/<name>.wav  (mono WAV files you can play in Finder)
"""

import asyncio
import os
import sys
import wave
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv(".env.local")

from livekit.agents import inference, utils  # noqa: E402

SAMPLE_TEXT = (
    "Good morning, Sir. I'm Yaadhamma, your personal voice assistant. "
    "How may I help you today?"
)

TTS_MODEL = "fishaudio/s2.1-pro"
OUT_DIR = "voice-samples"


@dataclass(frozen=True)
class CandidateVoice:
    name: str
    voice_id: str
    blurb: str


# Female English voices from the Fish Audio library (the same library the
# LiveKit docs' suggested voices come from). `0-current` is the placeholder
# voice the agent uses today, included so you can compare against it.
CANDIDATE_VOICES = [
    CandidateVoice(
        "0-current",
        "fa4c9eb3dccc4806b382b40d61c6b10a",
        "Current placeholder voice (LiveKit docs example)",
    ),
    CandidateVoice(
        "1-sarah",
        "933563129e564b19a115bedd57b7406a",
        "Young, soft, gentle, conversational",
    ),
    CandidateVoice(
        "2-hannah",
        "9a9cf47702da476aa4629e2506d4a857",
        "Professional, confident, clear, friendly",
    ),
    CandidateVoice(
        "3-laura",
        "e3cd384158934cc9a01029cd7d278634",
        "Deep, warm, calm, professional",
    ),
    CandidateVoice(
        "4-selene",
        "b347db033a6549378b48d00acb0d06cd",
        "Soft, calm, meditative",
    ),
    CandidateVoice(
        "5-paula",
        "c2623f0c075b4492ac367989aee1576f",
        "Articulate, professional, confident",
    ),
    CandidateVoice(
        "6-friendly",
        "b545c585f631496c914815291da4e893",
        "Young, bright, energetic",
    ),
]


def _check_env() -> None:
    missing = [
        var
        for var in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
        if not os.environ.get(var)
    ]
    if missing:
        print(
            "Missing LiveKit credentials in .env.local: " + ", ".join(missing),
            file=sys.stderr,
        )
        sys.exit(1)


async def _synth_one(candidate: CandidateVoice, out_path: str) -> None:
    tts = inference.TTS(model=TTS_MODEL, voice=candidate.voice_id)
    async with tts:
        stream = tts.synthesize(SAMPLE_TEXT)
        frame = await stream.collect()
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(frame.num_channels)
        wf.setsampwidth(2)  # 16-bit PCM
        wf.setframerate(frame.sample_rate)
        wf.writeframes(frame.data)


async def main() -> int:
    _check_env()
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f'Synthesizing: "{SAMPLE_TEXT}"\n')
    # The inference TTS plugin needs an HTTP session when used outside the
    # agent worker (it normally gets one from the job context).
    async with utils.http_context.open():
        for candidate in CANDIDATE_VOICES:
            out_path = os.path.join(OUT_DIR, f"{candidate.name}.wav")
            print(f"[{candidate.name}] {candidate.blurb} ...", flush=True)
            try:
                await _synth_one(candidate, out_path)
            except Exception as exc:
                print(f"[{candidate.name}] FAILED: {exc}")
                continue
            print(f"[{candidate.name}] wrote {out_path}")
    print(f"\nDone. Play the files in ./{OUT_DIR}/ and pick your favourite.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
