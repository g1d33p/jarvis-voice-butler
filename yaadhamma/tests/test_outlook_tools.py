"""Tests for the Outlook voice tools, including the send approval gate."""

import pytest
from livekit.agents.llm import ToolError

from audit import AuditLog
from outlook import OutlookAuthError
from outlook_tools import OutlookTools
from permissions import ApprovalManager


class FakeClient:
    def __init__(self):
        self.sent = []
        self.fail_with = None

    def _maybe_fail(self):
        if self.fail_with:
            raise self.fail_with

    def list_inbox(self, limit=10):
        self._maybe_fail()
        return [{"id": "m1", "subject": "Hi"}]

    def search_mail(self, query, limit=10):
        self._maybe_fail()
        return [{"id": "m2", "subject": f"match for {query}"}]

    def get_message(self, message_id):
        self._maybe_fail()
        return {"id": message_id, "body_text": "hello"}

    def list_calendar(self, days=1):
        self._maybe_fail()
        return [{"subject": "Standup"}]

    def send_mail(self, to, subject, body):
        self._maybe_fail()
        self.sent.append({"to": to, "subject": subject, "body": body})
        return {"sent": True, "to": to}


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
    return OutlookTools(client=FakeClient(), approvals=ApprovalManager(audit=audit))


async def test_read_inbox_passes_through(tools) -> None:
    result = await tools.read_inbox(_Context(), limit=3)
    assert result["messages"][0]["id"] == "m1"


async def test_search_email_passes_through(tools) -> None:
    result = await tools.search_email(_Context(), query="invoice")
    assert "invoice" in result["messages"][0]["subject"]


async def test_read_email_passes_through(tools) -> None:
    result = await tools.read_email(_Context(), message_id="m1")
    assert result["body_text"] == "hello"


async def test_check_calendar_passes_through(tools) -> None:
    result = await tools.check_calendar(_Context(), days=2)
    assert result["events"][0]["subject"] == "Standup"


async def test_auth_errors_become_tool_errors(tmp_path) -> None:
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    client = FakeClient()
    client.fail_with = OutlookAuthError("Outlook is not signed in.")
    tools = OutlookTools(client=client, approvals=ApprovalManager(audit=audit))
    with pytest.raises(ToolError, match="not signed in"):
        await tools.read_inbox(_Context())


async def test_send_email_asks_for_approval(tools) -> None:
    """A composed email stops and asks; nothing is sent yet."""
    with pytest.raises(ToolError, match="needs the user's approval"):
        await tools.send_email(
            _Context(),
            to="ana@example.com",
            subject="Dinner",
            body="Are you free on Friday evening?",
        )
    assert tools._client.sent == []


async def test_send_email_dictated_goes_directly(tools) -> None:
    """Word-for-word dictated short email: the policy pre-approves."""
    ctx = _Context(user_texts=["send an email to ana@example.com saying running late"])
    result = await tools.send_email(
        ctx, to="ana@example.com", subject="Late", body="running late"
    )
    assert result["sent"] is True
    assert tools._client.sent[0]["body"] == "running late"


async def test_send_email_rejects_bad_address(tools) -> None:
    with pytest.raises(ToolError, match="does not look like an email"):
        await tools.send_email(_Context(), to="Ana", subject="Hi", body="hello")


async def test_send_email_needs_all_fields(tools) -> None:
    with pytest.raises(ToolError, match="need a recipient"):
        await tools.send_email(_Context(), to="ana@example.com", subject="", body="hi")
