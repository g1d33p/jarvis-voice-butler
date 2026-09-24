"""Settings that can be changed without editing code (via .env.local)."""

import os

from dotenv import load_dotenv

# Read .env.local here, so settings are in place before anything uses them.
load_dotenv(".env.local")

# "split": the voice model talks and a background text model does multi-step
# work (Phase 3). "direct": the voice model does everything itself, as before.
MODE = os.environ.get("YAADHAMMA_MODE", "split").strip().lower()

# Meta Model API (dev.meta.ai): Yaadhamma's brain and ears.
META_API_KEY = os.environ.get("YAADHAMMA_MODEL_API_KEY")
META_BASE_URL = os.environ.get(
    "YAADHAMMA_MODEL_API_URL", "https://api.meta.ai/v1"
).rstrip("/")

# "pipeline": Muse Spark (LLM) + Voice Transcribe (STT) + LiveKit TTS.
# "realtime": the old Gemini Live speech-to-speech path.
VOICE_MODE = os.environ.get("YAADHAMMA_VOICE_MODE", "pipeline").strip().lower()

# The voice brain: Muse Spark via the Meta Model API.
VOICE_MODEL = os.environ.get("YAADHAMMA_VOICE_MODEL", "muse-spark-1.3")

# The background "brain": Muse Spark, stronger when stuck (higher effort).
BRAIN_MODEL = os.environ.get("YAADHAMMA_BRAIN_MODEL", "muse-spark-1.3")
ESCALATION_MODEL = os.environ.get("YAADHAMMA_ESCALATION_MODEL", "muse-spark-1.3")
ESCALATION_EFFORT = os.environ.get("YAADHAMMA_ESCALATION_EFFORT", "high")

# Speech-to-text for the voice pipeline.
STT_MODEL = os.environ.get("YAADHAMMA_STT_MODEL", "muse-voice-transcribe-1.0")
# PUSH_TO_TALK | ENDPOINTING | DIARIZATION. ENDPOINTING lets the model itself
# decide when Jeevan has finished speaking.
STT_MODE = os.environ.get("YAADHAMMA_STT_MODE", "ENDPOINTING")

# Speech output (LiveKit Inference; Meta has no TTS). Jeevan picks the final
# voice in Phase 2; this feminine placeholder stands in until then.
TTS_MODEL = os.environ.get("YAADHAMMA_TTS_MODEL", "fishaudio/s2.1-pro")
TTS_VOICE = os.environ.get("YAADHAMMA_TTS_VOICE", "fa4c9eb3dccc4806b382b40d61c6b10a")

# Limits for one task.
MAX_TASK_STEPS = int(os.environ.get("YAADHAMMA_MAX_TASK_STEPS", "15"))
TASK_TIMEOUT_SECONDS = float(os.environ.get("YAADHAMMA_TASK_TIMEOUT_SECONDS", "120"))
MODEL_TIMEOUT_SECONDS = float(os.environ.get("YAADHAMMA_MODEL_TIMEOUT_SECONDS", "40"))
