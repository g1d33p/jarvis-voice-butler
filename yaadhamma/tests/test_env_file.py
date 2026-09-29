"""Regression tests for the .env.local corruption of 2026-09-29.

A setting appended without a preceding newline glued two lines into one;
dotenv silently kept only the first KEY=, and two phone numbers fused into
one 23-digit value that surfaced as a confusing "no chat found" error.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import selftest

import config


def test_glued_lines_detected():
    text = "YAADHAMMA_SELF_CHAT_NUMBER=19408438446YAADHAMMA_X=919640520634\n"
    problems = selftest.env_file_problems(text)
    assert any("glued together" in p for p in problems), problems


def test_missing_trailing_newline_detected():
    problems = selftest.env_file_problems("YAADHAMMA_WAKE=off")
    assert any("newline" in p for p in problems), problems


def test_implausible_digit_run_detected():
    problems = selftest.env_file_problems(
        "YAADHAMMA_SELF_CHAT_NUMBER=19408438446919640520634\n"
    )
    assert any("digits" in p for p in problems), problems


def test_comma_separated_numbers_ok():
    problems = selftest.env_file_problems(
        "YAADHAMMA_SELF_CHATS=19408438446,919640520634\n"
    )
    assert problems == [], problems


def test_duplicate_key_detected():
    problems = selftest.env_file_problems("YAADHAMMA_WAKE=on\nYAADHAMMA_WAKE=off\n")
    assert any("already set" in p for p in problems), problems


def test_sane_file_passes():
    problems = selftest.env_file_problems(
        "# comment\nYAADHAMMA_WAKE=off\nYAADHAMMA_MONTHLY_BUDGET_USD=35\n"
    )
    assert problems == [], problems


def test_self_chat_number_rejects_implausible(monkeypatch):
    monkeypatch.setenv("YAADHAMMA_SELF_CHAT_NUMBER", "19408438446919640520634")
    with pytest.raises(ValueError, match="YAADHAMMA_SELF_CHAT_NUMBER"):
        config._self_chat_number()


def test_self_chat_number_rejection_names_file(monkeypatch):
    monkeypatch.setenv("YAADHAMMA_SELF_CHAT_NUMBER", "19408438446919640520634")
    with pytest.raises(ValueError, match=r"\.env\.local"):
        config._self_chat_number()


def test_self_chat_number_boundary(monkeypatch):
    monkeypatch.setenv("YAADHAMMA_SELF_CHAT_NUMBER", "1" * 15)
    assert config._self_chat_number() == "1" * 15  # 15 digits: valid E.164
    monkeypatch.setenv("YAADHAMMA_SELF_CHAT_NUMBER", "1" * 16)
    with pytest.raises(ValueError, match="at most 15 digits"):
        config._self_chat_number()


def test_self_chat_number_accepts_normal_and_empty(monkeypatch):
    monkeypatch.setenv("YAADHAMMA_SELF_CHAT_NUMBER", "+1 940-843-8446")
    assert config._self_chat_number() == "19408438446"
    monkeypatch.delenv("YAADHAMMA_SELF_CHAT_NUMBER", raising=False)
    assert config._self_chat_number() == ""
