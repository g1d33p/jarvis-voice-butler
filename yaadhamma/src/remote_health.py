"""Consecutive identical remote-poll failures, tracked for humans.

The remote poll runs every 2 minutes. When its failure is systemic — e.g.
the orchestrator factory raising on every single poll — the launchd log
fills with thousands of identical tracebacks that nobody reads (seen
2026-09-29: phone commands were dead for weeks before anyone looked).
This module keeps a tiny streak file so the morning brief and the
self-test can say so loudly instead.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

HEALTH_PATH = Path.home() / ".yaadhamma" / "remote-health.json"
ALERT_AFTER = 3  # consecutive identical failures before it is reported
FRESH_HOURS = 24  # a streak older than this is stale history, not news


def _signature(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:200]


def _read() -> dict:
    try:
        data = json.loads(HEALTH_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(data: dict) -> None:
    try:
        HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEALTH_PATH.write_text(json.dumps(data))
    except Exception:
        pass


def record_failure(exc: BaseException) -> dict:
    """Note one failed poll. Returns the updated streak."""
    now = datetime.now(timezone.utc).isoformat()
    sig = _signature(exc)
    data = _read()
    if data.get("signature") == sig:
        data["count"] = int(data.get("count", 0)) + 1
    else:
        data = {"signature": sig, "count": 1, "first": now}
    data["last"] = now
    _write(data)
    return data


def record_success() -> None:
    """A poll completed: any failure streak is over."""
    with contextlib.suppress(Exception):
        HEALTH_PATH.unlink(missing_ok=True)


def streak() -> dict:
    """The current failure streak, or {} when the last poll succeeded."""
    return _read()


def needs_attention(now: datetime | None = None) -> dict | None:
    """The streak when it is worth telling Jeevan about, else None."""
    data = _read()
    if int(data.get("count", 0)) < ALERT_AFTER:
        return None
    try:
        last = datetime.fromisoformat(data["last"])
    except Exception:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now - last > timedelta(hours=FRESH_HOURS):
        return None
    return data
