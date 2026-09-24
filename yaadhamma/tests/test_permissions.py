"""Tests for risk tiers and the unified approval gate (src/permissions.py)."""

import pytest
from livekit.agents.llm import ToolError

from audit import AuditLog
from permissions import (
    ApprovalManager,
    RiskTier,
    is_clear_approval,
    tier_for,
)


class _Item:
    def __init__(self, role, text, item_id="m1", created_at=0):
        self.type = "message"
        self.role = role
        self.text_content = text
        self.id = item_id
        self.created_at = created_at


class _History:
    def __init__(self, items):
        self.items = items


class _Session:
    def __init__(self, items):
        self.history = _History(items)


class _Context:
    def __init__(self, items):
        self.session = _Session(items)


def _said(*texts):
    items = []
    for i, text in enumerate(texts):
        items.append(_Item("user", text, item_id=f"u{i}"))
    return _Context(items)


# --- tiers -----------------------------------------------------------------


def test_read_only_tools_are_low() -> None:
    for tool in (
        "search_the_web",
        "open_url",
        "read_page",
        "inspect_page",
        "list_directory",
        "search_files",
        "list_running_apps",
        "read_clipboard",
    ):
        assert tier_for(tool) is RiskTier.LOW, tool


def test_state_changing_tools_are_medium() -> None:
    for tool in (
        "close_tab",
        "close_browser",
        "quit_application",
        "create_file",
        "move_to_trash",
        "rename_path",
    ):
        assert tier_for(tool) is RiskTier.MEDIUM, tool


def test_unknown_tools_fail_closed_to_medium() -> None:
    assert tier_for("some_future_tool") is RiskTier.MEDIUM


def test_risky_click_targets_are_high() -> None:
    assert tier_for("click", {"target": "#3", "label": "Buy now"}) is RiskTier.HIGH
    assert tier_for("click", {"target": "Send"}) is RiskTier.HIGH
    assert tier_for("click", {"target": "#7", "label": "Open chat"}) is RiskTier.LOW


def test_consequential_enter_is_high() -> None:
    assert (
        tier_for("press_key", {"key": "Enter", "consequential": True}) is RiskTier.HIGH
    )
    assert tier_for("press_key", {"key": "Enter"}) is RiskTier.LOW
    assert tier_for("press_key", {"key": "Escape"}) is RiskTier.LOW


# --- gate ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_risk_runs_immediately(tmp_path) -> None:
    manager = ApprovalManager(audit=AuditLog(tmp_path / "a.jsonl"))
    calls = []

    async def execute():
        calls.append(True)
        return {"ok": True}

    result = await manager.gate(
        tool_name="read_page",
        description="read the page",
        context=_said("read it"),
        execute=execute,
    )
    assert result == {"ok": True}
    assert calls == [True]
    assert manager.pending is None


@pytest.mark.asyncio
async def test_medium_with_spoken_yes_runs_without_asking(tmp_path) -> None:
    manager = ApprovalManager(audit=AuditLog(tmp_path / "a.jsonl"))
    context = _Context(
        [
            _Item("assistant", "Shall I quit Google Chrome?", item_id="a1"),
            _Item("user", "Yes, quit it.", item_id="u1"),
        ]
    )
    calls = []

    async def execute():
        calls.append(True)
        return "quit"

    result = await manager.gate(
        tool_name="quit_application",
        description="quit Google Chrome",
        context=context,
        execute=execute,
    )
    assert result == "quit"
    assert calls == [True]


@pytest.mark.asyncio
async def test_medium_without_approval_stops_and_asks(tmp_path) -> None:
    manager = ApprovalManager(audit=AuditLog(tmp_path / "a.jsonl"))
    calls = []

    async def execute():
        calls.append(True)

    with pytest.raises(ToolError, match="needs the user's approval"):
        await manager.gate(
            tool_name="quit_application",
            description="quit Google Chrome",
            context=_said("do something"),
            execute=execute,
        )
    assert calls == []
    assert manager.pending is not None
    assert manager.pending.description == "quit Google Chrome"


@pytest.mark.asyncio
async def test_high_accepts_spoken_yes_to_exact_draft(tmp_path) -> None:
    # The model proposed the exact draft and the user said yes: that spoken
    # approval counts for HIGH too (no redundant re-ask).
    manager = ApprovalManager(audit=AuditLog(tmp_path / "a.jsonl"))
    context = _Context(
        [
            _Item("assistant", 'How about: "Good morning!"?', item_id="a1"),
            _Item("user", "Yes.", item_id="u1"),
        ]
    )

    async def execute():
        return "sent"

    result = await manager.gate(
        tool_name="press_key",
        description="send 'Good morning!'",
        context=context,
        execute=execute,
        args={"key": "Enter", "consequential": True},
        quoted="Good morning!",
    )
    assert result == "sent"
    assert manager.pending is None


@pytest.mark.asyncio
async def test_high_still_asks_without_matching_spoken_approval(tmp_path) -> None:
    # A yes that answered a different question does not count for HIGH: the
    # tool stops and the model must ask about this exact action.
    manager = ApprovalManager(audit=AuditLog(tmp_path / "a.jsonl"))
    context = _Context(
        [
            _Item("assistant", "Shall I research the venue?", item_id="a1"),
            _Item("user", "Yes.", item_id="u1"),
        ]
    )

    async def execute():
        pytest.fail("must not run without a matching approval")

    with pytest.raises(ToolError, match="approve_pending_action"):
        await manager.gate(
            tool_name="click",
            description="send 'see you at seven'",
            context=context,
            execute=execute,
            args={"target": "Send", "label": "Send"},
            quoted="see you at seven",
        )
    assert manager.pending is not None


@pytest.mark.asyncio
async def test_pre_approved_policy_runs_high_risk(tmp_path) -> None:
    manager = ApprovalManager(audit=AuditLog(tmp_path / "a.jsonl"))

    async def execute():
        return "sent"

    result = await manager.gate(
        tool_name="click",
        description="send 'hi' to Ravi",
        context=_said("send hi to Ravi"),
        execute=execute,
        args={"target": "Send", "label": "Send"},
        pre_approved="dictated word for word",
    )
    assert result == "sent"
    assert manager.pending is None


# --- confirm ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_with_yes_runs_the_action(tmp_path) -> None:
    manager = ApprovalManager(audit=AuditLog(tmp_path / "a.jsonl"))
    ran = []

    async def execute():
        ran.append(True)
        return {"done": True}

    with pytest.raises(ToolError):
        await manager.gate(
            tool_name="quit_application",
            description="quit Google Chrome",
            context=_said("quit chrome"),
            execute=execute,
        )
    result = await manager.confirm(_said("yes"), "yes")
    assert result == {"done": True}
    assert ran == [True]
    assert manager.pending is None


@pytest.mark.asyncio
async def test_confirm_with_no_drops_it(tmp_path) -> None:
    manager = ApprovalManager(audit=AuditLog(tmp_path / "a.jsonl"))

    async def execute():
        pytest.fail("must not run")

    with pytest.raises(ToolError):
        await manager.gate(
            tool_name="quit_application",
            description="quit Google Chrome",
            context=_said("quit chrome"),
            execute=execute,
        )
    with pytest.raises(ToolError, match="said no"):
        await manager.confirm(_said("No, don't."), "no")
    assert manager.pending is None


@pytest.mark.asyncio
async def test_unclear_reply_keeps_it_waiting(tmp_path) -> None:
    manager = ApprovalManager(audit=AuditLog(tmp_path / "a.jsonl"))
    ran = []

    async def execute():
        ran.append(True)
        return "done"

    with pytest.raises(ToolError):
        await manager.gate(
            tool_name="quit_application",
            description="quit Google Chrome",
            context=_said("quit chrome"),
            execute=execute,
        )
    with pytest.raises(ToolError, match="not a clear yes"):
        await manager.confirm(_said("Very well."), "very well")
    assert ran == []
    assert manager.pending is not None
    # A later clear yes still works.
    assert await manager.confirm(_said("Yes."), "yes") == "done"
    assert ran == [True]
    assert manager.pending is None


@pytest.mark.asyncio
async def test_one_approval_covers_one_action(tmp_path) -> None:
    manager = ApprovalManager(audit=AuditLog(tmp_path / "a.jsonl"))

    async def execute():
        return "done"

    with pytest.raises(ToolError):
        await manager.gate(
            tool_name="quit_application",
            description="quit Google Chrome",
            context=_said("quit chrome"),
            execute=execute,
        )
    await manager.confirm(_said("yes"), "yes")
    with pytest.raises(ToolError, match="Nothing is waiting"):
        await manager.confirm(_said("yes"), "yes")


@pytest.mark.asyncio
async def test_expired_pending_cannot_be_confirmed(tmp_path) -> None:
    manager = ApprovalManager(audit=AuditLog(tmp_path / "a.jsonl"), ttl_seconds=-1)

    async def execute():
        pytest.fail("must not run")

    with pytest.raises(ToolError):
        await manager.gate(
            tool_name="quit_application",
            description="quit Google Chrome",
            context=_said("quit chrome"),
            execute=execute,
        )
    with pytest.raises(ToolError, match="Nothing is waiting"):
        await manager.confirm(_said("yes"), "yes")


@pytest.mark.asyncio
async def test_verify_runs_before_execute(tmp_path) -> None:
    manager = ApprovalManager(audit=AuditLog(tmp_path / "a.jsonl"))
    order = []

    async def verify():
        order.append("verify")

    async def execute():
        order.append("execute")
        return "ok"

    with pytest.raises(ToolError):
        await manager.gate(
            tool_name="click",
            description="send 'hi' to Ravi",
            context=_said("send it"),
            execute=execute,
            verify=verify,
            args={"target": "Send"},
        )
    assert await manager.confirm(_said("yes"), "yes") == "ok"
    assert order == ["verify", "execute"]


@pytest.mark.asyncio
async def test_verify_failure_blocks_execute(tmp_path) -> None:
    manager = ApprovalManager(audit=AuditLog(tmp_path / "a.jsonl"))

    async def verify():
        raise ToolError("The message changed.")

    async def execute():
        pytest.fail("must not run")

    with pytest.raises(ToolError):
        await manager.gate(
            tool_name="click",
            description="send 'hi' to Ravi",
            context=_said("send it"),
            execute=execute,
            verify=verify,
            args={"target": "Send"},
        )
    with pytest.raises(ToolError, match="changed"):
        await manager.confirm(_said("yes"), "yes")


def test_is_clear_approval_basics() -> None:
    assert is_clear_approval("Yes, do it.")
    assert is_clear_approval("haan, cheyyi")
    assert not is_clear_approval("No, don't.")
    assert not is_clear_approval("Very well.")
    assert not is_clear_approval("")
    # "send it" is an affirmative phrase either way; the sending flag only
    # matters for bare commands like "send".
    assert is_clear_approval("send it", sending=True)
    assert is_clear_approval("send it", sending=False)
    assert is_clear_approval("send", sending=True)
    assert not is_clear_approval("send", sending=False)


@pytest.mark.asyncio
async def test_pending_approval_expires(tmp_path, monkeypatch) -> None:
    import permissions as permissions_module

    manager = ApprovalManager(audit=AuditLog(tmp_path / "a.jsonl"))

    async def execute():
        pytest.fail("expired approval must not run")

    with pytest.raises(ToolError, match="approve_pending_action"):
        await manager.gate(
            tool_name="move_to_trash",
            description="move notes.txt to trash",
            context=_Context([]),
            execute=execute,
        )

    real = permissions_module.time.monotonic
    monkeypatch.setattr(
        permissions_module.time,
        "monotonic",
        lambda: real() + permissions_module.APPROVAL_TTL_SECONDS + 1,
    )
    with pytest.raises(ToolError, match="Nothing is waiting"):
        await manager.confirm(_Context([]), "yes")
    assert manager.pending is None


def _conversation(*pairs):
    """Build a context from (role, text) pairs, oldest first."""
    items = [
        _Item(role, text, item_id=f"m{i}", created_at=1000 + i)
        for i, (role, text) in enumerate(pairs)
    ]
    return _Context(items)


def test_spoken_approval_matches_inflected_action_word() -> None:
    """Regression: 'Shall I try sending it again?' names the send action."""
    from permissions import spoken_approval

    context = _conversation(
        ("assistant", "It still looks like a draft. Shall I try sending it again?"),
        ("user", "Yes, please."),
    )
    approved, approval_id = spoken_approval(
        context, "send 'Call you later'", quoted="Call you later"
    )
    assert approved
    assert approval_id == "m1"


def test_spoken_approval_rejects_substring_false_positive() -> None:
    """'hi' must not match inside 'this'."""
    from permissions import spoken_approval

    context = _conversation(
        ("assistant", "Is this message okay?"),
        ("user", "Yes."),
    )
    approved, _ = spoken_approval(context, "send 'hi'", quoted="hi")
    assert not approved


def test_names_action_basics() -> None:
    from permissions import _names_action, _words

    assert _names_action({"send"}, _words("shall i try sending it again"))
    assert _names_action({"sending"}, _words("shall i send it"))
    assert not _names_action({"hi"}, _words("is this okay"))
    assert not _names_action({"ok"}, _words("that is okay"))
    assert _names_action({"quit"}, _words("quit google chrome"))
