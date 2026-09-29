"""Tests for the wake-word state machine (src/wake.py).

No audio hardware: a fake detector stands in for openWakeWord/Porcupine.
Real microphone behaviour is unverified (see docs/BUILD_REPORT.md).
"""

import pytest

from wake import (
    FakeWakeDetector,
    MicrophonePermissionError,
    OpenWakeWordDetector,
    WakeConfigError,
    WakeMachine,
    WakeState,
    build_detector,
)


@pytest.fixture()
def events():
    return []


@pytest.fixture()
def machine(events):
    m = WakeMachine(
        idle_timeout_s=90.0,
        on_conversation_start=lambda: events.append("start"),
        on_conversation_end=lambda: events.append("end"),
    )
    return m


def test_wake_word_opens_conversation(machine, events) -> None:
    assert machine.state is WakeState.IDLE
    assert machine.on_wake_word(now=1000.0) is True
    assert machine.state is WakeState.CONVERSATION
    assert events == ["start"]


def test_silence_timeout_closes_conversation(machine, events) -> None:
    machine.on_wake_word(now=1000.0)
    assert machine.tick(now=1089.0) is False  # 89 s: still talking
    assert machine.state is WakeState.CONVERSATION
    assert machine.tick(now=1091.0) is True  # 91 s: timed out
    assert machine.state is WakeState.IDLE
    assert events == ["start", "end"]


def test_speech_resets_the_silence_timer(machine) -> None:
    machine.on_wake_word(now=1000.0)
    machine.on_speech(now=1080.0)  # user spoke at 80 s
    assert machine.tick(now=1169.0) is False  # 89 s after the speech
    assert machine.tick(now=1171.0) is True  # 91 s after the speech
    assert machine.state is WakeState.IDLE


def test_shortcut_opens_conversation(machine, events) -> None:
    assert machine.on_shortcut(now=1000.0) is True
    assert machine.state is WakeState.CONVERSATION
    assert events == ["start"]


def test_repeated_wakes_while_in_conversation_are_ignored(machine, events) -> None:
    machine.on_wake_word(now=1000.0)
    assert machine.on_wake_word(now=1001.0) is False
    assert machine.on_shortcut(now=1002.0) is False
    assert events == ["start"]  # no second session


def test_mute_stops_detection(machine, events) -> None:
    machine.on_mute()
    assert machine.state is WakeState.MUTED
    assert machine.on_wake_word(now=1000.0) is False
    assert machine.on_shortcut(now=1001.0) is False
    assert events == []
    machine.on_unmute()
    assert machine.state is WakeState.IDLE
    assert machine.on_wake_word(now=1002.0) is True


def test_mute_during_conversation_ends_it(machine, events) -> None:
    machine.on_wake_word(now=1000.0)
    machine.on_mute()
    assert machine.state is WakeState.MUTED
    assert events == ["start", "end"]


def test_conversation_can_end_and_wake_again(machine, events) -> None:
    machine.on_wake_word(now=1000.0)
    machine.tick(now=1200.0)
    assert machine.on_wake_word(now=1201.0) is True
    assert events == ["start", "end", "start"]


def test_fake_detector_hears_on_demand() -> None:
    detector = FakeWakeDetector(phrase="hey jarvis")
    assert detector.phrase == "hey jarvis"
    assert detector.check(b"\x00" * 16) is False
    detector.hear()
    assert detector.check(b"\x00" * 16) is True
    assert detector.check(b"\x00" * 16) is False  # one-shot
    detector.close()


def test_build_detector_defaults_to_openwakeword(monkeypatch) -> None:
    monkeypatch.setenv("YAADHAMMA_WAKE", "on")
    monkeypatch.setenv("YAADHAMMA_WAKE_ENGINE", "openwakeword")
    try:
        detector = build_detector()
    except WakeConfigError as exc:
        # openWakeWord is an optional dependency; without it installed the
        # only acceptable failure names the missing library.
        assert "not installed" in str(exc)
        return
    assert detector.phrase == "hey jarvis"
    detector.close()


def test_build_detector_disabled_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("YAADHAMMA_WAKE", "off")
    assert build_detector() is None


def test_porcupine_without_key_is_a_config_error(monkeypatch) -> None:
    monkeypatch.setenv("YAADHAMMA_WAKE", "on")
    monkeypatch.setenv("YAADHAMMA_WAKE_ENGINE", "porcupine")
    monkeypatch.delenv("YAADHAMMA_PICOVOICE_KEY", raising=False)
    with pytest.raises(WakeConfigError, match=r"[Pp]icovoice"):
        build_detector()


def test_porcupine_without_ppn_is_a_config_error(monkeypatch) -> None:
    monkeypatch.setenv("YAADHAMMA_WAKE", "on")
    monkeypatch.setenv("YAADHAMMA_WAKE_ENGINE", "porcupine")
    monkeypatch.setenv("YAADHAMMA_PICOVOICE_KEY", "test-key")
    monkeypatch.delenv("YAADHAMMA_WAKE_PPN", raising=False)
    with pytest.raises(WakeConfigError, match=r"[Pp]pn"):
        build_detector()


def test_unknown_engine_is_a_config_error(monkeypatch) -> None:
    monkeypatch.setenv("YAADHAMMA_WAKE", "on")
    monkeypatch.setenv("YAADHAMMA_WAKE_ENGINE", "shout")
    with pytest.raises(WakeConfigError, match="YAADHAMMA_WAKE_ENGINE"):
        build_detector()


def test_microphone_denial_is_actionable() -> None:
    def denied(*args, **kwargs):
        raise OSError("permission denied")

    from wake import MicStream

    stream = MicStream(audio_factory=denied)
    with pytest.raises(MicrophonePermissionError, match="Privacy & Security"):
        stream.start()


def _fake_openwakeword(monkeypatch, tmp_path):
    """Inject a fake openwakeword package: known model, fake downloader."""
    import sys
    import types

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    onnx_path = models_dir / "hey_jarvis_v0.1.onnx"

    pkg = types.ModuleType("openwakeword")
    pkg.MODELS = {
        "hey_jarvis": {"model_path": str(onnx_path).replace(".onnx", ".tflite")}
    }

    downloaded = []

    def fake_download_models(model_names):
        downloaded.append(list(model_names))
        onnx_path.write_bytes(b"fake-onnx")

    utils = types.ModuleType("openwakeword.utils")
    utils.download_models = fake_download_models
    pkg.utils = utils

    created = {}

    class FakeModel:
        def __init__(self, wakeword_models, inference_framework="tflite"):
            created["wakeword_models"] = wakeword_models
            created["inference_framework"] = inference_framework

        def close(self):
            pass

    pkg.Model = FakeModel
    monkeypatch.setitem(sys.modules, "openwakeword", pkg)
    monkeypatch.setitem(sys.modules, "openwakeword.utils", utils)
    return downloaded, created, onnx_path


def test_openwakeword_uses_onnx_and_downloads_missing_model(
    monkeypatch, tmp_path
) -> None:
    downloaded, created, onnx_path = _fake_openwakeword(monkeypatch, tmp_path)
    detector = OpenWakeWordDetector(phrase="hey jarvis")
    detector.close()
    # The missing model was fetched once...
    assert downloaded == [["hey_jarvis_v0.1"]]
    # ...and the detector explicitly asked for the ONNX engine, because
    # tflite-runtime ships no macOS wheels.
    assert created["inference_framework"] == "onnx"
    assert created["wakeword_models"] == ["hey_jarvis"]
    assert onnx_path.exists()


def test_openwakeword_skips_download_when_model_present(monkeypatch, tmp_path) -> None:
    downloaded, _created, onnx_path = _fake_openwakeword(monkeypatch, tmp_path)
    onnx_path.write_bytes(b"already-there")
    detector = OpenWakeWordDetector(phrase="hey jarvis")
    detector.close()
    assert downloaded == []


def test_openwakeword_unknown_phrase_is_a_config_error(monkeypatch, tmp_path) -> None:
    _fake_openwakeword(monkeypatch, tmp_path)
    with pytest.raises(WakeConfigError, match="no pre-trained model"):
        OpenWakeWordDetector(phrase="hey nobody")


def test_openwakeword_load_failure_surfaces_real_error(monkeypatch, tmp_path) -> None:
    import sys

    _fake_openwakeword(monkeypatch, tmp_path)
    pkg = sys.modules["openwakeword"]

    class BrokenModel:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("boom: onnx session failed")

    monkeypatch.setattr(pkg, "Model", BrokenModel)
    with pytest.raises(WakeConfigError, match="boom: onnx session failed"):
        OpenWakeWordDetector(phrase="hey jarvis")


def test_mic_error_detects_launchd_background_case() -> None:
    """PaMacCore err=-50 means the launchd background agent never got the
    macOS mic prompt: the message must say what to do instead of dumping a
    raw PortAudio error."""
    from wake import _mic_error

    err = _mic_error(Exception("Error opening InputStream: PaMacCore err=-50"))
    text = str(err)
    assert "foreground" in text
    assert "launchd" in text
    assert "uv run --extra wake --extra ui" in text


def test_mic_error_generic_case_keeps_standard_help() -> None:
    from wake import _mic_error

    err = _mic_error(Exception("some other portaudio failure"))
    text = str(err)
    assert "System Settings" in text
    assert "foreground" not in text


# ------------------------------------- push-to-talk session close (v2 Stage 1)


def test_session_closed_returns_to_idle(machine, events) -> None:
    machine.on_wake_word(100.0)
    assert machine.state is WakeState.CONVERSATION
    machine.on_session_closed()
    assert machine.state is WakeState.IDLE
    assert events == ["start", "end"]


def test_session_closed_while_idle_is_a_noop(machine, events) -> None:
    machine.on_session_closed()
    assert machine.state is WakeState.IDLE
    assert events == []


def test_wake_word_opens_again_after_session_closed(machine, events) -> None:
    machine.on_wake_word(100.0)
    machine.on_session_closed()
    machine.on_wake_word(200.0)
    assert machine.state is WakeState.CONVERSATION
    assert events == ["start", "end", "start"]
