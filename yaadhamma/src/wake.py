"""Wake word and the idle/conversation state machine.

Two detector backends, selected by config only:
- openWakeWord (default): pre-trained "hey jarvis" model, no account needed.
- Porcupine: custom phrase via a Picovoice AccessKey and .ppn file.
  Picovoice discontinued its free tier in June 2026 (enterprise only), so
  this path is for enterprise keys; pvporcupine is no longer in the wake
  extra and must be installed separately.

The state machine is pure Python: the caller feeds it wake events, speech
ticks and a monotonic clock. No audio hardware is touched here, so the
whole behaviour is testable. The daemon (scripts/yaadhamma_daemon.py)
owns the microphone and wires the callbacks to real Gemini Live sessions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol

DEFAULT_PHRASE = "hey jarvis"
DEFAULT_IDLE_TIMEOUT_S = 90.0


class WakeState(Enum):
    IDLE = "idle"  # listening locally; nothing leaves the Mac
    CONVERSATION = "conversation"  # a Gemini Live session is open
    MUTED = "muted"  # the user stopped detection from the menu bar


class WakeConfigError(Exception):
    """The wake-word settings are incomplete or contradictory."""


class MicrophonePermissionError(Exception):
    """The microphone could not be opened, with an actionable fix."""


_MIC_HELP = (
    "Yaadhamma could not open the microphone. On the Mac: System Settings > "
    "Privacy & Security > Microphone, and turn on access for the app that "
    "runs the daemon (Terminal, or Yaadhamma itself if it is bundled as an "
    "app). Then restart the daemon: python scripts/daemon_control.py start"
)

# Seen when the daemon runs as a launchd background agent: macOS never shows
# it the microphone permission prompt, so PortAudio fails deep in the HAL
# with PaMacCore err=-50 instead of a permission error.
_LAUNCHD_MIC_HELP = (
    "Yaadhamma could not open the microphone from the background daemon "
    "(PaMacCore err=-50): a launchd agent never gets the macOS microphone "
    "permission prompt, so there is nothing to grant. Run the daemon once "
    "in the foreground so the prompt can appear — from the project "
    "directory: uv run --extra wake --extra ui python "
    "scripts/yaadhamma_daemon.py — grant microphone access when macOS asks "
    "(the prompt names the uv binary), say 'Hey Jarvis' to confirm it "
    "hears you, stop it with Ctrl+C, then restart the background daemon: "
    "python scripts/daemon_control.py start"
)


def _mic_error(exc: Exception) -> MicrophonePermissionError:
    detail = str(exc)
    if "pamaccore" in detail.lower() and "-50" in detail:
        return MicrophonePermissionError(f"{_LAUNCHD_MIC_HELP} (details: {exc})")
    return MicrophonePermissionError(f"{_MIC_HELP} (details: {exc})")


class WakeDetector(Protocol):
    """Something that hears a wake phrase in raw 16-bit PCM audio."""

    @property
    def phrase(self) -> str: ...

    def check(self, pcm16: bytes) -> bool:
        """True when this chunk contains the wake phrase."""
        ...

    def close(self) -> None: ...


class FakeWakeDetector:
    """Test double: hear() makes the next check() fire, then it resets."""

    def __init__(self, phrase: str = DEFAULT_PHRASE) -> None:
        self._phrase = phrase
        self._heard = False

    @property
    def phrase(self) -> str:
        return self._phrase

    def hear(self) -> None:
        self._heard = True

    def check(self, pcm16: bytes) -> bool:
        heard, self._heard = self._heard, False
        return heard

    def close(self) -> None:
        pass


def _ensure_wakeword_model(model_name: str) -> None:
    """Download the wake-word model files once, when they are missing.

    openWakeWord never fetches models itself and its wheel ships with an
    empty models directory, so a fresh install fails without this. Fetches
    the shared audio-feature models plus the requested wake-word model
    (both .tflite and .onnx variants) from the project's GitHub releases.
    Logs while downloading so the daemon log never goes silent here.
    """
    import logging

    from openwakeword import MODELS
    from openwakeword.utils import download_models

    log = logging.getLogger("yaadhamma.wake")
    entry = MODELS.get(model_name)
    if entry is None:
        raise WakeConfigError(
            f"openWakeWord has no pre-trained model for {model_name!r}. "
            "Use YAADHAMMA_WAKE_ENGINE=porcupine with a trained .ppn "
            "for a custom phrase."
        )
    onnx_path = entry["model_path"].replace(".tflite", ".onnx")
    if os.path.exists(onnx_path):
        return
    log.info("downloading wake-word model %r (one-time)...", model_name)
    try:
        # download_models matches on substring: the versioned base name hits
        # both the .tflite and the .onnx release assets.
        base = os.path.splitext(os.path.basename(onnx_path))[0]
        download_models(model_names=[base])
    except Exception as exc:
        raise WakeConfigError(
            f"could not download the wake-word model {model_name!r}: {exc}"
        ) from exc
    if not os.path.exists(onnx_path):
        raise WakeConfigError(
            f"wake-word model {model_name!r} still missing after download "
            f"(expected at {onnx_path})"
        )
    log.info("wake-word model %r ready", model_name)


class OpenWakeWordDetector:
    """openWakeWord backend (default): pre-trained community models.

    Uses the ONNX inference path: onnxruntime ships in the ``wake`` extra,
    while tflite-runtime publishes no macOS wheels at all. Model files are
    downloaded once from the openWakeWord GitHub releases on first use
    (the wheel ships with an empty models directory).
    """

    def __init__(self, phrase: str = DEFAULT_PHRASE, sensitivity: float = 0.5) -> None:
        try:
            from openwakeword import Model as _Model
        except ImportError as exc:
            raise WakeConfigError(
                "openWakeWord is not installed. Install it with: uv sync --extra wake"
            ) from exc
        model_name = phrase.replace(" ", "_")
        _ensure_wakeword_model(model_name)
        try:
            self._model = _Model(
                wakeword_models=[model_name], inference_framework="onnx"
            )
        except Exception as exc:
            raise WakeConfigError(
                f"openWakeWord could not load {phrase!r}: {exc}"
            ) from exc
        self._phrase = phrase
        self._sensitivity = sensitivity

    @property
    def phrase(self) -> str:
        return self._phrase

    def check(self, pcm16: bytes) -> bool:
        import numpy as np

        frame = np.frombuffer(pcm16, dtype=np.int16)
        scores = self._model.predict(frame)
        return any(score >= self._sensitivity for score in scores.values())

    def close(self) -> None:
        pass


class PorcupineDetector:
    """Picovoice Porcupine backend: custom phrase via AccessKey + .ppn.

    Picovoice ended its free tier in June 2026; this backend needs an
    enterprise AccessKey, and pvporcupine is no longer bundled with the
    wake extra (install it separately).
    """

    def __init__(
        self,
        keyword_path: str,
        access_key: str,
        sensitivity: float = 0.5,
    ) -> None:
        try:
            import pvporcupine
        except ImportError as exc:
            raise WakeConfigError(
                "pvporcupine is not installed (it is no longer part of the "
                "wake extra). Install it with: pip install pvporcupine "
                "sounddevice"
            ) from exc
        try:
            self._porcupine = pvporcupine.create(
                access_key=access_key,
                keyword_paths=[keyword_path],
                sensitivities=[sensitivity],
            )
        except Exception as exc:
            raise WakeConfigError(
                f"Porcupine could not start with {keyword_path!r}: {exc}"
            ) from exc
        self._keyword_path = keyword_path

    @property
    def phrase(self) -> str:
        name = os.path.basename(self._keyword_path)
        return os.path.splitext(name)[0].replace("_", " ")

    @property
    def frame_samples(self) -> int:
        return self._porcupine.frame_length

    def check(self, pcm16: bytes) -> bool:
        import struct

        samples = struct.unpack(f"<{len(pcm16) // 2}h", pcm16)
        return self._porcupine.process(samples) >= 0

    def close(self) -> None:
        self._porcupine.delete()


def build_detector() -> WakeDetector | None:
    """Build the configured detector; None when the wake word is disabled.

    Raises WakeConfigError for incomplete settings, with the fix spelled out.
    """
    import config

    settings = config.wake_settings()
    if not settings["enabled"]:
        return None
    engine = settings["engine"]
    if engine == "openwakeword":
        return OpenWakeWordDetector(
            phrase=settings["phrase"], sensitivity=settings["sensitivity"]
        )
    if engine == "porcupine":
        if not settings["picovoice_key"]:
            raise WakeConfigError(
                "YAADHAMMA_WAKE_ENGINE=porcupine needs YAADHAMMA_PICOVOICE_KEY. "
                "Picovoice discontinued its free tier in June 2026 — this "
                "engine now needs an enterprise AccessKey (see "
                "docs/OPERATIONS.md)."
            )
        if not settings["keyword_path"]:
            raise WakeConfigError(
                "YAADHAMMA_WAKE_ENGINE=porcupine needs YAADHAMMA_WAKE_PPN "
                "pointing at the trained .ppn file, e.g. "
                "/path/to/hey-yaadhamma.ppn (see docs/OPERATIONS.md)."
            )
        if not os.path.exists(settings["keyword_path"]):
            raise WakeConfigError(
                f"YAADHAMMA_WAKE_PPN points at {settings['keyword_path']!r}, "
                "which does not exist."
            )
        return PorcupineDetector(
            keyword_path=settings["keyword_path"],
            access_key=settings["picovoice_key"],
            sensitivity=settings["sensitivity"],
        )
    raise WakeConfigError(
        f"Unknown YAADHAMMA_WAKE_ENGINE={engine!r}: use 'openwakeword' or 'porcupine'."
    )


class MicStream:
    """Raw microphone frames. Fails loudly on permission problems."""

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        frame_samples: int = 1280,
        audio_factory=None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_samples = frame_samples
        self._audio_factory = audio_factory
        self._stream = None

    def start(self) -> None:
        factory = self._audio_factory or self._default_factory
        try:
            self._stream = factory(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                blocksize=self.frame_samples,
            )
            self._stream.start()
        except Exception as exc:
            raise _mic_error(exc) from exc

    def read(self) -> bytes:
        frames, _overflowed = self._stream.read(self.frame_samples)
        return bytes(frames)

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    @staticmethod
    def _default_factory(**kwargs):
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise WakeConfigError(
                "sounddevice is not installed. Install it with: pip install sounddevice"
            ) from exc
        return sd.InputStream(**kwargs)


@dataclass
class WakeMachine:
    """Idle <-> conversation, driven by events and a caller-owned clock.

    `now` is monotonic seconds supplied by the caller, so tests control time.
    """

    idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S
    on_conversation_start: Callable[[], None] | None = None
    on_conversation_end: Callable[[], None] | None = None
    state: WakeState = field(default=WakeState.IDLE, init=False)
    last_speech_at: float = field(default=0.0, init=False)

    def _open_conversation(self, now: float) -> bool:
        if self.state is not WakeState.IDLE:
            return False
        self.state = WakeState.CONVERSATION
        self.last_speech_at = now
        if self.on_conversation_start:
            self.on_conversation_start()
        return True

    def _close_conversation(self) -> None:
        if self.state is not WakeState.CONVERSATION:
            return
        self.state = WakeState.IDLE
        if self.on_conversation_end:
            self.on_conversation_end()

    def on_wake_word(self, now: float) -> bool:
        """The detector heard the phrase. True when a session opened."""
        return self._open_conversation(now)

    def on_shortcut(self, now: float) -> bool:
        """Option+Space. True when a session opened."""
        return self._open_conversation(now)

    def on_speech(self, now: float) -> None:
        """The user spoke during a conversation: restart the silence timer."""
        if self.state is WakeState.CONVERSATION:
            self.last_speech_at = now

    def on_mute(self) -> None:
        if self.state is WakeState.CONVERSATION:
            self._close_conversation()
        self.state = WakeState.MUTED

    def on_session_closed(self) -> None:
        """The session was closed by something else (the push-to-talk idle
        timeout): back to idle so the next wake word is heard. Only acts
        when a conversation is actually open."""
        self._close_conversation()

    def on_unmute(self) -> None:
        if self.state is WakeState.MUTED:
            self.state = WakeState.IDLE

    def tick(self, now: float) -> bool:
        """Periodic check. True when a conversation just timed out."""
        if (
            self.state is WakeState.CONVERSATION
            and now - self.last_speech_at >= self.idle_timeout_s
        ):
            self._close_conversation()
            return True
        return False


def control_path() -> str:
    """Where the menu bar writes mute requests for the daemon to poll."""
    return os.path.expanduser("~/.yaadhamma/wake-control.json")
