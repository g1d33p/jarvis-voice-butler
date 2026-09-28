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


def test_wake_off_exits_quietly(daemon, monkeypatch, caplog) -> None:
    monkeypatch.setenv("YAADHAMMA_WAKE", "off")
    assert daemon.main() == 0


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


def test_voice_worker_start_stop(daemon, monkeypatch) -> None:
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

    worker = daemon.VoiceWorker()
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


def test_voice_worker_keeps_extras(daemon, monkeypatch) -> None:
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

    worker = daemon.VoiceWorker()
    worker.start()
    assert started[0][:2] == ["/usr/bin/uv", "run"]
    assert "wake" in started[0] and "ui" in started[0]
    assert started[0][-1] == "src/agent.py"
    worker.stop()
