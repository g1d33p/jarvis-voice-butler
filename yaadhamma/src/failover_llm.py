"""Runtime LLM failover for the Yaadhamma voice pipeline.

Background: on 2026-09-24 the Meta API (Muse Spark) timed out ~6 times over
90 seconds on Jeevan's Mac. The agent retried the same dead endpoint and went
silent, losing a fetched email summary. The pre-existing "Gemini fallback" in
agent.voice_components() is startup-only (no Meta key -> boot the Gemini Live
realtime path) and does nothing for a mid-conversation outage.

This module wraps the primary pipeline LLM (Muse Spark via Meta's
OpenAI-compatible endpoint) in LiveKit's FallbackAdapter (verified against
livekit-agents 1.6.10, which this repo pins) with Yaadhamma-specific
semantics layered on top:

- Fail over only on retryable/transport-level failures: APITimeoutError,
  APIConnectionError, asyncio.TimeoutError, or any APIError with
  retryable=True. Non-retryable errors (4xx auth/config, e.g. a bad Meta API
  key) are re-raised immediately: no failover, no circuit change. A broken
  key must stay loud, never be silently masked by the backup.
- Circuit breaker: after YAADHAMMA_FAILOVER_THRESHOLD consecutive primary
  failures (default 2) the primary is marked unavailable and turns route
  straight to Gemini. A background recovery probe (inherited from
  FallbackAdapter) re-tests the primary after each turn while it is down;
  when it succeeds the circuit closes and traffic fails back silently.
  Each backend attempt gets YAADHAMMA_FAILOVER_TIMEOUT seconds (default 5),
  so a hung primary fails over in ~5s instead of ~90s.
- One spoken notice per failover episode: the adapter emits
  FAILOVER_NOTICE_EVENT ("primary_unavailable", or "forced_test" for the
  test switch). agent.py subscribes and speaks it via session.say; STT and
  TTS are untouched so the voice stays Sarah throughout.
- Backend differences are isolated here: the primary is constructed with
  _strict_tool_schema=False (Meta rejects strict schemas); the backup is a
  plain livekit.plugins.google.LLM text model. The voice pipeline passes the
  same ChatContext/tools to whichever backend serves the turn.

Configuration (all optional, in .env.local):
  GOOGLE_API_KEY              failover is active only when this is set;
                              without it the primary is used unwrapped,
                              exactly as before.
  YAADHAMMA_FAILOVER_THRESHOLD  consecutive primary failures before the
                              circuit opens (default 2).
  YAADHAMMA_FAILOVER_TIMEOUT    per-attempt seconds before trying the backup
                              (default 5.0).
  YAADHAMMA_FAILOVER_MODEL      Gemini text model for the backup
                              (default "gemini-2.5-flash").
  YAADHAMMA_FORCE_FAILOVER=1    test switch: routes the next turn to the
                              backup (one-shot) and emits the notice, so
                              Jeevan can verify failover live without a real
                              outage.

Cost: Gemini bills only for turns actually served by the backup (plus a
small background probe per turn while the primary is down). Normal turns
cost exactly what they cost today.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from livekit.agents._exceptions import APIConnectionError, APIError
from livekit.agents.llm import (
    LLM,
    ChatContext,
    FallbackAdapter,
    LLMStream,
)
from livekit.agents.llm.chat_context import MetricsMetadata
from livekit.agents.llm.fallback_adapter import (
    AvailabilityChangedEvent,
    FallbackLLMStream,
)
from livekit.agents.llm.tool_context import Tool, ToolChoice
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    NotGivenOr,
)
from livekit.plugins import google

logger = logging.getLogger("yaadhamma.failover")

FAILOVER_NOTICE_EVENT = "yaadhamma_failover_notice"
"""Event name emitted once per failover episode for the spoken notice."""

DEFAULT_THRESHOLD = 2
DEFAULT_ATTEMPT_TIMEOUT = 5.0
DEFAULT_BACKUP_MODEL = "gemini-2.5-flash"


@dataclass
class FailoverNotice:
    reason: str
    """Why the notice fired: "primary_unavailable" or "forced_test"."""
    detail: str


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class _FailoverLLMStream(FallbackLLMStream):
    """FallbackLLMStream plus Yaadhamma's failover rules.

    Two deliberate deviations from the stock adapter (both covered by tests):
    - Non-retryable APIErrors (4xx auth/config) are re-raised immediately:
      no failover, no circuit change, no recovery probe.
    - The circuit opens only after `failover_threshold` consecutive primary
      failures (the stock adapter opens on the first), and background
      recovery probes run only for backends that are actually down (the
      stock adapter probes the healthy backup too, wasting a paid call).
    """

    def __init__(self, *args: Any, force_backup: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._force_backup = force_backup

    async def _run(self) -> None:
        adapter: FailoverLLM = self._fallback_adapter  # type: ignore[assignment]
        instances = adapter._llm_instances
        statuses = adapter._status

        all_failed = all(not s.available for s in statuses)
        if all_failed:
            logger.error("all LLMs are unavailable, retrying..")

        for i, llm in enumerate(instances):
            llm_status = statuses[i]
            if self._force_backup and llm is adapter.primary:
                # Test switch: skip the primary for this one turn.
                continue
            if llm_status.available or all_failed:
                text_sent: str = ""
                tool_calls_sent: list[str] = []
                try:
                    async for result in self._try_generate(
                        llm=llm, check_recovery=False
                    ):
                        if result.delta:
                            if result.delta.content:
                                text_sent += result.delta.content
                            for tool_call in result.delta.tool_calls:
                                tool_calls_sent.append(tool_call.name)
                        self._event_ch.send_nowait(result)

                    if llm is adapter.primary:
                        adapter._consec_primary_failures = 0
                    return
                except Exception as exc:  # already logged inside _try_generate
                    if isinstance(exc, APIError) and not exc.retryable:
                        # Auth/config errors stay loud.
                        raise
                    if llm is adapter.primary:
                        adapter._consec_primary_failures += 1
                        if (
                            adapter._consec_primary_failures
                            >= adapter.failover_threshold
                            and llm_status.available
                        ):
                            llm_status.available = False
                            adapter.emit(
                                "llm_availability_changed",
                                AvailabilityChangedEvent(llm=llm, available=False),
                            )
                            adapter.emit(
                                FAILOVER_NOTICE_EVENT,
                                FailoverNotice(
                                    reason="primary_unavailable",
                                    detail=(
                                        f"{adapter.failover_threshold} "
                                        "consecutive primary failures"
                                    ),
                                ),
                            )
                    elif llm_status.available:
                        llm_status.available = False
                        adapter.emit(
                            "llm_availability_changed",
                            AvailabilityChangedEvent(llm=llm, available=False),
                        )

                    if text_sent or tool_calls_sent:
                        extra = {
                            "text_sent": text_sent,
                            "tool_calls_sent": tool_calls_sent,
                        }
                        if not adapter._retry_on_chunk_sent:
                            logger.error(
                                f"{llm.label} failed after sending chunk, skip retrying. "
                                "Set `retry_on_chunk_sent` to `True` to enable retrying after chunks are sent.",
                                extra=extra,
                            )
                            raise
                        logger.warning(
                            f"{llm.label} failed after sending chunk, retrying..",
                            extra=extra,
                        )
            if not llm_status.available:
                self._try_recovery(llm)

        raise APIConnectionError(
            f"all LLMs failed ({[llm.label for llm in instances]})"
        )

    async def _metrics_monitor_task(self, event_aiter: Any) -> None:
        return


class FailoverLLM(FallbackAdapter):
    """Muse Spark primary with Gemini text-model backup for pipeline mode.

    Pipeline-only: STT/TTS are untouched, so failover never changes the
    voice. Implements the LiveKit LLM interface, so it drops into
    Agent(llm=...) wherever the plain primary was used.

    Emits the stock "llm_availability_changed" events plus
    FAILOVER_NOTICE_EVENT (see module docstring).
    """

    def __init__(
        self,
        primary: LLM,
        backup: LLM | None = None,
        *,
        failover_threshold: int = DEFAULT_THRESHOLD,
        attempt_timeout: float = DEFAULT_ATTEMPT_TIMEOUT,
        backup_model: str = DEFAULT_BACKUP_MODEL,
    ) -> None:
        self.primary = primary
        self.failover_threshold = failover_threshold
        self._consec_primary_failures = 0
        self._force_consumed = False
        if backup is None:
            backup = google.LLM(model=backup_model)
        self.backup = backup
        super().__init__(
            [primary, backup],
            attempt_timeout=attempt_timeout,
            max_retry_per_llm=0,
        )

    @property
    def primary_available(self) -> bool:
        """Whether the circuit is currently closed (primary serving)."""
        return self._status[0].available

    @property
    def metrics_metadata(self) -> MetricsMetadata:
        return self._active_instance.metrics_metadata

    def chat(
        self,
        *,
        chat_ctx: ChatContext,
        tools: list[Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
    ) -> LLMStream:
        force_backup = False
        if (
            not self._force_consumed
            and os.environ.get("YAADHAMMA_FORCE_FAILOVER") == "1"
        ):
            # One-shot test switch: this turn goes to the backup.
            self._force_consumed = True
            force_backup = True
            logger.warning(
                "YAADHAMMA_FORCE_FAILOVER=1: routing this turn to the backup LLM"
            )
            self.emit(
                FAILOVER_NOTICE_EVENT,
                FailoverNotice(
                    reason="forced_test",
                    detail="YAADHAMMA_FORCE_FAILOVER=1",
                ),
            )
        return _FailoverLLMStream(
            llm=self,
            conn_options=conn_options,
            chat_ctx=chat_ctx,
            tools=tools or [],
            parallel_tool_calls=parallel_tool_calls,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
            force_backup=force_backup,
        )


def maybe_wrap_with_failover(primary: LLM, *, backup: LLM | None = None) -> LLM:
    """Wrap the pipeline LLM with runtime failover when configured.

    Active only when GOOGLE_API_KEY is set (the backup needs it). Otherwise
    returns the primary unchanged — today's behavior exactly.
    """
    if not os.environ.get("GOOGLE_API_KEY"):
        logger.info("GOOGLE_API_KEY not set: LLM failover disabled, using primary only")
        return primary
    llm = FailoverLLM(
        primary,
        backup=backup,
        failover_threshold=_env_int("YAADHAMMA_FAILOVER_THRESHOLD", DEFAULT_THRESHOLD),
        attempt_timeout=_env_float(
            "YAADHAMMA_FAILOVER_TIMEOUT", DEFAULT_ATTEMPT_TIMEOUT
        ),
        backup_model=os.environ.get("YAADHAMMA_FAILOVER_MODEL", DEFAULT_BACKUP_MODEL),
    )
    logger.info(
        "LLM failover active: primary=%s backup=%s threshold=%d timeout=%.1fs",
        primary.label,
        llm.backup.label,
        llm.failover_threshold,
        llm._attempt_timeout,
    )
    return llm
