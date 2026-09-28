# Yaadhamma Operations

Day-to-day running of the assistant on Jeevan's Mac. Everything here
assumes the project checked out at `~/jarvis-voice-butler` and Python
managed with `uv`. Copy-paste the commands as-is; nothing here asks you
to edit code.

## First evening: the one-time setup

Do these once, in order. Each takes a couple of minutes.

1. **Check out the code**: `git clone <repo-url> ~/jarvis-voice-butler`
   (or `git fetch origin && git reset --hard origin/v1` if already cloned).
2. **Health check**: `cd ~/jarvis-voice-butler/yaadhamma &&
   uv run scripts/selftest.py` — 9 real checks, no network, takes seconds.
   Everything should say PASS.
3. **Secrets**: copy `.env.example` to `.env.local` and fill in
   `GOOGLE_API_KEY` plus the Google OAuth client ID/secret.
4. **Pair WhatsApp**: stop the agent if it is running, then
   `uv run scripts/whatsapp_signin.py` and scan the QR with the phone
   (WhatsApp > Settings > Linked devices).
5. **Sign in Gmail and Calendar**: `uv run scripts/gmail_signin.py` and
   `uv run scripts/calendar_signin.py` (three Gmail accounts; the label
   and archiving features need the `gmail.modify` scope — re-link any
   account whose `--check` says is missing it).
6. **Start everything**:
   `uv run scripts/digest_schedule.py on` (scheduled jobs),
   `uv run scripts/remote_schedule.py on` (phone access),
   `python scripts/daemon_control.py install` (always-on wake word).
7. **Say "Hey Jarvis"** and ask her something small.

Microphone permission lives in System Settings > Privacy & Security >
Microphone. Full per-service notes are below under "Sign-in and pairing".

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

### If the daemon fails to start

1. Run it once in the foreground to see the real error (the log can
   interleave and truncate tracebacks), with the extras so the wake/UI
   packages stay installed:
   `uv run --extra wake --extra ui python scripts/yaadhamma_daemon.py` —
   stop it with Ctrl+C when it fails.
2. Also check the tail: `tail -30 ~/.yaadhamma/daemon.log`.
   If the worker (the voice session) keeps crashing on launch, the daemon
   log says so loudly and the worker's own last output is captured in
   `~/.yaadhamma/voice-worker.log` — read that file first. After 3
   launch failures in a row the daemon stops retrying for 10 minutes
   rather than crash-looping on every wake word; fix the launch, then
   `python scripts/daemon_control.py stop` and
   `python scripts/daemon_control.py start`.
3. Usual causes: microphone permission (System Settings > Privacy & Security
   > Microphone, then restart), a missing extra (`uv sync --extra wake`
   and/or `--extra ui`), or a bad value in `.env.local`.
4. Reinstall after fixing: `python scripts/daemon_control.py uninstall`
   then `python scripts/daemon_control.py install`.

## Wake word

Out of the box she listens for **"Hey Jarvis"** using openWakeWord —
no account, no training. `YAADHAMMA_WAKE=off` in `.env.local` disables the
whole always-on listener. `Option+Space` also starts a conversation
(needs `pip install pynput`); 90 seconds of silence closes the session.

### Foreground test mode (microphone permission)

A launchd background agent never gets the macOS microphone permission
prompt: if the daemon log shows `PaMacCore err=-50`, the background
daemon can never open the mic on its own. Run it once in the foreground
from the project directory so the prompt can appear:

```
uv run --extra wake --extra ui python scripts/yaadhamma_daemon.py
```

Grant microphone access when macOS asks (the prompt names the `uv`
binary — permission attaches to the binary that opens the mic, not to
the script), say "Hey Jarvis" to confirm it hears you, then stop with
Ctrl+C and restart the background daemon:

```
python scripts/daemon_control.py start
```

This is also the fastest way to test any daemon change: the log goes
straight to the terminal instead of `~/.yaadhamma/daemon.log`.

### Custom wake phrase ("Hey Yaadhamma")

Stay on **"Hey Jarvis"** for now. The old Porcupine-based custom phrase
path needed a Picovoice AccessKey, and Picovoice discontinued its free
tier on 30 June 2026 (enterprise only) — so it is no longer practical
for personal use. A custom "Hey Yaadhamma" can be trained later with
openWakeWord's own training process, which needs no account.

(The `porcupine` engine code path is still in `src/wake.py` behind
`YAADHAMMA_WAKE_ENGINE=porcupine` for an enterprise Picovoice key; in
that case install `pvporcupine` separately — it is no longer in the
wake extra.)

Microphone permission: if the daemon logs a microphone error, open
System Settings > Privacy & Security > Microphone on the Mac, enable
access for the app running the daemon, and restart it. Real microphone
behaviour is unverified in the sandbox (see `docs/BUILD_REPORT.md`).

## Menu-bar UI

The daemon can show a menu-bar icon plus a small floating orb (dark
near-black/violet-blue, draggable, remembers its position). Install the UI
extra once: `uv sync --extra ui` from the project directory (needs macOS; it pulls in rumps /
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

## Nightly tidy-ups (3 am)

The 3 am job runs both tidy-ups, every other night:

- **File tidy** (`--tidy`): moves stray files into `Documents > Sorted` per a
  reviewed plan. Defaults to **apply**; `YAADHAMMA_TIDY=propose` in
  `.env.local` goes back to propose-only. Nothing is ever deleted, nothing
  is ever overwritten (name collisions get a free name), every move is
  verified at its destination with the source gone, and each move is logged
  for manual reversal.
- **Email tidy** (`--email-tidy`): labels and archives as described below;
  still propose-only until `YAADHAMMA_EMAIL_TIDY=apply`.

What the email tidy does:

- New inbox mail since the last run is classified (cheap rules, then the
  cheap model) and labelled `Yaadhamma/Jobs`, `Yaadhamma/Saayam`,
  `Yaadhamma/Finance`, `Yaadhamma/Receipts` or `Yaadhamma/Newsletters`
  (created on demand).
- Promotions/newsletters **older than 7 days** are archived (the INBOX label
  is removed). Never deleted, never marked read, never anything unlabelled
  or from a person.
- Every change is logged to `~/.yaadhamma/email-tidy.log` with how to reverse
  it by hand.
- The first run is propose-only: the plan goes to `~/Documents/Yaadhamma/`
  and is summarised in the morning brief. Set `YAADHAMMA_EMAIL_TIDY=apply`
  in `.env.local` to enable real changes.
- Label changes need the `gmail.modify` scope: all three accounts must be
  re-linked. `uv run scripts/gmail_signin.py --check` reports which, and
  prints the exact re-link command.

## Commitments

Promises Jeevan makes ("I'll call Ravi back by 5pm") are stored in
`~/.yaadhamma/commitments.db` with a parsed due date ("by 5pm" → today
17:00, "tomorrow"/"Monday 9am", "Jan 5", "end of week" → Friday 18:00;
vague timing like "soon" stores no due date rather than guessing).

- Every wake session reviews overdue + due-today commitments.
- The morning brief and each scheduled digest list "Commitments due today".
- Completion is explicit only: the `mark_done` voice tool completes the
  one commitment he named, and refuses when the name is ambiguous or
  unknown. Nothing ever infers that something is finished.

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

`YAADHAMMA_MONTHLY_BUDGET_USD` in `.env.local` (default 35). The morning
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
