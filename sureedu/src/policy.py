"""Rules for which messages Sureedu may send without asking first.

This is the first piece of the permission layer. The model can always choose
to ask for approval, but it can never skip approval unless these rules agree:
the language model proposes, this code decides.
"""

import re

# Longest message that can go out without asking.
MAX_AUTO_SEND_CHARS = 80

_SEND_VERBS = {
    "send",
    "text",
    "message",
    "reply",
    "tell",
    "say",
    "ping",
    "wish",
    "write",
    # Telugu / Hindi (romanised)
    "pampu",
    "pampinchu",
    "bhejo",
    "cheppu",
}

# Content that makes a message "serious" enough to always ask first.
_SENSITIVE_WORDS = {
    # money
    "pay",
    "paid",
    "payment",
    "money",
    "transfer",
    "salary",
    "invoice",
    "loan",
    "bank",
    "account",
    "card",
    "upi",
    "venmo",
    "zelle",
    "paypal",
    "dollars",
    "rupees",
    "lakh",
    "crore",
    "price",
    "refund",
    # credentials and identity
    "password",
    "passcode",
    "otp",
    "pin",
    "code",
    "ssn",
    "passport",
    "license",
    "address",
    "login",
    # commitments and decisions
    "resign",
    "resignation",
    "quit",
    "fired",
    "contract",
    "sign",
    "agree",
    "accept",
    "decline",
    "cancel",
    "offer",
    "deal",
    "confirm",
    "approve",
    "delete",
    "legal",
    "lawyer",
    # emotionally heavy
    "sorry",
    "love",
    "breakup",
    "divorce",
    "died",
    "death",
    "funeral",
    "hate",
}

_SENSITIVE_PATTERNS = (
    re.compile(r"\d{4,}"),  # phone, account, OTP or card numbers
    re.compile(r"[$€£₹]"),  # currency
    re.compile(r"https?://|www\.", re.IGNORECASE),  # links
    re.compile(r"\S+@\S+\.\S+"),  # email addresses
)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.casefold())


def can_send_without_asking(message: str, user_words: str) -> tuple[bool, str]:
    """Decide whether `message` may be sent without an approval question.

    Allowed only when all of these hold:
    - the user's own latest words ask for something to be sent;
    - the message is short;
    - the user dictated it: every word of it appears in what they said,
      so Sureedu did not compose or embellish it;
    - it contains nothing sensitive (money, codes, numbers, links, commitments,
      emotionally heavy topics).
    Returns (allowed, reason).
    """
    message = message.strip()
    if not message:
        return False, "There is no message text."

    said = _words(user_words)
    if not _SEND_VERBS.intersection(said):
        return False, "The user did not explicitly ask to send a message."

    if len(message) > MAX_AUTO_SEND_CHARS:
        return False, "The message is long."

    for pattern in _SENSITIVE_PATTERNS:
        if pattern.search(message):
            return False, "The message contains numbers, money, a link, or an email."

    message_words = _words(message)
    if _SENSITIVE_WORDS.intersection(message_words):
        return False, "The message touches a sensitive or serious topic."

    if not message_words or not set(message_words) <= set(said):
        return (
            False,
            "Sureedu wrote or changed the wording, so the user should check it.",
        )

    return True, "Short, simple message dictated by the user."
