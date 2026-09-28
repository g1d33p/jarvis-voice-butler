#!/usr/bin/env python3
"""Connect Yaadhamma to Google Calendar (one time).

Before running: in Google Cloud Console, open the same project as the Gmail
sign-in client, go to APIs & Services > Library, search "Google Calendar API"
and click Enable.

Then, from ~/jarvis-voice-butler/yaadhamma:
    uv run scripts/calendar_signin.py
and sign in with the calendar's account (deep.jeevan21@gmail.com).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env.local"))

from gcal import CalendarClient, GmailAuthError  # noqa: E402


def main() -> int:
    client = CalendarClient()
    try:
        client.sign_in_via_browser()
    except GmailAuthError as exc:
        print(f"Sign-in failed: {exc}")
        if "has not been used" in str(exc) or "disabled" in str(exc):
            print("Enable the Google Calendar API in Google Cloud Console first.")
        return 1
    except KeyboardInterrupt:
        print("\nCancelled. Nothing was stored.")
        return 1
    print("Done. Yaadhamma can now read your calendar and add events you approve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
