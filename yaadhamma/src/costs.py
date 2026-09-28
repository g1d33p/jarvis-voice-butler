"""Cost tracking for every model call Yaadhamma makes.

Each call records its feature (digest, voice, learning, planner), the model
name, token counts, and an estimated cost in the same local database as
tasks and digests (~/.yaadhamma/yaadhamma.db). Nothing here phones home;
the numbers are estimates for Jeevan's own dashboard, and the provider's
billing page is always the truth.

Recording never raises: a failed write must not break the call it measures.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("yaadhamma.costs")

DEFAULT_DB = Path.home() / ".yaadhamma" / "yaadhamma.db"

# Estimated price per 1K tokens as (input_usd, output_usd). These are
# rough 2026 estimates, not quotes; unknown models fall back to DEFAULT.
PRICE_PER_1K: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash-lite": (0.00010, 0.00040),
    "gemini-3.8-flash": (0.00050, 0.00150),
    "muse-spark": (0.00100, 0.00300),
}
DEFAULT_PRICE_PER_1K: tuple[float, float] = (0.00100, 0.00300)


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Estimated USD for one call. Never raises."""
    name = (model or "").lower()
    price = DEFAULT_PRICE_PER_1K
    for key, value in PRICE_PER_1K.items():
        if key in name:
            price = value
            break
    return (tokens_in / 1000) * price[0] + (tokens_out / 1000) * price[1]


class CostStore:
    """One row per model call: feature, model, tokens, estimated cost."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS model_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    feature TEXT NOT NULL,
                    model TEXT NOT NULL,
                    tokens_in INTEGER NOT NULL,
                    tokens_out INTEGER NOT NULL,
                    cost_usd REAL NOT NULL
                )"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_calls_ts ON model_calls (ts)"
            )

    def record(
        self, feature: str, model: str, tokens_in: int, tokens_out: int
    ) -> float:
        """Store one call; return its estimated cost. Never raises."""
        cost = estimate_cost(model, tokens_in, tokens_out)
        try:
            with sqlite3.connect(self.path) as db:
                db.execute(
                    "INSERT INTO model_calls "
                    "(ts, feature, model, tokens_in, tokens_out, cost_usd) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        datetime.now().isoformat(timespec="seconds"),
                        feature or "other",
                        model or "unknown",
                        int(tokens_in or 0),
                        int(tokens_out or 0),
                        cost,
                    ),
                )
        except Exception:
            logger.warning("could not record model call cost", exc_info=True)
        return cost

    def _rows_since(self, since: datetime) -> list[tuple]:
        try:
            with sqlite3.connect(self.path) as db:
                return db.execute(
                    "SELECT feature, model, tokens_in, tokens_out, cost_usd "
                    "FROM model_calls WHERE ts >= ?",
                    (since.isoformat(timespec="seconds"),),
                ).fetchall()
        except Exception:
            logger.warning("could not read model call costs", exc_info=True)
            return []

    def summary_7d(self) -> dict:
        """Last 7 days: total cost, calls, and a per-feature breakdown."""
        rows = self._rows_since(datetime.now() - timedelta(days=7))
        by_feature: dict[str, dict] = {}
        total = 0.0
        for feature, _model, tokens_in, tokens_out, cost in rows:
            entry = by_feature.setdefault(
                feature, {"calls": 0, "tokens": 0, "usd": 0.0}
            )
            entry["calls"] += 1
            entry["tokens"] += (tokens_in or 0) + (tokens_out or 0)
            entry["usd"] += cost or 0.0
            total += cost or 0.0
        return {"days": 7, "calls": len(rows), "usd": total, "by_feature": by_feature}

    def month_to_date_usd(self, now: datetime | None = None) -> float:
        """Estimated spend since the 1st of this month."""
        now = now or datetime.now()
        rows = self._rows_since(now.replace(day=1, hour=0, minute=0, second=0))
        return sum((cost or 0.0) for _, _, _, _, cost in rows)

    def over_budget(self) -> tuple[bool, float, float]:
        """(exceeded, month_to_date_usd, budget_usd). Never raises."""
        try:
            import config

            budget = float(config.MONTHLY_BUDGET_USD)
        except Exception:
            return False, 0.0, 0.0
        spent = self.month_to_date_usd()
        return spent > budget, spent, budget
