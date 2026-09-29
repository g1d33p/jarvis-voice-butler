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

import os
import plistlib
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
LABEL = "com.yaadhamma.daemon"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = Path.home() / ".yaadhamma" / "daemon.log"


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
    print(code_match_report(_daemon_info(), _checked_out_commit()))
    return 0


def _daemon_info() -> dict | None:
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
