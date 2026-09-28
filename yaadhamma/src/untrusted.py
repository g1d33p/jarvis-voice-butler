"""Mark content that arrived from outside as untrusted data, never instructions.

Every string that came from an email, a web page, a WhatsApp message, a file
or a search result is wrapped in an explicit envelope before it reaches any
model:

    <<UNTRUSTED_CONTENT source="email (work)">>
    ...the outside text, with any nested envelope markers removed...
    <<END_UNTRUSTED_CONTENT>>

The model's instructions (see prompts.py, digest.py, learning.py, planner.py,
tidy.py) all say the same thing: content inside the envelope is data to
summarise or act *about*, never instructions to follow. If it asks for an
action, the model reports that it did so instead of doing it.

Why an envelope rather than filtering: no filter can recognise every
phrasing of "ignore your instructions". Making the boundary explicit and
structural means the model does not have to detect an attack; it only has
to respect a container. The approval gates stay as the last line of defence.
"""

from __future__ import annotations

OPEN_MARKER = "<<UNTRUSTED_CONTENT"
CLOSE_MARKER = "<<END_UNTRUSTED_CONTENT>>"
# An earlier draft of this envelope used <<END>> as the closing marker;
# neutralise that form too so content cannot smuggle it in.
LEGACY_CLOSE_MARKER = "<<END>>"

_REMOVED_NOTE = "[untrusted envelope marker removed]"


def _clean_source(source: str) -> str:
    """Keep the source label short and marker-free (it can name a chat)."""
    text = (source or "unknown").replace("<", "").replace(">", "").replace('"', "")
    text = " ".join(text.split())
    return text[:80] or "unknown"


def _neutralise(text: str) -> str:
    """Remove anything that looks like our own envelope markers."""
    for marker in (CLOSE_MARKER, OPEN_MARKER, LEGACY_CLOSE_MARKER):
        text = text.replace(marker, _REMOVED_NOTE)
    return text


def wrap(text: str, source: str) -> str:
    """Wrap outside content so a model treats it as data, not instructions.

    Args:
        text: the untrusted string (email body, message text, page text...).
        source: where it came from, e.g. "email (work)", "WhatsApp SC1-Execs".
            Only used as a label; it is cleaned before use.

    Empty text is returned unchanged: there is nothing to mark.
    """
    if not text:
        return text
    body = _neutralise(text)
    return f'{OPEN_MARKER} source="{_clean_source(source)}">>\n{body}\n{CLOSE_MARKER}'


def is_wrapped(text: str) -> bool:
    """True when the text already carries a complete envelope."""
    return bool(text) and OPEN_MARKER in text and CLOSE_MARKER in text
