#!/usr/bin/env python3
"""Yaadhamma self-test: real checks, no network, no browser, no paid models.

Every check exercises the actual code paths (parsing, storage, guards)
against throwaway directories. A failure prints what failed and why;
exit code is non-zero when anything fails.

Usage: uv run scripts/selftest.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "src"))

CHECKS: list[tuple[str, object]] = []


def check(name: str):
    def decorator(fn):
        CHECKS.append((name, fn))
        return fn

    return decorator


@check("config loads with sane settings")
def _config():
    import config

    assert config.TIDY_MODE in ("apply", "propose"), config.TIDY_MODE
    assert config.email_tidy_settings()["mode"] in ("apply", "propose")
    assert config.MONTHLY_BUDGET_USD > 0
    assert config.wake_settings()["idle_timeout_s"] == 90
    assert config.remote_settings()["self_chats"] == [
        "19408438446",
        "919640520634",
    ]
    return f"TIDY_MODE={config.TIDY_MODE}, budget=${config.MONTHLY_BUDGET_USD:g}"


@check("due-date parser")
def _due_dates():
    from planner import parse_due_date

    now = datetime(2026, 9, 28, 10, 0, 0)  # a Monday
    cases = {
        "call Ravi back by 5pm": datetime(2026, 9, 28, 17, 0),
        "pay the bill tomorrow": datetime(2026, 9, 29, 18, 0),
        "review the PR Monday 9am": datetime(2026, 10, 5, 9, 0),
        "renew the domain Jan 5": datetime(2027, 1, 5, 18, 0),
        "finish the report end of week": datetime(2026, 10, 2, 18, 0),
    }
    for text, expected in cases.items():
        got = parse_due_date(text, now)
        assert got == expected, f"{text!r}: got {got}, want {expected}"
    for vague in ("look into that soon", "call me when you can", "call Ravi"):
        assert parse_due_date(vague, now) is None, f"{vague!r} should be None"
    return f"{len(cases)} dated + 3 vague parsed correctly"


@check("commitment store round-trip")
def _commitments():
    import task_store

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HOME"] = tmp
        store = task_store.CommitmentStore()
        task = store.committed_task(
            "call Ravi back", datetime(2026, 9, 28, 17, 0), "selftest"
        )
        assert len(store.pending()) == 1
        assert len(store.due_today(datetime(2026, 9, 28, 10))) == 1
        assert len(store.overdue(datetime(2026, 9, 29, 10))) == 1
        done = store.mark_done(task_id=task["id"])
        assert done["status"] == "completed"
        assert store.pending() == []
    return "commit -> pending -> due/overdue -> done"


@check("commitment completion is explicit")
def _explicit_done():
    import task_store

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HOME"] = tmp
        store = task_store.CommitmentStore()
        store.committed_task("call Ravi", None, "selftest")
        store.committed_task("email Ravi", None, "selftest")
        try:
            store.mark_done(text="ravi")
        except LookupError:
            pass
        else:
            raise AssertionError("ambiguous match completed something")
        assert len(store.pending()) == 2
    return "ambiguous name completed nothing"


@check("email tidy classification rules")
def _classify():
    from email_tidy import classify_email, is_person, should_archive

    def meta(sender="", **kw):
        base = {
            "from": sender,
            "subject": "",
            "date": datetime(2026, 9, 28, 10),
            "list_unsubscribe": False,
        }
        base.update(kw)
        return base

    assert classify_email(meta(subject="Your application for ML Engineer")) == (
        "Yaadhamma/Jobs"
    )
    assert classify_email(
        meta(sender="Bank <alerts@bank.com>", subject="Your statement is ready")
    ) == ("Yaadhamma/Finance")
    assert classify_email(meta(subject="Receipt for order #1234")) == (
        "Yaadhamma/Receipts"
    )
    assert classify_email(
        meta(
            sender="News <news@example.com>",
            subject="Weekly digest",
            list_unsubscribe=True,
        )
    ) == ("Yaadhamma/Newsletters")
    assert is_person(meta(sender="Ravi Kumar <ravi@example.com>")) is True
    assert is_person(meta(sender="News <newsletter@acme.com>")) is False
    # Guards: never archive from a person, never archive unlabelled mail.
    person_old = meta(
        sender="Ravi Kumar <ravi@example.com>",
        subject="Weekly newsletter",
        date=datetime(2026, 8, 1, 10),
        list_unsubscribe=True,
    )
    assert (
        should_archive(person_old, "Yaadhamma/Newsletters", datetime(2026, 9, 28, 12))
        is False
    )
    assert should_archive(person_old, None, datetime(2026, 9, 28, 12)) is False
    bulk_old = meta(
        sender="Store <deals@store.com>",
        subject="Sale",
        date=datetime(2026, 8, 1, 10),
        list_unsubscribe=True,
    )
    assert (
        should_archive(bulk_old, "Yaadhamma/Newsletters", datetime(2026, 9, 28, 12))
        is True
    )
    return "4 labels, person detection, archive guards"


@check("gmail scope detection")
def _scopes():
    import json

    import gmail

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HOME"] = tmp
        assert gmail.has_modify_scope("personal1") is False
        token_dir = Path(tmp) / ".yaadhamma"
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / "gmail-token-personal1.json").write_text(
            json.dumps({"scope": "https://www.googleapis.com/auth/gmail.modify"})
        )
        assert gmail.has_modify_scope("personal1") is True
    return "missing scope -> False, scoped token -> True"


@check("file tidy apply guards")
def _tidy_guards():
    import tidy

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HOME"] = tmp
        root = Path(tmp) / "inbox"
        root.mkdir()
        (root / "a.txt").write_text("a")
        (root / "b.txt").write_text("b")
        dest = Path(tmp) / "sorted"
        dest.mkdir()
        (dest / "b.txt").write_text("existing")  # collision: must not overwrite
        plan = tidy.TidyPlan(
            created=datetime.now().isoformat(),
            moves=[
                tidy.Move(
                    source=str(root / "a.txt"),
                    destination=str(dest / "a.txt"),
                    group="Text",
                ),
                tidy.Move(
                    source=str(root / "b.txt"),
                    destination=str(dest / "b.txt"),
                    group="Text",
                ),
                tidy.Move(
                    source=str(root / "gone.txt"),
                    destination=str(dest / "gone.txt"),
                    group="Text",
                ),
            ],
        )
        log = tidy.TidyLog()
        done = tidy.apply_plan(plan, log)
        assert done["moved"] == 2, done
        assert done["verified"] is True
        assert (dest / "a.txt").read_text() == "a"
        assert (dest / "b.txt").read_text() == "existing"  # untouched
        assert not (root / "a.txt").exists()
        assert len(done["skipped"]) == 1  # gone.txt
    return "2 verified moves, 1 collision kept, 1 missing skipped"


@check("remote command detection")
def _remote():
    import remote

    assert remote.is_command("Yaadhamma remind me to call mom") is not None
    assert remote.is_command("yaadhamma what is due today") is not None
    assert remote.is_command("hello there") is None
    assert remote.in_window(datetime(2026, 9, 28, 10, 0)) is True
    assert remote.in_window(datetime(2026, 9, 28, 3, 0)) is False
    return "command prefix + hours window"


@check("settings reference is complete")
def _settings_docs():
    documented = set(
        re.findall(r"`(YAADHAMMA_[A-Z_]+)`", (PROJECT / "docs/SETTINGS.md").read_text())
    )
    used: set[str] = set()
    for path in list((PROJECT / "src").glob("*.py")) + list(
        (PROJECT / "scripts").glob("*.py")
    ):
        used.update(re.findall(r"YAADHAMMA_[A-Z_]+", path.read_text(errors="ignore")))
    missing = sorted(v for v in used if v not in documented and v != "YAADHAMMA_DIR")
    assert not missing, f"undocumented settings: {missing}"
    return f"{len(used)} variables read, all documented"


@check("wake-word packages importable")
def _wake_packages():
    import importlib.util

    missing = [
        package
        for package in ("openwakeword", "sounddevice", "numpy")
        if importlib.util.find_spec(package) is None
    ]
    assert not missing, (
        f"missing wake-word packages: {', '.join(missing)} — "
        "on the Mac run: uv sync --extra wake"
    )
    return "openwakeword, sounddevice, numpy present"


@check("menu-bar UI packages importable")
def _ui_packages():
    import importlib.util
    import sys

    if sys.platform != "darwin":
        return "skipped: rumps is macOS-only"
    assert importlib.util.find_spec("rumps") is not None, (
        "rumps is missing — on the Mac run: uv sync --extra ui"
    )
    return "rumps present"


@check("microphone permission")
def _mic_permission():
    import sys

    if sys.platform != "darwin":
        return "skipped: not macOS"
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
    except ImportError:
        return (
            "unknown: pyobjc is not installed (uv sync --extra ui brings it); "
            "the daemon will ask for microphone access on first start"
        )
    status = AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
    if status == 3:  # AVAuthorizationStatusAuthorized
        return "microphone access authorized"
    if status in (1, 2):  # restricted / denied
        raise AssertionError(
            "microphone access is denied — on the Mac: System Settings > "
            "Privacy & Security > Microphone, enable access, then restart "
            "the daemon"
        )
    return (
        "not determined yet — the daemon will ask for microphone access on first start"
    )


@check("daemon job loaded")
def _daemon_job():
    import re
    import subprocess
    import sys

    label = "com.yaadhamma.daemon"
    if sys.platform != "darwin":
        return "skipped: not macOS (launchd)"
    result = subprocess.run(
        ["launchctl", "list", label], capture_output=True, text=True, timeout=10
    )
    combined = (result.stderr or "") + (result.stdout or "")
    if result.returncode != 0 or "Could not find service" in combined:
        raise AssertionError(
            f"daemon job {label} is not loaded — on the Mac run: "
            "python scripts/daemon_control.py install"
        )
    pid = re.search(r'"PID" = (\d+);', result.stdout or "")
    last = re.search(r'"LastExitStatus" = (\d+);', result.stdout or "")
    detail = f"PID {pid.group(1)}" if pid else "loaded"
    if last and int(last.group(1)) != 0:
        raise AssertionError(
            f"daemon is loaded ({detail}) but its last exit status was "
            f"{last.group(1)} — check: tail -30 ~/.yaadhamma/daemon.log"
        )
    return f"loaded ({detail}), last exit status 0"


@check("wake-word settings are complete")
def _wake_config():
    import config

    settings = config.wake_settings()
    if not settings["enabled"]:
        return "wake word disabled (YAADHAMMA_WAKE=off)"
    if settings["engine"] == "porcupine":
        assert settings["picovoice_key"], (
            "YAADHAMMA_WAKE_ENGINE=porcupine needs YAADHAMMA_PICOVOICE_KEY "
            "— Picovoice ended its free tier in June 2026 (enterprise only)"
        )
        assert settings["keyword_path"], (
            "YAADHAMMA_WAKE_ENGINE=porcupine needs YAADHAMMA_WAKE_PPN "
            "pointing at the trained .ppn file"
        )
    return f"engine={settings['engine']}, phrase={settings['phrase']!r}"


def main() -> int:
    # HOME is repointed per-check; start from a clean slate.
    os.environ.pop("HOME", None)
    real_home = str(Path("~").expanduser())
    failures = 0
    for name, fn in CHECKS:
        os.environ["HOME"] = real_home
        try:
            detail = fn()
        except Exception as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
            traceback.print_exc(limit=3)
        else:
            print(f"PASS {name} — {detail}")
    total = len(CHECKS)
    print(f"\n{total - failures}/{total} checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
