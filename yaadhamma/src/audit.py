"""Local audit log: every consequential action Yaadhamma takes, on this Mac.

One JSON object per line, append-only. The log never leaves the machine and
never contains secrets: values under sensitive keys are replaced with
"[redacted]" and long text is truncated. Logging is best-effort; a failure
to write must never break the action being logged.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_AUDIT_PATH = Path.home() / ".yaadhamma" / "audit.jsonl"

_SENSITIVE_KEY_PARTS = (
    "password",
    "passcode",
    "otp",
    "pin",
    "token",
    "secret",
    "api_key",
    "apikey",
    "auth",
    "credential",
    "ssn",
)

# Longest value kept verbatim; anything longer is cut with a marker.
_MAX_VALUE_CHARS = 500


def _is_sensitive_key(key: str) -> bool:
    key = key.casefold()
    return any(part in key for part in _SENSITIVE_KEY_PARTS)


def redact(value: Any) -> Any:
    """Return a log-safe copy of `value` with secrets replaced."""
    if isinstance(value, dict):
        return {
            str(k): "[redacted]" if _is_sensitive_key(str(k)) else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
        return value[:_MAX_VALUE_CHARS] + " ...(truncated)"
    return value


class AuditLog:
    """Append-only JSONL audit log."""

    def __init__(self, path: str | Path | None = None) -> None:
        raw = path if path is not None else os.environ.get("YAADHAMMA_AUDIT_PATH")
        self.path = Path(raw).expanduser() if raw else DEFAULT_AUDIT_PATH

    def record(self, event: str, **fields: Any) -> None:
        """Append one event. Never raises."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": event,
                **redact(fields),
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            # The audit trail must never break the action being audited.
            pass

    def read_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the newest `limit` entries (oldest first). Empty if none."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        entries = []
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
