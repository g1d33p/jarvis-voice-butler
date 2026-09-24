"""Tests for the WhatsApp voice tools (triage + approval-gated sending)."""

import pytest
from livekit.agents.llm import ToolError

from audit import AuditLog
from permissions import ApprovalManager
from whatsapp import WhatsAppError, WhatsAppNotPairedError
from whatsapp_tools import WhatsAppTools


def _chat(name, unread=0, preview="", time=""):
    return {"name": name, "unread": unread, "preview": preview, "time": time}


class FakeClient:
    def __init__(self, chats=(), paired=True):
        self._chats = list(chats)
        self._paired = paired
        self.sent = []
        self.read_calls = []

    def _need_paired(self):
        if not self._paired:
            raise WhatsAppNotPairedError("not paired")

    async def list_all_chats(self, max_rounds=6):
        self._need_paired()
        return list(self._chats)

    async def find_chat(self, name, max_rounds=6):
        self._need_paired()
        target = name.strip().casefold()
        exact = [c for c in self._chats if c["name"].casefold() == target]
        if len(exact) == 1:
            return exact[0]["name"]
        partial = [c for c in self._chats if target and target in c["name"].casefold()]
        if not partial:
            raise WhatsAppError(f"No WhatsApp chat named {name!r} found.")
        if len(partial) > 1:
            raise WhatsAppError(
                "Several WhatsApp chats match "
                + repr(name)
                + ": "
                + ", ".join(repr(c["name"]) for c in partial)
            )
        return partial[0]["name"]

    async def read_messages(self, chat_name, limit=15):
        self._need_paired()
        chat_name = await self.find_chat(chat_name)
        self.read_calls.append(chat_name)
        return {
            "chat": chat_name,
            "messages": [
                {
                    "sender": "Ravi",
                    "time": "10:30",
                    "text": f"hello from {chat_name}",
                    "outgoing": False,
                }
            ][:limit],
        }

    async def send_message(self, chat_name, text):
        self._need_paired()
        self.sent.append({"chat": chat_name, "text": text})
        return {"sent": True, "chat": chat_name}


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
    client = FakeClient(
        chats=[
            _chat("Ravi", unread=2, preview="see you at 6", time="10:30"),
            _chat("Family", unread=0, preview="ok", time="Yesterday"),
            _chat("Priya", unread=1, preview="thanks", time="Monday"),
        ]
    )
    return WhatsAppTools(client=client, approvals=ApprovalManager(audit=audit))


async def test_list_chats_returns_unread_counts(tools) -> None:
    result = await tools.whatsapp_list_chats(_Context())
    by_name = {c["name"]: c for c in result["chats"]}
    assert by_name["Ravi"]["unread"] == 2
    assert by_name["Family"]["unread"] == 0
    assert result["total"] == 3


async def test_list_chats_not_paired_gives_signin_hint(tmp_path) -> None:
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    tools = WhatsAppTools(
        client=FakeClient(paired=False), approvals=ApprovalManager(audit=audit)
    )
    with pytest.raises(ToolError, match="whatsapp_signin"):
        await tools.whatsapp_list_chats(_Context())


async def test_read_chat_resolves_name_and_reads(tools) -> None:
    result = await tools.whatsapp_read_chat(_Context(), chat_name="ravi")
    assert result["chat"] == "Ravi"
    assert result["messages"][0]["text"] == "hello from Ravi"


async def test_read_chat_needs_a_name(tools) -> None:
    with pytest.raises(ToolError, match="Which chat"):
        await tools.whatsapp_read_chat(_Context(), chat_name="  ")


async def test_read_chat_ambiguous_asks(tools) -> None:
    tools._client._chats.append(_chat("Ravi Kumar"))
    with pytest.raises(ToolError, match="Several WhatsApp chats match"):
        await tools.whatsapp_read_chat(_Context(), chat_name="rav")


async def test_read_chat_unknown_names_it(tools) -> None:
    with pytest.raises(ToolError, match="No WhatsApp chat named 'Nobody'"):
        await tools.whatsapp_read_chat(_Context(), chat_name="Nobody")


async def test_where_needed_covers_only_unread_chats(tools) -> None:
    result = await tools.whatsapp_where_needed(_Context())
    names = [c["chat"] for c in result["chats_needing_attention"]]
    assert names == ["Ravi", "Priya"]
    assert result["unread_chat_count"] == 2
    assert result["total_chats"] == 3
    first = result["chats_needing_attention"][0]
    assert first["unread"] == 2
    assert first["messages"][0]["text"] == "hello from Ravi"


async def test_where_needed_caps_chat_count(tools) -> None:
    result = await tools.whatsapp_where_needed(_Context(), max_chats=1)
    assert len(result["chats_needing_attention"]) == 1
    assert result["unread_chat_count"] == 2  # total still reported


async def test_where_needed_quiet_when_nothing_unread(tmp_path) -> None:
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    tools = WhatsAppTools(
        client=FakeClient(chats=[_chat("Family")]),
        approvals=ApprovalManager(audit=audit),
    )
    result = await tools.whatsapp_where_needed(_Context())
    assert result["chats_needing_attention"] == []
    assert result["unread_chat_count"] == 0


async def test_send_message_asks_for_approval(tools) -> None:
    """A composed message stops and asks; nothing is sent yet."""
    with pytest.raises(ToolError, match="needs the user's approval"):
        await tools.whatsapp_send_message(
            _Context(), chat_name="Ravi", message="Are you free on Friday evening?"
        )
    assert tools._client.sent == []


async def test_send_message_dictated_goes_directly(tools) -> None:
    ctx = _Context(user_texts=["send a whatsapp to Ravi saying running late"])
    result = await tools.whatsapp_send_message(
        ctx, chat_name="Ravi", message="running late"
    )
    assert result["sent"] is True
    assert tools._client.sent == [{"chat": "Ravi", "text": "running late"}]


async def test_send_message_never_auto_sends_twice_for_one_utterance(tools) -> None:
    ctx = _Context(user_texts=["send a whatsapp to Ravi saying running late"])
    await tools.whatsapp_send_message(ctx, chat_name="Ravi", message="running late")
    # Same dictated message again: the policy pre-approval is spent, so it asks.
    with pytest.raises(ToolError, match="needs the user's approval"):
        await tools.whatsapp_send_message(ctx, chat_name="Ravi", message="running late")
    assert len(tools._client.sent) == 1


async def test_send_message_resolves_chat_before_asking(tools) -> None:
    """An unknown chat fails fast instead of asking approval for a guess."""
    with pytest.raises(ToolError, match="No WhatsApp chat named 'Nobody'"):
        await tools.whatsapp_send_message(_Context(), chat_name="Nobody", message="hi")


async def test_send_message_ambiguous_chat_asks(tools) -> None:
    tools._client._chats.append(_chat("Ravi Kumar"))
    with pytest.raises(ToolError, match="Several WhatsApp chats match"):
        await tools.whatsapp_send_message(_Context(), chat_name="rav", message="hi")
    assert tools._client.sent == []


async def test_send_message_needs_text(tools) -> None:
    with pytest.raises(ToolError, match="need a chat and a message"):
        await tools.whatsapp_send_message(_Context(), chat_name="Ravi", message="  ")
