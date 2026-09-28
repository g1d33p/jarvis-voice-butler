#!/usr/bin/env python3
"""Yaadhamma status: one screen for cost and health.

    cd ~/jarvis-voice-butler/yaadhamma
    uv run scripts/status.py

Shows the last 7 days of model spend (per feature), the month-to-date
total against the budget, local health checks (WhatsApp pairing, Gmail
and calendar sign-in, disk space), and recent errors from the audit log.
Everything here is read-only; it changes nothing.
"""

import json
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

YAADHAMMA_DIR = Path.home() / ".yaadhamma"


def _cost_section() -> list[str]:
    from costs import CostStore

    store = CostStore()
    summary = store.summary_7d()
    spent, budget = store.month_to_date_usd(), None
    try:
        import config

        budget = float(config.MONTHLY_BUDGET_USD)
    except Exception:
        pass
    lines = ["== Model spend (estimates) =="]
    lines.append(f"Last 7 days: ${summary['usd']:.2f} across {summary['calls']} calls")
    for feature, entry in sorted(
        summary["by_feature"].items(), key=lambda kv: -kv[1]["usd"]
    ):
        lines.append(
            f"  {feature}: ${entry['usd']:.2f} "
            f"({entry['calls']} calls, {entry['tokens']} tokens)"
        )
    if not summary["by_feature"]:
        lines.append("  no model calls recorded yet")
    if budget:
        flag = " OVER BUDGET" if spent > budget else ""
        lines.append(f"This month: ${spent:.2f} of ${budget:.2f}{flag}")
    return lines


def _health_section() -> list[str]:
    lines = ["== Health =="]

    # WhatsApp: the last self-check result.
    try:
        from whatsapp_health import HealthLog

        latest = HealthLog().latest()
        if not latest:
            lines.append("WhatsApp: no self-check recorded yet")
        elif latest["ok"]:
            lines.append(f"WhatsApp: paired, last check ok ({latest['time']})")
        else:
            broken = [c["name"] for c in latest["checks"] if not c["ok"]]
            lines.append(
                f"WhatsApp: LAST CHECK FAILED ({latest['time']}): " + ", ".join(broken)
            )
    except Exception as exc:
        lines.append(f"WhatsApp: could not read health log ({exc})")

    # Gmail: token files present means signed in.
    try:
        from gmail import discover_labels

        labels = discover_labels()
        lines.append(
            "Gmail: signed in (" + ", ".join(labels) + ")"
            if labels
            else "Gmail: not signed in (run scripts/gmail_signin.py)"
        )
    except Exception as exc:
        lines.append(f"Gmail: could not check ({exc})")

    # Calendar: its own token file.
    cal_tokens = list(YAADHAMMA_DIR.glob("gcal-token-*.json"))
    lines.append(
        "Calendar: signed in"
        if cal_tokens
        else "Calendar: not signed in (run scripts/calendar_signin.py)"
    )

    # Disk space where the databases live.
    try:
        free_gb = shutil.disk_usage(YAADHAMMA_DIR).free / 1e9
        lines.append(f"Disk: {free_gb:.1f} GB free under ~/.yaadhamma")
    except Exception as exc:
        lines.append(f"Disk: could not check ({exc})")
    return lines


def _errors_section() -> list[str]:
    lines = ["== Recent errors (last 24h) =="]
    audit = YAADHAMMA_DIR / "audit.jsonl"
    if not audit.exists():
        return [*lines, "  audit log is empty"]
    cutoff = datetime.now() - timedelta(hours=24)
    failures: list[dict] = []
    try:
        for raw in audit.read_text(encoding="utf-8").splitlines()[-500:]:
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if entry.get("event") != "action_failed":
                continue
            try:
                ts = datetime.fromisoformat(entry["ts"])
            except (KeyError, ValueError):
                continue
            if ts.tzinfo is None:
                continue  # not our format; skip rather than guess
            if ts.replace(tzinfo=None) >= cutoff:
                failures.append(entry)
    except OSError as exc:
        return [*lines, f"  could not read audit log ({exc})"]
    if not failures:
        return [*lines, "  none"]
    for entry in failures[-8:]:
        lines.append(
            f"  {entry.get('ts', '?')} {entry.get('tool', '?')}: "
            f"{str(entry.get('error', ''))[:100]}"
        )
    if len(failures) > 8:
        lines.append(f"  ... and {len(failures) - 8} more")
    return lines


def main() -> int:
    print("\n".join(_cost_section()))
    print()
    print("\n".join(_health_section()))
    print()
    print("\n".join(_errors_section()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
