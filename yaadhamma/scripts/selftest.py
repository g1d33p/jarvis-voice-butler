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
    assert config.wake_settings()["idle_timeout_s"] == 20
    assert config.ptt_settings() == {
        "enabled": True,
        "key": "cmd_r",
        "hold_ms": 200,
        "idle_timeout_s": 20.0,
    }
    assert config.remote_settings()["self_chats"] == [
        "19408438446",
        "919640520634",
    ]
    return f"TIDY_MODE={config.TIDY_MODE}, budget=${config.MONTHLY_BUDGET_USD:g}"


def env_file_problems(text: str) -> list[str]:
    """Structural problems in a .env.local file's text.

    Split out from the check below so tests can feed it synthetic files.
    Catches the corruption that bit on 2026-09-29: a setting appended
    (e.g. with echo) without a preceding newline glues two lines into one,
    and dotenv silently keeps only the first KEY= — two phone numbers
    became one 23-digit value that surfaced as "no chat found".
    """
    problems: list[str] = []
    if text and not text.endswith("\n"):
        problems.append(
            "the file does not end with a newline — the next setting "
            "appended to it will glue onto the last line and corrupt it"
        )
    key_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
    pair_re = re.compile(r"[A-Z_][A-Z0-9_]*=")
    seen: dict[str, int] = {}
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        m = key_re.match(line)
        if not m:
            problems.append(f"line {lineno} is not a KEY=value setting: {line[:60]!r}")
            continue
        key = m.group(0)[:-1]
        value = line[m.end() :]
        if key in seen:
            problems.append(
                f"line {lineno}: {key} is already set on line {seen[key]} — "
                "the later value silently wins, so one of them is dead"
            )
        else:
            seen[key] = lineno
        # A second KEY= pair on the same line: the appended-setting glue.
        # Only all-caps keys count, so lowercase tokens and base64 padding
        # inside values are not flagged.
        glued = pair_re.findall(value)
        if glued:
            problems.append(
                f"line {lineno}: looks like two settings glued together "
                f"({key}=... then {glued[0]}=...) — fix it in a text editor, "
                "one setting per line"
            )
        if key.startswith("YAADHAMMA_"):
            v = value.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            # Comma-separated lists (e.g. YAADHAMMA_SELF_CHATS) are checked
            # part by part; a >15-digit run in one part is the concatenation
            # signature (E.164 numbers are at most 15 digits).
            for part in v.split(","):
                digits = "".join(ch for ch in part if ch.isdigit())
                if len(digits) > 15:
                    problems.append(
                        f"line {lineno}: {key} has {len(digits)} digits in "
                        "one value — implausible, probably two values "
                        "concatenated by a missing newline"
                    )
                    break
    return problems


@check(".env.local parses sanely")
def _env_local():
    path = PROJECT / ".env.local"
    if not path.exists():
        return "no .env.local — defaults in use"
    problems = env_file_problems(path.read_text())
    assert not problems, "; ".join(problems)
    return "no glued lines, no duplicate keys, no implausible values"


@check("remote poll failures are not repeating")
def _remote_poll_health():
    """The 2026-09 outage: every 2-minute poll crashed identically and the
    tracebacks only piled up in remote.log, which nobody reads. The streak
    file (written by scripts/remote_poll.py) makes the same failure loud
    here and in the morning brief."""
    from remote_health import needs_attention

    bad = needs_attention()
    assert bad is None, (
        f"remote poll failed {bad['count']} times in a row with: "
        f"{bad['signature']} (last {bad['last']}). "
        "See ~/.yaadhamma/remote.log for the tracebacks."
    )
    return "no repeated failures"


@check("remote orchestrator constructs")
def _remote_orchestrator():
    """Phone commands died silently because real_orchestrator_factory()
    called Orchestrator() with no arguments, so every poll crashed with
    TypeError. Build it exactly the way the poll does, with every store
    pointed at scratch so the check never touches real data."""
    import os
    import tempfile
    from pathlib import Path

    import task_manager
    from orchestrator import Orchestrator
    from remote import real_orchestrator_factory

    with tempfile.TemporaryDirectory(prefix="yaadhamma-selftest-") as tmp:
        scratch = Path(tmp)
        real_db = task_manager.DEFAULT_DB
        task_manager.DEFAULT_DB = scratch / "tasks.db"
        os.environ["YAADHAMMA_MEMORY_PATH"] = str(scratch / "memory.db")
        os.environ["YAADHAMMA_AUDIT_PATH"] = str(scratch / "audit.jsonl")
        try:
            orch = real_orchestrator_factory()
        finally:
            task_manager.DEFAULT_DB = real_db
            os.environ.pop("YAADHAMMA_MEMORY_PATH", None)
            os.environ.pop("YAADHAMMA_AUDIT_PATH", None)
    assert isinstance(orch, Orchestrator), f"expected Orchestrator, got {type(orch)}"
    tools = orch._tools()
    assert tools, "remote orchestrator built, but has no tools wired"
    return f"{len(tools)} tools wired"


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


@check("push-to-talk key resolves and hold detection works")
def _ptt_config():
    # Stage 1: right Command by default, a tap does nothing, the fn key is
    # honestly refused. Runs against a stand-in key enum and a fake timer so
    # the check needs neither pynput nor macOS.
    from types import SimpleNamespace

    import config
    from hotkey import HotkeyConfigError, HotkeyState, resolve_key

    settings = config.ptt_settings()
    assert settings["enabled"], "push-to-talk is disabled (YAADHAMMA_PTT=off)"

    class FakeKeys:
        cmd_r = "cmd_r"
        alt_r = "alt_r"
        ctrl_r = "ctrl_r"

    resolve_key(settings["key"], key_enum=FakeKeys)
    try:
        resolve_key("fn", key_enum=FakeKeys)
    except HotkeyConfigError:
        pass
    else:
        raise AssertionError("the fn key must not resolve — it is unsupported")

    fired: list[str] = []
    timers: list = []

    def fake_timer(delay, callback):
        return SimpleNamespace(
            start=lambda: timers.append(callback), cancel=lambda: None
        )

    state = HotkeyState(
        hold_ms=settings["hold_ms"],
        on_press_start=lambda: fired.append("press"),
        on_release=lambda: fired.append("release"),
        timer_factory=fake_timer,
    )
    state.key_down()
    state.key_up()  # a tap: nothing may fire
    assert fired == [], f"a tap must do nothing, got {fired}"
    state.key_down()
    for callback in timers:  # the hold threshold elapses while held
        callback()
    state.key_up()
    assert fired == ["press", "release"], f"hold/release mismatch: {fired}"
    return f"key={settings['key']}, hold={settings['hold_ms']} ms, tap-safe"


@check("push-to-talk listener is wired to the configured key")
def _ptt_listener():
    # Stage 5: the listener factory, the key routing and the start/stop
    # lifecycle — with a stand-in key enum and fake timer/listener so the
    # check needs neither pynput nor macOS. The real pynput resolution is
    # verified on macOS below.
    import sys
    from types import SimpleNamespace

    import config
    from hotkey import HotkeyListener, resolve_key

    settings = config.ptt_settings()

    class FakeKeys:
        cmd_r = "cmd_r"
        alt_r = "alt_r"
        ctrl_r = "ctrl_r"

    key = resolve_key(settings["key"], key_enum=FakeKeys)

    fired: list[str] = []
    timers: list = []
    callbacks: dict = {}

    def fake_timer(delay, callback):
        return SimpleNamespace(
            start=lambda: timers.append(callback), cancel=lambda: None
        )

    class FakeListener:
        def __init__(self, on_press, on_release):
            callbacks["on_press"] = on_press
            callbacks["on_release"] = on_release

        def start(self):
            fired.append("started")

        def stop(self):
            fired.append("stopped")

    listener = HotkeyListener(
        key=key,
        hold_ms=settings["hold_ms"],
        on_press_start=lambda: fired.append("press"),
        on_release=lambda: fired.append("release"),
        listener_factory=lambda on_press, on_release: FakeListener(
            on_press, on_release
        ),
        timer_factory=fake_timer,
    )
    listener.start()
    assert fired == ["started"], f"listener did not start: {fired}"
    callbacks["on_press"](key)  # the configured key, held…
    for callback in timers:  # …past the hold threshold
        callback()
    callbacks["on_press"]("other")
    callbacks["on_release"]("other")  # a wrong key must do nothing
    callbacks["on_release"](key)
    listener.stop()
    assert fired == ["started", "press", "release", "stopped"], (
        f"press/release did not route to the configured key only: {fired}"
    )

    if sys.platform == "darwin":
        try:
            resolve_key(settings["key"])  # the real pynput enum
        except Exception as exc:
            raise AssertionError(
                f"pynput cannot resolve {settings['key']!r} — "
                "on the Mac run: uv sync --extra wake --extra ui"
            ) from exc
        return f"key={settings['key']}: routed correctly, real pynput resolves"
    return f"key={settings['key']}: routed correctly (real pynput needs macOS)"


@check("wake state is readable")
def _wake_state():
    # Stage 5: the mute/listen control file the menu bar writes and the
    # daemon polls. Round-trips against a throwaway HOME, then reports the
    # live flags read-only.
    import os
    import tempfile

    from ui_state import read_control, write_control

    with tempfile.TemporaryDirectory(prefix="yaadhamma-selftest-") as tmp:
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = tmp
        try:
            assert read_control() == {"muted": False, "listening": True}, (
                "a missing control file must mean defaults"
            )
            write_control(muted=True)
            assert read_control()["muted"] is True
            write_control(listening=False)
            assert read_control() == {
                "muted": True,
                "listening": False,
            }, "flags must round-trip"
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
    live = read_control()
    return (
        "control file round-trips; live flags: "
        f"muted={live['muted']}, listening={live['listening']}"
    )


@check("remote poll job is loaded and its last outcome is known")
def _remote_poll():
    # Stage 5: the 2-minute phone-command poll must be loaded, and its last
    # outcome must be knowable — silent death was the 2026-09 failure mode.
    import subprocess
    import sys

    from remote_health import streak

    outcome = streak()
    outcome_txt = (
        f"{outcome.get('count')} consecutive failures: {outcome.get('signature')}"
        if outcome
        else "no failure streak on record"
    )
    if sys.platform != "darwin":
        return f"skipped: launchd is macOS-only; last outcome: {outcome_txt}"
    result = subprocess.run(
        ["launchctl", "list", "com.yaadhamma.remote"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    combined = (result.stderr or "") + (result.stdout or "")
    assert result.returncode == 0 and "Could not find service" not in combined, (
        "remote poll job com.yaadhamma.remote is not loaded — "
        "on the Mac run: python scripts/remote_schedule.py on"
    )
    return f"loaded; last outcome: {outcome_txt}"


@check("voice-note audio path is reachable")
def _voice_note_path():
    # Stage 5: the extractor must export the voice-note audio helper
    # (static, works anywhere); the live download path is Mac-only.
    import sys
    from pathlib import Path

    src = (PROJECT / "src" / "whatsapp_extractors.js").read_text()
    for name in ("waVoiceNote", "waVoiceNoteAudio"):
        assert name in src, (
            f"the WhatsApp extractor no longer exports {name} — "
            "voice-note downloads would break silently"
        )
    if sys.platform != "darwin":
        return "extractor exports waVoiceNoteAudio (live download needs the Mac)"
    profile = Path.home() / ".yaadhamma" / "chrome-profile"
    assert profile.exists(), (
        "WhatsApp browser profile missing — on the Mac run: "
        "uv run scripts/whatsapp_signin.py"
    )
    return (
        "extractor exports waVoiceNoteAudio; browser profile present "
        "(full reachability needs the paired session)"
    )


@check("voice tools build Gemini schemas")
def _voice_tool_schemas():
    # A tool whose type hints cannot be resolved crashes Gemini Live at
    # session startup (2026-09-28: planner.py's _RunContext). Build every
    # voice tool's schema the way the realtime session does, before a live
    # run. Stores go under a scratch dir; nothing is called.
    import tempfile
    from pathlib import Path

    from tool_schemas import check_voice_tool_schemas

    with tempfile.TemporaryDirectory(prefix="yaadhamma-selftest-") as tmp:
        names = check_voice_tool_schemas(Path(tmp))
    return f"{len(names)} voice tools describe cleanly to Gemini"


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
