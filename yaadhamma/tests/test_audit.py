"""Tests for the local audit log (src/audit.py)."""

import json

from audit import AuditLog, redact


def test_record_writes_json_lines(tmp_path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("tool_call", tool="click", tier="high")

    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event"] == "tool_call"
    assert entry["tool"] == "click"
    assert entry["tier"] == "high"
    assert "ts" in entry


def test_secrets_are_redacted(tmp_path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record(
        "tool_call",
        tool="send",
        args={"text": "hi", "api_key": "sk-secret", "password": "hunter2"},
    )

    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["args"]["text"] == "hi"
    assert entry["args"]["api_key"] == "[redacted]"
    assert entry["args"]["password"] == "[redacted]"


def test_nested_secrets_are_redacted() -> None:
    cleaned = redact({"outer": {"auth_token": "abc"}, "items": [{"pin": "1234"}]})
    assert cleaned == {
        "outer": {"auth_token": "[redacted]"},
        "items": [{"pin": "[redacted]"}],
    }


def test_long_values_are_truncated(tmp_path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("tool_call", tool="read_page", args={"text": "x" * 600})

    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["args"]["text"].endswith("...(truncated)")
    assert len(entry["args"]["text"]) < 600


def test_record_never_raises(tmp_path) -> None:
    log = AuditLog(tmp_path / "unwritable" / "deep" / "audit.jsonl")
    # Even a broken path must not break the action being audited.
    log.record("tool_call", tool="click")
    assert True


def test_read_recent_returns_newest_first_capped(tmp_path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(5):
        log.record("tool_call", tool=f"tool_{i}")

    recent = log.read_recent(limit=3)
    assert [e["tool"] for e in recent] == ["tool_2", "tool_3", "tool_4"]


def test_read_recent_empty_when_no_log(tmp_path) -> None:
    assert AuditLog(tmp_path / "missing.jsonl").read_recent() == []
