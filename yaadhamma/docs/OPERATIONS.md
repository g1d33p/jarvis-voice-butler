# Yaadhamma Operations

Day-to-day running of the assistant on Jeevan's Mac. Everything here
assumes the project checked out at `~/jarvis-voice-butler` and Python
managed with `uv`.

## Quick health check

```bash
cd ~/jarvis-voice-butler/yaadhamma
uv run scripts/status.py
```

One screen: last 7 days of model spend per feature, month-to-date
against the budget, WhatsApp pairing, Gmail/Calendar sign-in, disk
space, and recent errors from the audit log. Read-only; changes nothing.

## Starting and stopping

- Voice agent: run `uv run src/agent.py` in a terminal (foreground, so
  approval prompts reach Jeevan).
- Always-on daemon (wake word): `python scripts/daemon_control.py install`
  starts her at login and now; `status` / `start` / `stop` / `uninstall`
  control it. Logs land in `~/.yaadhamma/daemon.log`. The daemon only runs
  the wake loop — scheduled jobs stay in their own launchd jobs, so a daemon
  crash cannot stop the digests.
- Scheduled jobs: installed with `uv run scripts/digest_schedule.py on`,
  removed with `... off`. They run headless via launchd; logs land in
  `~/.yaadhamma/logs/`.

## Wake word

Out of the box she listens for **"Hey Jarvis"** using openWakeWord —
no account, no training. `YAADHAMMA_WAKE=off` in `.env.local` disables the
whole always-on listener. `Option+Space` also starts a conversation
(needs `pip install pynput`); 90 seconds of silence closes the session.

### Custom wake phrase ("Hey Yaadhamma") via Picovoice Porcupine

The custom phrase needs a trained `.ppn` model and a free Picovoice
AccessKey. Steps, done once on the Mac:

1. Create a free account at https://console.picovoice.ai/ and copy the
   **AccessKey** from the dashboard.
2. Open the **Porcupine** page in the console, choose **macOS** as the
   platform, type the wake phrase exactly as `Hey Yaadhamma`, and train.
   Download the resulting `hey-yaadhamma.ppn` file (keep it somewhere
   permanent, e.g. `~/.yaadhamma/hey-yaadhamma.ppn` — the daemon reads it
   on every start).
3. Install the engine: `pip install pvporcupine sounddevice`.
4. In `.env.local`, set:
   ```
   YAADHAMMA_WAKE_ENGINE=porcupine
   YAADHAMMA_PICOVOICE_KEY=<the AccessKey from step 1>
   YAADHAMMA_WAKE_PPN=/Users/jeevan/.yaadhamma/hey-yaadhamma.ppn
   ```
5. Restart the daemon: `python scripts/daemon_control.py stop`
   then `python scripts/daemon_control.py start`.

Switching back is config-only: set `YAADHAMMA_WAKE_ENGINE=openwakeword`
and restart. No code changes either way.

Microphone permission: if the daemon logs a microphone error, open
System Settings > Privacy & Security > Microphone on the Mac, enable
access for the app running the daemon, and restart it. Real microphone
behaviour is unverified in the sandbox (see `docs/BUILD_REPORT.md`).

## Menu-bar UI

The daemon can show a menu-bar icon plus a small floating orb (dark
near-black/violet-blue, draggable, remembers its position). Install the UI
extra once: `pip install "yaadhamma[ui]"` (needs macOS; it pulls in rumps /
PyObjC).

- The icon mirrors her state: listening, thinking, speaking, muted, error.
- Menu: Start/Stop listening · Mute · Pause background jobs · Today's cost ·
  WhatsApp status · Open plans folder · Settings · Quit.
- `YAADHAMMA_UI=off` in `.env.local` runs the daemon headless. If the UI
  ever fails to start, the daemon logs a warning and keeps listening —
  a UI failure can never take the voice loop down.
- `YAADHAMMA_UI_CAPTIONS=on` shows the last utterance and reply as fading
  text beneath the orb (off by default).
- Rendering needs a real Mac and is unverified here; the state mapping and
  menu actions are covered by `tests/test_ui.py`.

## Phone access over WhatsApp

From his phone he sends a message in his own chat starting with `Yaadhamma`
(for example "Yaadhamma remind me to call mom at 6"). The remote poll runs
it through the orchestrator like a voice task — same approval gates,
verification and audit.

- Only his own chats are polled (`YAADHAMMA_SELF_CHATS`, default
  19408438446 and 919640520634); anything else is ignored entirely.
- Replies and approval questions come back in the same chat. Answering
  "yes"/"no" to a pending question resolves it.
- Approvals only stay open a minute: answer promptly, or she asks again and,
  after two rounds, tells him the approval expired and to send the command
  again.
- Polls every 2 minutes, 08:00–23:00 Mac time, as its own launchd job:
  `uv run scripts/remote_schedule.py on`. Uses the digest browser profile,
  never the voice one. `YAADHAMMA_REMOTE=off` disables it.
- Real WhatsApp reading/sending is unverified in the sandbox; the command
  parsing, chat allowlist, dedupe, approval round-trip and hours window are
  covered by `tests/test_remote.py`.

## Sign-in and pairing

| Service | Script | Notes |
|---|---|---|
| WhatsApp | `scripts/whatsapp_signin.py` | Stop the agent first (the Chromium profile is single-window), then scan the QR with the phone |
| Gmail | `scripts/gmail_signin.py` | One token per account label, owner-only permissions |
| Calendar | `scripts/calendar_signin.py` | Google OAuth, same pattern as Gmail |

Tokens live under `~/.yaadhamma/` and are never committed.

## Approvals

Consequential actions (send a message or email, create a calendar
event, trash a file) pause for Jeevan's explicit approval in the
terminal or voice session. One approval = one action, expires after 60
seconds. Every approval, refusal and verification outcome is appended to
`~/.yaadhamma/audit.jsonl`.

## Budget

`YAADHAMMA_MONTHLY_BUDGET_USD` in `.env.local` (default 20). The morning
brief prints a warning when the month's estimated spend crosses it. It
never blocks anything — the provider billing page is the only real
meter. If Gemini calls start failing with 429s, check the AI Studio
spending cap first.

## Logs and databases

- `~/.yaadhamma/logs/` — launchd job output
- `~/.yaadhamma/audit.jsonl` — approvals and verification results
- `~/.yaadhamma/yaadhamma.db` — tasks, `model_calls` cost table,
  WhatsApp health history
- `~/.yaadhamma/memory.db` — durable memories
- `~/.yaadhamma/voice_metrics.csv` — per-reply latency and tokens

## When something breaks

1. Run `scripts/status.py` — it surfaces the last 24h of audit errors.
2. Check `~/.yaadhamma/logs/` for the failing job.
3. WhatsApp Web: run `scripts/whatsapp_read_check.py` (reads, sends
   nothing) or the full `digest_run.py --check`.
4. Working rule: diagnose and explain the causes and options, get
   Jeevan's go-ahead, then fix. Never jump straight to fixing.

## Updating the code

The Mac is a read-only mirror of GitHub: `git fetch origin &&
git reset --hard origin/<branch>`. Never commit on the Mac or the
histories diverge. Changes are built and pushed from the dev machine,
then pulled here.
