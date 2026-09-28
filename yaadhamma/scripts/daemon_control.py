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
    return 0


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
