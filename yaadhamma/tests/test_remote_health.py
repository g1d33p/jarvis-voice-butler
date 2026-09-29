"""Tests for remote-poll failure streak tracking (2026-09-29).

Every 2-minute poll crashed identically for weeks and the tracebacks only
piled up in remote.log, which nobody reads. The streak file turns that
into a morning-brief warning and a self-test failure instead.
"""

from datetime import datetime, timedelta, timezone

import pytest

import remote_health


@pytest.fixture()
def health_file(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_health, "HEALTH_PATH", tmp_path / "remote-health.json")


def _boom(msg="boom"):
    return RuntimeError(msg)


def test_first_failure_starts_streak(health_file):
    data = remote_health.record_failure(_boom())
    assert data["count"] == 1
    assert "RuntimeError: boom" in data["signature"]
    assert remote_health.streak()["count"] == 1


def test_identical_failures_increment(health_file):
    for _ in range(3):
        remote_health.record_failure(_boom())
    assert remote_health.streak()["count"] == 3


def test_different_failure_resets_streak(health_file):
    remote_health.record_failure(_boom("one"))
    remote_health.record_failure(_boom("one"))
    data = remote_health.record_failure(_boom("two"))
    assert data["count"] == 1
    assert "two" in data["signature"]


def test_success_clears_streak(health_file):
    remote_health.record_failure(_boom())
    remote_health.record_success()
    assert remote_health.streak() == {}
    assert remote_health.needs_attention() is None


def test_needs_attention_threshold(health_file):
    remote_health.record_failure(_boom())
    remote_health.record_failure(_boom())
    assert remote_health.needs_attention() is None  # only 2 so far
    remote_health.record_failure(_boom())
    bad = remote_health.needs_attention()
    assert bad is not None and bad["count"] == 3


def test_needs_attention_ignores_stale_streak(health_file):
    for _ in range(3):
        remote_health.record_failure(_boom())
    future = datetime.now(timezone.utc) + timedelta(hours=25)
    assert remote_health.needs_attention(now=future) is None


def test_needs_attention_with_no_file(health_file):
    assert remote_health.needs_attention() is None


def test_brief_line_silent_without_streak(health_file, monkeypatch):
    import digest

    assert digest._remote_health_line() == ""


def test_brief_line_loud_with_streak(health_file):
    import digest

    for _ in range(4):
        remote_health.record_failure(_boom("orchestrator exploded"))
    line = digest._remote_health_line()
    assert "Phone commands have failed 4 times in a row" in line
    assert "orchestrator exploded" in line
    assert "remote.log" in line
