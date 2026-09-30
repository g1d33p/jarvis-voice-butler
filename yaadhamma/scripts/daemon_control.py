#!/usr/bin/env python3
"""Control the Yaadhamma always-on daemon (macOS launchd).

Usage:
    python scripts/daemon_control.py install    # start at login + now
    python scripts/daemon_control.py uninstall  # stop and remove
    python scripts/daemon_control.py status
    python scripts/daemon_control.py start
    python scripts/daemon_control.py stop
"""

from __future__ import annotations

import json
import os
import plistlib
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
LABEL = "com.yaadhamma.daemon"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = Path.home() / ".yaadhamma" / "daemon.log"
SESSION_STATE_PATH = Path.home() / ".yaadhamma" / "voice-session.json"


def _launchd_only() -> int | None:
    if sys.platform != "darwin":
        print("The daemon runs on macOS (launchd). This machine is not macOS,")
        print("so install/start/stop are unavailable here.")
        return 1
    return None


def _program() -> list[str]:
    import shutil

    uv = shutil.which("uv")
    if uv:
        # The extras must ride along: plain `uv run` re-syncs the project
        # environment and would uninstall the wake/UI packages on every start.
        return [
            uv,
            "run",
            "--extra",
            "wake",
            "--extra",
            "ui",
            "scripts/yaadhamma_daemon.py",
        ]
    return [sys.executable, "scripts/yaadhamma_daemon.py"]


def install() -> int:
    if (code := _launchd_only()) is not None:
        return code
    LOG.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": _program(),
        "WorkingDirectory": str(PROJECT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "")},
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    with PLIST.open("wb") as fh:
        plistlib.dump(plist, fh)
    subprocess.run(["launchctl", "load", "-w", str(PLIST)], check=False)
    print(f"Installed {LABEL}; she starts at login and is running now.")
    print(f"Logs: {LOG}")
    return 0


def uninstall() -> int:
    if (code := _launchd_only()) is not None:
        return code
    subprocess.run(["launchctl", "unload", "-w", str(PLIST)], check=False)
    if PLIST.exists():
        PLIST.unlink()
    print(f"Uninstalled {LABEL}.")
    return 0


def status() -> int:
    if (code := _launchd_only()) is not None:
        return code
    result = subprocess.run(
        ["launchctl", "list", LABEL], capture_output=True, text=True
    )
    if result.returncode != 0 or "Could not find service" in result.stderr:
        print("Daemon is not loaded.")
        return 1
    print(result.stdout.strip())
    print()
    info = _daemon_info()
    print(format_mode(info))
    print(format_session_state(_session_state(), _pid_alive(info)))
    print(format_whatsapp_pairing())
    print()
    print(code_match_report(info, _checked_out_commit()))
    return 0


def _daemon_info() -> dict | None:
    """What the running daemon recorded about itself at startup."""
    try:
        import json

        data = json.loads((Path.home() / ".yaadhamma" / "daemon-info.json").read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _pid_alive(info: dict | None) -> bool | None:
    """Whether the daemon's recorded pid is still running.

    None when there is no pid to check (no record, or an old record
    without one). Uses signal 0: no process is harmed.
    """
    if not info:
        return None
    pid = info.get("pid")
    if not isinstance(pid, int):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # a process exists, it is just not ours
    except Exception:
        return None
    return True


def _session_state() -> dict | None:
    """The daemon's last session open/close record. Never raises."""
    try:
        import json

        data = json.loads(SESSION_STATE_PATH.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def format_mode(info: dict | None) -> str:
    """One line: which input path the running daemon listens on."""
    if not info:
        return "Listening via: unknown (the daemon never recorded its mode)."
    mode = info.get("mode") or "unknown"
    return f"Listening via: {mode}."


def format_session_state(state: dict | None, daemon_alive: bool | None) -> str:
    """One line: whether a voice session is currently open.

    Pure (no disk, no signals) so tests can cover it. A stale "open"
    record with a dead daemon is reported as closed, not open — the
    daemon writes the record on close, so "open" with no living daemon
    means it died without writing.
    """
    if not state:
        return (
            "Session: no record — no voice session has opened under this "
            "daemon (or it predates session tracking; restart it)."
        )
    if state.get("session_open"):
        if daemon_alive is False:
            return (
                "Session: the record says a session was open, but the "
                "daemon is not running — treating it as closed."
            )
        when = state.get("at", "?")
        return f"Session: a voice session is open (opened {when})."
    return "Session: no voice session open right now."


def _latest_whatsapp_health() -> dict | None:
    """The most recent recorded WhatsApp health check, if any.

    Read straight from the SQLite store — status must never launch a
    browser just to answer "is WhatsApp paired?".
    """
    try:
        db_path = Path.home() / ".yaadhamma" / "yaadhamma.db"
        with sqlite3.connect(db_path) as db:
            row = db.execute(
                "SELECT checked, ok, report FROM whatsapp_health "
                "ORDER BY checked DESC, rowid DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        checked, ok, report = row
        return {"time": checked, "ok": bool(ok), **json.loads(report)}
    except Exception:
        return None


def format_whatsapp_pairing(latest: dict | None = None) -> str:
    """One line: whether the digest WhatsApp profile is paired.

    Takes an optional pre-read health record so tests can cover it without
    touching the real database.
    """
    if latest is None:
        latest = _latest_whatsapp_health()
    if not latest:
        return "WhatsApp: no health check recorded yet."
    checks = {c["name"]: c for c in latest.get("checks", [])}
    paired = checks.get("paired")
    when = latest.get("time", "?")
    if paired is None:
        return f"WhatsApp: pairing unknown (last checked {when})."
    if paired.get("ok"):
        return f"WhatsApp: paired (last checked {when})."
    return (
        "WhatsApp: NOT PAIRED — the digest profile lost its pairing "
        f"(last checked {when}). Chat summaries and phone commands are "
        "paused until it is re-paired."
    )
    """What the running daemon recorded about itself at startup."""
    path = Path.home() / ".yaadhamma" / "daemon-info.json"
    try:
        import json

        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _checked_out_commit() -> str | None:
    result = subprocess.run(
        ["git", "-C", str(PROJECT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def code_match_report(info: dict | None, head: str | None) -> str:
    """Whether the running daemon matches the checked-out code.

    Pure function (no launchd, no git) so tests can cover it. In 2026-09 a
    daemon ran a day on pre-fix code with no visible clue; this is the
    check that would have caught it.
    """
    if not info:
        return (
            "Code version: unknown — the running daemon never recorded its "
            "version (it predates this check). Restart it to run the current "
            "code: python scripts/daemon_control.py stop, then start."
        )
    running = info.get("commit") or "unknown"
    lines = [
        f"Running daemon: commit {running[:12]} (started {info.get('started', '?')})."
    ]
    if info.get("argv"):
        lines.append(f"Launch args: {' '.join(info['argv'])}")
    if not head:
        lines.append("Could not determine the checked-out commit.")
    elif running == "unknown":
        lines.append(
            "The running daemon could not determine its own commit; restart "
            "it after pulling to be sure it runs the latest code."
        )
    elif running == head:
        lines.append(f"Matches the checked-out code ({head[:12]}).")
    else:
        lines.append(
            f"STALE: the running daemon is on {running[:12]} but the checkout "
            f"is at {head[:12]}. Restart it to run the latest code: "
            "python scripts/daemon_control.py stop, then start."
        )
    return "\n".join(lines)


def start() -> int:
    if (code := _launchd_only()) is not None:
        return code
    subprocess.run(["launchctl", "load", "-w", str(PLIST)], check=False)
    return status()


def stop() -> int:
    if (code := _launchd_only()) is not None:
        return code
    subprocess.run(["launchctl", "unload", "-w", str(PLIST)], check=False)
    print("Daemon stopped (it will not restart until you run start).")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {
        "install",
        "uninstall",
        "status",
        "start",
        "stop",
    }:
        print(__doc__.strip())
        return 2
    return {
        "install": install,
        "uninstall": uninstall,
        "status": status,
        "start": start,
        "stop": stop,
    }[argv[1]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
