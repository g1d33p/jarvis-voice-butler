#!/usr/bin/env python3
"""Turn the scheduled Saayam digest on or off (macOS launchd).

    uv run scripts/digest_schedule.py install     # 9am, 1pm, 5pm, 9pm daily
    uv run scripts/digest_schedule.py uninstall
    uv run scripts/digest_schedule.py status

Times are the Mac's local time. If the Mac is asleep at a scheduled time,
macOS runs the missed digest when it wakes.
"""

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.yaadhamma.digest"
HOURS = (9, 13, 17, 21)
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
PROJECT = Path(__file__).resolve().parent.parent
LOG = Path.home() / ".yaadhamma" / "logs" / "digest.log"


def build_plist(uv_path: str, project: Path = PROJECT, hours=HOURS) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [uv_path, "run", "scripts/digest_run.py"],
        "WorkingDirectory": str(project),
        "StartCalendarInterval": [{"Hour": h, "Minute": 0} for h in hours],
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "RunAtLoad": False,
    }


def install() -> int:
    uv = shutil.which("uv")
    if not uv:
        print(
            "Could not find the 'uv' command. Run this from the same Terminal you use for Yaadhamma."
        )
        return 1
    LOG.parent.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
    with open(PLIST, "wb") as f:
        plistlib.dump(build_plist(uv), f)
    done = subprocess.run(
        ["launchctl", "load", str(PLIST)], capture_output=True, text=True
    )
    if done.returncode != 0:
        print(f"macOS refused the schedule: {done.stderr.strip()}")
        return 1
    times = ", ".join(f"{h % 12 or 12}{'am' if h < 12 else 'pm'}" for h in HOURS)
    print(f"Digest scheduled daily at {times} (Mac local time).")
    print(f"Log: {LOG}")
    return 0


def uninstall() -> int:
    subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
    PLIST.unlink(missing_ok=True)
    print("Digest schedule removed.")
    return 0


def status() -> int:
    loaded = (
        subprocess.run(["launchctl", "list", LABEL], capture_output=True).returncode
        == 0
    )
    print(f"Scheduled: {'yes' if loaded and PLIST.exists() else 'no'}")
    return 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    actions = {"install": install, "uninstall": uninstall, "status": status}
    if command not in actions:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(actions[command]())
