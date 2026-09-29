"""Tests for the always-on daemon's testable pieces.

The live wake loop needs a microphone and macOS launchd, so it is not
exercised here: real microphone behaviour is unverified
(see docs/BUILD_REPORT.md).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load_daemon():
    spec = importlib.util.spec_from_file_location(
        "yaadhamma_daemon", SCRIPTS / "yaadhamma_daemon.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["yaadhamma_daemon"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def daemon():
    return _load_daemon()


def test_both_modes_off_exits_quietly(daemon, monkeypatch, caplog, tmp_path) -> None:
    monkeypatch.setenv("YAADHAMMA_WAKE", "off")
    monkeypatch.setenv("YAADHAMMA_PTT", "off")
    # main() records its version at startup; keep that out of the real home.
    monkeypatch.setattr(daemon, "DAEMON_INFO_PATH", tmp_path / "daemon-info.json")
    assert daemon.main() == 0
    assert (tmp_path / "daemon-info.json").exists()


def test_ptt_mode_starts_when_wake_is_off(daemon, monkeypatch, tmp_path) -> None:
    """v2 default: YAADHAMMA_WAKE is off, push-to-talk owns the session."""
    import ui

    monkeypatch.setenv("YAADHAMMA_WAKE", "off")
    monkeypatch.delenv("YAADHAMMA_PTT", raising=False)  # default: on
    monkeypatch.setattr(daemon, "DAEMON_INFO_PATH", tmp_path / "daemon-info.json")
    monkeypatch.setattr(daemon, "_check_extras", lambda **kwargs: None)

    calls = {}

    class FakePTTListener:
        def stop(self):
            calls["listener_stopped"] = True

    fake_listener = FakePTTListener()
    monkeypatch.setattr(
        daemon, "_start_ptt_listener", lambda controller, ptt: fake_listener
    )
    monkeypatch.setattr(daemon, "_install_ptt_shortcut", lambda controller: None)
    monkeypatch.setattr(
        daemon,
        "_ptt_loop",
        lambda controller, worker, listener: calls.update(
            {"loop_ran": True, "listener": listener, "worker": worker}
        ),
    )
    monkeypatch.setattr(ui, "run_with_optional_ui", lambda loop, *a: loop())

    assert daemon.main() == 0
    assert calls["loop_ran"] is True
    assert calls["listener"] is fake_listener
    assert isinstance(calls["worker"], daemon.VoiceWorker)


def test_control_file_missing_means_defaults(daemon, monkeypatch, tmp_path) -> None:
    from ui_state import read_control

    monkeypatch.setattr("ui_state.control_path", lambda: tmp_path / "nope.json")
    assert daemon._read_control() == {"muted": False, "listening": True}
    assert read_control() == {"muted": False, "listening": True}


def test_control_file_round_trip(daemon, monkeypatch, tmp_path) -> None:
    from ui_state import read_control, write_control

    path = tmp_path / "wake-control.json"
    monkeypatch.setattr("ui_state.control_path", lambda: path)
    write_control(muted=True, listening=False)
    assert daemon._read_control() == {"muted": True, "listening": False}
    assert read_control()["muted"] is True
    write_control(muted=False, listening=True)
    assert daemon._read_control() == {"muted": False, "listening": True}


def test_voice_worker_start_stop(daemon, monkeypatch, tmp_path) -> None:
    started = []

    class FakeProc:
        def __init__(self, cmd, **kwargs):
            started.append(cmd)
            self._alive = True

        def poll(self):
            return None if self._alive else 0

        def terminate(self):
            self._alive = False

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self._alive = False

    monkeypatch.setattr(daemon.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(
        daemon, "_which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )

    worker = daemon.VoiceWorker(log_path=tmp_path / "voice-worker.log")
    worker.start()
    assert worker.running
    assert started[0][:2] == ["/usr/bin/uv", "run"]
    worker.start()  # second start while running: no second process
    assert len(started) == 1
    worker.stop()
    assert not worker.running


def test_daemon_control_rejects_non_macos(monkeypatch, capsys) -> None:
    spec = importlib.util.spec_from_file_location(
        "daemon_control", SCRIPTS / "daemon_control.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["daemon_control"] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "platform", "linux")
    assert module.main(["daemon_control.py", "install"]) == 1
    out = capsys.readouterr().out
    assert "macOS" in out


def test_install_shortcut_callbacks_survive_pynput_inspection(
    daemon, monkeypatch
) -> None:
    """pynput inspects listener callbacks with inspect.getfullargspec(),
    which raises 'TypeError: unsupported callable' for built-in methods
    (e.g. set.discard). The daemon must only pass plain Python functions."""
    import inspect
    import sys
    import types

    from wake import WakeMachine

    created = {}

    class FakeListener:
        def __init__(self, on_press=None, on_release=None):
            # Mirror the real pynput behaviour that crashed the daemon:
            # _wrap() calls inspect.getfullargspec() on each callback.
            for callback in (on_press, on_release):
                if callback is not None:
                    inspect.getfullargspec(callback)
            created["on_press"] = on_press
            created["on_release"] = on_release
            self.daemon = False

        def start(self):
            created["started"] = True

    keyboard = types.ModuleType("pynput.keyboard")
    keyboard.Listener = FakeListener
    keyboard.Key = types.SimpleNamespace(
        alt="alt", alt_l="alt_l", alt_r="alt_r", space="space"
    )
    pynput = types.ModuleType("pynput")
    pynput.keyboard = keyboard
    monkeypatch.setitem(sys.modules, "pynput", pynput)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", keyboard)

    machine = WakeMachine(
        idle_timeout_s=90.0,
        on_conversation_start=lambda: None,
        on_conversation_end=lambda: None,
    )
    listener = daemon._install_shortcut(machine, True)
    assert listener is not None
    assert created["started"] is True

    # The release callback actually releases the tracked key.
    created["on_press"]("a")
    created["on_release"]("a")


def test_install_shortcut_disabled_returns_none(daemon) -> None:
    assert daemon._install_shortcut(None, False) is None


def _load_daemon_control():
    spec = importlib.util.spec_from_file_location(
        "daemon_control", SCRIPTS / "daemon_control.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["daemon_control"] = module
    spec.loader.exec_module(module)
    return module


def test_daemon_control_program_keeps_extras(monkeypatch) -> None:
    """Plain `uv run` re-syncs the environment and uninstalls the wake/UI
    packages on every daemon start. The launch arguments must carry the
    extras so they survive."""
    import shutil

    module = _load_daemon_control()
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    prog = module._program()
    assert prog[:2] == ["/usr/bin/uv", "run"]
    assert prog[-1] == "scripts/yaadhamma_daemon.py"
    assert "--extra" in prog
    assert "wake" in prog and "ui" in prog


def test_daemon_control_program_without_uv(monkeypatch) -> None:
    module = _load_daemon_control()
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    prog = module._program()
    assert prog[0] == sys.executable
    assert prog[-1] == "scripts/yaadhamma_daemon.py"


def test_voice_worker_keeps_extras(daemon, monkeypatch, tmp_path) -> None:
    """The voice worker is spawned by the running daemon via `uv run`; a bare
    `uv run` would re-sync and uninstall the wake/UI packages out from under
    the daemon's own lazy imports (sounddevice, openwakeword, pynput)."""
    started = []

    class FakeProc:
        def __init__(self, cmd, **kwargs):
            started.append(cmd)

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(daemon.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(daemon, "_which", lambda name: "/usr/bin/uv")

    worker = daemon.VoiceWorker(log_path=tmp_path / "voice-worker.log")
    worker.start()
    assert started[0][:2] == ["/usr/bin/uv", "run"]
    assert "wake" in started[0] and "ui" in started[0]
    assert started[0][-2] == "src/agent.py"
    assert started[0][-1] == "console"
    worker.stop()


def _set_package_presence(
    monkeypatch, present: tuple = (), missing: tuple = ()
) -> None:
    import importlib.util

    real_find_spec = importlib.util.find_spec

    def fake(name: str):
        if name in missing:
            return None
        if name in present:
            return object()  # any truthy spec stands in for "importable"
        return real_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake)


def test_voice_worker_launches_console_subcommand(
    daemon, monkeypatch, tmp_path
) -> None:
    """The LiveKit CLI needs a subcommand: without `console`, `uv run
    src/agent.py` prints help and exits immediately."""
    started = []

    class FakeProc:
        def __init__(self, cmd, **kwargs):
            started.append(cmd)

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(daemon.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(daemon, "_which", lambda name: "/usr/bin/uv")
    worker = daemon.VoiceWorker(log_path=tmp_path / "voice-worker.log")
    worker.start()
    assert started[0][-1] == "console"
    worker.stop()


def test_worker_quick_exit_detected(daemon, monkeypatch, tmp_path) -> None:
    """A worker that is already gone within seconds of starting failed to
    launch — the daemon must notice instead of silently going back to idle."""

    class FakeProc:
        def __init__(self, cmd, **kwargs):
            pass

        def poll(self):
            return 1  # exited immediately

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(daemon.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(daemon, "_which", lambda name: "/usr/bin/uv")
    worker = daemon.VoiceWorker(log_path=tmp_path / "voice-worker.log")
    worker.start()
    assert worker.quick_exit_code() == 1


def test_worker_slow_exit_not_flagged_as_quick(daemon, monkeypatch, tmp_path) -> None:
    """A worker that ran for a while and then died is a runtime failure,
    not a launch failure: no loud launch-failure report."""

    class FakeProc:
        def __init__(self, cmd, **kwargs):
            pass

        def poll(self):
            return 1

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(daemon.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(daemon, "_which", lambda name: "/usr/bin/uv")
    now = [1000.0]
    monkeypatch.setattr(daemon.time, "monotonic", lambda: now[0])
    worker = daemon.VoiceWorker(log_path=tmp_path / "voice-worker.log")
    worker.start()
    now[0] += 10.0  # the worker ran a while, then died: not a launch failure
    assert worker.quick_exit_code() is None


def _quick_fail_daemon(daemon, monkeypatch):
    """Popen double that exits immediately, leaving a traceback in the
    worker's captured stdout — the shape of a real launch crash."""

    class QuickFailProc:
        def __init__(self, cmd, **kwargs):
            out = kwargs.get("stdout")
            if out is not None and out is not daemon.subprocess.DEVNULL:
                out.write(
                    "Traceback (most recent call last):\n"
                    '  File "src/agent.py", line 1, in <module>\n'
                    "NameError: name '_RunContext' is not defined\n"
                )
                out.flush()

        def poll(self):
            return 1

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(daemon.subprocess, "Popen", QuickFailProc)
    monkeypatch.setattr(daemon, "_which", lambda name: "/usr/bin/uv")


def test_worker_quick_exit_logs_loudly_with_output(
    daemon, monkeypatch, tmp_path, caplog
) -> None:
    """A launch failure is logged loudly together with the worker's own
    last output, and the full output stays in the worker log file."""
    import logging

    _quick_fail_daemon(daemon, monkeypatch)
    log_file = tmp_path / "voice-worker.log"
    worker = daemon.VoiceWorker(log_path=log_file)
    assert worker.start() is True
    with caplog.at_level(logging.ERROR, logger="yaadhamma.daemon"):
        worker.note_exit()
    assert "could not start talking" in caplog.text
    assert "_RunContext" in caplog.text  # the worker's own last words
    assert "_RunContext" in log_file.read_text()


def test_worker_suppresses_after_repeated_quick_failures(
    daemon, monkeypatch, tmp_path
) -> None:
    """After MAX_QUICK_FAILURES consecutive launch failures the daemon
    stops retrying: start() returns False until the cooldown lapses."""
    _quick_fail_daemon(daemon, monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(daemon.time, "monotonic", lambda: now[0])
    worker = daemon.VoiceWorker(log_path=tmp_path / "voice-worker.log")
    for _ in range(worker.MAX_QUICK_FAILURES):
        assert worker.start() is True
        worker.note_exit()
    assert worker.suppressed
    assert worker.start() is False  # no silent retry on the next wake
    now[0] += worker.SUPPRESS_S + 1.0
    assert not worker.suppressed
    assert worker.start() is True


def test_worker_suppressed_start_warns_once(
    daemon, monkeypatch, tmp_path, caplog
) -> None:
    """While suppressed, wake words do not launch anything; the warning
    is logged once per suppression period, not on every wake."""
    import logging

    _quick_fail_daemon(daemon, monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(daemon.time, "monotonic", lambda: now[0])
    worker = daemon.VoiceWorker(log_path=tmp_path / "voice-worker.log")
    for _ in range(worker.MAX_QUICK_FAILURES):
        worker.start()
        worker.note_exit()
    with caplog.at_level(logging.WARNING, logger="yaadhamma.daemon"):
        assert worker.start() is False
        assert worker.start() is False
    assert caplog.text.count("start suppressed") == 1


def test_worker_healthy_run_resets_failures(daemon, monkeypatch, tmp_path) -> None:
    """A worker that lived past the launch window clears the failure
    counter: one bad launch long ago must not suppress a good one."""
    _quick_fail_daemon(daemon, monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(daemon.time, "monotonic", lambda: now[0])
    worker = daemon.VoiceWorker(log_path=tmp_path / "voice-worker.log")
    worker.start()
    worker.note_exit()
    assert worker._quick_failures == 1
    worker.start()
    now[0] += 10.0  # this run lived: a slow death is not a launch failure
    worker.note_exit()
    assert worker._quick_failures == 0
    assert not worker.suppressed


def test_check_extras_fails_fast_on_missing_wake(daemon, monkeypatch, caplog) -> None:
    import logging

    _set_package_presence(monkeypatch, missing=("openwakeword",))
    with caplog.at_level(logging.ERROR, logger="yaadhamma.daemon"):
        assert daemon._check_extras() == 4
    assert "uv sync --extra wake --extra ui" in caplog.text
    assert "openwakeword" in caplog.text


def test_check_extras_warns_on_missing_ui_only(daemon, monkeypatch, caplog) -> None:
    import logging

    _set_package_presence(
        monkeypatch,
        present=("openwakeword", "sounddevice", "numpy"),
        missing=("rumps",),
    )
    with caplog.at_level(logging.WARNING, logger="yaadhamma.daemon"):
        assert daemon._check_extras() is None
    assert "headless" in caplog.text
    assert "uv sync --extra wake --extra ui" in caplog.text


def test_main_exits_4_when_wake_packages_missing(daemon, monkeypatch, tmp_path) -> None:
    _set_package_presence(monkeypatch, missing=("openwakeword", "sounddevice"))
    monkeypatch.setenv("YAADHAMMA_WAKE", "on")
    # main() records its version at startup; keep that out of the real home.
    monkeypatch.setattr(daemon, "DAEMON_INFO_PATH", tmp_path / "daemon-info.json")
    assert daemon.main() == 4
    assert (tmp_path / "daemon-info.json").exists()


def _load_control():
    spec = importlib.util.spec_from_file_location(
        "daemon_control", SCRIPTS / "daemon_control.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["daemon_control"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def control():
    return _load_control()


def test_code_match_report_match(control) -> None:
    info = {
        "commit": "abc123def456",
        "argv": ["uv", "run", "scripts/yaadhamma_daemon.py"],
        "started": "2026-09-29T01:00:00+00:00",
    }
    report = control.code_match_report(info, "abc123def456")
    assert "Matches the checked-out code" in report
    assert "STALE" not in report


def test_code_match_report_stale(control) -> None:
    info = {"commit": "aaa111", "started": "2026-09-28T01:00:00+00:00"}
    report = control.code_match_report(info, "bbb222")
    assert "STALE" in report
    assert "aaa111" in report and "bbb222" in report
    assert "stop" in report


def test_code_match_report_no_info(control) -> None:
    report = control.code_match_report(None, "bbb222")
    assert "unknown" in report.lower()
    assert "Restart" in report


def test_code_match_report_unknown_running_commit(control) -> None:
    info = {"commit": "unknown", "started": "2026-09-29T01:00:00+00:00"}
    report = control.code_match_report(info, "bbb222")
    assert "restart" in report.lower()


def test_daemon_writes_info_at_start(daemon, monkeypatch, tmp_path) -> None:
    import json

    monkeypatch.setattr(daemon, "DAEMON_INFO_PATH", tmp_path / "daemon-info.json")
    info = daemon._write_daemon_info()
    assert info["commit"]
    assert info["argv"]
    assert info["pid"] > 0
    on_disk = json.loads((tmp_path / "daemon-info.json").read_text())
    assert on_disk["commit"] == info["commit"]
    assert on_disk["argv"] == info["argv"]


# --------------------------------------------- push-to-talk session control


class FakeSessionWorker:
    """Stand-in for VoiceWorker: records session actions, no subprocesses."""

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.paused = 0
        self.resumed = 0
        self._running = False
        self._busy = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def busy(self) -> bool:
        return self._busy

    def start(self) -> bool:
        self.started += 1
        self._running = True
        return True

    def stop(self) -> None:
        self.stopped += 1
        self._running = False

    def pause_listening(self) -> None:
        self.paused += 1

    def resume_listening(self) -> None:
        self.resumed += 1


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def ptt(daemon):
    clock = FakeClock()
    worker = FakeSessionWorker()
    controller = daemon.PTTController(worker=worker, idle_timeout_s=20.0, clock=clock)
    return controller, worker, clock


def test_ptt_press_starts_one_session(ptt) -> None:
    controller, worker, _clock = ptt
    controller.on_press_start()
    controller.on_press_start()  # held through a repeat: still one worker
    assert worker.started == 1
    assert worker.running


def test_ptt_release_pauses_mic_but_keeps_the_session(ptt) -> None:
    controller, worker, _clock = ptt
    controller.on_press_start()
    controller.on_release()
    assert worker.paused == 1
    assert worker.stopped == 0
    assert worker.running


def test_ptt_second_hold_reopens_listening_in_the_same_session(ptt) -> None:
    controller, worker, _clock = ptt
    controller.on_press_start()
    controller.on_release()
    controller.on_press_start()  # no second worker
    assert worker.started == 1
    assert worker.resumed == 1
    assert worker.running


def test_ptt_idle_timeout_closes_the_session(ptt) -> None:
    controller, worker, clock = ptt
    controller.on_press_start()
    clock.advance(19.9)
    assert controller.tick(clock()) is False
    clock.advance(0.2)
    assert controller.tick(clock()) is True
    assert worker.stopped == 1
    assert not worker.running


def test_ptt_busy_worker_delays_the_idle_close(ptt) -> None:
    controller, worker, clock = ptt
    controller.on_press_start()
    worker._busy = True  # she is speaking or a task is running
    clock.advance(60.0)
    assert controller.tick(clock()) is False
    assert worker.running
    worker._busy = False  # finished: the next tick closes her down
    assert controller.tick(clock()) is True
    assert worker.stopped == 1


def test_ptt_speech_resets_the_idle_timer(ptt) -> None:
    controller, worker, clock = ptt
    controller.on_press_start()
    clock.advance(19.0)
    controller.on_speech(clock())  # she hears or says something
    clock.advance(19.0)
    assert controller.tick(clock()) is False
    assert worker.running


def test_ptt_shortcut_toggles(ptt) -> None:
    controller, worker, _clock = ptt
    controller.on_shortcut()  # press: start
    assert worker.started == 1
    controller.on_shortcut()  # press again: stop
    assert worker.stopped == 1


def test_ptt_tick_with_no_session_is_quiet(ptt) -> None:
    controller, _worker, clock = ptt
    assert controller.tick(clock()) is False


def test_voice_worker_mic_gate_writes_the_file(daemon, monkeypatch, tmp_path) -> None:
    from hotkey import read_ptt_mic_enabled

    class FakeProc:
        def poll(self):
            return None  # still running

    monkeypatch.setenv("HOME", str(tmp_path))
    worker = daemon.VoiceWorker(log_path=tmp_path / "voice-worker.log")
    worker._proc = FakeProc()  # pretend a session is open; no subprocess started
    worker.pause_listening()
    assert read_ptt_mic_enabled() is False
    worker.resume_listening()
    assert read_ptt_mic_enabled() is True


def test_voice_worker_busy_reads_speech_and_task_state(
    daemon, monkeypatch, tmp_path
) -> None:
    import latency

    monkeypatch.setattr(latency, "ACTIVITY_PATH", tmp_path / "voice-activity.json")
    worker = daemon.VoiceWorker(log_path=tmp_path / "voice-worker.log")
    assert worker.busy is False
    latency.note_speech_activity()
    assert worker.busy is False  # a heartbeat alone is not "busy"
    latency.note_agent_speaking(True)
    assert worker.busy is True
    latency.note_agent_speaking(False)
    latency.note_task_running(True)
    assert worker.busy is True
    latency.note_task_running(False)
    assert worker.busy is False
