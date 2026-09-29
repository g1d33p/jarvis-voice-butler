#!/usr/bin/env python3
"""Yaadhamma always-on daemon: push-to-talk and the wake-word loop.

Started by launchd at login (see scripts/daemon_control.py). Two input
modes (v2 Stage 1), chosen by settings:

- push-to-talk (the default, YAADHAMMA_PTT=on): hold the key (right Command
  by default; a tap under 200 ms does nothing) to talk, release and she
  answers. Release only pauses the worker's microphone; the session and any
  task in flight keep running. A second hold rejoins the same session. The
  session closes after YAADHAMMA_IDLE_TIMEOUT_S (20 s) without input, unless
  she is speaking or a task is running.
- wake word (YAADHAMMA_WAKE=on, off by default): she listens locally;
  nothing leaves the Mac while idle. A wake starts the voice worker as a
  subprocess; the same idle timeout closes the session.

Option+Space always toggles her (press to start, press to stop).

The daemon NEVER runs scheduled jobs (digests, learning, briefs): those stay
in their own launchd jobs, so a daemon crash cannot stop them.

Mute comes from the menu bar (stage 4), which writes
~/.yaadhamma/wake-control.json {"muted": true/false}; the daemon polls it.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

import config  # noqa: E402
from latency import note_speech_activity  # noqa: E402
from wake import (  # noqa: E402
    MicrophonePermissionError,
    MicStream,
    WakeConfigError,
    WakeMachine,
    WakeState,
    build_detector,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [daemon] %(levelname)s %(message)s",
)
log = logging.getLogger("yaadhamma.daemon")

POLL_S = 0.2  # mic frame cadence
CONTROL_POLL_S = 1.0  # mute-file cadence

DAEMON_INFO_PATH = Path.home() / ".yaadhamma" / "daemon-info.json"
SESSION_STATE_PATH = Path.home() / ".yaadhamma" / "voice-session.json"


def _git_commit() -> str:
    """The checked-out commit, best-effort (a stale daemon is otherwise
    invisible: in 2026-09 one ran a day on pre-fix code with no clue in
    the log)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(PROJECT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _detection_mode() -> str:
    """Which input path this daemon is listening on. Never raises.

    Written into daemon-info.json at startup so `daemon_control status`
    can say it even when the config later changes under a running daemon.
    """
    try:
        import config

        wake = bool(config.wake_settings().get("enabled"))
        ptt = bool(config.ptt_settings().get("enabled"))
    except Exception:
        return "unknown"
    if wake and ptt:
        return "wake word + push-to-talk"
    if wake:
        return "wake word"
    if ptt:
        return "push-to-talk"
    return "none (detection off)"


def _write_daemon_info() -> dict:
    """Record which code this daemon is running, for daemon_control status."""
    info = {
        "commit": _git_commit(),
        "argv": sys.argv,
        "started": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "mode": _detection_mode(),
    }
    try:
        DAEMON_INFO_PATH.parent.mkdir(parents=True, exist_ok=True)
        DAEMON_INFO_PATH.write_text(json.dumps(info))
    except Exception:
        pass
    return info


def _write_session_state(is_open: bool, path: Path | None = None) -> None:
    """Record whether a voice session is currently open.

    The daemon writes it on every session open/close; `daemon_control
    status` reads it. A worker crash that skips the close-write leaves a
    stale "open" — `status` reports that as closed when the daemon itself
    is not running (a long conversation can legitimately stay open for
    minutes, so age alone is not a signal). Never raises.
    """
    try:
        target = path or SESSION_STATE_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "session_open": bool(is_open),
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
    except Exception:
        pass


def _read_control() -> dict:
    """Mute/listen flags from the menu bar. Never raises."""
    from ui_state import read_control

    return read_control()


class VoiceWorker:
    """Own the agent subprocess: one conversation = one process.

    QUICK_EXIT_S: a healthy worker lives for minutes; if the process is
    already gone this soon after start, the launch itself failed (e.g. a
    bad command line, an unresolvable tool schema) and the daemon says so
    loudly instead of silently dropping back to idle.

    The worker's own stdout/stderr is captured to
    ~/.yaadhamma/voice-worker.log (one launch per file); on a launch
    failure the tail is logged loudly so the crash reason is visible in
    the daemon log too.

    After MAX_QUICK_FAILURES consecutive launch failures the daemon stops
    retrying for SUPPRESS_S: a broken launch would otherwise crash-loop on
    every wake word. start() returns False while suppressed.
    """

    QUICK_EXIT_S = 5.0
    MAX_QUICK_FAILURES = 3
    SUPPRESS_S = 600.0
    LOG_TAIL_LINES = 40

    def __init__(
        self, log_path: Path | None = None, state_path: Path | None = None
    ) -> None:
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        # Session cost baseline: today's model spend at session start, so the
        # menu can show the session's estimated cost as the delta. None when
        # the store could not be read.
        self._session_cost_baseline: float | None = None
        self._log_path = log_path or Path.home() / ".yaadhamma" / "voice-worker.log"
        self._state_path = state_path or SESSION_STATE_PATH
        self._log_file = None
        self._quick_failures = 0
        self._suppress_until = 0.0
        self._suppress_logged = False

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def suppressed(self) -> bool:
        return time.monotonic() < self._suppress_until

    def quick_exit_code(self) -> int | None:
        """Exit code if the worker already exited within QUICK_EXIT_S of
        starting; None when it is still running, never started, was
        stopped normally, or lived longer than the threshold."""
        if self._proc is None or self._started_at is None:
            return None
        code = self._proc.poll()
        if code is None:
            return None
        if time.monotonic() - self._started_at < self.QUICK_EXIT_S:
            return code
        return None

    def start(self) -> bool:
        """Launch the worker. Returns False without launching when starts
        are suppressed after repeated launch failures."""
        if self.running:
            return True
        now = time.monotonic()
        if now < self._suppress_until:
            if not self._suppress_logged:
                self._suppress_logged = True
                log.warning(
                    "voice worker start suppressed for %.0f s more after %d "
                    "consecutive launch failures — wake words will not start "
                    "a worker until it lapses; fix the launch, then restart "
                    "the daemon: python scripts/daemon_control.py stop && "
                    "python scripts/daemon_control.py start",
                    self._suppress_until - now,
                    self._quick_failures,
                )
            return False
        uv = _which("uv")
        # Keep the extras on the command line (see daemon_control._program):
        # a bare `uv run` re-syncs the environment, which would uninstall the
        # wake/UI packages out from under this already-running daemon.
        # `console` is the LiveKit CLI subcommand that actually runs the
        # agent; without it the CLI prints help and exits immediately.
        cmd = (
            [uv, "run", "--extra", "wake", "--extra", "ui", "src/agent.py", "console"]
            if uv
            else [sys.executable, "src/agent.py", "console"]
        )
        log.info("wake: starting voice worker: %s", " ".join(cmd))
        self._close_log()
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            # The handle stays open while the child runs; closed in
            # _close_log()/stop().
            self._log_file = open(self._log_path, "w")  # noqa: SIM115
        except OSError as exc:
            log.warning("could not open worker log %s: %s", self._log_path, exc)
            self._log_file = None
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT),
            stdout=self._log_file if self._log_file is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        self._started_at = time.monotonic()
        self._session_cost_baseline = _today_cost_usd()
        # Silence is measured from the moment the session opens.
        note_speech_activity(time.monotonic())
        _write_session_state(is_open=True, path=self._state_path)
        return True

    def note_exit(self) -> None:
        """The wake loop saw the worker gone.

        A launch failure is logged loudly together with the worker's own
        last output; after MAX_QUICK_FAILURES in a row, further starts are
        suppressed for SUPPRESS_S. A worker that lived past the launch
        window resets the failure counter.
        """
        if self._proc is None:
            return  # suppressed start: nothing was launched, nothing to record
        self._close_log()
        code = self.quick_exit_code()
        if self._proc.poll() is not None:
            # The worker is gone: the session it owned is closed, whatever
            # the exit code. (A PTT mic release does not stop the worker,
            # so an open session survives releases — see _on_ptt_release.)
            _write_session_state(is_open=False, path=self._state_path)
        if code is None:
            self._quick_failures = 0
            return
        self._quick_failures += 1
        tail = self._read_log_tail()
        if self._quick_failures >= self.MAX_QUICK_FAILURES:
            self._suppress_until = time.monotonic() + self.SUPPRESS_S
            self._suppress_logged = False
            log.error(
                "voice worker failed to launch %d times in a row (exit code "
                "%s); not retrying for %.0f minutes. Last worker output:\n"
                "%s\nFix the launch, then restart the daemon: "
                "python scripts/daemon_control.py stop && "
                "python scripts/daemon_control.py start",
                self._quick_failures,
                code,
                self.SUPPRESS_S / 60,
                tail,
            )
        else:
            log.error(
                "voice worker exited within %.0f s of starting (exit code "
                "%s): the launch failed — the launch command is logged "
                "above; she heard the wake word but could not start talking. "
                "Last worker output:\n%s",
                self.QUICK_EXIT_S,
                code,
                tail,
            )

    def _read_log_tail(self) -> str:
        try:
            lines = self._log_path.read_text().splitlines()
        except OSError:
            return "(no worker output captured)"
        tail = lines[-self.LOG_TAIL_LINES :]
        return "\n".join(tail) if tail else "(worker produced no output)"

    def _close_log(self) -> None:
        fh, self._log_file = self._log_file, None
        if fh is not None:
            with contextlib.suppress(OSError):
                fh.close()

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        self._close_log()
        self._started_at = None
        self._session_cost_baseline = None
        _write_session_state(is_open=False, path=self._state_path)
        if proc is None:
            return
        if proc.poll() is not None:
            return
        log.info("silence timeout: stopping voice worker")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.warning("voice worker would not stop; killing it")
            proc.kill()

    def pause_listening(self) -> None:
        """Push-to-talk release: ask the worker to stop mic input, without
        touching the session. The worker polls ~/.yaadhamma/ptt-mic.json (a
        file, not a signal: this handle is the `uv run` parent, not the
        Python worker). Never raises."""
        if not self.running:
            return
        try:
            from hotkey import set_ptt_mic_enabled

            set_ptt_mic_enabled(False)
            log.info("push-to-talk: released — mic input paused, session continues")
        except Exception as exc:
            log.warning("push-to-talk: could not pause mic input: %s", exc)

    def resume_listening(self) -> None:
        """Push-to-talk press while a session is open: mic input back on in
        the same session. Never raises."""
        if not self.running:
            return
        try:
            from hotkey import set_ptt_mic_enabled

            set_ptt_mic_enabled(True)
            log.info("push-to-talk: pressed — mic input resumed in open session")
        except Exception as exc:
            log.warning("push-to-talk: could not resume mic input: %s", exc)

    @property
    def busy(self) -> bool:
        """True while she is speaking or a background task is running: the
        idle timeout must not close the session then. Never raises."""
        try:
            from latency import agent_is_speaking, task_is_running

            return agent_is_speaking() or task_is_running()
        except Exception:
            return False

    @property
    def session_elapsed_s(self) -> float:
        """Seconds since this session opened. 0 when no session is open."""
        if not self.running or self._started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self._started_at)

    @property
    def session_cost_usd(self) -> float | None:
        """Estimated model spend since the session opened (today's total
        minus the baseline at start). None when no session is open or the
        baseline could not be read. The session's own turns dominate this
        delta, so it is honest enough for a menu line; the provider's
        billing page is the truth."""
        if not self.running or self._session_cost_baseline is None:
            return None
        now = _today_cost_usd()
        if now is None:
            return None
        return max(0.0, now - self._session_cost_baseline)


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


class PTTController:
    """Own the voice session for push-to-talk (v2 Stage 1).

    Pure logic: the worker is injected and time comes from the injected
    clock, so tests drive it without subprocesses or real time. The daemon
    wires the hotkey's on_press_start/on_release here and calls tick() from
    its loop.

    Press (hold confirmed) opens a session, or rejoins an open one. Release
    only pauses mic input — the worker keeps running, finishes any task in
    flight, and speaks the answer. The idle timeout closes the session, but
    a busy worker (she is speaking or a task runs) delays the close; it is
    checked again on the next tick.
    """

    def __init__(self, *, worker, idle_timeout_s: float, clock=time.monotonic) -> None:
        self._worker = worker
        self._idle_timeout_s = idle_timeout_s
        self._clock = clock
        self._last_input_at = 0.0

    @property
    def idle_timeout_s(self) -> float:
        return self._idle_timeout_s

    def on_press_start(self) -> None:
        """The hold threshold was met: open a session, or rejoin an open one."""
        now = self._clock()
        if self._worker.running:
            self._worker.resume_listening()
        else:
            self._worker.start()
        self._last_input_at = now

    def on_release(self) -> None:
        """Key released: pause mic input, keep the session (and its task) alive."""
        now = self._clock()
        if self._worker.running:
            self._worker.pause_listening()
        self._last_input_at = now

    def on_shortcut(self) -> None:
        """Option+Space toggle: press to start, press again to stop."""
        if self._worker.running:
            self._worker.stop()
        else:
            self._worker.start()
        self._last_input_at = self._clock()

    def on_speech(self, at: float) -> None:
        """Speech activity observed (or a wake word opened a session): keep
        the session alive."""
        if at > self._last_input_at:
            self._last_input_at = at

    def tick(self, now: float) -> bool:
        """Periodic check. True when an idle session just closed."""
        if not self._worker.running:
            return False
        if now - self._last_input_at < self._idle_timeout_s:
            return False
        if self._worker.busy:
            # She is speaking or a task is still running: wait, and try the
            # close again on the next tick.
            return False
        self._worker.stop()
        return True


def _today_cost_usd() -> float | None:
    """Today's estimated model spend, from costs.py. None on any failure."""
    try:
        from costs import CostStore

        return float(CostStore().today_usd())
    except Exception:
        return None


def _session_info(worker: VoiceWorker) -> dict:
    """Facts for the menu's session line. Never raises."""
    try:
        return {
            "active": bool(worker.running),
            "elapsed_s": worker.session_elapsed_s,
            "cost_usd": worker.session_cost_usd,
        }
    except Exception:
        return {"active": False, "elapsed_s": 0.0, "cost_usd": None}


def _build_ui_controller(worker: VoiceWorker, toggle_session):
    """Build the menu-bar/orb controller.

    Construction is pure: it reads nothing and writes nothing — detection
    state (control file, mic gate, wake machine, worker) is untouched. The
    orb click / menu item calls toggle_session. Returns the controller.
    """
    from ui_state import Actions, UIController

    controller = UIController(
        actions=Actions(toggle_session=toggle_session),
        session_info=lambda: _session_info(worker),
    )
    return controller


def _ptt_toggle_session(worker: VoiceWorker, ptt_controller: PTTController):
    """Orb/menu toggle for push-to-talk mode: start a session, or finish
    input in the open one (same semantics as releasing the key)."""

    def toggle() -> str:
        try:
            if worker.running:
                ptt_controller.on_release()
                return "input finished — she will answer"
            ptt_controller.on_press_start()
            return "listening" if worker.running else "could not start the voice worker"
        except Exception as exc:
            log.warning("orb/menu session toggle failed: %s", exc)
            return "session toggle failed"

    return toggle


def _ptt_state_provider(worker: VoiceWorker):
    """Honest orb/menu state for push-to-talk mode."""

    def provider():
        from latency import agent_is_speaking
        from ui_state import map_wake_to_ui

        running = worker.running
        return map_wake_to_ui(
            "conversation" if running else "idle",
            mic_open=running,
            agent_speaking=agent_is_speaking(),
            user_speaking=False,
        )

    return provider


# The daemon is launched with `uv run --extra wake --extra ui`, but a stray
# `uv sync` without extras silently uninstalls these afterwards. The wake
# packages are mandatory (no detector/mic without them); the UI degrades to
# headless, so it only warns.
_WAKE_PACKAGES = ("openwakeword", "sounddevice", "numpy")
_PTT_PACKAGES = ("pynput",)
_UI_PACKAGES = ("rumps",)


def _check_extras(*, need_wake: bool = True, need_ptt: bool = False) -> int | None:
    """First-run guard: fail fast with the exact reinstall command instead
    of dying later on an ImportError. Only the enabled modes are checked.
    Returns an exit code, or None when everything needed is importable."""
    missing_wake = [
        p for p in _WAKE_PACKAGES if need_wake and importlib.util.find_spec(p) is None
    ]
    missing_ptt = [
        p for p in _PTT_PACKAGES if need_ptt and importlib.util.find_spec(p) is None
    ]
    missing_ui = [p for p in _UI_PACKAGES if importlib.util.find_spec(p) is None]
    if missing_ui:
        log.warning(
            "menu-bar UI packages missing (%s): the daemon will run headless. "
            "To restore the UI, run: cd %s && uv sync --extra wake --extra ui",
            ", ".join(missing_ui),
            PROJECT,
        )
    missing = missing_wake + missing_ptt
    if not missing:
        return None
    log.error(
        "cannot start: packages missing (%s) — a 'uv sync' without extras "
        "probably uninstalled them. Reinstall with: cd %s && "
        "uv sync --extra wake --extra ui",
        ", ".join(missing),
        PROJECT,
    )
    return 4


def _install_shortcut(machine: WakeMachine, enabled: bool):
    """Option+Space -> machine.on_shortcut. Warns and continues without it."""
    if not enabled:
        return None
    try:
        from pynput import keyboard
    except ImportError:
        log.warning(
            "pynput is not installed, so Option+Space will not wake her. "
            "Install it with: pip install pynput (the wake word still works)."
        )
        return None

    pressed: set = set()

    def on_press(key) -> None:
        pressed.add(key)
        try:
            alt_held = (
                keyboard.Key.alt in pressed
                or keyboard.Key.alt_l in pressed
                or keyboard.Key.alt_r in pressed
            )
            if key == keyboard.Key.space and alt_held:
                machine.on_shortcut(time.monotonic())
        except Exception:
            pass

    def on_release(key) -> None:
        # A plain function on purpose: pynput inspects its callbacks with
        # inspect.getfullargspec(), which rejects built-in methods such as
        # set.discard with "TypeError: unsupported callable".
        pressed.discard(key)

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    listener.start()
    log.info("Option+Space shortcut armed")
    return listener


def _install_ptt_shortcut(controller: PTTController):
    """Option+Space -> controller.on_shortcut (toggle). Warns and continues
    without it, like the wake-word shortcut."""
    try:
        from pynput import keyboard
    except ImportError:
        log.warning(
            "pynput is not installed, so Option+Space will not toggle her. "
            "Install it with: pip install pynput."
        )
        return None

    pressed: set = set()

    def on_press(key) -> None:
        pressed.add(key)
        try:
            alt_held = (
                keyboard.Key.alt in pressed
                or keyboard.Key.alt_l in pressed
                or keyboard.Key.alt_r in pressed
            )
            if key == keyboard.Key.space and alt_held:
                controller.on_shortcut()
        except Exception:
            pass

    def on_release(key) -> None:
        # A plain function on purpose: pynput inspects its callbacks with
        # inspect.getfullargspec(), which rejects built-in methods such as
        # set.discard with "TypeError: unsupported callable".
        pressed.discard(key)

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    listener.start()
    log.info("Option+Space toggle armed (push-to-talk mode)")
    return listener


def _start_ptt_listener(controller: PTTController, ptt: dict):
    """Arm the push-to-talk key. Returns the listener, or None when the key
    cannot be used (already logged)."""
    from hotkey import HotkeyConfigError, HotkeyListener

    try:
        listener = HotkeyListener(
            key_name=ptt["key"],
            hold_ms=ptt["hold_ms"],
            on_press_start=controller.on_press_start,
            on_release=controller.on_release,
        )
        listener.start()
    except HotkeyConfigError as exc:
        log.error("push-to-talk disabled: %s", exc)
        return None
    log.info(
        "push-to-talk armed: hold %s (tap under %d ms does nothing)",
        ptt["key"],
        ptt["hold_ms"],
    )
    return listener


def main() -> int:
    info = _write_daemon_info()
    log.info(
        "daemon starting: commit=%s argv=%s",
        info["commit"],
        " ".join(info["argv"]),
    )
    ptt = config.ptt_settings()
    wake = config.wake_settings()
    if not ptt["enabled"] and not wake["enabled"]:
        log.info("push-to-talk and wake word both disabled; daemon exits")
        return 0

    if (
        code := _check_extras(need_wake=wake["enabled"], need_ptt=ptt["enabled"])
    ) is not None:
        return code

    worker = VoiceWorker()
    ptt_controller = (
        PTTController(worker=worker, idle_timeout_s=ptt["idle_timeout_s"])
        if ptt["enabled"]
        else None
    )
    if wake["enabled"]:
        return _run_wake_mode(worker, ptt_controller, wake, ptt)
    assert ptt_controller is not None  # enabled above
    return _run_ptt_mode(worker, ptt_controller, ptt)


def _run_wake_mode(worker: VoiceWorker, ptt_controller, wake: dict, ptt: dict) -> int:
    """Wake-word mode, with the push-to-talk key optionally armed alongside."""
    try:
        detector = build_detector()
    except WakeConfigError as exc:
        log.error("wake-word configuration problem: %s", exc)
        return 2
    assert detector is not None  # enabled above

    machine = WakeMachine(
        idle_timeout_s=wake["idle_timeout_s"],
        on_conversation_start=worker.start,
        on_conversation_end=worker.stop,
    )
    log.info(
        "listening for %r (engine=%s). Nothing leaves the Mac while idle.",
        detector.phrase,
        wake["engine"],
    )

    mic = MicStream()
    try:
        mic.start()
    except MicrophonePermissionError as exc:
        log.error("%s", exc)
        return 3

    if ptt_controller is not None:
        _start_ptt_listener(ptt_controller, ptt)
        _install_ptt_shortcut(ptt_controller)
    else:
        _install_shortcut(machine, wake["shortcut"])

    from ui import run_with_optional_ui
    from ui_state import map_wake_to_ui

    mic_state = {"open": True}

    def wake_toggle() -> str:
        """Orb/menu toggle for wake-word mode: start a conversation, or end
        the open one."""
        from wake import WakeState

        try:
            if machine.state is WakeState.CONVERSATION:
                machine.on_session_closed()
                return "session finished"
            if machine.on_wake_word(time.monotonic()):
                return "listening"
            return "already listening"
        except Exception as exc:
            log.warning("orb/menu session toggle failed: %s", exc)
            return "session toggle failed"

    controller = _build_ui_controller(worker, wake_toggle)

    def state_provider():
        from latency import agent_is_speaking

        return map_wake_to_ui(
            machine.state.value,
            mic_open=mic_state["open"],
            agent_speaking=agent_is_speaking(),
            user_speaking=False,
        )

    run_with_optional_ui(
        lambda: _wake_loop(
            machine, detector, mic, worker, wake, mic_state, ptt_controller
        ),
        controller,
        state_provider,
    )
    return 0


def _run_ptt_mode(worker: VoiceWorker, ptt_controller: PTTController, ptt: dict) -> int:
    """Push-to-talk mode: no wake-word detector, no always-on mic. The key
    (and Option+Space) owns the session."""
    listener = _start_ptt_listener(ptt_controller, ptt)
    if listener is None:
        return 4  # already logged
    _install_ptt_shortcut(ptt_controller)

    from ui import run_with_optional_ui

    controller = _build_ui_controller(
        worker,
        _ptt_toggle_session(worker, ptt_controller),
    )
    state_provider = _ptt_state_provider(worker)

    run_with_optional_ui(
        lambda: _ptt_loop(ptt_controller, worker, listener),
        controller,
        state_provider,
    )
    return 0


def _ptt_loop(
    controller: PTTController, worker: VoiceWorker, listener, poll_s: float = POLL_S
) -> None:
    """The push-to-talk loop: key events arrive on the listener thread; here
    we feed speech heartbeats to the idle timer, close idle sessions, and
    notice a worker that died on its own."""
    from latency import last_speech_activity

    try:
        while True:
            now = time.monotonic()
            heard_at = last_speech_activity()
            if heard_at:
                controller.on_speech(heard_at)
            if controller.tick(now):
                log.info(
                    "idle timeout (%ds without input): session closed",
                    int(controller.idle_timeout_s),
                )
            # If the worker died on its own, say so loudly (a launch failure
            # is logged with the worker's own output; repeated failures are
            # suppressed, so a broken launch cannot spin on every keypress).
            if not worker.running:
                worker.note_exit()
            time.sleep(poll_s)
    except KeyboardInterrupt:
        log.info("daemon stopping")
    finally:
        listener.stop()
        worker.stop()
        log.info("push-to-talk loop stopped")


def _wake_loop(
    machine, detector, mic, worker, settings, mic_state, ptt_controller=None
) -> None:
    """The always-on loop. Runs on the main thread headless, or on a
    background thread when the menu-bar UI takes the main thread."""
    from latency import last_speech_activity

    mic_open = True
    mic_state["open"] = True
    last_control_poll = 0.0
    control = _read_control()
    try:
        while True:
            now = time.monotonic()
            if now - last_control_poll >= CONTROL_POLL_S:
                last_control_poll = now
                control = _read_control()
                want_active = control["listening"] and not control["muted"]
                if want_active and not mic_open:
                    log.info("detection resumed")
                    machine.on_unmute()
                    try:
                        mic.start()
                    except MicrophonePermissionError as exc:
                        log.error("%s", exc)
                        return
                    mic_open = True
                    mic_state["open"] = True
                elif not want_active and mic_open:
                    log.info("detection stopped from the menu bar")
                    mic.stop()
                    machine.on_mute()
                    mic_open = False
                    mic_state["open"] = False

            if machine.state is WakeState.IDLE and mic_open:
                try:
                    frame = mic.read()
                except MicrophonePermissionError as exc:
                    log.error("%s", exc)
                    return
                except Exception as exc:
                    log.warning("mic read failed: %s", exc)
                    time.sleep(1.0)
                    continue
                if detector.check(frame):
                    log.info("wake word heard")
                    if machine.on_wake_word(now) and ptt_controller is not None:
                        # A push-to-talk key holds the session open; a wake
                        # word only starts it. Arm the PTT idle timer so the
                        # session still closes without a hold.
                        ptt_controller.on_speech(now)
            elif machine.state is WakeState.CONVERSATION:
                # The agent worker owns the mic now; its speech heartbeats
                # feed the silence timer.
                heard_at = last_speech_activity()
                if heard_at > machine.last_speech_at:
                    machine.on_speech(heard_at)
                    if ptt_controller is not None:
                        ptt_controller.on_speech(heard_at)
                if ptt_controller is not None:
                    # Push-to-talk owns the idle timeout: it delays the
                    # close while she is speaking or a task runs, and only
                    # the PTT hold should reopen listening afterwards.
                    if ptt_controller.tick(now):
                        log.info(
                            "idle timeout (%ds without input): session closed",
                            int(ptt_controller.idle_timeout_s),
                        )
                        machine.on_session_closed()
                else:
                    if machine.tick(now) and not worker.busy:
                        log.info("idle timeout: session closed")
                # If the worker died on its own, go back to idle honestly.
                # note_exit() logs a launch failure loudly (with the
                # worker's own output) and suppresses further starts after
                # repeated failures, so a broken launch cannot crash-loop
                # on every wake word.
                if not worker.running:
                    worker.note_exit()
                    machine.tick(now + settings["idle_timeout_s"])
                time.sleep(POLL_S)
            else:  # muted or listening stopped
                time.sleep(CONTROL_POLL_S)
    except KeyboardInterrupt:
        log.info("daemon stopping")
    finally:
        worker.stop()
        mic.stop()
        detector.close()


if __name__ == "__main__":
    sys.exit(main())
