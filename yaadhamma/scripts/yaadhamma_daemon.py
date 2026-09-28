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
    """Own the agent subprocess: one conversation = one process."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.running:
            return
        uv = _which("uv")
        # Keep the extras on the command line (see daemon_control._program):
        # a bare `uv run` re-syncs the environment, which would uninstall the
        # wake/UI packages out from under this already-running daemon.
        cmd = (
            [uv, "run", "--extra", "wake", "--extra", "ui", "src/agent.py"]
            if uv
            else [sys.executable, "src/agent.py"]
        )
        log.info("wake: starting voice worker: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Silence is measured from the moment the session opens.
        note_speech_activity(time.monotonic())

    def stop(self) -> None:
        proc, self._proc = self._proc, None
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
                if not worker.running:
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
