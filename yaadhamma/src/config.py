"""Settings that can be changed without editing code (via .env.local)."""

import os

from dotenv import load_dotenv

# Read .env.local here, so settings are in place before anything uses them.
load_dotenv(".env.local")

# "split": the voice model talks and a background text model does multi-step
# work (Phase 3). "direct": the voice model does everything itself, as before.
MODE = os.environ.get("YAADHAMMA_MODE", "split").strip().lower()

# Voice: Gemini Live speech-to-speech only (the Meta pipeline was deleted in
# v1 Stage 1 by the owner's 2026-09-28 decision).

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

# The voice brain is Gemini Live itself (speech-to-speech). The background
# "brain" for multi-step tasks is the cheaper Gemini text models.
_BRAIN_DEFAULTS = {
    "gemini": ("gemini-3.5-flash-lite", "gemini-3.8-flash"),
}
BRAIN_PROVIDER = "gemini"
_brain, _escalation = _BRAIN_DEFAULTS[BRAIN_PROVIDER]


def _model_setting(name: str, default: str) -> str:
    """Read a model name, ignoring one left over from the old Meta setup.

    An old .env.local may still say muse-spark-1.3; sending that to Gemini
    would fail, so a name that does not start with "gemini" falls back.
    """
    value = os.environ.get(name, "").strip()
    return value if value.startswith("gemini") else default


BRAIN_MODEL = _model_setting("YAADHAMMA_BRAIN_MODEL", _brain)
# Used after two failed steps in a row.
ESCALATION_MODEL = _model_setting("YAADHAMMA_ESCALATION_MODEL", _escalation)
ESCALATION_EFFORT = os.environ.get("YAADHAMMA_ESCALATION_EFFORT", "high")

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

# Nightly file tidy-up: "propose" writes a plan for review and moves nothing;
# "apply" also moves the files. Start with propose (Jeevan, 2026-09-28).
TIDY_MODE = os.environ.get("YAADHAMMA_TIDY_MODE", "propose").strip().lower()

# Planning: a short web look (official site, recent news) for interview prep.
PLAN_WEB_RESEARCH = os.environ.get("YAADHAMMA_PLAN_WEB", "on").strip().lower() != "off"

# Monthly model-spend budget, USD. When the month-to-date estimate passes
# this, the morning brief says so loudly — but calls are never blocked.
MONTHLY_BUDGET_USD = float(os.environ.get("YAADHAMMA_MONTHLY_BUDGET_USD", "35"))
