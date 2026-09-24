"""Tests for the voice-sample candidate table (scripts/make_voice_samples.py)."""

import importlib.util
import os
import re

import pytest

from config import TTS_VOICE

_SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "make_voice_samples.py"
)


@pytest.fixture()
def voice_module():
    spec = importlib.util.spec_from_file_location("make_voice_samples", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_candidates_have_unique_valid_ids(voice_module):
    ids = [c.voice_id for c in voice_module.CANDIDATE_VOICES]
    assert len(ids) == len(set(ids)), "duplicate voice ids"
    for vid in ids:
        assert re.fullmatch(r"[a-f0-9]{32}", vid), f"bad voice id: {vid}"


def test_candidates_have_unique_names_and_blurbs(voice_module):
    names = [c.name for c in voice_module.CANDIDATE_VOICES]
    assert len(names) == len(set(names)), "duplicate voice names"
    for c in voice_module.CANDIDATE_VOICES:
        assert c.name and c.blurb, f"empty name/blurb for {c.voice_id}"


def test_current_default_voice_is_included_for_comparison(voice_module):
    ids = [c.voice_id for c in voice_module.CANDIDATE_VOICES]
    assert TTS_VOICE in ids, "agent's current TTS voice missing from samples"


def test_sample_text_is_speakable(voice_module):
    assert voice_module.SAMPLE_TEXT.strip()
