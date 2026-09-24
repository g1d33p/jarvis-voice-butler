"""Outlook skill via Microsoft Graph: mail and calendar on the Mac.

Auth uses the OAuth2 device-code flow, so Jeevan signs in once in his
browser — no client secret, no password stored. The token (with a refresh
token) is cached at ~/.yaadhamma/outlook-token.json with owner-only
permissions.

Setup (one time, ~5 minutes):
  1. https://portal.azure.com -> Microsoft Entra ID -> App registrations
     -> New registration (name it "Yaadhamma", single tenant is fine).
  2. Authentication -> Advanced settings -> "Allow public client flows" -> Yes.
  3. API permissions -> add Microsoft Graph delegated:
     Mail.Read, Mail.Send, Calendars.Read, offline_access.
  4. Copy the Application (client) ID into .env.local as
     YAADHAMMA_OUTLOOK_CLIENT_ID.

Least privilege: reading mail needs only Mail.Read; sending needs
Mail.Send and always goes through the approval gate in outlook_tools.
"""

import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

GRAPH = "https://graph.microsoft.com/v1.0"
_AUTHORITY = "https://login.microsoftonline.com/common"
_SCOPES = "Mail.Read Mail.Send Calendars.Read offline_access"
_TOKEN_SKEW = timedelta(seconds=60)
DEFAULT_TOKEN_PATH = Path.home() / ".yaadhamma" / "outlook-token.json"


class OutlookAuthError(Exception):
    """Sign-in, token, or permission problem with a human-readable message."""


def _default_http(method, url, payload=None, headers=None):
    """Minimal stdlib HTTP client returning parsed JSON."""
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = urllib.parse.urlencode(payload).encode()
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise OutlookAuthError(
            f"Outlook request failed (HTTP {e.code}): {detail}"
        ) from e


def _strip_html(content: str) -> str:
    text = re.sub(r"<[^>]+>", " ", content or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


class OutlookClient:
    """Microsoft Graph client with device-code auth and a cached token."""

    def __init__(self, client_id=None, token_path=None, http=None) -> None:
        env_id = os.environ.get("YAADHAMMA_OUTLOOK_CLIENT_ID")
        self.client_id = client_id or env_id
        self.token_path = Path(token_path) if token_path else DEFAULT_TOKEN_PATH
        self._http = http or _default_http

    # -- sign-in ---------------------------------------------------------
    def start_device_flow(self) -> dict:
        """Begin sign-in; return what to show Jeevan in the browser."""
        if not self.client_id:
            raise OutlookAuthError(
                "YAADHAMMA_OUTLOOK_CLIENT_ID is not set. Register the app in "
                "the Azure portal (see outlook.py header) and put the "
                "Application (client) ID in .env.local."
            )
        resp = self._http(
            "POST",
            f"{_AUTHORITY}/oauth2/v2.0/devicecode",
            {"client_id": self.client_id, "scope": _SCOPES},
        )
        return {
            "user_code": resp["user_code"],
            "verification_uri": resp["verification_uri"],
            "message": resp.get("message")
            or (f"Go to {resp['verification_uri']} and enter {resp['user_code']}"),
            "device_code": resp["device_code"],
            "interval": int(resp.get("interval", 5)),
            "expires_in": int(resp.get("expires_in", 900)),
        }

    def poll_for_token(
        self, device_code: str, interval: int = 5, timeout: int = 600
    ) -> dict:
        """Wait until Jeevan approves (or denies) the sign-in."""
        if not self.client_id:
            raise OutlookAuthError("YAADHAMMA_OUTLOOK_CLIENT_ID is not set.")
        deadline = time.monotonic() + timeout
        while True:
            resp = self._http(
                "POST",
                f"{_AUTHORITY}/oauth2/v2.0/token",
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": self.client_id,
                    "device_code": device_code,
                },
            )
            error = resp.get("error")
            if not error:
                self._save_token(resp)
                return resp
            if error == "authorization_pending":
                if time.monotonic() >= deadline:
                    raise OutlookAuthError("Sign-in timed out. Try again.")
                time.sleep(max(interval, 1))
                continue
            if error in ("authorization_declined", "access_denied"):
                raise OutlookAuthError(
                    "Sign-in was declined in the browser. Nothing was stored."
                )
            if error == "expired_token":
                raise OutlookAuthError(
                    "The sign-in code expired. Start again for a fresh code."
                )
            raise OutlookAuthError(
                f"Sign-in failed: {resp.get('error_description', error)}"
            )

    def _save_token(self, token: dict) -> None:
        payload = {
            "access_token": token["access_token"],
            "refresh_token": token.get("refresh_token", ""),
            "expires_at": (
                datetime.now(timezone.utc)
                + timedelta(seconds=int(token.get("expires_in", 3600)))
            ).isoformat(),
        }
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(payload))
        os.chmod(self.token_path, 0o600)

    def _load_token(self) -> dict | None:
        if not self.token_path.exists():
            return None
        try:
            return json.loads(self.token_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _refresh(self, refresh_token: str) -> dict:
        resp = self._http(
            "POST",
            f"{_AUTHORITY}/oauth2/v2.0/token",
            {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": refresh_token,
                "scope": _SCOPES,
            },
        )
        if resp.get("error"):
            raise OutlookAuthError(
                "Outlook session expired. Sign in again with the Outlook sign-in tool."
            )
        if not resp.get("refresh_token"):
            resp["refresh_token"] = refresh_token
        self._save_token(resp)
        return resp

    def _access_token(self) -> str:
        token = self._load_token()
        if not token or not token.get("access_token"):
            raise OutlookAuthError(
                "Outlook is not signed in. Use the Outlook sign-in tool first."
            )
        expires_at = datetime.fromisoformat(token["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at - _TOKEN_SKEW <= datetime.now(timezone.utc):
            token = self._refresh(token["refresh_token"])
        return token["access_token"]

    # -- graph calls ------------------------------------------------------
    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{GRAPH}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._http(
            "GET",
            url,
            headers={"Authorization": f"Bearer {self._access_token()}"},
        )

    def list_inbox(self, limit: int = 10) -> list[dict]:
        """Most recent inbox messages, newest first."""
        resp = self._get(
            "/me/messages",
            {
                "$top": max(1, min(limit, 25)),
                "$orderby": "receivedDateTime desc",
                "$select": "id,subject,from,receivedDateTime,bodyPreview,isRead",
            },
        )
        return [self._summarize(m) for m in resp.get("value", [])]

    def search_mail(self, query: str, limit: int = 10) -> list[dict]:
        """Full-text search across the mailbox."""
        resp = self._get(
            "/me/messages",
            {
                "$search": f'"{query}"',
                "$top": max(1, min(limit, 25)),
                "$select": "id,subject,from,receivedDateTime,bodyPreview,isRead",
            },
        )
        return [self._summarize(m) for m in resp.get("value", [])]

    def get_message(self, message_id: str) -> dict:
        """One message with its plain-text body."""
        m = self._get(
            f"/me/messages/{message_id}",
            {"$select": "id,subject,from,toRecipients,receivedDateTime,body"},
        )
        body = m.get("body", {})
        content = body.get("content", "")
        if body.get("contentType", "text").lower() == "html":
            content = _strip_html(content)
        return {
            "id": m.get("id"),
            "subject": m.get("subject", ""),
            "from": self._address(m.get("from")),
            "to": [self._address(r) for r in m.get("toRecipients", [])],
            "received": m.get("receivedDateTime", ""),
            "body_text": content.strip()[:4000],
        }

    def _post_json(self, path: str, payload: dict) -> dict:
        url = f"{GRAPH}{path}"
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        if self._http is _default_http:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={**headers, "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read().decode("utf-8", "replace")
                    return json.loads(body) if body.strip() else {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                raise OutlookAuthError(
                    f"Outlook request failed (HTTP {e.code}): {detail}"
                ) from e
        return self._http("POST", url, payload=payload, headers=headers)

    def create_draft(self, to: str, subject: str, body: str) -> dict:
        """Save a draft without sending; returns its id."""
        resp = self._post_json(
            "/me/messages",
            {
                "subject": subject,
                "body": {"contentType": "text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
        )
        return {"draft_id": resp.get("id", "")}

    def send_mail(self, to: str, subject: str, body: str) -> dict:
        """Send an email. Call only after the approval gate clears it."""
        self._post_json(
            "/me/sendMail",
            {
                "message": {
                    "subject": subject,
                    "body": {"contentType": "text", "content": body},
                    "toRecipients": [{"emailAddress": {"address": to}}],
                },
                "saveToSentItems": True,
            },
        )
        return {"sent": True, "to": to}

    def list_calendar(self, days: int = 1) -> list[dict]:
        """Upcoming events, soonest first."""
        start = datetime.now(timezone.utc)
        end = start + timedelta(days=max(1, days))
        resp = self._get(
            "/me/calendarview",
            {
                "startDateTime": start.isoformat(),
                "endDateTime": end.isoformat(),
                "$orderby": "start/dateTime",
                "$top": 25,
                "$select": "subject,start,end,location",
            },
        )
        events = []
        for e in resp.get("value", []):
            events.append(
                {
                    "subject": e.get("subject", ""),
                    "start": e.get("start", {}).get("dateTime", ""),
                    "end": e.get("end", {}).get("dateTime", ""),
                    "location": e.get("location", {}).get("displayName", ""),
                }
            )
        return events

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _address(recipient: dict | None) -> str:
        if not recipient:
            return ""
        return (recipient.get("emailAddress") or {}).get("address", "")

    @classmethod
    def _summarize(cls, m: dict) -> dict:
        return {
            "id": m.get("id"),
            "subject": m.get("subject", ""),
            "from": cls._address(m.get("from")),
            "received": m.get("receivedDateTime", ""),
            "preview": m.get("bodyPreview", ""),
            "is_read": bool(m.get("isRead", True)),
        }
