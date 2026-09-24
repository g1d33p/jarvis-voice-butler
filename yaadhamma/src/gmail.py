"""Gmail skill: read and send mail across Jeevan's linked Gmail accounts.

Auth is Google OAuth2 for a desktop app (RFC 8252 loopback flow), so
Jeevan signs in once per account in his browser — no client secret or
password stored anywhere except the token files. One token per account is
cached at ~/.yaadhamma/gmail-token-<label>.json with owner-only
permissions.

Setup (one time, ~10 minutes):
  1. https://console.cloud.google.com -> new project (name it "Yaadhamma")
     -> APIs & Services -> Library -> enable the Gmail API.
  2. OAuth consent screen -> External -> fill in the app name -> add
     Jeevan's Gmail addresses as Test users (the app stays in Testing
     mode; only test users can sign in).
  3. Credentials -> Create Credentials -> OAuth client ID -> Desktop app.
     (Desktop-app clients accept any http://127.0.0.1 redirect without
     registering it.)
  4. Copy the client ID and client secret into .env.local as
     YAADHAMMA_GOOGLE_CLIENT_ID and YAADHAMMA_GOOGLE_CLIENT_SECRET.
  5. Run: uv run scripts/gmail_signin.py <label>   (once per account;
     the label is free text like "personal1") and sign in as the right
     Google account when the browser opens.

Least privilege: reading mail needs only gmail.readonly; saving drafts
needs gmail.compose; sending needs gmail.send and always goes through the
approval gate in gmail_tools.
"""

import base64
import html
import http.server
import json
import os
import re
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://gmail.googleapis.com/gmail/v1"
_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly"
    " https://www.googleapis.com/auth/gmail.send"
    " https://www.googleapis.com/auth/gmail.compose"
)
_TOKEN_SKEW = timedelta(seconds=60)
DEFAULT_TOKEN_DIR = Path.home() / ".yaadhamma"
_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")


class GmailAuthError(Exception):
    """Sign-in, token, or permission problem with a human-readable message."""


def _default_http(method, url, form=None, json_body=None, headers=None):
    """Minimal stdlib HTTP client returning parsed JSON.

    form: dict sent as application/x-www-form-urlencoded (OAuth token
    endpoint). json_body: dict sent as application/json (Gmail API).
    """
    data = None
    request_headers = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        request_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise GmailAuthError(f"Gmail request failed (HTTP {e.code}): {detail}") from e


def _strip_html(content: str) -> str:
    text = re.sub(r"<[^>]+>", " ", content or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _b64decode(data: str) -> str:
    """Decode Gmail's base64url (unpadded) payload data to text."""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", "replace")


class _AuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    """Records Google's ?code= (or ?error=) redirect, then answers."""

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self.server.auth_result = {
            "code": params.get("code", [None])[0],
            "error": params.get("error", [None])[0],
        }
        page = (
            b"<html><body><h2>Signed in.</h2>"
            b"<p>Yaadhamma has the code it needs; you can close this tab.</p>"
            b"</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, *args):  # keep the sign-in script quiet
        pass


def _wait_for_auth_code(server, timeout: int = 300) -> tuple[str | None, str | None]:
    """Serve loopback requests until Google's redirect arrives or times out."""
    server.auth_result = None
    server.timeout = 2
    deadline = time.monotonic() + timeout
    while server.auth_result is None:
        if time.monotonic() >= deadline:
            return None, "timeout"
        server.handle_request()
    result = server.auth_result
    return result.get("code"), result.get("error")


def discover_labels(token_dir: Path | None = None) -> list[str]:
    """Labels of every linked Gmail account (one token file each)."""
    directory = Path(token_dir) if token_dir else DEFAULT_TOKEN_DIR
    labels = []
    if not directory.is_dir():
        return labels
    for path in sorted(directory.glob("gmail-token-*.json")):
        label = path.name[len("gmail-token-") : -len(".json")]
        if _LABEL_RE.fullmatch(label):
            labels.append(label)
    return labels


class GmailClient:
    """Gmail REST client with per-account OAuth2 tokens."""

    def __init__(
        self,
        client_id=None,
        client_secret=None,
        label: str = "default",
        token_path=None,
        http=None,
    ) -> None:
        if not _LABEL_RE.fullmatch(label or ""):
            raise GmailAuthError(
                f"Bad account label {label!r}: use letters, digits, '-' or "
                "'_', up to 32 characters (e.g. 'personal1')."
            )
        self.label = label
        self.client_id = client_id or os.environ.get("YAADHAMMA_GOOGLE_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get(
            "YAADHAMMA_GOOGLE_CLIENT_SECRET"
        )
        if token_path:
            self.token_path = Path(token_path)
        else:
            self.token_path = DEFAULT_TOKEN_DIR / f"gmail-token-{label}.json"
        self._http = http or _default_http

    def _credentials(self) -> tuple[str, str]:
        if not self.client_id or not self.client_secret:
            raise GmailAuthError(
                "YAADHAMMA_GOOGLE_CLIENT_ID / YAADHAMMA_GOOGLE_CLIENT_SECRET "
                "are not set. Create a Desktop-app OAuth client in the Google "
                "Cloud Console (see gmail.py header) and put both values in "
                ".env.local."
            )
        return self.client_id, self.client_secret

    # -- sign-in ---------------------------------------------------------
    def build_auth_url(self, redirect_uri: str, state: str | None = None) -> str:
        """The Google consent URL for the loopback sign-in."""
        client_id, _ = self._credentials()
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": _SCOPES,
            "access_type": "offline",  # ask for a refresh token
            "prompt": "consent",  # re-grant, so re-linking keeps working
            "include_granted_scopes": "true",
        }
        if state:
            params["state"] = state
        return f"{_AUTH_URL}?{urllib.parse.urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        """Trade the browser's authorization code for tokens; saves them."""
        client_id, client_secret = self._credentials()
        resp = self._http(
            "POST",
            TOKEN_URL,
            form={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
        )
        if resp.get("error"):
            raise GmailAuthError(
                "Google refused the sign-in: "
                f"{resp.get('error_description', resp['error'])}"
            )
        self._save_token(resp)
        return resp

    def sign_in_via_browser(
        self, open_browser: bool = True, timeout: int = 300
    ) -> dict:
        """Full loopback flow: open Google, wait for approval, store tokens."""
        self._credentials()  # fail fast with setup instructions
        server = http.server.HTTPServer(("127.0.0.1", 0), _AuthCallbackHandler)
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/"
        url = self.build_auth_url(redirect_uri)
        if open_browser:
            webbrowser.open(url)
            print("Opened the Google sign-in page in the browser.")
        else:
            print(f"Open this URL to sign in:\n{url}")
        print("Waiting for approval in the browser (Ctrl-C to cancel)...")
        try:
            code, error = _wait_for_auth_code(server, timeout=timeout)
        finally:
            server.server_close()
        if error == "timeout" or not code:
            raise GmailAuthError(
                "Sign-in timed out or was not approved. Nothing was stored."
            )
        token = self.exchange_code(code, redirect_uri)
        try:
            profile = self.get_profile()
            print(f"Linked {profile.get('email', '?')} as '{self.label}'.")
        except GmailAuthError:
            print(f"Signed in as '{self.label}'.")
        return token

    # -- token cache ------------------------------------------------------
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
        client_id, client_secret = self._credentials()
        resp = self._http(
            "POST",
            TOKEN_URL,
            form={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
        )
        if resp.get("error"):
            raise GmailAuthError(
                f"Gmail session for '{self.label}' expired "
                f"({resp.get('error_description', resp['error'])}). Sign in "
                f"again: uv run scripts/gmail_signin.py {self.label}"
            )
        if not resp.get("refresh_token"):
            resp["refresh_token"] = refresh_token  # Google may omit it
        self._save_token(resp)
        return resp

    def _access_token(self) -> str:
        token = self._load_token()
        if not token or not token.get("access_token"):
            raise GmailAuthError(
                f"Gmail account '{self.label}' is not signed in. Run: "
                f"uv run scripts/gmail_signin.py {self.label}"
            )
        expires_at = datetime.fromisoformat(token["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at - _TOKEN_SKEW <= datetime.now(timezone.utc):
            token = self._refresh(token["refresh_token"])
        return token["access_token"]

    # -- gmail api ---------------------------------------------------------
    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{API_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._http(
            "GET", url, headers={"Authorization": f"Bearer {self._access_token()}"}
        )

    def _post(self, path: str, payload: dict) -> dict:
        return self._http(
            "POST",
            f"{API_BASE}{path}",
            json_body=payload,
            headers={"Authorization": f"Bearer {self._access_token()}"},
        )

    @staticmethod
    def _headers(payload: dict) -> dict[str, str]:
        headers = {}
        for h in payload.get("headers", []):
            name = h.get("name", "").lower()
            if name in ("from", "to", "subject", "date"):
                headers[name] = h.get("value", "")
        return headers

    @classmethod
    def _summarize(cls, full: dict) -> dict:
        headers = cls._headers(full.get("payload", {}))
        label_ids = full.get("labelIds", [])
        try:
            internal_date = int(full.get("internalDate", 0) or 0)
        except (TypeError, ValueError):
            internal_date = 0
        return {
            "id": full.get("id"),
            "subject": headers.get("subject", ""),
            "from": headers.get("from", ""),
            "date": headers.get("date", ""),
            "internal_date": internal_date,
            "snippet": full.get("snippet", ""),
            "is_read": "UNREAD" not in label_ids,
        }

    def _metadata(self, message_id: str) -> dict:
        full = self._get(
            f"/users/me/messages/{message_id}",
            {
                "format": "metadata",
                "metadataHeaders": "From,Subject,Date",
            },
        )
        return self._summarize(full)

    def list_messages(self, limit: int = 10) -> list[dict]:
        """Most recent inbox messages, newest first (Gmail's native order)."""
        resp = self._get(
            "/users/me/messages",
            {"labelIds": "INBOX", "maxResults": max(1, min(limit, 25))},
        )
        return [self._metadata(m["id"]) for m in resp.get("messages", [])]

    def search_mail(self, query: str, limit: int = 10) -> list[dict]:
        """Gmail search syntax (from:, subject:, after:, is:unread, ...)."""
        resp = self._get(
            "/users/me/messages",
            {"q": query, "maxResults": max(1, min(limit, 25))},
        )
        return [self._metadata(m["id"]) for m in resp.get("messages", [])]

    def get_message(self, message_id: str) -> dict:
        """One message with its plain-text body."""
        full = self._get(f"/users/me/messages/{message_id}", {"format": "full"})
        headers = self._headers(full.get("payload", {}))
        try:
            internal_date = int(full.get("internalDate", 0) or 0)
        except (TypeError, ValueError):
            internal_date = 0
        return {
            "id": full.get("id"),
            "subject": headers.get("subject", ""),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "date": headers.get("date", ""),
            "internal_date": internal_date,
            "snippet": full.get("snippet", ""),
            "body_text": self._extract_body(full.get("payload", {})),
        }

    @classmethod
    def _extract_body(cls, payload: dict) -> str:
        """First text/plain part; falls back to stripped text/html."""
        html_fallback = None

        def walk(part: dict) -> str | None:
            nonlocal html_fallback
            mime = part.get("mimeType", "")
            data = (part.get("body") or {}).get("data")
            if data:
                if mime == "text/plain":
                    return _b64decode(data)
                if mime == "text/html" and html_fallback is None:
                    html_fallback = _strip_html(_b64decode(data))
            for sub in part.get("parts", []):
                text = walk(sub)
                if text:
                    return text
            return None

        return (walk(payload) or html_fallback or "").strip()[:4000]

    @staticmethod
    def _raw_message(to: str, subject: str, body: str) -> str:
        rfc822 = (
            f"To: {to}\r\n"
            f"Subject: {subject}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            f"{body}"
        )
        return base64.urlsafe_b64encode(rfc822.encode("utf-8")).decode().rstrip("=")

    def send_mail(self, to: str, subject: str, body: str) -> dict:
        """Send an email. Call only after the approval gate clears it."""
        resp = self._post(
            "/users/me/messages/send",
            {"raw": self._raw_message(to, subject, body)},
        )
        return {"sent": True, "to": to, "id": resp.get("id")}

    def create_draft(self, to: str, subject: str, body: str) -> dict:
        """Save a draft without sending; returns its id."""
        resp = self._post(
            "/users/me/drafts",
            {"message": {"raw": self._raw_message(to, subject, body)}},
        )
        return {"draft_id": resp.get("id", "")}

    def get_profile(self) -> dict:
        """The linked account's address (used after sign-in)."""
        profile = self._get("/users/me/profile")
        return {"email": profile.get("emailAddress", "")}
