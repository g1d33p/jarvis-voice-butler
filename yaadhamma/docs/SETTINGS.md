# Yaadhamma Settings Reference

Every `YAADHAMMA_*` environment variable, its safe default, and what it
does. All of them are optional; set the ones you need in `.env.local`
(project root, never committed). Anything not listed here is not read.

`scripts/selftest.py` checks that every variable the code reads is
documented here, so this page cannot drift out of date silently.

## Voice

| Variable | Default | What it does |
|---|---|---|
| `YAADHAMMA_MODE` | `split` | `split`: voice talks, background brain does multi-step tasks. `direct`: voice model does everything itself (older behaviour). |
| `YAADHAMMA_REALTIME_MODEL` | `gemini-3.8-live` | Speech-to-speech model for the voice loop. |
| `YAADHAMMA_REALTIME_VOICE` | `Sulafat` | Voice for Gemini Live (Jeevan's pick). |
| `YAADHAMMA_REALTIME_LANGUAGE` | _(empty)_ | BCP-47 language hint, e.g. `en-IN`. Empty = model default. |
| `YAADHAMMA_END_OF_SPEECH_MS` | `800` | Silence (ms) before the voice model treats an utterance as finished. |
| `YAADHAMMA_CONTEXT_TRIGGER_TOKENS` | `16000` | Compact the voice context once it passes this many tokens. |
| `YAADHAMMA_CONTEXT_TARGET_TOKENS` | `8000` | Compact the voice context down to roughly this many tokens. |
| `YAADHAMMA_LOCAL_VAD` | `on` | `off` disables local voice-activity detection. |

## Background brain

| Variable | Default | What it does |
|---|---|---|
| `YAADHAMMA_BRAIN_MODEL` | `gemini-3.5-flash-lite` | Cheap model for routine multi-step work. |
| `YAADHAMMA_ESCALATION_MODEL` | `gemini-3.8-flash` | Used after two tool failures in a row. |
| `YAADHAMMA_ESCALATION_EFFORT` | `high` | Reasoning effort on escalation. |
| `YAADHAMMA_LEARNING_MODEL` | _(escalation model)_ | Model for overnight learning (defaults to the stronger one). |
| `YAADHAMMA_MAX_TASK_STEPS` | `15` | Hard cap on steps per background task. |
| `YAADHAMMA_TASK_TIMEOUT_SECONDS` | `120` | Whole-task timeout. |
| `YAADHAMMA_TOOL_TIMEOUT_SECONDS` | `90` | Per-tool-call timeout. |
| `YAADHAMMA_MODEL_TIMEOUT_SECONDS` | `40` | Per-model-call timeout. |

## Wake word and daemon

| Variable | Default | What it does |
|---|---|---|
| `YAADHAMMA_WAKE` | `on` | `off` disables the always-on listener entirely. |
| `YAADHAMMA_WAKE_ENGINE` | `openwakeword` | `openwakeword` (free, "Hey Jarvis") or `porcupine` (custom phrase). |
| `YAADHAMMA_WAKE_PHRASE` | `hey jarvis` | Phrase the openWakeWord engine listens for. |
| `YAADHAMMA_WAKE_SENSITIVITY` | `0.5` | Detection sensitivity, 0–1. |
| `YAADHAMMA_WAKE_PPN` | _(empty)_ | Path to the trained `.ppn` file (Porcupine engine). |
| `YAADHAMMA_PICOVOICE_KEY` | _(empty)_ | Picovoice AccessKey (Porcupine engine). |
| `YAADHAMMA_WAKE_IDLE_TIMEOUT_S` | `90` | Seconds of silence before the session closes. |
| `YAADHAMMA_WAKE_SHORTCUT` | `on` | `off` disables the Option+Space shortcut. |

## Menu-bar UI

| Variable | Default | What it does |
|---|---|---|
| `YAADHAMMA_UI` | `on` | `off` runs the daemon headless (no menu bar, no orb). |
| `YAADHAMMA_UI_CAPTIONS` | `off` | `on` shows fading captions under the orb. |

## WhatsApp and phone access

| Variable | Default | What it does |
|---|---|---|
| `YAADHAMMA_WHATSAPP_WATCHLIST` | `Saayam,SC1,SC2,SC3` | Chats the digest watches (case-insensitive "name contains"). |
| `YAADHAMMA_SELF_CHAT_NUMBER` | _(empty)_ | Digits of his own "(You)" chat, where digests are delivered. |
| `YAADHAMMA_SELF_CHATS` | `19408438446,919640520634` | Own chats the remote poll may read; anything else is ignored. |
| `YAADHAMMA_REMOTE` | `on` | `off` disables phone access over WhatsApp. |

## Email

| Variable | Default | What it does |
|---|---|---|
| `YAADHAMMA_EMAIL_KEYWORDS` | `job,jobs,interview,recruiter,…` | Words that pull mail out of Gmail's Promotions/Social tabs into the digest. |
| `YAADHAMMA_EMAIL_UNREAD_ONLY` | _(empty)_ | `1`/`true`/`yes`: digest email covers unread mail only. |
| `YAADHAMMA_GOOGLE_CLIENT_ID` | _(empty)_ | Google Cloud OAuth client ID (Gmail + Calendar sign-in). |
| `YAADHAMMA_GOOGLE_CLIENT_SECRET` | _(empty)_ | Google Cloud OAuth client secret. |
| `YAADHAMMA_EMAIL_TIDY` | `propose` | `apply`: the nightly email tidy may label and archive. Anything else only proposes. |

## Calendar

| Variable | Default | What it does |
|---|---|---|
| `YAADHAMMA_TIMEZONE` | `America/Chicago` | Timezone for calendar display. |

## File tidy

| Variable | Default | What it does |
|---|---|---|
| `YAADHAMMA_TIDY` | `apply` | `propose`: the file tidy writes a plan and moves nothing. (Legacy `YAADHAMMA_TIDY_MODE` still works.) |

## Memory

| Variable | Default | What it does |
|---|---|---|
| `YAADHAMMA_MEMORY_PATH` | `~/.yaadhamma/memory.db` | Where durable memories live. |

## Audit

| Variable | Default | What it does |
|---|---|---|
| `YAADHAMMA_AUDIT_PATH` | `~/.yaadhamma/audit.jsonl` | Where approvals and verification outcomes are logged. |

## Planning

| Variable | Default | What it does |
|---|---|---|
| `YAADHAMMA_PLAN_WEB` | `on` | `off` disables the web look-up for interview prep. |

## Budget

| Variable | Default | What it does |
|---|---|---|
| `YAADHAMMA_MONTHLY_BUDGET_USD` | `35` | Monthly model-spend budget. The morning brief warns past it; it never blocks anything. |
