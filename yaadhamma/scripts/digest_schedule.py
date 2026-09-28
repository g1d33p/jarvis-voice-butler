#!/usr/bin/env python3
"""Turn the scheduled digest and morning brief on or off (macOS launchd).

    uv run scripts/digest_schedule.py install     # learn 2am; tidy 3am; brief 8:45; digests
    uv run scripts/digest_schedule.py uninstall
    uv run scripts/digest_schedule.py status

Times are the Mac's local time. If the Mac is asleep at a scheduled time,
macOS runs the missed job when it wakes.
"""

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
LOG = Path.home() / ".yaadhamma" / "logs" / "digest.log"
AGENTS = Path.home() / "Library" / "LaunchAgents"

# (launchd label, [(hour, minute), ...], extra arguments, description)
JOBS = [
    ("com.yaadhamma.learn", [(2, 0)], ["--learn"], "Overnight learning at 2am"),
    (
        "com.yaadhamma.tidy",
        [(3, 0)],
        ["--tidy", "--email-tidy"],
        "File + email tidy-up at 3am, every other night",
    ),
    ("com.yaadhamma.morning", [(8, 45)], ["--morning"], "Morning brief at 8:45am"),
    ("com.yaadhamma.weekly", [(20, 0)], ["--weekly"], "Weekly plan, Sunday 8pm"),
    (
        "com.yaadhamma.digest",
        [(9, 0), (13, 0), (17, 0), (21, 0)],
        [],
        "Digest at 9am, 1pm, 5pm and 9pm",
    ),
]


# Jobs that run on one weekday only (0 = Sunday in launchd).
WEEKDAYS = {"com.yaadhamma.weekly": 0}


def build_plist(
    uv_path: str, label: str, times, extra, project: Path = PROJECT
) -> dict:
    schedule = [{"Hour": h, "Minute": m} for h, m in times]
    if label in WEEKDAYS:
        for entry in schedule:
            entry["Weekday"] = WEEKDAYS[label]
    return {
        "Label": label,
        "ProgramArguments": [uv_path, "run", "scripts/digest_run.py", *extra],
        "WorkingDirectory": str(project),
        "StartCalendarInterval": schedule,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "RunAtLoad": False,
    }


def _plist_path(label: str) -> Path:
    return AGENTS / f"{label}.plist"


def install() -> int:
    uv = shutil.which("uv")
    if not uv:
        print(
            "Could not find the 'uv' command. Run this from the same Terminal you use for Yaadhamma."
        )
        return 1
    LOG.parent.mkdir(parents=True, exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)
    for label, times, extra, description in JOBS:
        path = _plist_path(label)
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        with open(path, "wb") as f:
            plistlib.dump(build_plist(uv, label, times, extra), f)
        done = subprocess.run(
            ["launchctl", "load", str(path)], capture_output=True, text=True
        )
        if done.returncode != 0:
            print(f"macOS refused {label}: {done.stderr.strip()}")
            return 1
        print(f"Scheduled: {description} (Mac local time).")
    print(f"Log: {LOG}")
    return 0


def uninstall() -> int:
    for label, *_ in JOBS:
        path = _plist_path(label)
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        path.unlink(missing_ok=True)
    print("All Yaadhamma schedules removed.")
    return 0


def status() -> int:
    for label, _times, _extra, description in JOBS:
        loaded = (
            subprocess.run(["launchctl", "list", label], capture_output=True).returncode
            == 0
        )
        on = loaded and _plist_path(label).exists()
        print(f"{description}: {'on' if on else 'off'}")
    return 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    actions = {"install": install, "uninstall": uninstall, "status": status}
    if command not in actions:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(actions[command]())
