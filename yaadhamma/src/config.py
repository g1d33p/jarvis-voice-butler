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

# "realtime" (default): Gemini Live hears, thinks and speaks in one model.
# "pipeline": the Meta path (Muse Spark + Voice Transcribe + LiveKit TTS),
# kept for two weeks as a rollback, then removed.
VOICE_MODE = os.environ.get("YAADHAMMA_VOICE_MODE", "realtime").strip().lower()

# Gemini Live voice (realtime mode).
REALTIME_MODEL = os.environ.get("YAADHAMMA_REALTIME_MODEL", "gemini-3.8-live")
# Jeevan picked Sulafat (2026-09-25, after trying Gacrux).
REALTIME_VOICE = os.environ.get("YAADHAMMA_REALTIME_VOICE", "Sulafat")
# Empty = let Gemini detect the language (best for English/Telugu mixing).
REALTIME_LANGUAGE = os.environ.get("YAADHAMMA_REALTIME_LANGUAGE", "").strip()
# How long a pause (ms) before Gemini decides he has finished speaking.
# Longer = fewer sentences cut in half, slightly slower replies.
END_OF_SPEECH_SILENCE_MS = int(os.environ.get("YAADHAMMA_END_OF_SPEECH_MS", "800"))
# Keep only a recent window of the conversation, because Gemini Live re-bills
# the whole conversation on every reply. Trigger/target are in tokens.
CONTEXT_TRIGGER_TOKENS = int(
    os.environ.get("YAADHAMMA_CONTEXT_TRIGGER_TOKENS", "16000")
)
CONTEXT_TARGET_TOKENS = int(os.environ.get("YAADHAMMA_CONTEXT_TARGET_TOKENS", "8000"))
# A small voice detector on the Mac, used to time replies accurately.
# Set to "off" if she ever interrupts herself.
LOCAL_VAD = os.environ.get("YAADHAMMA_LOCAL_VAD", "on").strip().lower() != "off"
LOCAL_VAD_SILENCE_S = 0.25

# The voice brain: Muse Spark via the Meta Model API.
VOICE_MODEL = os.environ.get("YAADHAMMA_VOICE_MODEL", "muse-spark-1.3")

# The background "brain" for multi-step tasks.
# "gemini" (default since 2026-09-25): cheap, fast Gemini via Google's
# OpenAI-compatible API. "meta": Muse Spark (rollback only).
BRAIN_PROVIDER = os.environ.get("YAADHAMMA_BRAIN_PROVIDER", "gemini").strip().lower()
_BRAIN_DEFAULTS = {
    "gemini": ("gemini-3.5-flash-lite", "gemini-3.8-flash"),
    "meta": ("muse-spark-1.3", "muse-spark-1.3"),
}
_brain, _escalation = _BRAIN_DEFAULTS.get(BRAIN_PROVIDER, _BRAIN_DEFAULTS["gemini"])


def _model_setting(name: str, default: str) -> str:
    """Read a model name, ignoring one left over from the other provider.

    An old .env.local may still say muse-spark-1.3; sending that to Gemini
    would fail, so a name that does not match the provider falls back.
    """
    value = os.environ.get(name, "").strip()
    prefix = "gemini" if BRAIN_PROVIDER == "gemini" else "muse"
    return value if value.startswith(prefix) else default


BRAIN_MODEL = _model_setting("YAADHAMMA_BRAIN_MODEL", _brain)
# Used after two failed steps in a row.
ESCALATION_MODEL = _model_setting("YAADHAMMA_ESCALATION_MODEL", _escalation)
ESCALATION_EFFORT = os.environ.get("YAADHAMMA_ESCALATION_EFFORT", "high")

# Speech-to-text for the voice pipeline.
STT_MODEL = os.environ.get("YAADHAMMA_STT_MODEL", "muse-voice-transcribe-1.0")
# PUSH_TO_TALK | ENDPOINTING | DIARIZATION. ENDPOINTING lets the model itself
# decide when Jeevan has finished speaking.
STT_MODE = os.environ.get("YAADHAMMA_STT_MODE", "ENDPOINTING")

# Speech output (LiveKit Inference; Meta has no TTS). Jeevan picked Sarah
# in Phase 2; override with YAADHAMMA_TTS_VOICE to try another voice.
TTS_MODEL = os.environ.get("YAADHAMMA_TTS_MODEL", "fishaudio/s2.1-pro")
TTS_VOICE = os.environ.get("YAADHAMMA_TTS_VOICE", "933563129e564b19a115bedd57b7406a")

# Limits for one task.
MAX_TASK_STEPS = int(os.environ.get("YAADHAMMA_MAX_TASK_STEPS", "15"))
TASK_TIMEOUT_SECONDS = float(os.environ.get("YAADHAMMA_TASK_TIMEOUT_SECONDS", "120"))
# Longest a single tool call inside a task may run.
TOOL_TIMEOUT_SECONDS = float(os.environ.get("YAADHAMMA_TOOL_TIMEOUT_SECONDS", "90"))
MODEL_TIMEOUT_SECONDS = float(os.environ.get("YAADHAMMA_MODEL_TIMEOUT_SECONDS", "40"))

# WhatsApp chats Jeevan wants eyes on (case-insensitive "name contains").
# Comma-separated; change in .env.local without touching code.
WHATSAPP_WATCHLIST = [
    part.strip()
    for part in os.environ.get(
        "YAADHAMMA_WHATSAPP_WATCHLIST", "Saayam,SC1,SC2,SC3"
    ).split(",")
    if part.strip()
]

# His own "(You)" WhatsApp chat, found by its phone digits (the digest is
# delivered there). Set YAADHAMMA_SELF_CHAT_NUMBER in .env.local.
SELF_CHAT_NUMBER = "".join(
    ch for ch in os.environ.get("YAADHAMMA_SELF_CHAT_NUMBER", "") if ch.isdigit()
)

# Words that pull an email out of Gmail's Promotions/Social tabs into the
# digest (job mail often lands there).
EMAIL_KEYWORDS = [
    w.strip()
    for w in os.environ.get(
        "YAADHAMMA_EMAIL_KEYWORDS",
        "job,jobs,interview,recruiter,recruiting,hiring,job offer,application,"
        "applied,position,opportunity,Saayam",
    ).split(",")
    if w.strip()
]

# Digest email: include emails he already opened (default) or only unread.
EMAIL_UNREAD_ONLY = os.environ.get("YAADHAMMA_EMAIL_UNREAD_ONLY", "").strip() in {
    "1",
    "true",
    "yes",
}

# Overnight learning reads a day of messages and must judge what matters, so
# it uses the stronger Flash model (a few cents a night).
LEARNING_MODEL = os.environ.get("YAADHAMMA_LEARNING_MODEL", ESCALATION_MODEL)
