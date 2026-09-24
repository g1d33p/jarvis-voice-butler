import pytest

from policy import can_send_without_asking


@pytest.mark.parametrize(
    ("message", "user_words"),
    [
        (
            "Hi",
            "Go back to the WhatsApp tab and in the first chat. Send hi to the guy.",
        ),
        ("On my way", "text Ravi on my way"),
        ("Good morning!", "send good morning to the family"),
        ("Running late", "Reply running late"),
    ],
)
def test_short_dictated_messages_can_go_straight_out(message, user_words) -> None:
    allowed, reason = can_send_without_asking(message, user_words)
    assert allowed, reason


@pytest.mark.parametrize(
    ("message", "user_words", "why"),
    [
        # Yaadhamma composed or embellished the wording.
        ("Hey! Hope you're doing well today", "send a greeting to Ravi", "wrote"),
        ("Hi there, how are you?", "send hi", "wrote"),
        # The user did not ask to send anything.
        ("Hi", "open the first chat and type hi", "did not explicitly ask"),
        # Sensitive content.
        ("My OTP is 482913", "send my OTP is 482913", "numbers"),
        ("Pay the rent", "send pay the rent", "sensitive"),
        ("I resign", "send I resign to my manager", "sensitive"),
        ("See https://example.com", "send see https://example.com", "link"),
        ("I'm sorry", "text her I'm sorry", "sensitive"),
        # Too long.
        (
            "hi " * 30,
            "send " + "hi " * 30,
            "long",
        ),
    ],
)
def test_serious_composed_or_unrequested_messages_need_approval(
    message, user_words, why
) -> None:
    allowed, reason = can_send_without_asking(message, user_words)
    assert not allowed
    assert why in reason
