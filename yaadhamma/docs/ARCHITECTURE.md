# Yaadhamma Architecture

Yaadhamma is a voice-first personal assistant that runs on Jeevan's Mac.
One codebase (`yaadhamma/`), three faces: a realtime voice loop, a
background brain that does multi-step work, and scheduled jobs (digest,
morning brief, overnight learning, file tidy).

## Voice

Microphone -> Gemini Live (gemini-3.8-live, speech-to-speech)
    -> Speaker

`src/agent.py` wires a LiveKit `AgentSession` to Gemini Live. The model
hears, thinks and speaks in one realtime session, so interruption and
barge-in work natively. The voice is Sulafat (Jeevan's pick).

(The old Meta pipeline was deleted in v1 Stage 1; there is no rollback.)

`src/latency.py` records per-reply latency (target < 1s) and a token cost
estimate to `~/.yaadhamma/voice_metrics.csv`.

## Background brain

`src/orchestrator.py` runs multi-step tasks through `GeminiBrainClient`
(`src/meta_client.py`), a thin wrapper over an OpenAI-compatible Chat
Completions endpoint:

- Routine work: `gemini-3.5-flash-lite`
- Escalation: `gemini-3.8-flash`
- After two consecutive tool failures the orchestrator escalates to the
  larger model, or raises reasoning effort when already on it.
- Old tool results are shrunk once more than two large results
  accumulate, keeping the latest two whole.

Every model call is recorded by `src/costs.py` into a `model_calls` table
in `~/.yaadhamma/yaadhamma.db` with timestamp, feature, model, token
counts and estimated USD. Estimates only; the provider billing page is
the truth. `YAADHAMMA_MONTHLY_BUDGET_USD` (default 35) sets the budget
the morning brief warns about. Recording never breaks a call and never
blocks one.

## Tools

The voice loop and the background brain share one tool surface, grouped
by module:

| Module | What it does |
|---|---|
| `file_tools.py` | Create, rename, move, copy, trash files and folders |
| `memory_tools.py` | Remember, recall, correct, forget, export memories |
| `gmail_tools.py`, `gmail.py` | Read and send Gmail (approval-gated) |
| `calendar_tools.py`, `gcal.py` | Read and create Google Calendar events |
| `whatsapp_tools.py`, `whatsapp.py` | WhatsApp Web chats and sending (approval-gated) |
| `mac_tools.py` | macOS control (open apps, volume, quit with confirmation) |
| `tools.py` | Web search, page reading, interview research |
| `planner.py` | Plans ("plan today / this week") from calendar, email, memory |
| `tidy.py` | File tidy-up proposals and application |
| `learning.py` | Overnight learning: the day's chats, email and calendar into memory |
| `commitment_tools.py`, `task_store.py` | Promises ("I'll call Ravi by 5pm") stored with parsed due dates; surfaced by voice sessions, the morning brief and digests. Explicit-only completion. |

## Always-on and remote control

- **Wake word** (`src/wake.py`): openWakeWord listens for "Hey Jarvis"
  locally (no account); an optional Picovoice engine supports a trained
  "Hey Yaadhamma" phrase. Option+Space also starts a conversation;
  90 seconds of silence closes the session. `scripts/yaadhamma_daemon.py`
  runs the wake loop at login via launchd; the UI is optional and can
  never take the voice loop down.
- **Menu-bar UI** (`src/ui.py`): icon + floating orb mirroring her state
  (listening, thinking, speaking, muted, error). Optional, headless with
  `YAADHAMMA_UI=off`.
- **Phone access** (`src/remote.py`): a 2-minute poll reads his own
  WhatsApp chats for messages starting with `Yaadhamma`, runs them through
  the real orchestrator (durable approvals, dedupe, same gates and audit),
  and answers back in the chat. Only the two configured own chats are
  read; 08:00–23:00 Mac time; `YAADHAMMA_REMOTE=off` disables it.

## Email tidy

`src/email_tidy.py` (run by the 3 am job): new mail is labelled
`Yaadhamma/Jobs`, `Yaadhamma/Saayam`, `Yaadhamma/Finance`,
`Yaadhamma/Receipts` or `Yaadhamma/Newsletters`; promotions and
newsletters older than 7 days are archived (INBOX label removed). Never
deletes, never marks read, never touches unlabelled mail or anything
from a person. Propose-only until `YAADHAMMA_EMAIL_TIDY=apply`; every
change is logged with how to reverse it. Label changes need the
`gmail.modify` scope on all accounts.

## Trust model

Three layers, all local:

1. **Untrusted-content boundary** (`src/untrusted.py`). Anything that
   arrives from the outside world (email, WhatsApp, calendar, web pages)
   is wrapped in `<<UNTRUSTED_CONTENT source="...">>` markers before a
   model ever sees it, and every instruction block tells the model that
   wrapped text is data to report on, never instructions to follow.
2. **Approval gates** (`src/permissions.py`). Every consequential action
   (sending a message or email, creating a calendar event, trashing a
   file) pauses for Jeevan's explicit approval. One approval = one
   action, 60-second expiry. Everything is written to
   `~/.yaadhamma/audit.jsonl`.
3. **Verification** (Stage 2 hardening). After a consequential action the
   agent re-checks reality: the file is really there, the memory reads
   back the same, the calendar event re-fetches, the WhatsApp message
   appears in the chat. `verified: false` means "unconfirmed", never
   "definitely failed". The audit log records the verification outcome.

Standing rule: memory is not permission. The agent remembering
something Jeevan said never authorizes acting on it later.

## Scheduled jobs (macOS launchd)

`scripts/digest_schedule.py` installs them; logs go to
`~/.yaadhamma/logs/`.

| Label | When | What |
|---|---|---|
| `com.yaadhamma.learn` | 2:00 am | Overnight learning into memory |
| `com.yaadhamma.tidy` | 3:00 am, every other night | File tidy-up (applies the plan by default) + email tidy |
| `com.yaadhamma.remote` | every 2 min, 08:00–23:00 | Phone access over WhatsApp |
| `com.yaadhamma.morning` | 8:45 am | Morning brief (calendar-first, no model for the core) |
| `com.yaadhamma.weekly` | Sunday 8:00 pm | Weekly plan |
| `com.yaadhamma.digest` | 9am, 1pm, 5pm, 9pm | Saayam digest: unread watched WhatsApp chats + new Gmail, summarized to his own "(You)" chat |

The digest only ever delivers to his own "(You)" chat; that is enforced
in code, not by prompt.

## Data locations (on the Mac)

| Path | Contents |
|---|---|
| `~/.yaadhamma/yaadhamma.db` | Tasks and the `model_calls` cost table |
| `~/.yaadhamma/memory.db` | Durable memories (SQLite + FTS5) |
| `~/.yaadhamma/commitments.db` | Promises with due dates (separate store) |
| `~/.yaadhamma/audit.jsonl` | Approval and verification audit log |
| `~/.yaadhamma/chrome-profile` | Dedicated Chromium profile for WhatsApp Web |
| `~/.yaadhamma/digest-profile` | Separate Chromium profile for the digest's browser |
| `~/.yaadhamma/voice_metrics.csv` | Per-reply latency and token estimates |
| `.env.local` (project root) | API keys, gitignored |

All AI inference bills through his Google AI Studio account.

## Test discipline

`pytest`, `ruff`, `uv`. One test file per step/feature. Core agent
behavior is written test-first. Model calls are never made in tests;
fake brain clients stand in. `scripts/status.py` gives the one-screen
cost and health view.
