"""Tests for the always-on daemon's testable pieces.

The live wake loop needs a microphone and macOS launchd, so it is not
exercised here: real microphone behaviour is unverified
(see docs/BUILD_REPORT.md).
"""

import importlib.util
import json
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


def test_mute_file_missing_means_unmuted(daemon, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(daemon, "control_path", lambda: str(tmp_path / "nope.json"))
    assert daemon._read_muted() is False


def test_mute_file_round_trip(daemon, monkeypatch, tmp_path) -> None:
    path = tmp_path / "wake-control.json"
    monkeypatch.setattr(daemon, "control_path", lambda: str(path))
    path.write_text(json.dumps({"muted": True}))
    assert daemon._read_muted() is True
    path.write_text(json.dumps({"muted": False}))
    assert daemon._read_muted() is False


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
