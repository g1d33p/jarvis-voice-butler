"""Push-to-talk key detection (v2 Stage 1).

Hold the configured key to talk; release it and she answers. A tap shorter
than the hold threshold does nothing.

Two halves, kept separate so each is testable:

- HotkeyState: the pure press/hold/release state machine. No pynput, no
  threads of its own; the hold timer is injected, so tests drive it with a
  fake.
- HotkeyListener: the pynput glue. It translates key events into
  HotkeyState.key_down()/key_up() calls; tests inject a fake listener.

The daemon also gates the worker's microphone through a small file
(~/.yaadhamma/ptt-mic.json): release writes {"enabled": false}, press writes
{"enabled": true}, and the worker polls it. A file (not a signal) because the
worker is launched through `uv run`, so the daemon's subprocess handle is
uv's PID, not the Python worker's — signals would hit the wrong process.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from pathlib import Path


class HotkeyConfigError(Exception):
    """The push-to-talk key cannot be used."""


# Keys pynput can actually see on macOS. There is no Key.fn: the fn key
# arrives as an unmapped flags-changed event that pynput reports as a bare
# release, so press and release are indistinguishable through it.
_SUPPORTED_KEYS = ("cmd_r", "alt_r", "ctrl_r")


def resolve_key(name: str, *, key_enum=None):
    """Map a YAADHAMMA_PTT_KEY name to its pynput key.

    key_enum is for tests (a stand-in with cmd_r/alt_r/ctrl_r attributes);
    production passes nothing and gets pynput.keyboard.Key.
    """
    lname = (name or "").strip().lower()
    if lname == "fn":
        raise HotkeyConfigError(
            "YAADHAMMA_PTT_KEY=fn is not supported: pynput cannot reliably "
            "detect the fn key on macOS (it arrives as an unmapped modifier "
            "event). Use cmd_r (the default), alt_r or ctrl_r."
        )
    if lname not in _SUPPORTED_KEYS:
        raise HotkeyConfigError(
            f"unknown YAADHAMMA_PTT_KEY {name!r}: use one of "
            f"{', '.join(_SUPPORTED_KEYS)}."
        )
    if key_enum is None:
        try:
            from pynput.keyboard import Key

            key_enum = Key
        except ImportError as exc:
            raise HotkeyConfigError(
                "pynput is not installed, so push-to-talk cannot listen for "
                "the key. Reinstall with: uv sync --extra wake --extra ui"
            ) from exc
    return getattr(key_enum, lname)


def ptt_mic_path() -> Path:
    """Where the daemon leaves the mic gate for the worker."""
    return Path.home() / ".yaadhamma" / "ptt-mic.json"


def set_ptt_mic_enabled(enabled: bool) -> None:
    """Daemon side: open or close the worker's microphone. Never raises."""
    try:
        ptt_mic_path().parent.mkdir(parents=True, exist_ok=True)
        ptt_mic_path().write_text(json.dumps({"enabled": bool(enabled)}))
    except Exception:
        pass


def read_ptt_mic_enabled() -> bool:
    """Worker side: should the microphone be open? True when the file is
    missing or unreadable (fail open: a fresh session starts listening)."""
    try:
        return bool(json.loads(ptt_mic_path().read_text()).get("enabled", True))
    except Exception:
        return True


class HotkeyState:
    """Press-and-hold detection without any I/O.

    key_down() arms a hold timer; when it elapses with the key still down,
    on_press_start fires (start listening). key_up() before that cancels the
    timer: a tap does nothing. key_up() after a confirmed hold fires
    on_release (stop listening, keep working).

    timer_factory(delay_s, callback) builds the hold timer; production uses
    threading.Timer, tests use a fake they fire by hand.
    """

    def __init__(
        self,
        *,
        hold_ms: float,
        on_press_start,
        on_release,
        timer_factory=threading.Timer,
    ) -> None:
        self._hold_s = max(0.0, float(hold_ms) / 1000.0)
        self._on_press_start = on_press_start
        self._on_release = on_release
        self._timer_factory = timer_factory
        self._lock = threading.Lock()
        self._down = False
        self._confirmed = False
        self._timer = None

    def key_down(self) -> None:
        """A physical press of the key. Repeats while held are ignored."""
        with self._lock:
            if self._down:
                return
            self._down = True
            self._confirmed = False
            self._cancel_locked()
            self._timer = self._timer_factory(self._hold_s, self._hold_elapsed)
            self._timer.start()

    def key_up(self) -> None:
        """A physical release of the key."""
        with self._lock:
            if not self._down:
                return
            self._down = False
            self._cancel_locked()
            if self._confirmed:
                self._confirmed = False
                callback = self._on_release
            else:
                callback = None  # a tap: nothing starts
        if callback is not None:
            callback()

    def stop(self) -> None:
        """The listener is stopping: drop any pending hold."""
        with self._lock:
            self._down = False
            self._confirmed = False
            self._cancel_locked()

    def _hold_elapsed(self) -> None:
        with self._lock:
            if not self._down or self._confirmed:
                return
            self._confirmed = True
            callback = self._on_press_start
        callback()

    def _cancel_locked(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            with contextlib.suppress(Exception):
                timer.cancel()


def _pynput_listener_factory(on_press, on_release):
    """Build a real pynput keyboard listener. Raises HotkeyConfigError when
    pynput is missing (the daemon checks first; this is the backstop)."""
    try:
        from pynput import keyboard
    except ImportError as exc:
        raise HotkeyConfigError(
            "pynput is not installed, so push-to-talk cannot listen for the "
            "key. Reinstall with: uv sync --extra wake --extra ui"
        ) from exc
    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    return listener


class HotkeyListener:
    """Listen for the push-to-talk key and drive the callbacks.

    Pass key= with an already-resolved key object (tests use a stand-in), or
    key_name= to resolve through pynput. listener_factory builds the
    underlying listener; tests pass a fake they drive by hand.
    """

    def __init__(
        self,
        *,
        key=None,
        key_name: str = "cmd_r",
        hold_ms: float = 200,
        on_press_start,
        on_release,
        listener_factory=None,
        timer_factory=threading.Timer,
    ) -> None:
        self._key = key if key is not None else resolve_key(key_name)
        self._state = HotkeyState(
            hold_ms=hold_ms,
            on_press_start=on_press_start,
            on_release=on_release,
            timer_factory=timer_factory,
        )
        self._listener_factory = listener_factory or _pynput_listener_factory
        self._listener = None
        self._clock = time.monotonic

    def start(self) -> None:
        """Begin listening. Never raises for a missing key; pynput problems
        surface as HotkeyConfigError."""
        if self._listener is not None:
            return
        self._listener = self._listener_factory(self._on_press, self._on_release)
        self._listener.start()

    def stop(self) -> None:
        listener, self._listener = self._listener, None
        self._state.stop()
        if listener is not None:
            with contextlib.suppress(Exception):
                listener.stop()

    def _on_press(self, key) -> None:
        if key == self._key:
            self._state.key_down()

    def _on_release(self, key) -> None:
        if key == self._key:
            self._state.key_up()
