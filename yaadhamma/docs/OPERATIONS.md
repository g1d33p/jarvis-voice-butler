# Yaadhamma Operations (v3)

Day-to-day running of the assistant on Jeevan's Mac. Everything here
assumes the project checked out at `~/jarvis-voice-butler` on the `v3`
branch, and Python managed with `uv`. Copy-paste the commands as-is;
nothing here asks you to edit code.

## First evening: the one-time setup

Do these once, in order. Each takes a couple of minutes.

1. **Check out the code**: `git clone <repo-url> ~/jarvis-voice-butler`
   (or `git fetch origin && git reset --hard origin/v3` if already cloned).
2. **Install the voice extras**: `cd ~/jarvis-voice-butler/yaadhamma &&
   uv sync --extra wake --extra ui` — the wake-word listener, the
   push-to-talk key listener, and the menu-bar UI all need these.
3. **Health check**: `uv run scripts/selftest.py` — 23 real checks, no
   network, takes seconds. Everything should say PASS.
4. **Secrets**: copy `.env.example` to `.env.local` and fill in
   `GOOGLE_API_KEY` plus the Google OAuth client ID/secret.
5. **Pair WhatsApp**: stop the agent if it is running, then
   `uv run scripts/whatsapp_signin.py` and scan the QR with the phone
   (WhatsApp > Settings > Linked devices).
6. **Sign in Gmail and Calendar**: `uv run scripts/gmail_signin.py` and
   `uv run scripts/calendar_signin.py` (three Gmail accounts; the label
   and archiving features need the `gmail.modify` scope — re-link any
   account whose `--check` says is missing it).
7. **Start everything**:
   `uv run scripts/digest_schedule.py on` (scheduled jobs),
   `uv run scripts/remote_schedule.py on` (phone access),
   `python scripts/daemon_control.py install` (the always-on daemon).
8. **Hold the right Command key** and ask her something small.

### Editing settings (.env.local)

Always edit `.env.local` in a text editor (TextEdit, VS Code, `nano`) —
never append with `echo ... >> .env.local`. If the file does not end with
a newline, the appended text glues onto the last line and silently
corrupts it: on 2026-09-29 two phone numbers fused into one 23-digit
value, which only surfaced later as a confusing "no chat found" error.
Keep one `KEY=value` per line and leave the trailing newline in place.
`scripts/selftest.py` has a ".env.local parses sanely" check that catches
glued lines, duplicate keys, and implausible values before a live run —
run it after any settings change. Every setting is documented in
`docs/SETTINGS.md`.

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
- Always-on daemon: `python scripts/daemon_control.py install`
  starts her at login and now; `status` / `start` / `stop` / `uninstall`
  control it. Logs land in `~/.yaadhamma/daemon.log`. The daemon only runs
  the voice loop — scheduled jobs stay in their own launchd jobs, so a
  daemon crash cannot stop the digests.
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
   > Microphone, then restart), a missing extra (`uv sync --extra wake --extra ui`),
   or a bad value in `.env.local`.
4. Reinstall after fixing: `python scripts/daemon_control.py uninstall`
   then `python scripts/daemon_control.py install`.
5. `python scripts/daemon_control.py status` reports three things: which
   input she is listening on (push-to-talk, wake word, or both), whether
   a voice session is currently open, and whether the running daemon
   matches the checked-out code (it records its git commit at every
   start; status says STALE when the checkout has moved on). Restart the
   daemon after every `git pull`.

## Talking to her: push-to-talk (v2)

The primary way to talk to her is **hold-to-talk**: press and hold the
**right Command key**. Keep holding while you speak, release when you are
done.

- She starts listening after the key has been held **200 milliseconds** —
  a quick tap does nothing, so brushing the key never triggers her.
- **Release only pauses the microphone.** Your session and anything she
  is doing keep running. Hold the key again and you rejoin the same
  session — you never have to start over mid-thought.
- **Option+Space** still works as a start/stop toggle, as before.
- 20 seconds of silence closes the session on its own.

### Changing the key

In `.env.local`:

- `YAADHAMMA_PTT=off` disables push-to-talk entirely.
- `YAADHAMMA_PTT_KEY` picks the key: `cmd_r` (default), `alt_r`, or
  `ctrl_r`. The `fn` key is refused on purpose — pynput cannot reliably
  tell fn presses apart, so she would miss holds or fire on taps.
- `YAADHAMMA_PTT_HOLD_MS` changes the hold threshold (default `200`).
- `YAADHAMMA_IDLE_TIMEOUT_S` changes the silence timeout (default `20`).

Restart the daemon after changing any of these.

### If the key does nothing

1. Run `uv run scripts/selftest.py` — the "push-to-talk listener is wired
   to the configured key" check proves the listener routes your key, and
   tells you if pynput is missing (`uv sync --extra wake --extra ui`
   installs it).
2. macOS needs the terminal app to have **Input Monitoring** permission
   (System Settings > Privacy & Security > Input Monitoring) for any app
   to watch key presses. Enable it, then restart the daemon.
3. If you changed `YAADHAMMA_PTT_KEY`, check the spelling: only `cmd_r`,
   `alt_r`, `ctrl_r`.

## The orb and honest state

The daemon can show a menu-bar icon plus a small floating orb (dark
near-black/violet-blue, draggable, remembers its position). Install the UI
extra once: `uv sync --extra ui` from the project directory (needs macOS;
it pulls in rumps / PyObjC).

- **Click the orb** to start a voice session; click again to finish your
  input (this pauses the microphone the same way releasing the key does —
  the session stays alive until the silence timeout).
- The icon and orb always show her real state: **idle** (daemon up, mic
  closed), **listening** (mic open, waiting for you), **thinking** (in a
  session, neither side speaking), **speaking** (she is talking), **muted**,
  or **error**. She never pretends to listen when she isn't.
- Menu: Start/Stop listening · Mute · Pause background jobs · Today's cost ·
  WhatsApp status · Open plans folder · Settings · Quit.
- `YAADHAMMA_UI=off` in `.env.local` runs the daemon headless. If the UI
  ever fails to start, the daemon logs a warning and keeps listening —
  a UI failure can never take the voice loop down.
- `YAADHAMMA_UI_CAPTIONS=on` shows the last utterance and reply as fading
  text beneath the orb (off by default).
- Rendering needs a real Mac and is unverified here; the state mapping and
  menu actions are covered by `tests/test_ui.py`.

## The wake word (off by default in v2)

Out of the box in v2 she does **not** listen for "Hey Jarvis" — push-to-talk
is the primary input, and always-on listening is off.

To re-enable it, set `YAADHAMMA_WAKE=on` in `.env.local` and restart the
daemon. She listens for **"Hey Jarvis"** using openWakeWord — no account,
no training. `Option+Space` also starts a conversation. 20 seconds of
silence closes the session.

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

## Cost model

Every model call she makes is recorded in `~/.yaadhamma/yaadhamma.db`
(the `model_calls` table) with an estimated dollar cost, tagged by
feature (`voice`, `voice_note`, `digest`, `email_triage`, …).

- **Session cost**: when a voice session opens, she notes today's total
  spend; the orb menu shows today's spend minus that baseline — what
  *this session* has cost so far. It updates as calls are recorded.
- **Monthly budget**: `YAADHAMMA_MONTHLY_BUDGET_USD` in `.env.local`
  (default 35). The morning brief warns when the month's estimated spend
  crosses it. It never blocks anything.
- **The provider billing page is the only real meter.** Her numbers are
  estimates for your awareness, not invoices. If Gemini calls start
  failing with 429s, check the AI Studio spending cap first.
- `uv run scripts/status.py` shows the last 7 days of spend per feature
  and month-to-date against the budget.

## How the digest decides what needs you (email)

New email goes through two passes before anything lands under *Needs you*
in the digest:

1. **Pass 1 — cheap sort.** She looks at sender, subject and preview only
   (no model call, no cost) and files each email as `noise` (bulk mail,
   marketing, automated noreply with no personal signal), `needs_reading`
   (a question, deadline or his name, but the preview cannot decide), or
   `clearly_needs_him` (addressed to him by name with a direct request, or
   a request tied to a date/deadline/meeting).
2. **Pass 2 — deep read.** She opens the full body of up to 8 emails from
   the latter two groups (clearest first; `YAADHAMMA_EMAIL_DEEP_READ` in
   `.env.local` changes the cap) and the model decides which ones genuinely
   need his reply, decision or action — with a one-line reason each.

Only pass 2 can put an email under *Needs you*. A bulk job alert stays out
however relevant the keywords look; a marketing email never needs him. A
`noise` email can still get its own line under *Email* (account, security
and billing notices do), but never under *Needs you*. Gmail is read-only
throughout: nothing is marked read, archived or labelled.

Honest limits: pass 1 is English-centric heuristics. An automated security
alert from a `noreply@` address with no question in the preview is filed as
noise — visible under *Email*, never *Needs you*. Emails past the deep-read
cap never get a verdict either. If either behaviour is wrong for him, say so
and the rules change.

## WhatsApp voice notes

From his own chat, Jeevan can send her a **voice note** instead of typing.
No "Yaadhamma" prefix needed — a voice note in his own chat is always
meant for her.

- She downloads it (up to 8 MB), transcribes it, and runs the transcript
  as a command — same tools, same approval gates, same audit as a typed
  message.
- She replies saying what she heard and what she did about it.
- Voice notes **longer than 60 seconds** are skipped: she sends exactly
  one reply saying it was too long, and does nothing else.
  (`YAADHAMMA_VOICE_NOTE_MAX_S` changes the limit.)
- If the download or transcription fails, she says so honestly and does
  nothing — she never acts on a guess.
- Only his own chats are polled (`YAADHAMMA_SELF_CHATS`); voice notes
  anywhere else are ignored.
- A voice note replaces a waiting approval question (newest wins), the
  same as a typed message.
- Real WhatsApp audio download and transcription quality are unverified
  in the sandbox; the detection, limits, and failure behaviour are
  covered by `tests/test_remote.py` and the self-test's "voice-note
  audio path is reachable" check.

## Sending files over WhatsApp

Ask her in a voice session or by WhatsApp message: "send the file
report.pdf to my own chat". She **always asks first** — the approval
names the file, the folder it comes from, and the recipient — and she
only sends to **his own chats** (refusing anything else is enforced in
code, not left to her judgement).

- She finds the file by exact path, by the tidy log (the nightly tidy may
  have moved it), or by searching Desktop, Documents, Downloads and the
  home folder. If she finds nothing — or several files with that name —
  she asks instead of guessing.
- Refusals before any approval: files outside the home folder, and files
  over 64 MB.
- She verifies the attachment actually appears in the chat before saying
  it sent. If anything looks wrong, she reports the failure and never
  retries blindly (a blind retry could deliver the file twice).
- Every file send is appended to `~/.yaadhamma/audit.jsonl`.

## Phone access over WhatsApp

From his phone he sends a message in his own chat starting with `Yaadhamma`
(for example "Yaadhamma remind me to call mom at 6"). The remote poll runs
it through the orchestrator like a voice task — same approval gates,
verification and audit.

- Only his own chats are polled (`YAADHAMMA_SELF_CHATS`, default
  19408438446 and 919640520634); anything else is ignored entirely.
- Replies and approval questions come back in the same chat. When a
  question is waiting, his next message in that chat is the answer — no
  `Yaadhamma` prefix needed, any wording ("yes", "no", "the second one").
  Unanswered questions expire after 30 minutes. A new `Yaadhamma …`
  command replaces the waiting question.
- Approvals only stay open a minute: answer promptly, or she asks again and,
  after two rounds, tells him the approval expired and to send the command
  again.
- One resident poller, polling every 2 minutes around the clock, as its own
  launchd job: `uv run scripts/remote_schedule.py on`. It opens a single
  WhatsApp browser at startup and reuses it for every poll (no fresh browser
  each time). Uses the digest browser profile, never the voice one.
  `YAADHAMMA_REMOTE=off` disables it, or
  `uv run scripts/remote_schedule.py off` unloads the job entirely.
- The poller never acts on a partial chat list: it waits for the list to
  settle and refuses loudly (morning brief, status,
  `~/.yaadhamma/remote.log`) rather than summarise a fraction of his chats
  as if it were all of them.
- **"WhatsApp is still syncing"** means the chat list did not finish loading
  in time (or came back under half of its usual size). The poller skipped
  that run rather than act on incomplete data. Occasional syncing is normal
  right after the Mac wakes or the network flaps. If every poll says it for
  an hour: check the network, then look at `~/.yaadhamma/remote.log`; if the
  chat count itself collapsed, he may have archived a large number of chats
  (the guard relearns the baseline from full reads).
- Each poll writes exactly one timestamped line to `~/.yaadhamma/remote.log`:
  `remote poll: chats_seen=N self_chats_matched=N commands_found=N
  actions_taken=N`. One line per poll — a quiet log means quiet polls.
- If the poll itself keeps failing the same way (3 times in a row), the
  morning brief says so loudly and `uv run scripts/selftest.py` fails —
  the tracebacks are in `~/.yaadhamma/remote.log`. A passing self-test also
  proves the remote orchestrator actually constructs.
- **Lost pairing**: if WhatsApp evicts the linked device, the poller parks
  its browser, records one health failure, waits 15 minutes, and says so
  loudly in the morning brief. Re-pair with
  `uv run scripts/whatsapp_signin.py` (stop the agent first — the profile
  is single-window). To make surprise unpairings rarer, prune stale entries
  on the phone: WhatsApp → Settings → Linked devices, and remove
  anything not recognised.

### The poller stays invisible (mostly)

The poller's Chromium runs headless and the code never brings its window
forward, so normally nothing ever appears on screen — no window, no Dock
bounce. Honest limits: this was verified against scripted fakes, not a real
Mac. If a Chromium window or Dock icon ever does appear during a poll, that
is a bug — report what was on screen and what `~/.yaadhamma/remote.log`
said at that minute. To disable the poller entirely:
`uv run scripts/remote_schedule.py off` (or `YAADHAMMA_REMOTE=off` in
`.env.local`, then restart the job).

## Browser troubleshooting

Her WhatsApp runs in a dedicated Chromium window with its own profile
(`~/.yaadhamma/chrome-profile`) — never Jeevan's own Chrome.

1. **"WhatsApp is not paired"**: stop the agent first (the profile is
   single-window — two processes cannot hold it), then
   `uv run scripts/whatsapp_signin.py` and scan the QR with the phone
   (WhatsApp > Settings > Linked devices).
2. **Read-only check**: `uv run scripts/whatsapp_read_check.py` reads
   chats and sends nothing. Safe to run any time.
3. **A second Chromium window is open**: close every window that might be
   holding the profile (including a stray sign-in run), then restart the
   agent. The profile lock is the usual "it worked yesterday" culprit.
4. **Stale session / weird page state**: `tail -40 ~/.yaadhamma/logs/*.log`
   for the job that failed; the WhatsApp health history is in
   `~/.yaadhamma/yaadhamma.db`, and the morning brief warns when the
   WhatsApp check is failing.
5. **File sends failing at the attach step**: WhatsApp Web changes its
   composer markup from time to time. The failure is loud and sends
   nothing — report it, and the attach selectors in
   `src/whatsapp_extractors.js` need updating to match the new markup.

## Microphone troubleshooting

1. **No permission prompt ever appeared**: a launchd background agent never
   gets the macOS microphone prompt. If the daemon log shows
   `PaMacCore err=-50`, run the daemon once in the foreground so the
   prompt can appear:
   `uv run --extra wake --extra ui python scripts/yaadhamma_daemon.py`
   Grant microphone access when macOS asks, confirm she hears you, then
   stop with Ctrl+C and restart the background daemon:
   `python scripts/daemon_control.py start`.
2. **Permission was granted but she can't hear anything**: open
   System Settings > Privacy & Security > Microphone and check the entry
   is enabled. Note the permission attaches to the **`uv` binary** that
   opened the mic, not to the script — this matters (see the app-bundle
   note below).
3. **Input Monitoring for the key listener**: the push-to-talk key watcher
   needs System Settings > Privacy & Security > Input Monitoring enabled
   for the terminal app, or key presses never arrive.
4. **`uv run scripts/selftest.py`**: the "microphone permission" check
   reports the current authorisation state on the Mac (authorised, denied,
   or not yet asked).
5. Real microphone behaviour is unverified in the sandbox
   (see `docs/BUILD_REPORT.md`).

### The stable app bundle (deferred — write-up only, not built)

macOS microphone permission is keyed to the **binary that opens the mic**.
Today that binary is `uv` — and every `uv` self-update replaces the
binary, which silently invalidates the permission. The symptom is "she
stopped hearing me after an update" with no error pointing at the cause.

The durable fix, not yet built: a tiny `Yaadhamma.app` bundle with a
stable bundle identifier, an `NSMicrophoneUsageDescription` string, and
proper code signing. The launchd plist would run the bundle's launcher
instead of `uv run`, so the permission attaches to Yaadhamma herself and
survives updates. Building and signing it needs Jeevan's go-ahead (it
touches his keychain and Developer ID choices), so it stays a plan until
then. The foreground-test workaround above remains the fix in the
meantime.

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
event, trash a file, send a file) pause for Jeevan's explicit approval in the
terminal or voice session. One approval = one action, expires after 60
seconds. Every approval, refusal and verification outcome is appended to
`~/.yaadhamma/audit.jsonl`.

## Morning brief warnings

The 8:45 brief carries the plan, the calendar, and — when something needs
him — a loud line:

- **Phone commands keep failing**: the remote poll has failed the same way
  3+ times in a row. Details in `~/.yaadhamma/remote.log`.
- **The daemon is not running**: the wake word and push-to-talk do nothing
  until it is restarted (`python scripts/daemon_control.py start`).
- **Nothing is listening**: the wake word and push-to-talk are both off.

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
- `~/.yaadhamma/voice-session.json` — whether a voice session is open
  (written by the daemon)

## When something breaks

1. Run `uv run scripts/selftest.py` — 23 checks covering config, the
   PTT listener, wake state, the remote poll, and voice notes.
2. Run `uv run scripts/status.py` — it surfaces the last 24h of audit errors.
3. Check `~/.yaadhamma/logs/` for the failing job.
4. WhatsApp Web: run `scripts/whatsapp_read_check.py` (reads, sends
   nothing) or the full `digest_run.py --check`.
5. Working rule: diagnose and explain the causes and options, get
   Jeevan's go-ahead, then fix. Never jump straight to fixing.

## Updating the code

The Mac is a read-only mirror of GitHub: `git fetch origin &&
git reset --hard origin/v2`. Never commit on the Mac or the
histories diverge. Changes are built and pushed from the dev machine,
then pulled here. After every pull, restart the daemon
(`python scripts/daemon_control.py stop`, then `start`) and confirm
`status` no longer says STALE.
