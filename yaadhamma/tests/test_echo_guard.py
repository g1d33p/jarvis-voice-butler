"""Tests for the voice echo guard (assistant's own TTS transcribed as user).

2026-09-24 live run: Yaadhamma's own spoken fragments ("Seems the tool got
stuck, sir.", "to be your message about", "It was today.") came back through
STT as user speech and interrupted her own turns. The guard drops final
transcripts that near-verbatim match what she just said, without breaking
genuine user barge-in.
"""

import asyncio

from livekit.agents import stt

from echo_guard import EchoGuard, filter_echo_events


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _guard(**kwargs):
    clock = _Clock()
    kwargs.setdefault("clock", clock)
    return EchoGuard(**kwargs), clock


def _final(text):
    return stt.SpeechEvent(
        type=stt.SpeechEventType.FINAL_TRANSCRIPT,
        alternatives=[stt.SpeechData(language="en", text=text)],
    )


def _interim(text):
    return stt.SpeechEvent(
        type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
        alternatives=[stt.SpeechData(language="en", text=text)],
    )


async def _collect(events):
    return [e async for e in events]


# --- Live cases from 2026-09-24 -------------------------------------------


def test_live_case_tool_got_stuck() -> None:
    guard, _ = _guard()
    guard.note_assistant_text("Seems the tool got stuck, sir. Let me try that again.")
    assert guard.is_echo("Seems the tool got stuck, sir.")


def test_live_case_message_fragment() -> None:
    guard, _ = _guard()
    guard.note_assistant_text(
        "I was not able to be your message about the budget read out."
    )
    assert guard.is_echo("to be your message about")


def test_live_case_it_was_today() -> None:
    guard, _ = _guard()
    guard.note_assistant_text("Yes — the pairing happened, it was today.")
    assert guard.is_echo("It was today.")


# --- Guard must not eat genuine user speech ---------------------------------


def test_genuine_barge_in_passes_through() -> None:
    guard, _ = _guard()
    guard.note_assistant_text(
        "I found three unread chats. The first one is from Ravi about dinner "
        "tonight, the second is the family group, and the third is Priya."
    )
    assert not guard.is_echo("stop, I meant the other chat")
    assert not guard.is_echo("no, read the family one first")


def test_short_transcript_is_not_echo() -> None:
    guard, _ = _guard()
    guard.note_assistant_text("Ok, done sir.")
    assert not guard.is_echo("ok")  # too short to be a confident echo


def test_no_recent_speech_is_not_echo() -> None:
    guard, _ = _guard()
    assert not guard.is_echo("hello, are you there?")


def test_empty_transcript_is_not_echo() -> None:
    guard, _ = _guard()
    guard.note_assistant_text("Some fairly long assistant utterance here.")
    assert not guard.is_echo("")
    assert not guard.is_echo("   ")


def test_echo_window_expires() -> None:
    guard, clock = _guard(window_s=15.0)
    guard.note_assistant_text("Seems the tool got stuck, sir.")
    assert guard.is_echo("seems the tool got stuck sir")
    clock.now += 30.0
    assert not guard.is_echo("seems the tool got stuck sir")


def test_normalization_ignores_case_and_punctuation() -> None:
    guard, _ = _guard()
    guard.note_assistant_text("Seems the tool got stuck, sir!")
    assert guard.is_echo("SEEMS the tool got STUCK sir")


def test_mangled_echo_detected_by_similarity() -> None:
    # STT mishears one word of her own sentence: not a substring, but a
    # near-duplicate.
    guard, _ = _guard()
    guard.note_assistant_text("I will call you back in five minutes")
    assert guard.is_echo("i will call you back in fine minutes")


def test_user_quoting_partially_is_not_echo() -> None:
    guard, _ = _guard()
    guard.note_assistant_text("The meeting is at three pm tomorrow in the main hall.")
    # A short overlap inside a longer, different sentence is not near-verbatim.
    assert not guard.is_echo("is the meeting still on for tomorrow morning")


# --- Event-level filtering ---------------------------------------------------


def test_filter_drops_final_echo_and_keeps_the_rest() -> None:
    guard, _ = _guard()
    guard.note_assistant_text("Seems the tool got stuck, sir. Let me try that again.")
    events = [
        _interim("seems the tool"),  # interims never commit turns: left alone
        _final("Seems the tool got stuck, sir."),  # echo: dropped
        _final("no wait, keep going"),  # genuine: kept
        "a-raw-string-event",  # non-SpeechEvent: passed through untouched
    ]

    async def source():
        for e in events:
            yield e

    kept = asyncio.run(_collect(filter_echo_events(source(), guard)))
    assert len(kept) == 3
    assert kept[0] is events[0]
    assert kept[1] is events[2]
    assert kept[2] == "a-raw-string-event"


def test_filter_without_recent_speech_passes_everything() -> None:
    guard, _ = _guard()

    async def source():
        yield _final("hello, are you there?")

    kept = asyncio.run(_collect(filter_echo_events(source(), guard)))
    assert len(kept) == 1
