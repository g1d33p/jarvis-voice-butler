#!/usr/bin/env python3
"""Run one Saayam digest now (the schedule calls this 4 times a day).

Uses the digest's own browser and WhatsApp link (~/.yaadhamma/digest-profile),
so it never collides with the voice agent. Its window opens off-screen.

Manual run:
    cd ~/jarvis-voice-butler/yaadhamma
    uv run scripts/digest_run.py
Delivery test (sends one line to your own "(You)" chat, nothing else):
    uv run scripts/digest_run.py --test
Morning brief (today's calendar and this week's clashes):
    uv run scripts/digest_run.py --morning
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


async def main() -> int:
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
            if "--test" in sys.argv[1:]:
                chat = await send_test_message(client)
                print(f"Test message sent to {chat}. Check your phone.")
                return 0
            calendar = (
                CalendarClient() if CalendarClient().token_path.exists() else None
            )
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
