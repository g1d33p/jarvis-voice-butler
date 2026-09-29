# Yaadhamma v2 build report

Branch `v2` (cut from `v1` tip `0d3551c`). One section per stage, newest
last. Each stage is finished, tested and committed before the next begins.
Push target: `v2` only. Nothing here touches `main`, `gemini-live`,
`hardening` or `v1`.

---

## Stage 1 — Hold-to-talk on the Mac (built, sandbox-tested)

### What was built
- `src/hotkey.py` (new): press/release listener for the configured key, with a
  hold threshold so a tap does nothing. Key handling and the state machine are
  separately testable with a fake listener.
- `src/config.py`: `ptt_settings()` — `YAADHAMMA_PTT` (default on),
  `YAADHAMMA_PTT_KEY` (default `cmd_r`), `YAADHAMMA_PTT_HOLD_MS` (default 200),
  `YAADHAMMA_IDLE_TIMEOUT_S` (default 20). `YAADHAMMA_WAKE` is now off by
  default; wake and push-to-talk share the same 20-second idle timeout.
- `scripts/yaadhamma_daemon.py`: press starts the voice worker (one per
  session); release pauses the microphone without killing the worker, so an
  in-flight task runs to completion and she speaks the answer. A second hold
  during an open session re-opens listening in the same session. The session
  closes 20 s after the last activity unless a task is running. Option+Space
  still toggles start/stop.
- `src/agent.py`: a 150 ms watcher inside the worker reads the microphone
  flag (`~/.yaadhamma/ptt-mic.json`) and mutes the LiveKit console microphone
  on release — the conversation and any running task are untouched.
- `src/latency.py`: `note_task_running()` / `task_is_running()` so the daemon
  knows when a background task is busy and must not close the session.
- `src/orchestrator.py`: `TaskTools` takes an optional `activity_hook` so the
  busy flag is set and cleared reliably even when a task fails.
- `scripts/selftest.py`: new "push-to-talk" check (key resolves, tap does
  nothing, hold fires press then release, fn is refused honestly).

### Tests (all green in the sandbox)
- `tests/test_hotkey.py` (new): tap starts nothing; hold fires press/release;
  duplicate presses suppressed; release without press is safe.
- `tests/test_daemon.py`: press starts exactly one worker; release does not
  kill it; a second hold reuses the same session; busy tasks delay the idle
  close; `YAADHAMMA_PTT=off` disables the key entirely.
- `tests/test_latency.py`, `tests/test_orchestrator.py`, `tests/test_wake.py`:
  activity flag set/cleared on success and failure, stale flags expire, wake
  returns to idle after a PTT session.
- `ruff check` and `ruff format`: clean.

### What could not be verified here
- A real press and release of Right Command on macOS — the sandbox has no
  GUI, no keyboard events and no microphone. The pynput listener path is
  tested with fakes only.
- The LiveKit console microphone mute (`set_microphone_enabled`) is reached
  through an internal API; if LiveKit 1.8.3 changes it, the release will mute
  silently fail open (mic stays hot) rather than crash. Watch the daemon log
  for `ptt-mic` lines on the first Mac run.
- The full 20 s idle-timeout lifecycle on the Mac, and the exact moment her
  answer ends relative to the close.

### Needs Jeevan (Mac acceptance)
1. Hold Right Command, speak, release — she should answer after release and
   the session should close quietly about 20 s later.
2. Start a slow task ("summarise my inbox"), then release mid-task — the task
   must finish and she must speak the result.
3. Tap Right Command quickly — nothing should happen.
4. Say the old wake word — nothing should happen (wake is off by default).
5. Option+Space should still toggle start/stop.

### Known limitation
- `fn` is accepted as a configuration value but refused at startup with a
  clear error: pynput 1.8.2 does not expose Fn press/release reliably on
  macOS. Use `cmd_r` (Right Command), `alt_r` or `ctrl_r`.

---

## Stage 2 — Orb click, honest state, session cost (built, sandbox-tested)

### What was built
- **Orb click** (`src/ui_macos.py`): clicking the orb calls
  `controller.toggle_session()` — first click starts a session, the next
  finishes input (same as releasing the push-to-talk key). The click handler
  never raises out of the AppKit event.
- **Honest states**: the daemon's state provider already maps to
  idle/listening/thinking/speaking; Stage 2 wires both modes through one
  shared `_build_ui_controller()` helper in `scripts/yaadhamma_daemon.py`.
- **Startup-bug fix**: menu/orb construction is pure — it reads and writes
  nothing. A test snapshots the control file, the mic gate and the worker
  around construction and asserts nothing changed.
- **Session cost line**: the menu shows `Session: 3:12 · ~$0.04` (elapsed
  time plus estimated cost) and still shows today's total from `costs.py`.
  Session cost = today's spend now minus the baseline taken at session
  start; when the baseline is unavailable the line says `cost n/a` instead
  of inventing a number.
- The menu also gains a `Start session` / `Finish input` item that mirrors
  the orb.
- `YAADHAMMA_UI=off` still disables the UI entirely, and `src/ui.py`
  guarantees a UI failure can never take the daemon down.

### Tests (all green in the sandbox)
- `tests/test_ui.py`: construction calls no actions; menu titles reflect
  session state; the session item triggers the toggle; session line
  formatting including missing-cost and never-raises cases.
- `tests/test_daemon.py`: construction leaves detection state untouched;
  toggle starts a session when idle and finishes input (mic paused, session
  alive) when running; toggle never raises; session cost is the
  today-minus-baseline delta and `None` when the baseline is missing.
- `ruff check` and `ruff format`: clean.

### What could not be verified here
- The orb click itself, the Core Animation state changes, and the menu
  rendering — all need macOS. The wiring behind them (controller, actions,
  state mapping) is tested; the drawing is not.
- Whether the session-cost delta feels right in practice; other model spend
  during a session (e.g. a scheduled digest landing mid-call) is attributed
  to the session.

### Needs Jeevan (Mac acceptance)
1. Click the orb — she should start listening; click again — she answers.
2. The menu bar icon should move through idle → listening → thinking →
   speaking honestly.
3. The menu's session line should show elapsed time and a plausible cost,
   and today's total should keep working.

---

## Stage 3 — WhatsApp voice notes from his own chat (2026-09-29)

### What was built
- Voice-note detection in `src/whatsapp_extractors.js`: `waReadMessages`
  now flags a message with `voice: {duration_s}` when its bubble contains
  an `<audio>` element or a recognisable play control (`audio-play` /
  `ptt` testids or icons). The duration is read from the player subtree
  only — never the whole bubble, because the message timestamp also looks
  like mm:ss. Detection is conservative: no audio, no flag. `null`
  duration means "unknown", not "short".
- `waVoiceNoteAudio(doc, meta)`: downloads the note by fetching the
  audio blob URL in page context and returning base64. Refuses audio over
  8 MB. Message-not-found / no-audio / fetch failures are reported as
  structured `{ok: false, reason}` instead of throwing.
- `WhatsAppClient.download_voice_note(chat, message)` in `src/whatsapp.py`:
  re-opens the chat through the existing browser session, decodes the
  base64, refuses > 8 MB after decode.
- `src/voice_transcribe.py` (new): one-shot Gemini transcription with
  `config.BRAIN_MODEL`, live-only (needs `GOOGLE_API_KEY`). Empty results
  are failures, not empty commands. `log_voice_note_cost()` records an
  estimate under feature `voice_note` (~32 audio tokens/second, noted as
  approximate).
- `src/config.py`: `voice_note_settings()` —
  `YAADHAMMA_VOICE_NOTE_MAX_S`, default 60, sanitised.
- `src/remote.py`: `RemotePoller` takes an injected `transcriber`
  (default: the live Gemini one). `_handle_message` routes his outgoing
  voice notes to `_handle_voice_note`:
  - Only his own already-resolved chats (the poller never opens others);
    first-poll backlog swallowing and handle-once hashes apply unchanged.
  - No "Yaadhamma" prefix required.
  - Over the limit: skipped, one honest reply naming the lengths.
  - Download/transcription failure: honest reply, nothing executed.
  - The transcript is wrapped with `untrusted.wrap` and run through the
    same orchestrator, tools, approval gates and audit route as a typed
    command; the reply is "Heard: '…'. Done: …".
  - A voice note replaces a waiting approval question (newest wins).

### Tests
- `tests/test_remote.py`: 9 new tests (26 total, all pass) — no-prefix
  execution, untrusted wrapping of a hostile transcript, 60 s skip without
  downloading, incoming notes ignored, handle-once, backlog swallowing,
  honest transcription/download failures, voice note replacing a pending
  question. The fake client and fake transcriber mean no WhatsApp sends,
  downloads or model calls.
- `tests/js/wa_extractors.test.cjs`: 3 new fixture-DOM tests — voice
  flagged with duration 37 s, plain text never flagged, download of a
  missing message reported honestly. 31 pass.
- `ruff check` and `ruff format`: clean.

### What could not be verified here
- The real WhatsApp Web player DOM: the audio/play/duration selectors are
  best-effort from the documented structure. If WhatsApp changes the
  player markup, detection silently stops flagging notes (safe: nothing
  executes) and must be re-probed on the Mac.
- The download round-trip through a real browser session (blob URL fetch,
  Opus-in-Ogg bytes), the Gemini transcription quality, and the real cost
  figure — all need the Mac with his paired WhatsApp and keys.
- The ~32 tokens/second audio estimate is approximate; the provider's
  billing page is the truth.

### Needs Jeevan (Mac acceptance)
1. Send yourself a short voice note (< 60 s) from your own chat — she
   should reply "Heard: '…'." and do what you said.
2. Send a note longer than 60 s — she should skip it with one reply, not
   transcribe it.
3. Check the morning cost line includes the voice-note estimate.

---

## Stage 4 — whatsapp_send_file (2026-09-29)

### What was built
- New tool `whatsapp_send_file(chat_name, file_path, caption="")` in
  `src/whatsapp_tools.py`, registered in the shared background tool
  registry (voice and phone commands get it identically).
- Own-chat restriction is code, not the model: the tool resolves the
  configured self-chat numbers to their exact titles and refuses any
  other chat before any approval is requested.
- Always `RiskTier.HIGH` (`src/permissions.py`): no dictated-file bypass —
  every file send asks first, and the approval names the file, the folder
  it comes from, and the recipient.
- Refusals before approval: paths outside his home folder; files over
  64 MB (`MAX_SEND_FILE_BYTES`).
- File resolution: exact path first, then the tidy log
  (`where_did_file_go` — the nightly tidy may have moved it), then a
  bounded filename search of Desktop/Documents/Downloads and the home
  top level. Zero matches — or several — stop with a question instead
  of guessing.
- Sending uses the real WhatsApp file input: new `waAttachFile` /
  `waConfirmFileSend` extractors click the attach picker, the file is set
  on WhatsApp Web's hidden `<input type=file>` through a new narrow
  `BrowserManager.set_input_files` primitive (the native chooser cannot be
  driven by automation), the caption is typed into the preview, and send
  is pressed.
- Verification before success is reported: a new outgoing message must
  appear whose text contains the file name or which carries an attachment
  marker (new `waHasAttachment` detection in `waReadMessages`). On any
  doubt it raises without retrying — a blind retry could deliver the file
  twice.

### Tests
- `tests/test_whatsapp_send_file.py`: 11 tests, all pass — HIGH tier,
  approval names file/folder/recipient, approval then verified send,
  non-own chat refused before approval, outside-home refused, 64 MB
  refused, missing file asks, partial name found, ambiguous names ask,
  verification failure reported honestly, tidy-log lookup followed.
  Fake client only; no browser, no sends.
- `tests/js/wa_extractors.test.cjs`: 3 new tests — attach picker reports
  its file inputs, clean failure with no file input, clean failure with
  no send button. 34 pass.
- `tool_schemas.check_voice_tool_schemas`: all 21 voice tools still
  describe cleanly (file sending goes through `run_task` like other
  WhatsApp tools, not the small voice set — consistent with
  `whatsapp_send_message`).
- `ruff check` and `ruff format`: clean.

### What could not be verified here
- The real attach flow on WhatsApp Web: the attach-button selectors, the
  hidden file inputs' `accept` filters, the preview caption box and the
  send button are best-effort. If WhatsApp changes the composer chrome,
  sending fails loudly before anything is sent (safe direction).
- The attachment marker used for verification is heuristic (download
  link or media/document icon); the file-name-in-text check is the
  primary signal for documents.
- `set_input_files` on WhatsApp's hidden input is the standard Playwright
  approach, but the change-handler behaviour needs the Mac run.

### Needs Jeevan (Mac acceptance)
1. "Send the file report.pdf to my own chat" — she should ask, naming
   the file, its folder, and the chat; say yes — the file should arrive
   and she should confirm it.
2. Ask for a file by partial name when two exist — she should ask which.
3. Ask her to send a file to any other chat — she should refuse.

---

## Stage 5 — Failure visibility (2026-09-29)

### What was built
- Self-test (`scripts/selftest.py`) gained four checks:
  - "push-to-talk listener is wired to the configured key": the real
    `HotkeyListener` drives a fake listener through hold/release, proving
    presses route to the configured key only; on macOS it also resolves
    the key through the real pynput enum.
  - "wake state is readable": the mute/listen control file round-trips
    against a throwaway HOME, then the live flags are reported read-only.
  - "remote poll job is loaded and its last outcome is known": launchd
    job `com.yaadhamma.remote` on macOS plus the remote-health streak
    outcome everywhere.
  - "voice-note audio path is reachable": the extractor must export
    `waVoiceNote`/`waVoiceNoteAudio` (static, works anywhere); on macOS
    the WhatsApp browser profile must exist.
- The same commit fixed a real gap these checks surfaced: v2's new
  settings (`YAADHAMMA_PTT`, `YAADHAMMA_PTT_KEY`, `YAADHAMMA_PTT_HOLD_MS`,
  `YAADHAMMA_IDLE_TIMEOUT_S`, `YAADHAMMA_VOICE_NOTE_MAX_S`) were missing
  from `docs/SETTINGS.md`, and the `YAADHAMMA_WAKE` default was still
  documented as `on`. All documented now; the retired
  `YAADHAMMA_WAKE_IDLE_TIMEOUT_S` is marked retired.
- Morning brief (`src/digest.py`) gained two loud warning lines:
  - `_daemon_line()`: the daemon's recorded pid is dead, or it never
    recorded a startup — the wake word and push-to-talk do nothing until
    it is restarted. Joins the existing repeated-remote-failure line.
  - `_detection_line()`: wake word and push-to-talk both off — nothing
    is listening for him at all.
- `daemon_control.py status` now shows three things: the detection mode
  the running daemon recorded at startup ("push-to-talk",
  "wake word + push-to-talk", …), whether a voice session is open
  (written by the daemon on every session open/close to
  `~/.yaadhamma/voice-session.json`; a stale "open" with a dead daemon
  is reported as closed, not open), and the running-commit vs
  checked-out-commit match.
- `tests/test_stage5_visibility.py`: 11 tests — session open/close
  records on worker start/stop/exit, no record on suppressed starts,
  mode naming, the three status lines, pid liveness, and the two brief
  warnings. Fakes and scratch dirs only.

### Tests
- `tests/test_stage5_visibility.py`: 11 pass.
- `tests/test_daemon.py`, `tests/test_digest.py` with it: 81 pass.
- `scripts/selftest.py`: 22/23 pass in the sandbox. The one failure is
  environmental, not a regression: `openwakeword` is not installed in
  this sandbox (`uv sync --extra wake` installs it on the Mac, where the
  check passes).
- `ruff check` and `ruff format`: clean.

### What could not be verified here
- The launchd and pid-liveness paths need macOS; in the sandbox they
  report "skipped" honestly.
- A crashed daemon that never writes the close record relies on the
  stale-open rule in `status`; the brief's daemon warning relies on the
  pid check. Both are best-effort, in the safe direction.

### Needs Jeevan (Mac acceptance)
1. `uv run scripts/selftest.py` — all 23 should pass (openwakeword
   installs with `uv sync --extra wake --extra ui`).
2. `python scripts/daemon_control.py status` — should show the
   detection mode, "no voice session open", and a matching commit.
3. Hold right Command and keep holding: `status` should say a voice
   session is open; release and wait 20 s: it should close.
4. The next morning brief should carry no new warning lines.
