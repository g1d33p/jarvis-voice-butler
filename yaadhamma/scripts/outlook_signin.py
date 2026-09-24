#!/usr/bin/env python3
"""One-time Outlook sign-in for Yaadhamma (Microsoft Graph device flow).

Run on the Mac:
    cd ~/jarvis-voice-butler/yaadhamma
    uv run scripts/outlook_signin.py

It prints a microsoft.com/devicelogin code; sign in in the browser, approve,
and the token is cached at ~/.yaadhamma/outlook-token.json. Afterwards the
voice tools can read mail and calendar; sending still asks Jeevan first.

Requires YAADHAMMA_OUTLOOK_CLIENT_ID in .env.local (see src/outlook.py).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env.local"))
load_dotenv()

from outlook import OutlookAuthError, OutlookClient  # noqa: E402


def main() -> int:
    try:
        client = OutlookClient()
        flow = client.start_device_flow()
    except OutlookAuthError as exc:
        print(f"Cannot start sign-in: {exc}")
        return 1
    print(flow["message"])
    print("Waiting for approval in the browser (Ctrl-C to cancel)...")
    try:
        client.poll_for_token(flow["device_code"], interval=flow["interval"])
    except OutlookAuthError as exc:
        print(f"Sign-in failed: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nCancelled. Nothing was stored.")
        return 1
    print("Signed in. Token cached at ~/.yaadhamma/outlook-token.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
