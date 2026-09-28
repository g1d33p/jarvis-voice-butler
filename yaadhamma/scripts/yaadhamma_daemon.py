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

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

import config  # noqa: E402
from latency import last_speech_activity, note_speech_activity  # noqa: E402
from wake import (  # noqa: E402
    MicrophonePermissionError,
    MicStream,
    WakeConfigError,
    WakeMachine,
    WakeState,
    build_detector,
    control_path,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [daemon] %(levelname)s %(message)s",
)
log = logging.getLogger("yaadhamma.daemon")

POLL_S = 0.2  # mic frame cadence
CONTROL_POLL_S = 1.0  # mute-file cadence


def _read_muted() -> bool:
    try:
        return bool(json.loads(Path(control_path()).read_text()).get("muted"))
    except Exception:
        return False


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
        cmd = [uv, "run", "src/agent.py"] if uv else [sys.executable, "src/agent.py"]
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

    listener = keyboard.Listener(on_press=on_press, on_release=pressed.discard)
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

    # Stage 4 UI runs in this process when it exists; headless until then.
    try:
        import ui_daemon  # noqa: F401

        log.info("menu-bar UI module present")
    except ImportError:
        log.info("no menu-bar UI module yet (stage 4); running headless")

    last_control_poll = 0.0
    muted = False
    try:
        while True:
            now = time.monotonic()
            if now - last_control_poll >= CONTROL_POLL_S:
                last_control_poll = now
                want_muted = _read_muted()
                if want_muted != muted:
                    muted = want_muted
                    if muted:
                        log.info("muted from the menu bar: stopping detection")
                        mic.stop()
                        machine.on_mute()
                    else:
                        log.info("unmuted: resuming detection")
                        machine.on_unmute()
                        try:
                            mic.start()
                        except MicrophonePermissionError as exc:
                            log.error("%s", exc)
                            return 3

            if machine.state is WakeState.IDLE:
                try:
                    frame = mic.read()
                except MicrophonePermissionError as exc:
                    log.error("%s", exc)
                    return 3
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
            else:  # MUTED
                time.sleep(CONTROL_POLL_S)
    except KeyboardInterrupt:
        log.info("daemon stopping")
    finally:
        worker.stop()
        mic.stop()
        detector.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
