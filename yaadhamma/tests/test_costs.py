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
from meta_client import GeminiBrainClient


def test_estimate_cost_known_model() -> None:
    # gemini-3.5-flash-lite: $0.00030/1K in, $0.00250/1K out
    # (published $0.30/$2.50 per 1M, checked 2026-09-28).
    assert estimate_cost("gemini-3.5-flash-lite", 1000, 1000) == pytest.approx(0.00280)


def test_estimate_cost_unknown_model_uses_default() -> None:
    assert estimate_cost("some-future-model-9", 1000, 1000) == pytest.approx(0.00400)


def test_estimate_cost_zero_tokens_is_zero() -> None:
    assert estimate_cost("gemini-3.5-flash-lite", 0, 0) == 0.0


def test_estimate_cost_flash_introductory_rate() -> None:
    # gemini-3.8-flash: $0.00075/1K in, $0.00375/1K out (introductory,
    # through 2026-12-31).
    assert estimate_cost("gemini-3.8-flash", 1000, 1000) == pytest.approx(0.00450)


def test_estimate_live_cost_uses_four_stream_rates() -> None:
    from costs import estimate_live_cost

    # 1M audio in + 1M audio out: $3.00 + $12.00.
    assert estimate_live_cost(audio_in=1_000_000, audio_out=1_000_000) == pytest.approx(
        15.0
    )
    # 1M text in + 1M text out: $0.75 + $4.50.
    assert estimate_live_cost(text_in=1_000_000, text_out=1_000_000) == pytest.approx(
        5.25
    )


def test_record_returns_cost_and_stores_row(tmp_path) -> None:
    store = CostStore(path=tmp_path / "c.db")
    cost = store.record("digest", "gemini-3.5-flash-lite", 2000, 500)
    assert cost == pytest.approx(0.00185)
    rows = sqlite3.connect(store.path).execute("SELECT * FROM model_calls").fetchall()
    assert len(rows) == 1
    assert rows[0][2] == "digest"
    assert rows[0][3] == "gemini-3.5-flash-lite"


def test_record_accepts_a_precise_cost_override(tmp_path) -> None:
    # Voice sessions price audio and text separately, then record the exact
    # figure instead of the single-pair estimate.
    store = CostStore(path=tmp_path / "c.db")
    cost = store.record("voice", "gemini-3.8-live", 10_000, 5_000, cost_usd=0.042)
    assert cost == pytest.approx(0.042)
    rows = (
        sqlite3.connect(store.path)
        .execute("SELECT feature, cost_usd FROM model_calls")
        .fetchall()
    )
    assert rows == [("voice", 0.042)]


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
    assert store.month_to_date_usd() == pytest.approx(0.00030)
    last_month = (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")
    with sqlite3.connect(store.path) as db:
        db.execute(
            "INSERT INTO model_calls "
            "(ts, feature, model, tokens_in, tokens_out, cost_usd) "
            "VALUES (?, 'digest', 'm', 1, 0, 50.0)",
            (last_month,),
        )
    assert store.month_to_date_usd() == pytest.approx(0.00030)


def test_over_budget_warns_without_blocking(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "MONTHLY_BUDGET_USD", 0.00001)
    store = CostStore(path=tmp_path / "c.db")
    store.record("digest", "gemini-3.5-flash-lite", 1000, 0)
    exceeded, spent, budget = store.over_budget()
    assert exceeded is True
    assert spent == pytest.approx(0.00030)
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
    brain = GeminiBrainClient(client=_FakeOpenAI())

    async def run():
        return await brain.generate(
            "gemini-3.5-flash-lite", [{"role": "user"}], [], feature="digest"
        )

    asyncio.run(run())
    summary = CostStore().summary_7d()
    assert summary["calls"] == 1
    assert summary["by_feature"]["digest"]["calls"] == 1
    assert summary["usd"] == pytest.approx(0.00030 + 0.00125)


def test_generate_still_works_when_cost_recording_breaks(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise OSError("disk is gone")

    monkeypatch.setattr(costs, "CostStore", boom)
    brain = GeminiBrainClient(client=_FakeOpenAI())

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


def test_today_usd_counts_only_today(tmp_path) -> None:
    from datetime import datetime, timedelta

    store = CostStore(path=tmp_path / "c.db")
    now = datetime.now()
    # A row from yesterday: insert directly with an old timestamp.
    import sqlite3

    store.record("brain", "gemini-3.5-flash-lite", 1000, 1000)
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE model_calls SET ts = ?",
            ((now - timedelta(days=1)).isoformat(),),
        )
    assert store.today_usd(now=now) == pytest.approx(0.0)
    store.record("brain", "gemini-3.5-flash-lite", 1000, 1000)
    assert store.today_usd(now=now) == pytest.approx(0.00280)
