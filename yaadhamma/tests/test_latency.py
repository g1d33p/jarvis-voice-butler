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
    m = VoiceMetrics(csv_path=path)
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
