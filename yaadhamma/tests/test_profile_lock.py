"""Tests for Chromium profile-lock contention detection (src/browser.py).

On 2026-09-24 the WhatsApp "not paired" report was ambiguous: one plausible
cause is the sign-in browser still holding the profile lock on
~/.yaadhamma/chrome-profile while the agent tried to launch. The launch must
then fail with a clear "close that window" message — never a misleading
"not paired". No Chromium needed: the lock files are faked on disk.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from browser import (
    BrowserError,
    BrowserManager,
    _another_process_holds_profile,
)


def _write_lock(profile_dir: Path, pid: int) -> None:
    # Chromium's process-singleton lock: SingletonLock is a symlink to
    # "<host>-<pid>" while a browser owns the profile.
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "SingletonLock").symlink_to(f"fakehost-{pid}")


def _dead_pid() -> int:
    proc = subprocess.Popen(["sleep", "0.05"])
    pid = proc.pid
    proc.wait()
    return pid


def test_no_lock_files_means_free(tmp_path):
    assert _another_process_holds_profile(tmp_path) is False


def test_live_holder_pid_is_detected(tmp_path):
    _write_lock(tmp_path, os.getppid())  # parent shell: alive, not us
    assert _another_process_holds_profile(tmp_path) is True


def test_stale_lock_from_crashed_browser_is_ignored(tmp_path):
    _write_lock(tmp_path, _dead_pid())
    assert _another_process_holds_profile(tmp_path) is False


def test_our_own_lock_is_not_a_conflict(tmp_path):
    _write_lock(tmp_path, os.getpid())
    assert _another_process_holds_profile(tmp_path) is False


def test_launch_refuses_when_profile_is_held(monkeypatch, tmp_path):
    monkeypatch.setattr("browser._another_process_holds_profile", lambda _d: True)
    manager = BrowserManager(profile_dir=tmp_path)
    with pytest.raises(BrowserError, match="in use by another window"):
        asyncio.run(manager._launch())


def test_launch_proceeds_when_profile_is_free(monkeypatch, tmp_path):
    # The pre-flight check must not block a free profile: it returns False
    # and the launch continues (here it fails later at Playwright startup,
    # which we fake — the point is the check itself passed).
    monkeypatch.setattr("browser._another_process_holds_profile", lambda _d: False)

    class _BoomPlaywright:
        async def start(self):
            raise RuntimeError("playwright unavailable in test")

    monkeypatch.setattr("browser.async_playwright", lambda: _BoomPlaywright())
    manager = BrowserManager(profile_dir=tmp_path)
    with pytest.raises(RuntimeError, match="playwright unavailable"):
        asyncio.run(manager._launch())
