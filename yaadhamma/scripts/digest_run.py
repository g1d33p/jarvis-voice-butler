#!/usr/bin/env python3
"""Run one Saayam digest now (the schedule calls this 4 times a day).

Uses the digest's own browser and WhatsApp link (~/.yaadhamma/digest-profile),
so it never collides with the voice agent. Its window opens off-screen.

Manual run:
    cd ~/jarvis-voice-butler/yaadhamma
    uv run scripts/digest_run.py
WhatsApp self-check (proves reading and searching still work; sends nothing):
    uv run scripts/digest_run.py --check
Delivery test (sends one line to your own "(You)" chat, nothing else):
    uv run scripts/digest_run.py --test
Plan preview (prints a plan here, sends nothing):
    uv run scripts/digest_run.py --plan today      (or: --plan week)
File tidy-up (3 am, every other night; proposes a plan unless YAADHAMMA_TIDY_MODE=apply):
    uv run scripts/digest_run.py --tidy          (add --now to ignore "every other night")
Apply the latest reviewed tidy plan:
    uv run scripts/digest_run.py --tidy-apply
Overnight learning (2 am; chats, email and calendar into memory):
    uv run scripts/digest_run.py --learn
Morning brief (today's plan, calendar and clashes):
    uv run scripts/digest_run.py --morning
Weekly plan (Sunday evening):
    uv run scripts/digest_run.py --weekly
Email preview (last 24 hours; prints the summary here, sends nothing):
    uv run scripts/digest_run.py --email-preview 24
"""

import asyncio
import fcntl
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from browser import DIGEST_PROFILE_DIR, BrowserManager
from digest import (
    DigestStore,
    preview_email,
    run_digest,
    run_morning_brief,
    send_test_message,
)
from gcal import CalendarClient
from gmail import GmailClient, discover_labels
from meta_client import brain_client_from_config
from whatsapp import WhatsAppClient

LOCK = Path.home() / ".yaadhamma" / "digest.lock"
OFF_SCREEN = ["--window-position=-3000,-3000", "--window-size=1280,900"]


async def email_preview(hours: float) -> int:
    gmail = [GmailClient(label=label) for label in discover_labels()]
    if not gmail:
        print("No Gmail accounts are signed in.")
        return 1
    print(f"Checking the last {hours:g} hours of email in {len(gmail)} account(s)...")
    result = await preview_email(gmail, brain_client_from_config(), hours)
    print("Accounts: " + ", ".join(result["accounts"]))
    for dup in result["duplicate_accounts"]:
        print(
            f"  Warning: {dup} is linked more than once. Delete one of its "
            "~/.yaadhamma/gmail-token-*.json files and link your missing account."
        )
    print(f"Emails found: {result['found']}")
    for error in result["errors"]:
        print(f"  Problem: {error}")
    print("\n----- Digest preview (not sent) -----")
    print(result["summary"] or "(nothing new in that window)")
    return 0


async def tidy(now_flag: bool) -> int:
    from datetime import datetime, timedelta

    import config
    from tidy import TidyLog, apply_plan, build_plan, plan_summary, write_plan_file

    log = TidyLog()
    last = log.last_run()
    if last and not now_flag and datetime.now() - last < timedelta(hours=44):
        print("Tidy-up ran last night; it runs every other night. Skipping.")
        return 0
    plan = await build_plan(brain_client_from_config())
    log.save_plan(plan)
    path = write_plan_file(plan)
    if config.TIDY_MODE == "apply" and plan.moves:
        done = apply_plan(plan, log)
        print(f"Tidy-up applied: {done['moved']} files moved. Plan: {path}")
    else:
        print(plan_summary(plan))
        print(f"Plan: {path}")
    return 0


async def tidy_apply() -> int:
    from tidy import TidyLog, apply_plan

    log = TidyLog()
    latest = log.latest_plan()
    if not latest:
        print("There is no tidy plan yet.")
        return 1
    status, plan = latest
    if status == "applied":
        print("The latest tidy plan has already been applied.")
        return 0
    done = apply_plan(plan, log)
    print(f"Moved {done['moved']} files into Documents/Sorted.")
    for item in done["skipped"]:
        print("  skipped:", item)
    return 0


async def plan(horizon: str) -> int:
    from digest import DigestStore
    from memory_store import MemoryStore
    from planner import make_plan

    calendar = CalendarClient() if CalendarClient().token_path.exists() else None
    gmail = [GmailClient(label=label) for label in discover_labels()]
    print(f"Planning your {'week' if horizon == 'week' else 'day'}...")
    text = await make_plan(
        brain_client_from_config(),
        horizon,
        calendar,
        gmail,
        MemoryStore(),
        DigestStore(),
    )
    print("\n" + (text or "(no plan came back)"))
    return 0


KNOWN_FLAGS = {
    "--test",
    "--check",
    "--morning",
    "--learn",
    "--tidy",
    "--now",
    "--tidy-apply",
    "--email-preview",
    "--plan",
}


async def main() -> int:
    # An unknown option must never fall through to a real digest run
    # (2026-09-28: "--plan" before the planning patch sent two digests).
    unknown = [a for a in sys.argv[1:] if a.startswith("--") and a not in KNOWN_FLAGS]
    if unknown:
        print(f"Unknown option {unknown[0]}. Nothing was run.")
        print(__doc__)
        return 2
    if "--plan" in sys.argv:
        after = sys.argv[sys.argv.index("--plan") + 1 :]
        return await plan(after[0] if after else "today")
    if "--tidy-apply" in sys.argv:
        return await tidy_apply()
    if "--tidy" in sys.argv:
        return await tidy("--now" in sys.argv)
    if "--email-preview" in sys.argv:
        after = sys.argv[sys.argv.index("--email-preview") + 1 :]
        return await email_preview(float(after[0]) if after else 24)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lock:
        # The 8:45 brief may still be running at 9:00: wait for it (up to 5
        # minutes) rather than skip the digest.
        for _ in range(60):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(5)
        else:
            print("Another digest has been running for 5 minutes; skipping this one.")
            return 0
        browser = BrowserManager(
            headless=False, profile_dir=DIGEST_PROFILE_DIR, launch_args=OFF_SCREEN
        )
        client = WhatsAppClient(browser=browser)
        try:
            if "--check" in sys.argv[1:]:
                from whatsapp_health import HealthLog, run_health_check

                report = await run_health_check(client)
                HealthLog().record(report)
                for check in report.checks:
                    mark = "ok  " if check.ok else "FAIL"
                    print(
                        f"  {mark} {check.name}"
                        + (f": {check.detail}" if check.detail else "")
                    )
                print("\n" + report.summary())
                return 0 if report.ok else 1
            if "--test" in sys.argv[1:]:
                chat = await send_test_message(client)
                print(f"Test message sent to {chat}. Check your phone.")
                return 0
            calendar = (
                CalendarClient() if CalendarClient().token_path.exists() else None
            )
            if "--learn" in sys.argv[1:]:
                from learning import LearningLog, run_learning
                from memory_store import MemoryStore

                gmail = [GmailClient(label=label) for label in discover_labels()]
                learned = await run_learning(
                    client,
                    brain_client_from_config(),
                    MemoryStore(),
                    LearningLog(),
                    gmail_clients=gmail,
                    calendar=calendar,
                )
                print(
                    f"Learning {learned.status}: {len(learned.added)} new, "
                    f"{len(learned.updated)} updated. {learned.error}"
                )
                for item in learned.added:
                    print("  +", item)
                if learned.skipped_unread:
                    print(
                        f"  (skipped {learned.skipped_unread} personal chats with unread messages)"
                    )
                return 0 if learned.status != "failed" else 1
            if "--morning" in sys.argv[1:]:
                if calendar is None:
                    print(
                        "Google Calendar is not connected: uv run scripts/calendar_signin.py"
                    )
                    return 1
                result = await run_morning_brief(client, calendar, DigestStore())
            else:
                gmail = [GmailClient(label=label) for label in discover_labels()]
                result = await run_digest(
                    client,
                    brain_client_from_config(),
                    DigestStore(),
                    gmail_clients=gmail,
                    calendar=calendar,
                )
        finally:
            await browser.close()
    print(f"Digest {result.status}. {result.error or result.summary[:200]}")
    return 0 if result.status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
