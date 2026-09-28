"""Tests that the voice stack is Gemini Live realtime only (no keys)."""

import os

import pytest

import agent


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


def test_voice_is_always_realtime(_fake_realtime_model):
    """voice_components() builds the Gemini Live realtime model."""
    llm = agent.voice_components()
    assert isinstance(llm, agent.google.beta.realtime.RealtimeModel)


def test_realtime_model_uses_jeevans_voice_and_cost_controls(_fake_realtime_model):
    """The realtime model carries the chosen voice and cost controls."""
    import config

    agent.voice_components()
    made = _fake_realtime_model
    assert made["voice"] == config.REALTIME_VOICE
    assert made["model"] == config.REALTIME_MODEL
    assert "context_window_compression" in made
