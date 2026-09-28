#!/usr/bin/env python3
"""Yaadhamma always-on daemon: the wake-word loop.

Started by launchd at login (see scripts/daemon_control.py). She listens
locally for the wake word; nothing leaves the Mac while idle. A wake (or
Option+Space) starts the voice worker as a subprocess; 90 s without speech
stops it and returns to idle.

The daemon NEVER runs scheduled jobs (digests, learning, briefs): those stay
in their own launchd jobs, so a daemon crash cannot stop them.

Mute comes from the menu bar (stage 4), which writes
~/.yaadhamma/wake-control.json {"muted": true/false}; the daemon polls it.
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import subprocess
import sys
import time
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

    def __init__(self, log_path: Path | None = None) -> None:
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._log_path = log_path or Path.home() / ".yaadhamma" / "voice-worker.log"
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
        # Silence is measured from the moment the session opens.
        note_speech_activity(time.monotonic())
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


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


# The daemon is launched with `uv run --extra wake --extra ui`, but a stray
# `uv sync` without extras silently uninstalls these afterwards. The wake
# packages are mandatory (no detector/mic without them); the UI degrades to
# headless, so it only warns.
_WAKE_PACKAGES = ("openwakeword", "sounddevice", "numpy")
_UI_PACKAGES = ("rumps",)


def _check_extras() -> int | None:
    """First-run guard: fail fast with the exact reinstall command instead
    of dying later on an ImportError. Returns an exit code, or None when
    everything the daemon needs is importable."""
    missing_wake = [p for p in _WAKE_PACKAGES if importlib.util.find_spec(p) is None]
    missing_ui = [p for p in _UI_PACKAGES if importlib.util.find_spec(p) is None]
    if missing_ui:
        log.warning(
            "menu-bar UI packages missing (%s): the daemon will run headless. "
            "To restore the UI, run: cd %s && uv sync --extra wake --extra ui",
            ", ".join(missing_ui),
            PROJECT,
        )
    if not missing_wake:
        return None
    log.error(
        "cannot start: wake-word packages missing (%s) — a 'uv sync' without "
        "extras probably uninstalled them. Reinstall with: cd %s && "
        "uv sync --extra wake --extra ui",
        ", ".join(missing_wake),
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


def main() -> int:
    settings = config.wake_settings()
    if not settings["enabled"]:
        log.info("wake word disabled (YAADHAMMA_WAKE=off); daemon exits")
        return 0

    if (code := _check_extras()) is not None:
        return code

    try:
        detector = build_detector()
    except WakeConfigError as exc:
        log.error("wake-word configuration problem: %s", exc)
        return 2
    assert detector is not None  # enabled above

    worker = VoiceWorker()
    machine = WakeMachine(
        idle_timeout_s=settings["idle_timeout_s"],
        on_conversation_start=worker.start,
        on_conversation_end=worker.stop,
    )
    log.info(
        "listening for %r (engine=%s). Nothing leaves the Mac while idle.",
        detector.phrase,
        settings["engine"],
    )

    mic = MicStream()
    try:
        mic.start()
    except MicrophonePermissionError as exc:
        log.error("%s", exc)
        return 3

    _install_shortcut(machine, settings["shortcut"])

    from ui import run_with_optional_ui
    from ui_state import UIController, map_wake_to_ui

    controller = UIController()
    mic_state = {"open": True}

    def state_provider():
        from latency import agent_is_speaking

        return map_wake_to_ui(
            machine.state.value,
            mic_open=mic_state["open"],
            agent_speaking=agent_is_speaking(),
            user_speaking=False,
        )

    run_with_optional_ui(
        lambda: _wake_loop(machine, detector, mic, worker, settings, mic_state),
        controller,
        state_provider,
    )
    return 0


def _wake_loop(machine, detector, mic, worker, settings, mic_state) -> None:
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
                    machine.on_wake_word(now)
            elif machine.state is WakeState.CONVERSATION:
                # The agent worker owns the mic now; its speech heartbeats
                # feed the silence timer.
                heard_at = last_speech_activity()
                if heard_at > machine.last_speech_at:
                    machine.on_speech(heard_at)
                if machine.tick(now):
                    log.info("90 s without speech: conversation closed")
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
