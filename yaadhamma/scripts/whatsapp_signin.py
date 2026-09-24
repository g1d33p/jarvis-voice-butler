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

The window closes itself a few seconds after pairing is confirmed. This
matters: while the window is open it holds an exclusive lock on the
browser profile, and the agent's own browser cannot start until that lock
is released.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from browser import BrowserError, BrowserManager
from whatsapp import WhatsAppClient, WhatsAppNotPairedError

# Seconds the sign-in window stays open after pairing is confirmed, so the
# user sees the success message before it closes.
POST_PAIRING_GRACE_S = 5.0


async def pair_then_release(
    client: WhatsAppClient,
    browser: BrowserManager,
    *,
    grace_s: float = POST_PAIRING_GRACE_S,
) -> None:
    """Wait for the QR scan, then close the browser to release the profile.

    Separated from main() so it is unit-testable without a real browser.
    Raises WhatsAppNotPairedError if the scan never happens (the caller
    closes the browser on that path).
    """
    await client.wait_for_login(timeout_s=300)
    print()
    print("Paired! The session is saved in ~/.yaadhamma/chrome-profile,")
    print("so Yaadhamma stays logged in.")
    print(
        f"This window will close in {grace_s:.0f} seconds — Yaadhamma's own "
        "browser takes over from here. It never uses your regular Chrome, "
        "so WhatsApp staying open in your Chrome is unrelated."
    )
    await asyncio.sleep(grace_s)
    await browser.close()


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
        await pair_then_release(client, browser)
    except WhatsAppNotPairedError:
        print("Timed out waiting for the scan. Run this script again when ready.")
        await browser.close()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
