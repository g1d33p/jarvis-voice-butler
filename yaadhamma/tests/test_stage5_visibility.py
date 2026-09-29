"""Stage 5 (v2): failure visibility.

Covers the new self-test surface, the morning-brief warnings, and the
daemon_control status additions. The live paths (launchd, the real
microphone, the paired WhatsApp session) are Mac-only; everything here
runs against fakes and scratch directories.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SRC = Path(__file__).resolve().parents[1] / "src"

sys.path.insert(0, str(SRC))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def daemon():
    return _load("yaadhamma_daemon_stage5", "yaadhamma_daemon.py")


@pytest.fixture()
def daemon_control():
    return _load("daemon_control_stage5", "daemon_control.py")


@pytest.fixture()
def digest():
    import digest as digest_module

    return digest_module


class FakeProc:
    """A worker process the test drives by hand."""

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._alive = False


def _worker(daemon, monkeypatch, tmp_path):
    monkeypatch.setattr(daemon.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(
        daemon, "_which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    state_path = tmp_path / "voice-session.json"
    monkeypatch.setattr(daemon, "SESSION_STATE_PATH", state_path)
    worker = daemon.VoiceWorker(
        log_path=tmp_path / "voice-worker.log", state_path=state_path
    )
    return worker, state_path


def _state_of(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


# --- daemon: session open/close records -------------------------------------


def test_session_opens_when_worker_starts(daemon, monkeypatch, tmp_path) -> None:
    worker, state_path = _worker(daemon, monkeypatch, tmp_path)
    assert worker.start() is True
    state = _state_of(state_path)
    assert state is not None and state["session_open"] is True
    worker.stop()


def test_session_closes_when_worker_stops(daemon, monkeypatch, tmp_path) -> None:
    worker, state_path = _worker(daemon, monkeypatch, tmp_path)
    worker.start()
    worker.stop()
    state = _state_of(state_path)
    assert state is not None and state["session_open"] is False


def test_session_closes_when_worker_exits_on_its_own(
    daemon, monkeypatch, tmp_path
) -> None:
    worker, state_path = _worker(daemon, monkeypatch, tmp_path)
    worker.start()
    worker._proc._alive = False  # the process died by itself
    worker.note_exit()
    state = _state_of(state_path)
    assert state is not None and state["session_open"] is False


def test_suppressed_start_writes_no_session_state(
    daemon, monkeypatch, tmp_path
) -> None:
    worker, state_path = _worker(daemon, monkeypatch, tmp_path)
    worker._suppress_until = 9999999999.0  # suppressed: nothing launches
    assert worker.start() is False
    assert not state_path.exists()


def test_detection_mode_names_the_active_input(daemon, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(daemon, "DAEMON_INFO_PATH", tmp_path / "daemon-info.json")
    monkeypatch.setenv("YAADHAMMA_WAKE", "off")
    monkeypatch.delenv("YAADHAMMA_PTT", raising=False)  # default: on
    info = daemon._write_daemon_info()
    assert info["mode"] == "push-to-talk"
    monkeypatch.setenv("YAADHAMMA_WAKE", "on")
    assert daemon._detection_mode() == "wake word + push-to-talk"
    monkeypatch.setenv("YAADHAMMA_PTT", "off")
    assert daemon._detection_mode() == "wake word"
    monkeypatch.setenv("YAADHAMMA_WAKE", "off")
    assert daemon._detection_mode() == "none (detection off)"


# --- daemon_control: status lines -------------------------------------------


def test_status_shows_mode_session_and_code(daemon_control) -> None:
    assert daemon_control.format_mode({"mode": "push-to-talk"}) == (
        "Listening via: push-to-talk."
    )
    assert "unknown" in daemon_control.format_mode(None)
    open_state = {"session_open": True, "at": "2026-09-29T00:00:00+00:00"}
    line = daemon_control.format_session_state(open_state, True)
    assert "a voice session is open" in line
    # A stale "open" with a dead daemon must not read as open.
    line = daemon_control.format_session_state(open_state, False)
    assert "not running" in line and "closed" in line
    line = daemon_control.format_session_state({"session_open": False}, True)
    assert "no voice session open" in line
    assert "no record" in daemon_control.format_session_state(None, None)


def test_pid_alive_reports_honestly(daemon_control) -> None:
    assert daemon_control._pid_alive({"pid": os.getpid()}) is True
    assert daemon_control._pid_alive({"pid": 2**30}) is False
    assert daemon_control._pid_alive(None) is None
    assert daemon_control._pid_alive({}) is None


# --- digest: morning-brief warnings ------------------------------------------


def _daemon_info_file(tmp_path: Path, pid) -> Path:
    info_dir = tmp_path / ".yaadhamma"
    info_dir.mkdir(parents=True, exist_ok=True)
    path = info_dir / "daemon-info.json"
    path.write_text(json.dumps({"pid": pid, "mode": "push-to-talk"}))
    return path


def test_daemon_line_quiet_when_daemon_runs(digest, monkeypatch, tmp_path) -> None:
    _daemon_info_file(tmp_path, os.getpid())
    monkeypatch.setenv("HOME", str(tmp_path))
    assert digest._daemon_line() == ""


def test_daemon_line_loud_when_daemon_is_dead(digest, monkeypatch, tmp_path) -> None:
    _daemon_info_file(tmp_path, 2**30)
    monkeypatch.setenv("HOME", str(tmp_path))
    line = digest._daemon_line()
    assert "not running" in line
    assert "daemon_control.py start" in line


def test_daemon_line_loud_when_never_started(digest, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))  # no daemon-info.json here
    line = digest._daemon_line()
    assert "no startup record" in line


def test_detection_line_loud_only_when_nothing_listens(digest, monkeypatch) -> None:
    monkeypatch.setenv("YAADHAMMA_WAKE", "off")
    monkeypatch.setenv("YAADHAMMA_PTT", "off")
    line = digest._detection_line()
    assert "Nothing is listening" in line
    monkeypatch.delenv("YAADHAMMA_PTT", raising=False)  # default: on
    assert digest._detection_line() == ""
