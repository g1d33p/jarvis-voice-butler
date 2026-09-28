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

# Estimated price per 1K tokens as (input_usd, output_usd).
#
# Published Gemini API paid-tier rates, checked 2026-09-28 against the Gemini
# API pricing page and two independent trackers; estimates only, never quotes
# — the Google billing page is the truth. Gemini 3.8 Flash is on introductory
# pricing through 2026-12-31 and doubles on 2027-01-01, so refresh these
# before that date.
PRICE_PER_1K: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash-lite": (0.00030, 0.00250),  # $0.30 / $2.50 per 1M
    "gemini-3.8-flash": (0.00075, 0.00375),  # $0.75 / $3.75 per 1M (introductory)
    "muse-spark": (0.00100, 0.00300),
}
DEFAULT_PRICE_PER_1K: tuple[float, float] = (0.00100, 0.00300)

# Gemini Live realtime voice (gemini-3.8-live) does not price as one
# input/output pair: audio and text tokens bill at different rates. Per-1K
# rates for the four streams, same published source and date as above.
# Single source of truth — latency.py derives its per-1M table from this so
# the voice meter and the cost dashboard can never disagree.
LIVE_PRICE_PER_1K: dict[str, float] = {
    "audio_in": 0.00300,  # $3.00 per 1M
    "text_in": 0.00075,  # $0.75 per 1M
    "audio_out": 0.01200,  # $12.00 per 1M
    "text_out": 0.00450,  # $4.50 per 1M
}


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Estimated USD for one call. Never raises."""
    name = (model or "").lower()
    price = DEFAULT_PRICE_PER_1K
    for key, value in PRICE_PER_1K.items():
        if key in name:
            price = value
            break
    return (tokens_in / 1000) * price[0] + (tokens_out / 1000) * price[1]


def estimate_live_cost(
    audio_in: int = 0,
    text_in: int = 0,
    audio_out: int = 0,
    text_out: int = 0,
) -> float:
    """Estimated USD for Gemini Live voice tokens, using the four published
    per-stream rates. Never raises."""
    try:
        return (
            (audio_in / 1000) * LIVE_PRICE_PER_1K["audio_in"]
            + (text_in / 1000) * LIVE_PRICE_PER_1K["text_in"]
            + (audio_out / 1000) * LIVE_PRICE_PER_1K["audio_out"]
            + (text_out / 1000) * LIVE_PRICE_PER_1K["text_out"]
        )
    except Exception:
        logger.warning("could not estimate live voice cost", exc_info=True)
        return 0.0


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
        self,
        feature: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float | None = None,
    ) -> float:
        """Store one call; return its estimated cost. Never raises.

        cost_usd overrides the estimate: pass it when the caller priced the
        call more precisely than a single input/output pair allows (e.g.
        Gemini Live voice, whose audio and text tokens bill separately).
        """
        cost: float
        if cost_usd is None:
            cost = estimate_cost(model, tokens_in, tokens_out)
        else:
            try:
                cost = float(cost_usd)
            except (TypeError, ValueError):
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
