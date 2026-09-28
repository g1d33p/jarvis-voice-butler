#!/usr/bin/env python3
"""Install the WhatsApp remote poll as its own launchd job.

Polls his own chats every 2 minutes; remote.py itself enforces the
08:00-23:00 window and the YAADHAMMA_REMOTE=off switch.

    uv run scripts/remote_schedule.py on      # install + load
    uv run scripts/remote_schedule.py off     # unload + remove
    uv run scripts/remote_schedule.py         # status
"""

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
AGENTS = Path.home() / "Library" / "LaunchAgents"
LOG = Path.home() / ".yaadhamma" / "remote.log"
LABEL = "com.yaadhamma.remote"


def build_plist(uv_path: str, project: Path = PROJECT) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [uv_path, "run", "scripts/remote_poll.py"],
        "WorkingDirectory": str(project),
        "StartInterval": 120,  # every 2 minutes; the 08:00-23:00 window is in code
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "RunAtLoad": False,
    }


def _plist_path() -> Path:
    return AGENTS / f"{LABEL}.plist"


def install() -> int:
    uv = shutil.which("uv")
    if not uv:
        print(
            "Could not find the 'uv' command. Run this from the same Terminal you use for Yaadhamma."
        )
        return 1
    LOG.parent.mkdir(parents=True, exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)
    path = _plist_path()
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    with open(path, "wb") as f:
        plistlib.dump(build_plist(uv), f)
    done = subprocess.run(
        ["launchctl", "load", str(path)], capture_output=True, text=True
    )
    if done.returncode != 0:
        print(f"macOS refused {LABEL}: {done.stderr.strip()}")
        return 1
    print(
        "Scheduled: WhatsApp remote poll every 2 minutes (08:00-23:00, Mac local time)."
    )
    print(f"Log: {LOG}")
    return 0


def uninstall() -> int:
    path = _plist_path()
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    path.unlink(missing_ok=True)
    print("WhatsApp remote poll removed.")
    return 0


def status() -> int:
    loaded = (
        subprocess.run(["launchctl", "list", LABEL], capture_output=True).returncode
        == 0
    )
    on = loaded and _plist_path().exists()
    print(f"WhatsApp remote poll: {'on' if on else 'off'}")
    return 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == "on":
        raise SystemExit(install())
    if command == "off":
        raise SystemExit(uninstall())
    raise SystemExit(status())
