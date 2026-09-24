"""Tests for the Gmail skill (Google OAuth2 loopback flow + Gmail REST).

All HTTP goes through an injected fake: no network. The loopback-callback
test only talks to 127.0.0.1.
"""

import base64
import json
import os
import stat
import threading
import urllib.parse
import urllib.request

import pytest

from gmail import (
    API_BASE,
    TOKEN_URL,
    GmailAuthError,
    GmailClient,
    discover_labels,
)


class FakeHttp:
    """Canned (method, url-prefix) -> response queue; records requests."""

    def __init__(self):
        self.calls = []
        self.routes = []

    def add(self, method, url_prefix, response):
        self.routes.append((method, url_prefix, response))

    def __call__(self, method, url, form=None, json_body=None, headers=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "form": form,
                "json_body": json_body,
                "headers": headers or {},
            }
        )
        for route_method, prefix, response in self.routes:
            if method == route_method and url.startswith(prefix):
                if isinstance(response, Exception):
                    raise response
                if callable(response):
                    return response(self.calls[-1])
                return response
        raise AssertionError(f"unexpected HTTP call: {method} {url}")


def _token(expires_in=3600):
    return {
        "access_token": "access-123",
        "refresh_token": "refresh-123",
        "expires_in": expires_in,
        "token_type": "Bearer",
    }


@pytest.fixture()
def tmp_token(tmp_path):
    return tmp_path / "gmail-token-personal1.json"


@pytest.fixture()
def client(tmp_token):
    http = FakeHttp()
    return GmailClient(
        client_id="test-client-id",
        client_secret="test-secret",
        label="personal1",
        token_path=tmp_token,
        http=http,
    ), http


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_build_auth_url_requests_offline_access(client):
    gmail, _ = client
    url = gmail.build_auth_url("http://127.0.0.1:54321/")
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert params["client_id"] == ["test-client-id"]
    assert params["redirect_uri"] == ["http://127.0.0.1:54321/"]
    assert params["response_type"] == ["code"]
    assert params["access_type"] == ["offline"]  # refresh token
    assert params["prompt"] == ["consent"]
    scope = params["scope"][0]
    assert "https://www.googleapis.com/auth/gmail.readonly" in scope
    assert "https://www.googleapis.com/auth/gmail.send" in scope
    assert "https://www.googleapis.com/auth/gmail.compose" in scope


def test_exchange_code_saves_token_with_private_permissions(client):
    gmail, http = client
    http.add("POST", TOKEN_URL, _token())
    token = gmail.exchange_code("auth-code-xyz", "http://127.0.0.1:54321/")
    assert token["access_token"] == "access-123"
    call = http.calls[0]
    assert call["form"]["grant_type"] == "authorization_code"
    assert call["form"]["code"] == "auth-code-xyz"
    assert call["form"]["client_secret"] == "test-secret"
    saved = json.loads(gmail.token_path.read_text())
    assert saved["refresh_token"] == "refresh-123"
    mode = stat.S_IMODE(os.stat(gmail.token_path).st_mode)
    assert mode == 0o600


def test_exchange_code_error_raises_helpful_message(client):
    gmail, http = client
    http.add(
        "POST",
        TOKEN_URL,
        {"error": "invalid_grant", "error_description": "Bad code."},
    )
    with pytest.raises(GmailAuthError, match="Bad code"):
        gmail.exchange_code("bad-code", "http://127.0.0.1:1/")


def test_loopback_server_captures_code_from_real_request():
    """A real GET to the callback server yields the ?code= Google sends."""
    import http.server

    from gmail import _AuthCallbackHandler, _wait_for_auth_code

    server = http.server.HTTPServer(("127.0.0.1", 0), _AuthCallbackHandler)
    port = server.server_address[1]
    got = {}

    def run():
        got["code"], got["error"] = _wait_for_auth_code(server, timeout=10)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/?code=auth-code-xyz&scope=y", timeout=10
    ):
        pass
    thread.join(timeout=12)
    assert got["code"] == "auth-code-xyz"
    assert got["error"] is None
    server.server_close()


def test_loopback_handler_parses_query():
    """A real GET to the callback handler yields the code and 200 page."""
    import http.server

    from gmail import _AuthCallbackHandler

    server = http.server.HTTPServer(("127.0.0.1", 0), _AuthCallbackHandler)
    server.auth_result = None
    port = server.server_address[1]

    def serve_once():
        server.handle_request()

    thread = threading.Thread(target=serve_once, daemon=True)
    thread.start()
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/?code=auth-code-123&scope=x", timeout=10
    ) as resp:
        body = resp.read().decode()
    thread.join(timeout=10)
    assert resp.status == 200
    assert "close" in body.lower()
    assert server.auth_result["code"] == "auth-code-123"
    server.server_close()


def _authed(client):
    gmail, http = client
    gmail._save_token(_token(expires_in=3600))
    return gmail, http


def _metadata_message(mid, subject, sender, internal_ms, unread=True):
    labels = ["INBOX"]
    if unread:
        labels.append("UNREAD")
    return {
        "id": mid,
        "threadId": f"t-{mid}",
        "labelIds": labels,
        "snippet": f"snippet of {subject}",
        "internalDate": str(internal_ms),
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Thu, 24 Sep 2026 10:00:00 -0500"},
            ]
        },
    }


def test_list_messages_parses_metadata(client):
    gmail, http = _authed(client)
    # Specific message routes first: FakeHttp matches by URL prefix.
    http.add(
        "GET",
        f"{API_BASE}/users/me/messages/m1",
        _metadata_message("m1", "Hello", "a@example.com", 1000),
    )
    http.add(
        "GET",
        f"{API_BASE}/users/me/messages/m2",
        _metadata_message("m2", "World", "b@example.com", 2000, unread=False),
    )
    http.add(
        "GET",
        f"{API_BASE}/users/me/messages",
        {"messages": [{"id": "m1"}, {"id": "m2"}], "resultSizeEstimate": 2},
    )
    messages = gmail.list_messages(limit=5)
    assert len(messages) == 2
    assert messages[0]["id"] == "m1"
    assert messages[0]["subject"] == "Hello"
    assert messages[0]["from"] == "a@example.com"
    assert messages[0]["is_read"] is False
    assert messages[1]["is_read"] is True
    list_call = http.calls[0]
    assert list_call["headers"]["Authorization"] == "Bearer access-123"
    assert "labelIds=INBOX" in list_call["url"]
    assert "maxResults=5" in list_call["url"]


def test_list_messages_empty_inbox(client):
    gmail, http = _authed(client)
    http.add("GET", f"{API_BASE}/users/me/messages", {})
    assert gmail.list_messages() == []


def test_search_mail_passes_query(client):
    gmail, http = _authed(client)
    # Specific message route first: FakeHttp matches by URL prefix.
    http.add(
        "GET",
        f"{API_BASE}/users/me/messages/m9",
        _metadata_message("m9", "Invoice", "shop@example.com", 500),
    )
    http.add(
        "GET",
        f"{API_BASE}/users/me/messages",
        {"messages": [{"id": "m9"}]},
    )
    results = gmail.search_mail("invoice from:shop", limit=3)
    assert len(results) == 1
    assert "q=invoice" in http.calls[0]["url"]
    assert results[0]["subject"] == "Invoice"


def _full_message(mid):
    return {
        "id": mid,
        "snippet": "snippet",
        "internalDate": "3000",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "a@example.com"},
                {"name": "To", "value": "me@gmail.com"},
                {"name": "Subject", "value": "Report"},
                {"name": "Date", "value": "Thu, 24 Sep 2026 11:00:00 -0500"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64("Plain body here.")},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": _b64("<p>HTML body</p>")},
                },
            ],
        },
    }


def test_get_message_prefers_plain_text_body(client):
    gmail, http = _authed(client)
    http.add("GET", f"{API_BASE}/users/me/messages/m1", _full_message("m1"))
    m = gmail.get_message("m1")
    assert m["subject"] == "Report"
    assert m["from"] == "a@example.com"
    assert m["to"] == "me@gmail.com"
    assert m["body_text"] == "Plain body here."
    assert "<" not in m["body_text"]


def test_get_message_falls_back_to_stripped_html(client):
    gmail, http = _authed(client)
    msg = _full_message("m2")
    msg["payload"]["parts"] = [
        {
            "mimeType": "text/html",
            "body": {"data": _b64("<p>Only <b>html</b></p>")},
        }
    ]
    http.add("GET", f"{API_BASE}/users/me/messages/m2", msg)
    m = gmail.get_message("m2")
    assert m["body_text"] == "Only html"


def test_send_mail_posts_base64url_rfc822(client):
    gmail, http = _authed(client)
    http.add("POST", f"{API_BASE}/users/me/messages/send", {"id": "sent1"})
    gmail.send_mail(to="c@example.com", subject="Re: Hello", body="Sounds good.")
    call = http.calls[-1]
    raw = base64.urlsafe_b64decode(call["json_body"]["raw"] + "===").decode()
    assert "To: c@example.com" in raw
    assert "Subject: Re: Hello" in raw
    assert "Sounds good." in raw


def test_create_draft_posts_to_drafts_endpoint(client):
    gmail, http = _authed(client)
    http.add("POST", f"{API_BASE}/users/me/drafts", {"id": "draft1"})
    result = gmail.create_draft(to="d@example.com", subject="Idea", body="What if...")
    assert result["draft_id"] == "draft1"
    call = http.calls[-1]
    assert call["url"].endswith("/users/me/drafts")
    raw = base64.urlsafe_b64decode(call["json_body"]["message"]["raw"] + "===").decode()
    assert "To: d@example.com" in raw


def test_get_profile_returns_email(client):
    gmail, http = _authed(client)
    http.add(
        "GET",
        f"{API_BASE}/users/me/profile",
        {"emailAddress": "me@gmail.com", "messagesTotal": 42},
    )
    assert gmail.get_profile()["email"] == "me@gmail.com"


def test_expired_token_refreshes_before_call(client):
    gmail, http = client
    gmail._save_token(_token(expires_in=-10))  # already expired
    http.add(
        "POST",
        TOKEN_URL,
        {"access_token": "access-456", "expires_in": 3600},
    )
    http.add("GET", f"{API_BASE}/users/me/messages", {})
    gmail.list_messages()
    token_calls = [c for c in http.calls if c["url"] == TOKEN_URL]
    assert len(token_calls) == 1
    assert token_calls[0]["form"]["grant_type"] == "refresh_token"
    # The old refresh token is kept when Google does not return a new one.
    saved = json.loads(gmail.token_path.read_text())
    assert saved["refresh_token"] == "refresh-123"
    api_call = next(c for c in http.calls if "/users/me/messages" in c["url"])
    assert api_call["headers"]["Authorization"] == "Bearer access-456"


def test_refresh_failure_raises_signin_hint(client):
    gmail, http = client
    gmail._save_token(_token(expires_in=-10))
    http.add(
        "POST",
        TOKEN_URL,
        {"error": "invalid_grant", "error_description": "Token revoked."},
    )
    with pytest.raises(GmailAuthError, match="Sign in again"):
        gmail.list_messages()


def test_missing_client_credentials_raise(tmp_token):
    with pytest.raises(GmailAuthError, match="YAADHAMMA_GOOGLE_CLIENT"):
        GmailClient(
            client_id=None,
            client_secret=None,
            label="x",
            token_path=tmp_token,
            http=FakeHttp(),
        ).build_auth_url("http://127.0.0.1:1/")


def test_bad_label_rejected(tmp_path):
    with pytest.raises(GmailAuthError, match="label"):
        GmailClient(
            client_id="id",
            client_secret="s",
            label="../../evil",
            token_path=tmp_path / "t.json",
            http=FakeHttp(),
        )


def test_unauthenticated_api_call_raises(tmp_token):
    gmail = GmailClient(
        client_id="id",
        client_secret="s",
        label="personal1",
        token_path=tmp_token,
        http=FakeHttp(),
    )
    with pytest.raises(GmailAuthError, match="not signed in"):
        gmail.list_messages()


def test_api_http_error_becomes_auth_error(client):
    gmail, http = _authed(client)
    http.add(
        "GET",
        f"{API_BASE}/users/me/messages",
        GmailAuthError("Gmail request failed (HTTP 401): Invalid credentials"),
    )
    with pytest.raises(GmailAuthError, match="HTTP 401"):
        gmail.list_messages()


def test_discover_labels_finds_token_files(tmp_path):
    (tmp_path / "gmail-token-personal1.json").write_text("{}")
    (tmp_path / "gmail-token-work.json").write_text("{}")
    (tmp_path / "outlook-token.json").write_text("{}")
    (tmp_path / "gmail-token-.json").write_text("{}")
    assert discover_labels(tmp_path) == ["personal1", "work"]
