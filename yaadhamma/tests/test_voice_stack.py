"""Tests for voice_components(): pipeline vs realtime selection (no keys)."""

import logging
import os

import pytest

import agent
from meta_client import MetaRealtimeSTT


@pytest.fixture(autouse=True)
def _no_proxies(monkeypatch):
    """This sandbox sets malformed proxy env vars that break httpx URL
    parsing; real machines don't. Strip them so client construction works."""
    for var in list(os.environ):
        if "proxy" in var.lower():
            monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def _fake_realtime_model(monkeypatch):
    made = {}

    class FakeRealtimeModel:
        def __init__(self, **kwargs):
            made.update(kwargs)

    monkeypatch.setattr(agent.google.beta.realtime, "RealtimeModel", FakeRealtimeModel)
    return made


def _env(monkeypatch, key=None, mode=None):
    # A real GOOGLE_API_KEY in .env.local switches on the Gemini failover
    # wrapper; these tests check the unwrapped stack, so hide it.
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    if key is None:
        monkeypatch.delenv("YAADHAMMA_MODEL_API_KEY", raising=False)
    else:
        monkeypatch.setenv("YAADHAMMA_MODEL_API_KEY", key)
    if mode is None:
        monkeypatch.delenv("YAADHAMMA_VOICE_MODE", raising=False)
    else:
        monkeypatch.setenv("YAADHAMMA_VOICE_MODE", mode)


def test_pipeline_selected_when_asked_for_with_meta_key(
    monkeypatch, _fake_realtime_model
):
    _env(monkeypatch, key="k", mode="pipeline")
    monkeypatch.setenv("LIVEKIT_API_KEY", "dummy-livekit-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "dummy-livekit-secret")
    llm, stt, tts, mode = agent.voice_components()
    assert mode == "pipeline"
    assert isinstance(stt, MetaRealtimeSTT)
    assert tts is not None
    assert llm is not None


def test_pipeline_llm_disables_strict_tool_schema(monkeypatch, _fake_realtime_model):
    # Meta's OpenAI-compatible API rejects strict tool schemas (and non-"auto"
    # tool_choice) with a 400, so the pipeline LLM must not send them.
    _env(monkeypatch, key="k", mode="pipeline")
    monkeypatch.setenv("LIVEKIT_API_KEY", "dummy-livekit-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "dummy-livekit-secret")
    llm, _stt, _tts, mode = agent.voice_components()
    assert mode == "pipeline"
    assert llm._strict_tool_schema is False  # private escape hatch, by design


def test_realtime_forced_by_env(monkeypatch, _fake_realtime_model):
    _env(monkeypatch, key="k", mode="realtime")
    llm, stt, tts, mode = agent.voice_components()
    assert mode == "realtime"
    assert stt is None and tts is None
    assert isinstance(llm, agent.google.beta.realtime.RealtimeModel)


def test_pipeline_without_key_falls_back_to_realtime_with_warning(
    monkeypatch, _fake_realtime_model, caplog
):
    _env(monkeypatch, key=None, mode="pipeline")
    with caplog.at_level(logging.WARNING, logger="yaadhamma"):
        _llm, stt, tts, mode = agent.voice_components()
    assert mode == "realtime"
    assert stt is None and tts is None
    assert any("falling back" in r.message for r in caplog.records)


def test_gemini_live_is_the_default_even_with_a_meta_key(
    monkeypatch, _fake_realtime_model
):
    """2026-09-25: Jeevan chose all-Google; the Meta pipeline is rollback only."""
    _env(monkeypatch, key="k")
    _llm, stt, tts, mode = agent.voice_components()
    assert mode == "realtime"
    assert stt is None and tts is None


def test_realtime_uses_gemini_38_live_and_gacrux(monkeypatch, _fake_realtime_model):
    _env(monkeypatch, key=None)
    monkeypatch.delenv("YAADHAMMA_REALTIME_LANGUAGE", raising=False)
    agent.voice_components()
    made = _fake_realtime_model
    assert made["model"] == "gemini-3.8-live"
    assert made["voice"] == "Gacrux"
    assert made["tool_response_scheduling"] == (
        agent.genai_types.FunctionResponseScheduling.WHEN_IDLE
    )
    # No fixed language, so English/Telugu mixing is auto-detected.
    assert "language" not in made
