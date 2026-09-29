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
