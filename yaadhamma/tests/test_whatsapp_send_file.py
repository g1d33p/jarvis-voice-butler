"""Tests for whatsapp_send_file (v2 Stage 4).

No network, no browser, no real sends: a fake client and a real
ApprovalManager with a scratch audit log stand in. Real WhatsApp file
sending is unverified (see docs/BUILD_REPORT.md).
"""

import pytest
from livekit.agents.llm import ToolError

from audit import AuditLog
from permissions import ApprovalManager, RiskTier, tier_for
from whatsapp import WhatsAppError
from whatsapp_tools import WhatsAppTools

SELF_NUMBER = "19408438446"
SELF_TITLE = "Jeevan (You)"
OTHER_TITLE = "Ravi"


class FakeClient:
    def __init__(self, chats=()):
        self._chats = dict(chats)
        self._titles = {title for _, title in chats}
        self.sent_files = []  # (chat, path, caption)
        self.fail_send = None

    async def find_chat(self, name, max_rounds=6):
        # Numbers resolve to their titles; titles resolve to themselves.
        if name in self._chats:
            return self._chats[name]
        if name in self._titles:
            return name
        raise WhatsAppError(f"No WhatsApp chat named {name!r} found.")

    async def send_file(self, chat_name, path, caption=""):
        if self.fail_send:
            raise WhatsAppError(self.fail_send)
        self.sent_files.append((chat_name, path, caption))
        return {"sent": True, "chat": chat_name, "verified": True}


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
def tools(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("YAADHAMMA_SELF_CHATS", SELF_NUMBER)
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    client = FakeClient(chats=[(SELF_NUMBER, SELF_TITLE), ("15550001111", OTHER_TITLE)])
    return WhatsAppTools(client=client, approvals=ApprovalManager(audit=audit))


def _make(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 16)
    return path


async def _approve_and_run(tools):
    """Simulate his 'yes': run the action the gate stashed."""
    pending = tools._approvals._pending
    assert pending is not None
    assert pending.tool_name == "whatsapp_send_file"
    return await pending.execute()


async def test_file_sends_are_always_high_risk() -> None:
    assert tier_for("whatsapp_send_file", {}) is RiskTier.HIGH


async def test_send_file_asks_first_naming_file_folder_recipient(
    tools, tmp_path
) -> None:
    target = _make(tmp_path / "Documents" / "report.pdf")
    with pytest.raises(ToolError) as exc:
        await tools.whatsapp_send_file(
            _Context(), SELF_TITLE, str(target), "the report"
        )
    asked = str(exc.value)
    assert "report.pdf" in asked  # the file name
    assert "Documents" in asked  # the folder
    assert SELF_TITLE in asked  # the recipient


async def test_send_file_runs_after_approval(tools, tmp_path) -> None:
    target = _make(tmp_path / "Documents" / "report.pdf")
    with pytest.raises(ToolError):
        await tools.whatsapp_send_file(_Context(), SELF_TITLE, str(target), "hi")
    result = await _approve_and_run(tools)
    client = tools._client
    assert client.sent_files == [(SELF_TITLE, str(target), "hi")]
    assert result["verified"] is True


async def test_send_file_refuses_other_chats(tools, tmp_path) -> None:
    """The own-chat restriction is code, not the model: a resolved
    non-own chat is refused before any approval."""
    target = _make(tmp_path / "report.pdf")
    with pytest.raises(ToolError) as exc:
        await tools.whatsapp_send_file(_Context(), OTHER_TITLE, str(target))
    assert "own chats" in str(exc.value)
    assert tools._client.sent_files == []
    assert tools._approvals._pending is None  # never even asked


async def test_send_file_refuses_paths_outside_home(tools, tmp_path) -> None:
    outside = tmp_path.parent / "report.pdf"
    outside.write_bytes(b"x")
    with pytest.raises(ToolError) as exc:
        await tools.whatsapp_send_file(_Context(), SELF_TITLE, str(outside))
    assert "home folder" in str(exc.value)
    assert tools._client.sent_files == []


async def test_send_file_refuses_over_64_mb(tools, tmp_path, monkeypatch) -> None:
    target = _make(tmp_path / "big.bin")
    # Shrink the limit instead of writing a 65 MB file.
    monkeypatch.setattr("whatsapp_tools.MAX_SEND_FILE_BYTES", 10)
    with pytest.raises(ToolError) as exc:
        await tools.whatsapp_send_file(_Context(), SELF_TITLE, str(target))
    assert "64 MB" in str(exc.value)
    assert tools._client.sent_files == []


async def test_send_file_missing_file_asks_for_more(tools) -> None:
    with pytest.raises(ToolError) as exc:
        await tools.whatsapp_send_file(_Context(), SELF_TITLE, "no-such-file.pdf")
    assert "couldn't find" in str(exc.value)
    assert tools._client.sent_files == []


async def test_send_file_finds_by_name(tools, tmp_path) -> None:
    target = _make(tmp_path / "Downloads" / "boarding-pass.pdf")
    with pytest.raises(ToolError):
        await tools.whatsapp_send_file(_Context(), SELF_TITLE, "boarding-pass.pdf")
    result = await _approve_and_run(tools)
    assert tools._client.sent_files[0][1] == str(target)
    assert result["verified"] is True


async def test_send_file_ambiguous_name_asks_which(tools, tmp_path) -> None:
    _make(tmp_path / "Documents" / "notes.txt")
    _make(tmp_path / "Downloads" / "notes.txt")
    with pytest.raises(ToolError) as exc:
        await tools.whatsapp_send_file(_Context(), SELF_TITLE, "notes.txt")
    asked = str(exc.value)
    assert "Which one" in asked or "which one" in asked
    assert "Documents" in asked and "Downloads" in asked
    assert tools._client.sent_files == []


async def test_send_file_verification_failure_is_honest(tools, tmp_path) -> None:
    """The client could not confirm the attachment landed: the tool
    reports failure, never success."""
    target = _make(tmp_path / "report.pdf")
    tools._client.fail_send = "no file message appeared"
    with pytest.raises(ToolError):
        await tools.whatsapp_send_file(_Context(), SELF_TITLE, str(target))
    with pytest.raises(ToolError) as exc:
        await _approve_and_run(tools)
    assert "no file message appeared" in str(exc.value)


async def test_send_file_uses_tidy_log(monkeypatch, tools, tmp_path) -> None:
    """where_did_file_go: the nightly tidy moved it, the tool follows."""
    moved_to = _make(tmp_path / "Archive" / "old-report.pdf")

    class _Tidy:
        def find_moved(self, query, limit=10):
            return [
                {
                    "moved": "today",
                    "was": "~/Downloads/old-report.pdf",
                    "now": str(moved_to),
                }
            ]

    monkeypatch.setattr("tidy.TidyLog", lambda: _Tidy())
    with pytest.raises(ToolError):
        await tools.whatsapp_send_file(_Context(), SELF_TITLE, "old-report.pdf")
    await _approve_and_run(tools)
    assert tools._client.sent_files[0][1] == str(moved_to)
