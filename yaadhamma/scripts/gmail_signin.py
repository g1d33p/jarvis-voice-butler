#!/usr/bin/env python3
"""One-time Gmail sign-in per account for Yaadhamma (OAuth2 loopback flow).

Run on the Mac, once per Gmail account:
    cd ~/jarvis-voice-butler/yaadhamma
    uv run scripts/gmail_signin.py personal1

The label (e.g. "personal1") becomes part of the token filename:
~/.yaadhamma/gmail-token-<label>.json. The script opens the browser;
sign in as the right Google account and approve, and the token is cached
with owner-only permissions. Afterwards the voice tools can read and
search all linked accounts; sending still asks Jeevan first.

Requires YAADHAMMA_GOOGLE_CLIENT_ID and YAADHAMMA_GOOGLE_CLIENT_SECRET
in .env.local (see src/gmail.py header for the Cloud Console setup).

Scope check (the email tidy needs the gmail.modify scope; old links lack it):
    uv run scripts/gmail_signin.py --check
Re-link an account that is missing the scope:
    uv run scripts/gmail_signin.py <label>
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env.local"))
load_dotenv()

from gmail import (  # noqa: E402
    GmailAuthError,
    GmailClient,
    discover_labels,
    has_modify_scope,
)


def check_scopes() -> int:
    """Report which linked accounts have the label-modifying scope."""
    labels = discover_labels()
    if not labels:
        print("No Gmail accounts linked yet.")
        print("Link one: uv run scripts/gmail_signin.py personal1")
        return 1
    lacking = [label for label in labels if not has_modify_scope(label)]
    for label in labels:
        mark = "ok  " if label not in lacking else "FAIL"
        print(
            f"  {mark} {label}: gmail.modify scope {'present' if label not in lacking else 'MISSING'}"
        )
    if lacking:
        print()
        print("Re-link each account to grant label changes and archiving:")
        for label in lacking:
            print(f"  uv run scripts/gmail_signin.py {label}")
        return 1
    print("All linked accounts can change labels.")
    return 0


def main() -> int:
    if "--check" in sys.argv:
        return check_scopes()
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        label = sys.argv[1]
    else:
        label = input("Account label (e.g. personal1): ").strip()
    if not label:
        print("A label is required: uv run scripts/gmail_signin.py personal1")
        return 1
    try:
        client = GmailClient(label=label)
    except GmailAuthError as exc:
        print(f"Cannot start sign-in: {exc}")
        return 1
    try:
        client.sign_in_via_browser()
    except GmailAuthError as exc:
        print(f"Sign-in failed: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nCancelled. Nothing was stored.")
        return 1
    print(f"Done. Token cached at ~/.yaadhamma/gmail-token-{label}.json")
    print("Run this script again with a different label for each Gmail account.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
