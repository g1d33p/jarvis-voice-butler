#!/usr/bin/env python3
"""Run one Saayam digest now (the schedule calls this 4 times a day).

Uses the digest's own browser and WhatsApp link (~/.yaadhamma/digest-profile),
so it never collides with the voice agent. Its window opens off-screen.

Manual run:
    cd ~/jarvis-voice-butler/yaadhamma
    uv run scripts/digest_run.py
"""

import asyncio
import fcntl
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from browser import DIGEST_PROFILE_DIR, BrowserManager
from digest import DigestStore, run_digest
from meta_client import brain_client_from_config
from whatsapp import WhatsAppClient

LOCK = Path.home() / ".yaadhamma" / "digest.lock"
OFF_SCREEN = ["--window-position=-3000,-3000", "--window-size=1280,900"]


async def main() -> int:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("A digest is already running; skipping this one.")
            return 0
        browser = BrowserManager(
            headless=False, profile_dir=DIGEST_PROFILE_DIR, launch_args=OFF_SCREEN
        )
        client = WhatsAppClient(browser=browser)
        try:
            result = await run_digest(client, brain_client_from_config(), DigestStore())
        finally:
            await browser.close()
    print(f"Digest {result.status}. {result.error or result.summary[:200]}")
    return 0 if result.status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
