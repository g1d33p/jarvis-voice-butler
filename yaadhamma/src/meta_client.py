"""Yaadhamma's background brain: Gemini, via Google's OpenAI-compatible API.

History: this module used to hold the Meta Model API client too (Muse Spark
brain + Voice Transcribe STT + the pipeline voice path). The owner decided on
2026-09-28 to go all-in on Gemini, so every Meta path was deleted in v1
Stage 1. What remains is the orchestrator's background brain
(GeminiBrainClient), which multi-step tasks use for planning and tool calls.

The voice itself is Gemini Live speech-to-speech (see agent.py); this client
is only for the cheaper background text models.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from openai import AsyncOpenAI

logger = logging.getLogger("yaadhamma.brain")


class BrainConfigError(RuntimeError):
    """Raised when the brain client cannot be built (usually a missing key)."""


@dataclass
class ModelTurn:
    """One model reply, reduced to what the orchestrator needs."""

    calls: list[tuple[str, dict]]
    text: str
    # Legacy payload kept for the old Gemini loop; the Meta loop ignored it.
    content: object | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    # The provider's own tool-call objects, sent back unchanged on the next
    # request. Gemini 3 attaches a "thought signature" to each call and
    # rejects follow-up requests that drop it.
    raw_tool_calls: list[dict] | None = None


# Google's OpenAI-compatible endpoint for the Gemini API.
GEMINI_OPENAI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


class GeminiBrainClient:
    """The orchestrator's brain on Gemini, via Google's OpenAI-compatible API.

    The HTTP client is built lazily so importing this module and running the
    test suite never needs an API key. Uses GOOGLE_API_KEY (the paid key).
    """

    def __init__(self, client: AsyncOpenAI | None = None) -> None:
        self._client = client

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            import os

            key = os.environ.get("GOOGLE_API_KEY")
            if not key:
                raise BrainConfigError(
                    "GOOGLE_API_KEY is not set in .env.local; the background "
                    "brain (Gemini) cannot start."
                )
            self._client = AsyncOpenAI(base_url=GEMINI_OPENAI_URL, api_key=key)
        return self._client

    async def generate(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        reasoning_effort: str | None = None,
        feature: str = "other",
    ) -> ModelTurn:
        client = self._get_client()
        kwargs: dict = {"model": model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        if reasoning_effort:
            kwargs["extra_body"] = {"reasoning_effort": reasoning_effort}
        response = await client.chat.completions.create(**kwargs)
        message = response.choices[0].message
        calls = []
        raw_tool_calls = [
            tool_call.model_dump(exclude_none=True)
            for tool_call in message.tool_calls or []
            if hasattr(tool_call, "model_dump")
        ]
        for tool_call in message.tool_calls or []:
            try:
                args = json.loads(tool_call.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError, AttributeError):
                args = {}
            calls.append((tool_call.function.name, args))
        usage = response.usage
        turn = ModelTurn(
            calls=calls,
            text=message.content or "",
            tokens_in=usage.prompt_tokens if usage else 0,
            tokens_out=usage.completion_tokens if usage else 0,
            raw_tool_calls=raw_tool_calls or None,
        )
        try:
            from costs import CostStore

            CostStore().record(feature, model, turn.tokens_in, turn.tokens_out)
        except Exception:
            pass  # cost tracking must never break a model call
        return turn


def brain_client_from_config() -> GeminiBrainClient:
    """The orchestrator's brain client. Always Gemini since v1."""
    return GeminiBrainClient()
