"""Tests for pronunciation fixes applied before TTS (src/pronunciation.py).

The Fish Audio model behind LiveKit Inference has no pronunciation lexicon,
so words it mispronounces are respelled just before synthesis. Transcripts
keep the original spelling; only the spoken audio changes.
"""

from livekit.agents import inference

from pronunciation import PronunciationTTS, apply_pronunciations


def test_yaadhamma_is_respelt():
    assert (
        apply_pronunciations("Good morning, Sir. I'm Yaadhamma.")
        == "Good morning, Sir. I'm Yaah-dh-um-ah."
    )


def test_case_insensitive():
    assert apply_pronunciations("YAADHAMMA online") == "Yaah-dh-um-ah online"


def test_possessive_keeps_suffix():
    assert apply_pronunciations("Yaadhamma's voice") == "Yaah-dh-um-ah's voice"


def test_no_partial_word_replacement():
    assert apply_pronunciations("Yaadhammas") == "Yaadhammas"


def test_unrelated_text_untouched():
    text = "The weather is nice today."
    assert apply_pronunciations(text) == text


def _make_tts() -> PronunciationTTS:
    return PronunciationTTS(
        model="fishaudio/s2.1-pro",
        voice="933563129e564b19a115bedd57b7406a",
        api_key="test",
        api_secret="test",
    )


def test_synthesize_rewrites_text(monkeypatch):
    seen = {}

    def fake_synthesize(self, text, *, conn_options=None):
        seen["text"] = text
        return "chunked-stream"

    monkeypatch.setattr(inference.TTS, "synthesize", fake_synthesize)
    tts = _make_tts()
    out = tts.synthesize("Hi, I'm Yaadhamma.")
    assert out == "chunked-stream"
    assert seen["text"] == "Hi, I'm Yaah-dh-um-ah."


def test_stream_rewrites_word_split_across_tokens(monkeypatch):
    pushed = []
    ended = []

    class FakeStream:
        def push_text(self, token):
            pushed.append(token)

        def flush(self):
            pass

        def end_input(self):
            ended.append(True)

        async def aclose(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    def fake_stream(self, *, conn_options=None):
        return FakeStream()

    monkeypatch.setattr(inference.TTS, "stream", fake_stream)
    tts = _make_tts()
    stream = tts.stream()
    # "Yaadhamma" arrives split across two tokens; it must still be respelled.
    stream.push_text("Hello, Yaad")
    stream.push_text("hamma, how are")
    stream.push_text(" you?")
    stream.end_input()

    assert "".join(pushed) == "Hello, Yaah-dh-um-ah, how are you?"
    assert ended == [True]


def test_stream_flush_releases_holdback(monkeypatch):
    pushed = []
    flushed = []

    class FakeStream:
        def push_text(self, token):
            pushed.append(token)

        def flush(self):
            flushed.append(True)

        def end_input(self):
            self.flush()

        async def aclose(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    monkeypatch.setattr(inference.TTS, "stream", lambda self, **kw: FakeStream())
    stream = _make_tts().stream()
    stream.push_text("I am Yaadhamma")
    stream.flush()

    assert "".join(pushed) == "I am Yaah-dh-um-ah"
    assert flushed == [True]
