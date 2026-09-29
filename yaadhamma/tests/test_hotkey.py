"""Tests for push-to-talk key detection (v2 Stage 1, src/hotkey.py).

Pure logic with a fake timer, fake keys, and a fake listener: no pynput,
no macOS, no real time.
"""

import pytest

from hotkey import (
    HotkeyConfigError,
    HotkeyListener,
    HotkeyState,
    read_ptt_mic_enabled,
    resolve_key,
    set_ptt_mic_enabled,
)


class FakeKeys:
    """Stand-in for pynput.keyboard.Key: distinct sentinels."""

    cmd_r = object()
    alt_r = object()
    ctrl_r = object()


class FakeTimer:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False
        self.started = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


def _state(hold_ms=200.0):
    timers = []
    fired = []

    def factory(delay, callback):
        timer = FakeTimer(delay, callback)
        timers.append(timer)
        return timer

    state = HotkeyState(
        hold_ms=hold_ms,
        on_press_start=lambda: fired.append("press"),
        on_release=lambda: fired.append("release"),
        timer_factory=factory,
    )
    return state, timers, fired


# ---------------------------------------------------------------- key config


def test_supported_keys_resolve() -> None:
    assert resolve_key("cmd_r", key_enum=FakeKeys) is FakeKeys.cmd_r
    assert resolve_key("alt_r", key_enum=FakeKeys) is FakeKeys.alt_r
    assert resolve_key("ctrl_r", key_enum=FakeKeys) is FakeKeys.ctrl_r


def test_fn_is_refused_honestly() -> None:
    with pytest.raises(HotkeyConfigError, match="fn"):
        resolve_key("fn", key_enum=FakeKeys)


def test_unknown_key_is_refused() -> None:
    with pytest.raises(HotkeyConfigError, match="unknown"):
        resolve_key("super_duper", key_enum=FakeKeys)


# ------------------------------------------------------------ hold detection


def test_a_tap_does_nothing() -> None:
    state, timers, fired = _state()
    state.key_down()
    state.key_up()
    assert fired == []
    assert timers[0].cancelled


def test_hold_fires_press_then_release() -> None:
    state, timers, fired = _state()
    state.key_down()
    timers[-1].fire()
    assert fired == ["press"]
    state.key_up()
    assert fired == ["press", "release"]


def test_hold_threshold_matches_config() -> None:
    state, timers, fired = _state(hold_ms=200.0)
    state.key_down()
    assert timers[-1].delay == pytest.approx(0.2)
    assert fired == []


def test_duplicate_press_is_ignored() -> None:
    state, timers, fired = _state()
    state.key_down()
    state.key_down()  # auto-repeat while held: must not re-arm
    assert len(timers) == 1
    timers[-1].fire()
    state.key_up()
    assert fired == ["press", "release"]


def test_release_without_press_is_a_noop() -> None:
    state, _timers, fired = _state()
    state.key_up()
    assert fired == []


def test_stop_cancels_a_pending_hold() -> None:
    state, timers, fired = _state()
    state.key_down()
    state.stop()
    timers[-1].fire()  # the timer still fires late: must not start anything
    assert fired == []


# ------------------------------------------------------------------ listener


class FakeListener:
    def __init__(self, on_press, on_release):
        self._on_press = on_press
        self._on_release = on_release
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _listener(hold_ms=200.0):
    created = []
    fired = []

    def factory(on_press, on_release):
        listener = FakeListener(on_press, on_release)
        created.append(listener)
        return listener

    timers = []

    def timer_factory(delay, callback):
        timer = FakeTimer(delay, callback)
        timers.append(timer)
        return timer

    listener = HotkeyListener(
        key=FakeKeys.cmd_r,
        hold_ms=hold_ms,
        on_press_start=lambda: fired.append("press"),
        on_release=lambda: fired.append("release"),
        listener_factory=factory,
        timer_factory=timer_factory,
    )
    return listener, created, timers, fired


def test_listener_only_drives_on_the_configured_key() -> None:
    listener, created, timers, fired = _listener()
    listener.start()
    assert created[0].started
    fake = created[0]
    fake._on_press(FakeKeys.alt_r)  # some other key: ignored
    fake._on_release(FakeKeys.alt_r)
    assert fired == []
    assert timers == []
    fake._on_press(FakeKeys.cmd_r)
    timers[-1].fire()
    fake._on_release(FakeKeys.cmd_r)
    assert fired == ["press", "release"]


def test_listener_stop_stops_the_underlying_listener() -> None:
    listener, created, _timers, _fired = _listener()
    listener.start()
    listener.stop()
    assert created[0].stopped


# ------------------------------------------------------------------- mic gate


def test_mic_gate_round_trip(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    set_ptt_mic_enabled(False)
    assert read_ptt_mic_enabled() is False
    set_ptt_mic_enabled(True)
    assert read_ptt_mic_enabled() is True


def test_mic_gate_missing_file_defaults_to_enabled(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert read_ptt_mic_enabled() is True


def test_mic_gate_never_raises(monkeypatch, tmp_path) -> None:
    import hotkey

    # Point the gate somewhere unwritable: the write must fail silently.
    monkeypatch.setattr(
        hotkey, "ptt_mic_path", lambda: tmp_path / "nope" / ".." / "\x00bad"
    )
    set_ptt_mic_enabled(False)


# ------------------------------------------------------------- config values


def test_ptt_defaults(monkeypatch) -> None:
    monkeypatch.delenv("YAADHAMMA_PTT", raising=False)
    monkeypatch.delenv("YAADHAMMA_PTT_KEY", raising=False)
    monkeypatch.delenv("YAADHAMMA_PTT_HOLD_MS", raising=False)
    monkeypatch.delenv("YAADHAMMA_IDLE_TIMEOUT_S", raising=False)
    import config

    assert config.ptt_settings() == {
        "enabled": True,
        "key": "cmd_r",
        "hold_ms": 200,
        "idle_timeout_s": 20.0,
    }


def test_ptt_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("YAADHAMMA_PTT", "off")
    monkeypatch.setenv("YAADHAMMA_PTT_KEY", "alt_r")
    monkeypatch.setenv("YAADHAMMA_PTT_HOLD_MS", "500")
    monkeypatch.setenv("YAADHAMMA_IDLE_TIMEOUT_S", "30")
    import config

    settings = config.ptt_settings()
    assert settings["enabled"] is False
    assert settings["key"] == "alt_r"
    assert settings["hold_ms"] == 500
    assert settings["idle_timeout_s"] == 30.0


def test_wake_defaults_to_off_with_shared_idle_timeout(monkeypatch) -> None:
    monkeypatch.delenv("YAADHAMMA_WAKE", raising=False)
    monkeypatch.delenv("YAADHAMMA_IDLE_TIMEOUT_S", raising=False)
    import config

    settings = config.wake_settings()
    assert settings["enabled"] is False
    assert settings["idle_timeout_s"] == 20.0
