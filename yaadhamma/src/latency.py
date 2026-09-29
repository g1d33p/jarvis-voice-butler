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
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from costs import LIVE_PRICE_PER_1K

logger = logging.getLogger("yaadhamma.latency")

DEFAULT_CSV = Path.home() / ".yaadhamma" / "voice_metrics.csv"

# Where the voice session leaves a "he just spoke" heartbeat for the
# always-on daemon (v1 Stage 3): the daemon closes the Gemini Live session
# after 90 s without speech, and it cannot hear the mic while the agent
# worker owns it. Monotonic seconds, same boot clock as the daemon.
ACTIVITY_PATH = Path.home() / ".yaadhamma" / "voice-activity.json"


def note_speech_activity(when: float | None = None) -> None:
    """Record that the user just spoke. Never raises."""
    _merge_activity({"monotonic": when or time.monotonic()})


def note_agent_speaking(speaking: bool) -> None:
    """Record whether she is speaking (for the menu-bar icon). Never raises."""
    _merge_activity({"agent_speaking": speaking})


def _merge_activity(update: dict) -> None:
    import json
    import time

    try:
        ACTIVITY_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(ACTIVITY_PATH.read_text())
        except Exception:
            data = {}
        data.update(update)
        data.setdefault("monotonic", time.monotonic())
        ACTIVITY_PATH.write_text(json.dumps(data))
    except Exception:
        pass


def last_speech_activity() -> float:
    """Monotonic time of the last recorded speech, 0 when unknown."""
    import json

    try:
        return float(json.loads(ACTIVITY_PATH.read_text()).get("monotonic", 0))
    except Exception:
        return 0.0


def agent_is_speaking() -> bool:
    """Whether the voice session last reported her speaking."""
    import json

    try:
        return bool(json.loads(ACTIVITY_PATH.read_text()).get("agent_speaking"))
    except Exception:
        return False


# How long a task_running=True flag counts as fresh. The flag is cleared in
# a finally when the task ends; a stale True (worker crashed mid-task) must
# not pin a future session open forever. Well above TASK_TIMEOUT_SECONDS.
TASK_RUNNING_FRESH_S = 300.0


def note_task_running(running: bool) -> None:
    """Record whether the voice worker has a background task in flight, so
    the daemon's idle timeout never closes a session mid-task. Never raises."""
    import time

    _merge_activity(
        {"task_running": bool(running), "task_running_at": time.monotonic()}
    )


def task_is_running() -> bool:
    """Whether the voice worker recently reported a task in flight."""
    import json
    import time

    try:
        data = json.loads(ACTIVITY_PATH.read_text())
    except Exception:
        return False
    if not data.get("task_running"):
        return False
    try:
        at = float(data.get("task_running_at", 0))
    except (TypeError, ValueError):
        return False
    return time.monotonic() - at < TASK_RUNNING_FRESH_S


# Gemini Live price per 1M tokens, derived from the published per-1K rates in
# costs.LIVE_PRICE_PER_1K (checked 2026-09-28). One table, two views: costs.py
# is the source of truth so the voice meter and the cost dashboard agree.
PRICE_PER_MILLION = {key: rate * 1000 for key, rate in LIVE_PRICE_PER_1K.items()}


@dataclass
class VoiceMetrics:
    mode: str = "realtime"
    csv_path: Path = DEFAULT_CSV
    # Model whose tokens are being counted (for the cost record).
    model: str = "gemini-3.8-live"
    # Local cost database for this session's voice row; None means the
    # default ~/.yaadhamma/yaadhamma.db. Tests point it at tmp_path.
    cost_db: Path | None = None
    # The voice detector says "stopped" only after this much silence.
    speech_end_offset_s: float = 0.0
    # False when no local voice detector runs: the Gemini plugin then marks
    # "user stopped" when her reply finishes generating, so timings are wrong.
    timing_reliable: bool = True
    started: datetime = field(default_factory=datetime.now)
    latencies: list[float] = field(default_factory=list)
    tokens: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(PRICE_PER_MILLION, 0)
    )
    # Input tokens Google served from its cache (billed at a discount).
    cached_in: int = 0
    _user_stopped_at: float | None = None

    # ------------------------------------------------------------ events

    def on_user_state(self, event) -> None:
        """He stopped talking: start the clock."""
        if event.new_state == "speaking":
            # Heartbeat for the always-on daemon's 90 s silence timeout.
            note_speech_activity()
        if event.old_state == "speaking" and event.new_state != "speaking":
            self._user_stopped_at = event.created_at - self.speech_end_offset_s

    def on_agent_state(self, event) -> None:
        """She started speaking: stop the clock."""
        # Heartbeat for the menu-bar icon (speaking vs thinking).
        if event.new_state == "speaking":
            note_agent_speaking(True)
        elif event.old_state == "speaking":
            note_agent_speaking(False)
        if event.new_state != "speaking" or self._user_stopped_at is None:
            return
        if not self.timing_reliable:
            self._user_stopped_at = None
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
        self.cached_in += getattr(inp, "cached_tokens", 0) or 0

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
            "cached_in": self.cached_in,
            # Counts cached input at full price, so this is an upper bound.
            "est_cost_usd": round(self.estimated_cost(), 4),
        }

    async def write_summary(self, *_args) -> None:
        """Append this session's summary row to the CSV (called at shutdown),
        and record the session's voice cost in the cost store under the
        "voice" feature. Neither may break shutdown."""
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
        try:
            from costs import CostStore, estimate_live_cost

            store = CostStore(path=self.cost_db) if self.cost_db else CostStore()
            cost = estimate_live_cost(
                audio_in=self.tokens["audio_in"],
                text_in=self.tokens["text_in"],
                audio_out=self.tokens["audio_out"],
                text_out=self.tokens["text_out"],
            )
            store.record(
                "voice",
                self.model,
                self.tokens["audio_in"] + self.tokens["text_in"],
                self.tokens["audio_out"] + self.tokens["text_out"],
                cost_usd=cost,
            )
        except Exception:  # cost recording must never break shutdown either
            logger.warning("could not record voice cost", exc_info=True)
