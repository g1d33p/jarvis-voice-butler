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
- Failover is SILENT per Jeevan's explicit instruction: no spoken notice,
  ever. The switch (and the switch back) is recorded in structured logs
  only. Each backup-served turn carries an invisible developer-context
  note in the backup's copy of the chat context -- never added to the
  session history, never spoken -- so the assistant can answer truthfully
  if Jeevan explicitly asks what happened, and never volunteers it
  otherwise. STT and TTS are untouched so the voice stays Sarah
  throughout.
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
  YAADHAMMA_FAILOVER_BACKUP_TIMEOUT
                              the backup's own Google request deadline in
                              seconds (default 30.0, floored at 10.0 because
                              Google rejects shorter deadlines). This is
                              separate from YAADHAMMA_FAILOVER_TIMEOUT: the
                              short timeout kills a hung primary fast, while
                              the backup gets a deadline Google accepts.
  YAADHAMMA_FAILOVER_MODEL      Gemini text model for the backup
                              (default "gemini-3.8-flash"). Google retired
                              gemini-2.5-flash for new API keys on
                              2026-09-24; do not go back to it.
  YAADHAMMA_FORCE_FAILOVER=1    test switch: routes the next turn to the
                              backup (one-shot) so Jeevan can verify
                              failover live without a real outage. Silent,
                              like a real failover.

Cost: Gemini bills only for turns actually served by the backup (plus a
small background probe per turn while the primary is down). Normal turns
cost exactly what they cost today.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from google.genai import types as genai_types
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

DEFAULT_THRESHOLD = 2
DEFAULT_ATTEMPT_TIMEOUT = 5.0
DEFAULT_BACKUP_MODEL = "gemini-3.8-flash"
# 2026-09-24: Google retired gemini-2.5-flash for new API keys; their 404
# names gemini-3.8-flash as the replacement. Kept here (not hard-coded in
# maybe_wrap_with_failover) so tests can assert the exact default.
DEFAULT_BACKUP_TIMEOUT_S = 30.0
MIN_BACKUP_TIMEOUT_S = 10.0
# 2026-09-24 (second live test): every backup turn died with 400
# "Manually set deadline 5s is too short. Minimum allowed deadline is 10s."
# The 5s attempt timeout was leaking into the backup request as the Google
# request deadline. The backup therefore gets its OWN request deadline
# (>= 10s) via http_options on the google.LLM instance: the LiveKit Google
# plugin only fills the deadline from conn_options when http_options has no
# explicit timeout (verified against livekit-plugins-google 1.6.10). The
# primary keeps the short attempt timeout so failover still kicks in fast.

BACKUP_CONTEXT_NOTE = (
    "Developer note (not for the user): this turn is being answered by "
    "Yaadhamma's backup language model because the primary model (Muse "
    "Spark) was unreachable or timed out. Continue the conversation "
    "normally. NEVER mention the backup, the failover, or model names, "
    "and never volunteer that anything changed, unless Jeevan explicitly "
    "asks what happened or which model answered -- then answer truthfully."
)
"""Invisible context note appended to the backup's copy of the chat context.

It is provider-visible only: never added to the session history, never
spoken, never surfaced. Its only purpose is truthfulness if Jeevan asks
"what just happened?". See _FailoverLLMStream._run.
"""


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


def _backup_http_options() -> genai_types.HttpOptions:
    """Request options for the backup LLM with a Google-legal deadline.

    Google's generate_content API rejects manually set deadlines under 10s,
    so the backup's own request deadline is floored there regardless of the
    (shorter) primary attempt timeout. YAADHAMMA_FAILOVER_BACKUP_TIMEOUT
    (seconds) raises it if Jeevan ever needs more headroom.
    """
    timeout_s = _env_float(
        "YAADHAMMA_FAILOVER_BACKUP_TIMEOUT", DEFAULT_BACKUP_TIMEOUT_S
    )
    timeout_s = max(timeout_s, MIN_BACKUP_TIMEOUT_S)
    return genai_types.HttpOptions(timeout=int(timeout_s * 1000))


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

        # The session's context stays untouched: the backup sees a per-turn
        # copy with the invisible developer note appended (so it can answer
        # truthfully if Jeevan asks what happened); the primary always sees
        # the original. The note never enters session history and is never
        # spoken -- failover is silent by Jeevan's instruction.
        base_ctx = self._chat_ctx
        backup_ctx: ChatContext | None = None

        for i, llm in enumerate(instances):
            llm_status = statuses[i]
            if self._force_backup and llm is adapter.primary:
                # Test switch: skip the primary for this one turn.
                continue
            if llm_status.available or all_failed:
                if llm is adapter.primary:
                    self._chat_ctx = base_ctx
                else:
                    if backup_ctx is None:
                        backup_ctx = base_ctx.copy()
                        backup_ctx.add_message(
                            role="system", content=BACKUP_CONTEXT_NOTE
                        )
                    self._chat_ctx = backup_ctx
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
                            logger.warning(
                                "failover: circuit opened, routing to backup "
                                f"({adapter.failover_threshold} consecutive "
                                "primary failures); switching silently"
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

    Emits the stock "llm_availability_changed" events; all failover
    transitions are also written to the structured log. There is no spoken
    notice: failover is silent by Jeevan's explicit instruction.
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
            # The backup gets its own long request deadline: the short
            # attempt timeout (5s) is what kills the primary quickly, but it
            # must never leak into the backup's Google request (Google
            # rejects deadlines < 10s).
            backup = google.LLM(model=backup_model, http_options=_backup_http_options())
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
            # Silent, like a real failover: the forced turn is logged only.
            self._force_consumed = True
            force_backup = True
            logger.warning(
                "YAADHAMMA_FORCE_FAILOVER=1: routing this turn to the backup LLM"
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
