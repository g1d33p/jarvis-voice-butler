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
def _self_chat_number() -> str:
    """Phone digits for his own chat. Rejects implausible values loudly: the
    classic .env.local corruption (a setting appended without a preceding
    newline) glues two values into one over-long digit string, which used to
    surface later as a confusing "no chat found" error."""
    raw = os.environ.get("YAADHAMMA_SELF_CHAT_NUMBER", "")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) > 15:  # E.164 phone numbers are at most 15 digits
        raise ValueError(
            f"YAADHAMMA_SELF_CHAT_NUMBER in .env.local has {len(digits)} "
            f"digits ({digits}): a phone number is at most 15 digits. The "
            "file was probably corrupted by appending a setting without a "
            "preceding newline — edit .env.local in a text editor and fix "
            "the value."
        )
    return digits


SELF_CHAT_NUMBER = _self_chat_number()

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

# Nightly file tidy-up: "apply" moves files per the reviewed plan (default);
# "propose" only writes a plan for review. Nothing is ever deleted either way.
# YAADHAMMA_TIDY=propose goes back to propose-only.
_tidy_raw = (
    os.environ.get("YAADHAMMA_TIDY", os.environ.get("YAADHAMMA_TIDY_MODE", "apply"))
    .strip()
    .lower()
)
TIDY_MODE = "propose" if _tidy_raw == "propose" else "apply"

# Planning: a short web look (official site, recent news) for interview prep.
PLAN_WEB_RESEARCH = os.environ.get("YAADHAMMA_PLAN_WEB", "on").strip().lower() != "off"

# Monthly model-spend budget, USD. When the month-to-date estimate passes
# this, the morning brief says so loudly — but calls are never blocked.
MONTHLY_BUDGET_USD = float(os.environ.get("YAADHAMMA_MONTHLY_BUDGET_USD", "35"))


# Push-to-talk (v2 Stage 1): hold a key to talk instead of the wake word.
# Read fresh from the environment on each call so tests can reconfigure it.
def ptt_settings() -> dict:
    """Push-to-talk settings. Every key has a safe default; the whole thing
    is optional and YAADHAMMA_PTT=off disables it entirely."""
    return {
        "enabled": os.environ.get("YAADHAMMA_PTT", "on").strip().lower() != "off",
        "key": os.environ.get("YAADHAMMA_PTT_KEY", "cmd_r").strip().lower(),
        "hold_ms": int(os.environ.get("YAADHAMMA_PTT_HOLD_MS", "200")),
        "idle_timeout_s": float(os.environ.get("YAADHAMMA_IDLE_TIMEOUT_S", "20")),
    }


# Wake word (v1 Stage 3): always-on local listening. Read fresh from the
# environment on each call (not a module constant) so tests can reconfigure
# it and the daemon can pick up .env.local changes on restart.
def wake_settings() -> dict:
    """Wake-word settings. Every key has a safe default; the whole thing is
    optional and YAADHAMMA_WAKE=off disables it entirely.

    v2: the wake word is OFF by default (push-to-talk is the primary input),
    and the idle timeout is the shared YAADHAMMA_IDLE_TIMEOUT_S (20 s, was
    the wake-specific YAADHAMMA_WAKE_IDLE_TIMEOUT_S at 90 s)."""
    return {
        "enabled": os.environ.get("YAADHAMMA_WAKE", "off").strip().lower() != "off",
        "engine": os.environ.get("YAADHAMMA_WAKE_ENGINE", "openwakeword")
        .strip()
        .lower(),
        "phrase": os.environ.get("YAADHAMMA_WAKE_PHRASE", "hey jarvis").strip().lower(),
        "sensitivity": float(os.environ.get("YAADHAMMA_WAKE_SENSITIVITY", "0.5")),
        "picovoice_key": os.environ.get("YAADHAMMA_PICOVOICE_KEY", "").strip(),
        "keyword_path": os.environ.get("YAADHAMMA_WAKE_PPN", "").strip(),
        "idle_timeout_s": float(os.environ.get("YAADHAMMA_IDLE_TIMEOUT_S", "20")),
        "shortcut": os.environ.get("YAADHAMMA_WAKE_SHORTCUT", "on").strip().lower()
        != "off",
    }


def _parse_hours(raw: str) -> tuple:
    """Parse 'START-END' (24h) into a (start, end) window. Unparseable or
    degenerate input means all day — polling hours must never silently
    become "never" because of a typo in an env var."""
    try:
        start_s, end_s = raw.split("-", 1)
        start = max(0, min(24, int(start_s.strip())))
        end = max(0, min(24, int(end_s.strip())))
    except (ValueError, AttributeError):
        return (0, 24)
    if start == end:
        return (0, 24)
    return (start, end)


def remote_settings() -> dict:
    """Phone access over WhatsApp. YAADHAMMA_REMOTE=off disables it entirely.

    self_chats are his own chats only, as digit strings; a chat that does not
    resolve to one of these numbers is never polled, enforced in code.
    """
    raw = os.environ.get("YAADHAMMA_SELF_CHATS", "19408438446,919640520634")
    chats = ["".join(ch for ch in part if ch.isdigit()) for part in raw.split(",")]
    return {
        "enabled": os.environ.get("YAADHAMMA_REMOTE", "on").strip().lower() != "off",
        "self_chats": [c for c in chats if c],
        # Polling window, 24h "START-END". Stage 2 defaults this to all day;
        # Stage 1 keeps the v2 behaviour (08:00-23:00).
        "hours": _parse_hours(os.environ.get("YAADHAMMA_REMOTE_HOURS", "8-23")),
        # The resident poller tries headless first (invisible, no focus to
        # steal). Set to "off" to fall back to a visible off-screen window.
        "poller_headless": os.environ.get("YAADHAMMA_POLLER_HEADLESS", "on")
        .strip()
        .lower()
        != "off",
    }


def voice_note_settings() -> dict:
    """WhatsApp voice notes from his own chats.

    YAADHAMMA_VOICE_NOTE_MAX_S: notes longer than this are skipped with a
    single reply (default 60).
    """
    try:
        max_s = float(os.environ.get("YAADHAMMA_VOICE_NOTE_MAX_S", "60"))
    except ValueError:
        max_s = 60.0
    return {"max_s": max_s if max_s > 0 else 60.0}


def email_tidy_settings() -> dict:
    """Nightly Gmail labels and archiving. Safe default: propose-only.

    YAADHAMMA_EMAIL_TIDY=apply lets the run change labels and archive;
    anything else (including unset) only proposes.
    """
    return {
        "mode": "apply"
        if os.environ.get("YAADHAMMA_EMAIL_TIDY", "propose").strip().lower() == "apply"
        else "propose",
    }
