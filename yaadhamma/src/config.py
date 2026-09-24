"""Settings that can be changed without editing code (via .env.local)."""

import os

from dotenv import load_dotenv

# Read .env.local here, so settings are in place before anything uses them.
load_dotenv(".env.local")

# "split": the voice model talks and a background text model does multi-step
# work (Phase 3). "direct": the voice model does everything itself, as before.
MODE = os.environ.get("YAADHAMMA_MODE", "split").strip().lower()

# The background "brain": cheap and fast by default, stronger when stuck.
BRAIN_MODEL = os.environ.get("YAADHAMMA_BRAIN_MODEL", "gemini-3.5-flash-lite")
ESCALATION_MODEL = os.environ.get("YAADHAMMA_ESCALATION_MODEL", "gemini-3.6-flash")

# Limits for one task.
MAX_TASK_STEPS = int(os.environ.get("YAADHAMMA_MAX_TASK_STEPS", "15"))
TASK_TIMEOUT_SECONDS = float(os.environ.get("YAADHAMMA_TASK_TIMEOUT_SECONDS", "120"))
MODEL_TIMEOUT_SECONDS = float(os.environ.get("YAADHAMMA_MODEL_TIMEOUT_SECONDS", "40"))
