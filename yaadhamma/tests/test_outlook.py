"""Tests for the Outlook skill (Microsoft Graph, device-code flow).

All HTTP goes through an injected fake: no network, no fixtures on disk.
"""

import json
import os
import stat

import pytest

from outlook import (
    GRAPH,
    OutlookAuthError,
    OutlookClient,
)


class FakeHttp:
    """Canned (method, url-prefix) -> response queue; records requests."""

    def __init__(self):
        self.calls = []
        self.routes = []

    def add(self, method, url_prefix, response):
        self.routes.append((method, url_prefix, response))

    def __call__(self, method, url, payload=None, headers=None):
        self.calls.append(
            {"method": method, "url": url, "payload": payload, "headers": headers or {}}
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
    return tmp_path / "outlook-token.json"


@pytest.fixture()
def client(tmp_token):
    http = FakeHttp()
    return OutlookClient(
        client_id="test-client-id", token_path=tmp_token, http=http
    ), http


def test_start_device_flow_parses_response(client, tmp_token):
    outlook, http = client
    http.add(
        "POST",
        "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode",
        {
            "user_code": "ABCD-1234",
            "verification_uri": "https://microsoft.com/devicelogin",
            "message": "Go to https://microsoft.com/devicelogin and enter ABCD-1234",
            "device_code": "device-abc",
            "expires_in": 900,
            "interval": 5,
        },
    )
    flow = outlook.start_device_flow()
    assert flow["user_code"] == "ABCD-1234"
    assert flow["verification_uri"] == "https://microsoft.com/devicelogin"
    assert "ABCD-1234" in flow["message"]
    # Scopes requested are least-privilege.
    sent = http.calls[0]["payload"]
    assert "Mail.Read" in sent["scope"]
    assert "offline_access" in sent["scope"]


def test_poll_device_flow_waits_then_succeeds(client):
    outlook, http = client
    attempts = {"n": 0}

    def token_endpoint(call):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return {"error": "authorization_pending"}
        return _token()

    http.add(
        "POST",
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        token_endpoint,
    )
    token = outlook.poll_for_token("device-abc", interval=0)
    assert token["access_token"] == "access-123"
    assert attempts["n"] == 3
    # Token persisted to disk, private permissions.
    saved = json.loads(outlook.token_path.read_text())
    assert saved["access_token"] == "access-123"
    assert saved["refresh_token"] == "refresh-123"
    mode = stat.S_IMODE(os.stat(outlook.token_path).st_mode)
    assert mode == 0o600


def test_poll_device_flow_denied_raises(client):
    outlook, http = client
    http.add(
        "POST",
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        {"error": "authorization_declined"},
    )
    with pytest.raises(OutlookAuthError, match=r"declined|denied"):
        outlook.poll_for_token("device-abc", interval=0)


def _authed(client, tmp_token):
    """Client with a fresh cached token on disk."""
    outlook, http = client
    outlook._save_token(_token(expires_in=3600))
    return outlook, http


def test_list_inbox_parses_messages(client, tmp_token):
    outlook, http = _authed(client, tmp_token)
    http.add(
        "GET",
        f"{GRAPH}/me/messages",
        {
            "value": [
                {
                    "id": "m1",
                    "subject": "Hello",
                    "from": {"emailAddress": {"address": "a@example.com"}},
                    "receivedDateTime": "2026-09-24T10:00:00Z",
                    "bodyPreview": "Hi there",
                    "isRead": False,
                },
            ]
        },
    )
    messages = outlook.list_inbox(limit=5)
    assert len(messages) == 1
    m = messages[0]
    assert m["id"] == "m1"
    assert m["subject"] == "Hello"
    assert m["from"] == "a@example.com"
    assert m["preview"] == "Hi there"
    assert m["is_read"] is False
    # Bearer token sent, top param honored ($ is percent-encoded; Graph accepts it).
    call = http.calls[-1]
    assert call["headers"]["Authorization"] == "Bearer access-123"
    assert "top=5" in call["url"]


def test_search_mail_uses_search_query(client, tmp_token):
    outlook, http = _authed(client, tmp_token)
    http.add("GET", f"{GRAPH}/me/messages", {"value": []})
    outlook.search_mail("quarterly report")
    assert "search=" in http.calls[-1]["url"]
    assert "quarterly" in http.calls[-1]["url"]


def test_get_message_returns_text_body(client, tmp_token):
    outlook, http = _authed(client, tmp_token)
    http.add(
        "GET",
        f"{GRAPH}/me/messages/m1",
        {
            "id": "m1",
            "subject": "Hello",
            "from": {"emailAddress": {"address": "a@example.com"}},
            "toRecipients": [{"emailAddress": {"address": "b@example.com"}}],
            "body": {"contentType": "html", "content": "<p>Hi <b>there</b></p>"},
        },
    )
    m = outlook.get_message("m1")
    assert m["subject"] == "Hello"
    assert "Hi there" in m["body_text"]
    assert "<" not in m["body_text"]


def test_send_mail_posts_correct_payload(client, tmp_token):
    outlook, http = _authed(client, tmp_token)
    http.add("POST", f"{GRAPH}/me/sendMail", {})
    outlook.send_mail(to="c@example.com", subject="Re: Hello", body="Sounds good.")
    call = http.calls[-1]
    msg = call["payload"]["message"]
    assert msg["subject"] == "Re: Hello"
    assert msg["toRecipients"][0]["emailAddress"]["address"] == "c@example.com"
    assert "Sounds good." in msg["body"]["content"]


def test_expired_token_refreshes_before_call(client, tmp_token):
    outlook, http = client
    outlook._save_token(_token(expires_in=-10))  # already expired
    http.add(
        "POST",
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        {
            "access_token": "access-456",
            "refresh_token": "refresh-456",
            "expires_in": 3600,
        },
    )
    http.add("GET", f"{GRAPH}/me/messages", {"value": []})
    outlook.list_inbox()
    token_calls = [c for c in http.calls if c["url"].endswith("/token")]
    assert len(token_calls) == 1
    assert token_calls[0]["payload"]["grant_type"] == "refresh_token"
    api_call = next(c for c in http.calls if "/me/messages" in c["url"])
    assert api_call["headers"]["Authorization"] == "Bearer access-456"


def test_list_calendar_parses_events(client, tmp_token):
    outlook, http = _authed(client, tmp_token)
    http.add(
        "GET",
        f"{GRAPH}/me/calendarview",
        {
            "value": [
                {
                    "subject": "Standup",
                    "start": {
                        "dateTime": "2026-09-25T09:00:00",
                        "timeZone": "America/Chicago",
                    },
                    "end": {
                        "dateTime": "2026-09-25T09:30:00",
                        "timeZone": "America/Chicago",
                    },
                    "location": {"displayName": "Teams"},
                },
            ]
        },
    )
    events = outlook.list_calendar(days=1)
    assert len(events) == 1
    assert events[0]["subject"] == "Standup"
    assert events[0]["start"] == "2026-09-25T09:00:00"
    assert events[0]["location"] == "Teams"


def test_missing_client_id_raises_helpful_error(tmp_token):
    with pytest.raises(OutlookAuthError, match="YAADHAMMA_OUTLOOK_CLIENT_ID"):
        OutlookClient(
            client_id=None, token_path=tmp_token, http=FakeHttp()
        ).start_device_flow()


def test_unauthenticated_api_call_raises(tmp_token):
    outlook = OutlookClient(client_id="x", token_path=tmp_token, http=FakeHttp())
    with pytest.raises(OutlookAuthError, match="not signed in"):
        outlook.list_inbox()
