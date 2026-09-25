"""Measure how fast Yaadhamma replies, and roughly what the voice costs.

Two numbers matter to Jeevan:

- Reply latency: from the moment he stops talking to the moment she starts
  speaking. Target: under 1 second.
- Voice cost: tokens used by the Gemini Live voice model, priced at the
  published per-token rates (an estimate; the Google billing page is the
  truth).

Every reply prints one line in the console, for example:
    reply latency 0.84s (session median 0.91s over 6 replies)
When the session ends, one summary row is added to
~/.yaadhamma/voice_metrics.csv.
"""

from __future__ import annotations

import csv
import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("yaadhamma.latency")

DEFAULT_CSV = Path.home() / ".yaadhamma" / "voice_metrics.csv"

# Gemini Live price per 1M tokens (Gemini API pricing page, Sep 2026):
# audio in $3, audio out $12; text in $0.75, text out $4.50.
PRICE_PER_MILLION = {
    "audio_in": 3.00,
    "audio_out": 12.00,
    "text_in": 0.75,
    "text_out": 4.50,
}


@dataclass
class VoiceMetrics:
    mode: str = "realtime"
    csv_path: Path = DEFAULT_CSV
    started: datetime = field(default_factory=datetime.now)
    latencies: list[float] = field(default_factory=list)
    tokens: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(PRICE_PER_MILLION, 0)
    )
    _user_stopped_at: float | None = None

    # ------------------------------------------------------------ events

    def on_user_state(self, event) -> None:
        """He stopped talking: start the clock."""
        if event.old_state == "speaking" and event.new_state != "speaking":
            self._user_stopped_at = event.created_at

    def on_agent_state(self, event) -> None:
        """She started speaking: stop the clock."""
        if event.new_state != "speaking" or self._user_stopped_at is None:
            return
        latency = max(0.0, event.created_at - self._user_stopped_at)
        self._user_stopped_at = None
        self.latencies.append(latency)
        logger.info(
            "reply latency %.2fs (session median %.2fs over %d replies)",
            latency,
            statistics.median(self.latencies),
            len(self.latencies),
        )

    def on_metrics(self, event) -> None:
        """Add up realtime-model token use (other metric types are ignored)."""
        m = getattr(event, "metrics", None)
        if getattr(m, "type", "") != "realtime_model_metrics":
            return
        inp, out = m.input_token_details, m.output_token_details
        # Cached input is billed at a discount; counting it at full price keeps
        # the estimate on the safe (high) side.
        self.tokens["audio_in"] += inp.audio_tokens
        self.tokens["text_in"] += inp.text_tokens
        self.tokens["audio_out"] += out.audio_tokens
        self.tokens["text_out"] += out.text_tokens

    # ------------------------------------------------------------ results

    def estimated_cost(self) -> float:
        return sum(
            self.tokens[key] / 1_000_000 * price
            for key, price in PRICE_PER_MILLION.items()
        )

    def summary(self) -> dict[str, object]:
        ordered = sorted(self.latencies)

        def pct(p: float) -> float:
            if not ordered:
                return 0.0
            return ordered[min(len(ordered) - 1, round(p * (len(ordered) - 1)))]

        return {
            "session_start": self.started.isoformat(timespec="seconds"),
            "minutes": round((datetime.now() - self.started).total_seconds() / 60, 1),
            "mode": self.mode,
            "replies": len(ordered),
            "median_s": round(statistics.median(ordered), 2) if ordered else 0.0,
            "p90_s": round(pct(0.9), 2),
            "slowest_s": round(ordered[-1], 2) if ordered else 0.0,
            **self.tokens,
            "est_cost_usd": round(self.estimated_cost(), 4),
        }

    async def write_summary(self, *_args) -> None:
        """Append this session's summary row to the CSV (called at shutdown)."""
        row = self.summary()
        logger.info("voice session summary: %s", row)
        try:
            self.csv_path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not self.csv_path.exists()
            with self.csv_path.open("a", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(row))
                if new_file:
                    writer.writeheader()
                writer.writerow(row)
        except OSError as exc:  # metrics must never break shutdown
            logger.warning("could not write voice metrics: %s", exc)
