import csv
from types import SimpleNamespace

import pytest

from latency import PRICE_PER_MILLION, VoiceMetrics


def _user(old, new, at):
    return SimpleNamespace(old_state=old, new_state=new, created_at=at)


def _agent(new, at):
    return SimpleNamespace(old_state="thinking", new_state=new, created_at=at)


def _realtime_metrics(audio_in=0, text_in=0, audio_out=0, text_out=0):
    return SimpleNamespace(
        metrics=SimpleNamespace(
            type="realtime_model_metrics",
            input_token_details=SimpleNamespace(
                audio_tokens=audio_in, text_tokens=text_in
            ),
            output_token_details=SimpleNamespace(
                audio_tokens=audio_out, text_tokens=text_out
            ),
        )
    )


def test_reply_latency_is_stop_talking_to_start_speaking(tmp_path) -> None:
    m = VoiceMetrics(csv_path=tmp_path / "m.csv")
    m.on_user_state(_user("speaking", "listening", 100.0))
    m.on_agent_state(_agent("thinking", 100.3))
    m.on_agent_state(_agent("speaking", 100.8))

    assert m.latencies == pytest.approx([0.8])


def test_speech_without_a_user_turn_is_not_counted(tmp_path) -> None:
    m = VoiceMetrics(csv_path=tmp_path / "m.csv")
    m.on_agent_state(_agent("speaking", 5.0))  # e.g. her greeting
    assert m.latencies == []


def test_one_user_turn_counts_once(tmp_path) -> None:
    """'On it' and the later task result are one reply, timed at 'On it'."""
    m = VoiceMetrics(csv_path=tmp_path / "m.csv")
    m.on_user_state(_user("speaking", "listening", 10.0))
    m.on_agent_state(_agent("speaking", 10.6))
    m.on_agent_state(_agent("speaking", 25.0))
    assert m.latencies == pytest.approx([0.6])


def test_cost_estimate_uses_published_rates(tmp_path) -> None:
    m = VoiceMetrics(csv_path=tmp_path / "m.csv")
    m.on_metrics(_realtime_metrics(audio_in=1_000_000, audio_out=1_000_000))
    m.on_metrics(SimpleNamespace(metrics=SimpleNamespace(type="llm_metrics")))

    expected = PRICE_PER_MILLION["audio_in"] + PRICE_PER_MILLION["audio_out"]
    assert m.estimated_cost() == expected


async def test_summary_row_is_appended_to_csv(tmp_path) -> None:
    path = tmp_path / "sub" / "voice_metrics.csv"
    m = VoiceMetrics(csv_path=path, cost_db=tmp_path / "costs.db")
    for start, spoke in ((0, 0.5), (10, 11.0), (20, 20.7)):
        m.on_user_state(_user("speaking", "listening", start))
        m.on_agent_state(_agent("speaking", spoke))

    await m.write_summary()
    await m.write_summary()

    rows = list(csv.DictReader(path.open()))
    assert len(rows) == 2  # one header, two sessions
    assert rows[0]["replies"] == "3"
    assert rows[0]["median_s"] == "0.7"
    assert rows[0]["slowest_s"] == "1.0"


def test_detector_silence_is_subtracted(tmp_path) -> None:
    m = VoiceMetrics(csv_path=tmp_path / "m.csv", speech_end_offset_s=0.25)
    m.on_user_state(_user("speaking", "listening", 100.25))  # really stopped at 100.0
    m.on_agent_state(_agent("speaking", 101.0))
    assert m.latencies == pytest.approx([1.0])


def test_unreliable_timing_is_not_recorded(tmp_path) -> None:
    """Without the local detector, Gemini's 'stopped' marker is meaningless."""
    m = VoiceMetrics(csv_path=tmp_path / "m.csv", timing_reliable=False)
    m.on_user_state(_user("speaking", "listening", 100.0))
    m.on_agent_state(_agent("speaking", 104.5))
    assert m.latencies == []


def test_cached_input_is_counted_separately(tmp_path) -> None:
    m = VoiceMetrics(csv_path=tmp_path / "m.csv")
    event = _realtime_metrics(audio_in=100, text_in=900)
    event.metrics.input_token_details.cached_tokens = 700
    m.on_metrics(event)
    assert m.summary()["cached_in"] == 700


def test_latency_rates_agree_with_costs_table() -> None:
    """The voice meter and the cost dashboard price from one table."""
    from costs import LIVE_PRICE_PER_1K

    assert {
        key: rate * 1000 for key, rate in LIVE_PRICE_PER_1K.items()
    } == PRICE_PER_MILLION
    assert PRICE_PER_MILLION == {
        "audio_in": 3.00,
        "text_in": 0.75,
        "audio_out": 12.00,
        "text_out": 4.50,
    }


async def test_voice_session_cost_is_recorded_under_voice(tmp_path) -> None:
    """Stage 0: voice tokens reach the 7-day cost report as feature 'voice'."""
    from costs import CostStore

    m = VoiceMetrics(csv_path=tmp_path / "m.csv", cost_db=tmp_path / "c.db")
    m.on_metrics(
        _realtime_metrics(
            audio_in=1_000_000, text_in=1_000_000, audio_out=500_000, text_out=2_000_000
        )
    )

    await m.write_summary()

    summary = CostStore(path=tmp_path / "c.db").summary_7d()
    voice = summary["by_feature"]["voice"]
    assert voice["calls"] == 1
    # 1M audio in ($3) + 1M text in ($0.75) + 0.5M audio out ($6) + 2M text out ($9).
    assert voice["usd"] == pytest.approx(18.75)
    assert summary["usd"] == pytest.approx(18.75)


async def test_voice_cost_recording_never_breaks_shutdown(
    tmp_path, monkeypatch
) -> None:
    """A broken cost database must not take down session shutdown."""

    def boom(*args, **kwargs):
        raise OSError("disk is gone")

    monkeypatch.setattr("costs.CostStore", boom)
    m = VoiceMetrics(csv_path=tmp_path / "m.csv")
    m.on_metrics(_realtime_metrics(audio_in=100))

    await m.write_summary()  # must not raise

    assert (tmp_path / "m.csv").exists()


# ------------------------------------- task activity (v2 Stage 1, PTT idle)


def test_task_activity_defaults_to_not_running(monkeypatch, tmp_path) -> None:
    import latency

    monkeypatch.setattr(latency, "ACTIVITY_PATH", tmp_path / "voice-activity.json")
    assert latency.task_is_running() is False


def test_task_activity_round_trip(monkeypatch, tmp_path) -> None:
    import latency

    monkeypatch.setattr(latency, "ACTIVITY_PATH", tmp_path / "voice-activity.json")
    latency.note_task_running(True)
    assert latency.task_is_running() is True
    latency.note_task_running(False)
    assert latency.task_is_running() is False


def test_stale_task_activity_does_not_pin_the_session(monkeypatch, tmp_path) -> None:
    import json
    import time

    import latency

    path = tmp_path / "voice-activity.json"
    monkeypatch.setattr(latency, "ACTIVITY_PATH", path)
    monkeypatch.setattr(
        latency, "TASK_RUNNING_FRESH_S", 0.05
    )  # shrink the freshness window
    latency.note_task_running(True)
    assert latency.task_is_running() is True
    time.sleep(0.08)
    assert latency.task_is_running() is False
    # The file still says running: only its age decides.
    assert json.loads(path.read_text())["task_running"] is True
