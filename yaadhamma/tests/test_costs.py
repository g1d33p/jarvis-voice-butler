"""Stage 3: every model call is recorded with its estimated cost."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import config
import costs
from costs import CostStore, estimate_cost
from meta_client import MetaBrainClient


def test_estimate_cost_known_model() -> None:
    # gemini-3.5-flash-lite: $0.00010/1K in, $0.00040/1K out.
    assert estimate_cost("gemini-3.5-flash-lite", 1000, 1000) == pytest.approx(0.00050)


def test_estimate_cost_unknown_model_uses_default() -> None:
    assert estimate_cost("some-future-model-9", 1000, 1000) == pytest.approx(0.00400)


def test_estimate_cost_zero_tokens_is_zero() -> None:
    assert estimate_cost("gemini-3.5-flash-lite", 0, 0) == 0.0


def test_record_returns_cost_and_stores_row(tmp_path) -> None:
    store = CostStore(path=tmp_path / "c.db")
    cost = store.record("digest", "gemini-3.5-flash-lite", 2000, 500)
    assert cost == pytest.approx(0.00040)
    rows = sqlite3.connect(store.path).execute("SELECT * FROM model_calls").fetchall()
    assert len(rows) == 1
    assert rows[0][2] == "digest"
    assert rows[0][3] == "gemini-3.5-flash-lite"


def test_record_never_raises(tmp_path) -> None:
    store = CostStore(path=tmp_path / "c.db")
    # Even nonsense input returns a number instead of blowing up.
    assert isinstance(store.record("", "", -1, -1), float)


def test_summary_7d_breaks_down_by_feature(tmp_path) -> None:
    store = CostStore(path=tmp_path / "c.db")
    store.record("digest", "gemini-3.5-flash-lite", 1000, 0)
    store.record("digest", "gemini-3.5-flash-lite", 1000, 0)
    store.record("learning", "gemini-3.8-flash", 1000, 0)
    summary = store.summary_7d()
    assert summary["calls"] == 3
    assert summary["by_feature"]["digest"]["calls"] == 2
    assert summary["by_feature"]["learning"]["calls"] == 1
    assert summary["usd"] == pytest.approx(
        summary["by_feature"]["digest"]["usd"]
        + summary["by_feature"]["learning"]["usd"]
    )


def test_summary_ignores_calls_older_than_7_days(tmp_path) -> None:
    store = CostStore(path=tmp_path / "c.db")
    old = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
    with sqlite3.connect(store.path) as db:
        db.execute(
            "INSERT INTO model_calls "
            "(ts, feature, model, tokens_in, tokens_out, cost_usd) "
            "VALUES (?, 'digest', 'm', 1000000, 0, 999.0)",
            (old,),
        )
    store.record("digest", "gemini-3.5-flash-lite", 1000, 0)
    summary = store.summary_7d()
    assert summary["calls"] == 1
    assert summary["usd"] < 1.0


def test_month_to_date_counts_this_month_only(tmp_path) -> None:
    store = CostStore(path=tmp_path / "c.db")
    store.record("digest", "gemini-3.5-flash-lite", 1000, 0)
    assert store.month_to_date_usd() == pytest.approx(0.00010)
    last_month = (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")
    with sqlite3.connect(store.path) as db:
        db.execute(
            "INSERT INTO model_calls "
            "(ts, feature, model, tokens_in, tokens_out, cost_usd) "
            "VALUES (?, 'digest', 'm', 1, 0, 50.0)",
            (last_month,),
        )
    assert store.month_to_date_usd() == pytest.approx(0.00010)


def test_over_budget_warns_without_blocking(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "MONTHLY_BUDGET_USD", 0.00001)
    store = CostStore(path=tmp_path / "c.db")
    store.record("digest", "gemini-3.5-flash-lite", 1000, 0)
    exceeded, spent, budget = store.over_budget()
    assert exceeded is True
    assert spent == pytest.approx(0.00010)
    assert budget == pytest.approx(0.00001)


def test_under_budget_is_quiet(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "MONTHLY_BUDGET_USD", 1000.0)
    store = CostStore(path=tmp_path / "c.db")
    exceeded, _, _ = store.over_budget()
    assert exceeded is False


def _fake_reply(tokens=(1000, 500)):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))
        ],
        usage=SimpleNamespace(prompt_tokens=tokens[0], completion_tokens=tokens[1]),
    )


class _FakeCompletions:
    async def create(self, **kwargs):
        return _fake_reply()


class _FakeOpenAI:
    chat = SimpleNamespace(completions=_FakeCompletions())


def test_generate_records_the_call_with_its_feature(tmp_path) -> None:
    brain = MetaBrainClient(client=_FakeOpenAI())

    async def run():
        return await brain.generate(
            "gemini-3.5-flash-lite", [{"role": "user"}], [], feature="digest"
        )

    asyncio.run(run())
    summary = CostStore().summary_7d()
    assert summary["calls"] == 1
    assert summary["by_feature"]["digest"]["calls"] == 1
    assert summary["usd"] == pytest.approx(0.00010 + 0.00020)


def test_generate_still_works_when_cost_recording_breaks(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise OSError("disk is gone")

    monkeypatch.setattr(costs, "CostStore", boom)
    brain = MetaBrainClient(client=_FakeOpenAI())

    async def run():
        return await brain.generate("m", [{"role": "user"}], [])

    turn = asyncio.run(run())
    assert turn.text == "ok"


def test_budget_line_warns_only_when_over(monkeypatch) -> None:
    from digest import _budget_line

    monkeypatch.setattr(config, "MONTHLY_BUDGET_USD", 1000.0)
    assert _budget_line() == ""

    monkeypatch.setattr(config, "MONTHLY_BUDGET_USD", 0.00001)
    CostStore().record("digest", "gemini-3.5-flash-lite", 1000, 0)
    line = _budget_line()
    assert "over the" in line and "budget" in line
    assert "Nothing is blocked" in line
