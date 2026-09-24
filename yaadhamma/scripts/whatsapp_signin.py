#!/usr/bin/env python3
"""One-time WhatsApp pairing for Yaadhamma.

Opens WhatsApp Web in a VISIBLE Chromium window using Yaadhamma's own
persistent browser profile (~/.yaadhamma/chrome-profile) and waits for
Jeevan to scan the QR code with his phone. The session then persists in
the profile, so the voice tools stay logged in afterwards.

Run on the Mac, once:
    cd ~/jarvis-voice-butler/yaadhamma
    uv run scripts/whatsapp_signin.py

Close the Yaadhamma agent first if it is running: the browser profile can
only be used by one window at a time.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from browser import BrowserError, BrowserManager
from whatsapp import WhatsAppClient, WhatsAppNotPairedError


async def main() -> int:
    print("Opening WhatsApp Web in a visible browser window...")
    try:
        browser = BrowserManager(headless=False)
        client = WhatsAppClient(browser=browser)
        await client.ensure_tab(browser)
    except BrowserError as exc:
        print(f"Could not start the browser: {exc}")
        print(
            "If Yaadhamma is running, stop it first: the browser profile "
            "can only be used by one window at a time."
        )
        return 1

    print()
    print("Scan the QR code with your phone:")
    print("  WhatsApp > Settings > Linked devices > Link a device")
    print("Waiting up to 5 minutes...")
    try:
        await client.wait_for_login(timeout_s=300)
    except WhatsAppNotPairedError:
        print("Timed out waiting for the scan. Run this script again when ready.")
        await browser.close()
        return 1

    print()
    print("Paired! The session is saved in ~/.yaadhamma/chrome-profile,")
    print("so Yaadhamma stays logged in. You can close this window.")
    await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
