"""Tests for the Gmail voice tools (multi-account merge + send gate)."""

import pytest
from livekit.agents.llm import ToolError

from audit import AuditLog
from gmail import GmailAuthError
from gmail_tools import GmailTools
from permissions import ApprovalManager


class FakeClient:
    def __init__(self, label, messages=(), fail_with=None):
        self.label = label
        self._messages = list(messages)
        self.fail_with = fail_with
        self.sent = []

    def _maybe_fail(self):
        if self.fail_with:
            raise self.fail_with

    def list_messages(self, limit=10):
        self._maybe_fail()
        return self._messages[:limit]

    def search_mail(self, query, limit=10):
        self._maybe_fail()
        return [m for m in self._messages if query in m["subject"]][:limit]

    def get_message(self, message_id):
        self._maybe_fail()
        for m in self._messages:
            if m["id"] == message_id:
                return {**m, "body_text": f"body of {message_id}"}
        raise GmailAuthError(f"message {message_id} not found")

    def send_mail(self, to, subject, body):
        self._maybe_fail()
        self.sent.append({"to": to, "subject": subject, "body": body})
        return {"sent": True, "to": to}


def _msg(mid, subject, internal_date):
    return {
        "id": mid,
        "subject": subject,
        "from": "a@example.com",
        "internal_date": internal_date,
        "is_read": True,
    }


class _Item:
    def __init__(self, role, text):
        self.type = "message"
        self.role = role
        self.id = f"id-{text[:8]}"
        self.text_content = text


class _History:
    def __init__(self, items):
        self.items = items


class _Session:
    def __init__(self, items):
        self.history = _History(items)


class _Context:
    def __init__(self, user_texts=()):
        self.session = _Session([_Item("user", t) for t in user_texts])


@pytest.fixture()
def tools(tmp_path):
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    clients = [
        FakeClient("personal1", [_msg("a1", "Older", 1000)]),
        FakeClient("personal2", [_msg("b1", "Newer", 2000)]),
    ]
    return GmailTools(clients=clients, approvals=ApprovalManager(audit=audit))


async def test_read_inbox_merges_accounts_newest_first(tools) -> None:
    result = await tools.gmail_read_inbox(_Context(), limit=5)
    subjects = [m["subject"] for m in result["messages"]]
    assert subjects == ["Newer", "Older"]
    accounts = {m["account"] for m in result["messages"]}
    assert accounts == {"personal1", "personal2"}


async def test_read_inbox_message_ids_route_back(tools) -> None:
    result = await tools.gmail_read_inbox(_Context(), limit=5)
    ids = [m["id"] for m in result["messages"]]
    assert "personal2:b1" in ids
    full = await tools.gmail_read_email(_Context(), message_id="personal2:b1")
    assert full["body_text"] == "body of b1"


async def test_read_email_without_label_tries_each_account(tools) -> None:
    full = await tools.gmail_read_email(_Context(), message_id="b1")
    assert full["body_text"] == "body of b1"


async def test_read_email_unknown_label_raises(tools) -> None:
    with pytest.raises(ToolError, match="not a linked Gmail account"):
        await tools.gmail_read_email(_Context(), message_id="nope:b1")


async def test_search_email_searches_all_accounts(tools) -> None:
    result = await tools.gmail_search_email(_Context(), query="Newer", limit=5)
    assert len(result["messages"]) == 1
    assert result["messages"][0]["account"] == "personal2"


async def test_per_account_errors_are_reported_not_fatal(tmp_path) -> None:
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    clients = [
        FakeClient("ok", [_msg("a1", "Fine", 1000)]),
        FakeClient("broken", fail_with=GmailAuthError("Token revoked.")),
    ]
    tools = GmailTools(clients=clients, approvals=ApprovalManager(audit=audit))
    result = await tools.gmail_read_inbox(_Context())
    assert [m["subject"] for m in result["messages"]] == ["Fine"]
    assert any("broken" in e for e in result["account_errors"])


async def test_no_accounts_linked_gives_signin_hint(tmp_path) -> None:
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    tools = GmailTools(clients=[], approvals=ApprovalManager(audit=audit))
    with pytest.raises(ToolError, match="gmail_signin"):
        await tools.gmail_read_inbox(_Context())


async def test_send_email_asks_for_approval(tools) -> None:
    """A composed email stops and asks; nothing is sent yet."""
    with pytest.raises(ToolError, match="needs the user's approval"):
        await tools.gmail_send_email(
            _Context(),
            to="ana@example.com",
            subject="Dinner",
            body="Are you free on Friday evening?",
        )
    assert all(c.sent == [] for c in tools._clients)


async def test_send_email_dictated_goes_directly(tools) -> None:
    """Word-for-word dictated short email: the policy pre-approves."""
    ctx = _Context(user_texts=["send an email to ana@example.com saying running late"])
    result = await tools.gmail_send_email(
        ctx, to="ana@example.com", subject="Late", body="running late"
    )
    assert result["sent"] is True
    assert tools._clients[0].sent[0]["body"] == "running late"


async def test_send_email_names_sending_account(tools) -> None:
    with pytest.raises(ToolError, match="personal1"):
        await tools.gmail_send_email(
            _Context(),
            to="ana@example.com",
            subject="Dinner",
            body="Are you free on Friday evening?",
        )


async def test_send_email_unknown_account_label_raises(tools) -> None:
    with pytest.raises(ToolError, match="not a linked Gmail account"):
        await tools.gmail_send_email(
            _Context(),
            to="ana@example.com",
            subject="Hi",
            body="hello",
            account_label="nope",
        )


async def test_send_email_rejects_bad_address(tools) -> None:
    with pytest.raises(ToolError, match="does not look like an email"):
        await tools.gmail_send_email(_Context(), to="Ana", subject="Hi", body="hello")


async def test_send_email_needs_all_fields(tools) -> None:
    with pytest.raises(ToolError, match="need a recipient"):
        await tools.gmail_send_email(
            _Context(), to="ana@example.com", subject="", body="hi"
        )


async def test_auth_errors_are_reported_per_account(tmp_path) -> None:
    """A revoked token on one account does not kill the whole read."""
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    tools = GmailTools(
        clients=[FakeClient("x", fail_with=GmailAuthError("not signed in"))],
        approvals=ApprovalManager(audit=audit),
    )
    result = await tools.gmail_read_inbox(_Context())
    assert result["messages"] == []
    assert any("not signed in" in e for e in result["account_errors"])
