"""2026-09-28: digests delivered but reported unconfirmed; unknown options."""

import sys
from pathlib import Path

import pytest
from test_step1g import _KeyboardBrowser
from test_whatsapp import _chat

from whatsapp import WhatsAppClient, _same_message


def test_formatting_and_line_breaks_do_not_fail_confirmation() -> None:
    sent = "Yaadhamma digest, Mon 09:00 AM\n\n*Needs you*\n- Reply to Priya (email)"
    shown = "Yaadhamma digest, Mon 09:00 AM Needs you - Reply to Priya (email)"
    assert _same_message(shown, sent)
    assert not _same_message("Something else entirely", sent)


class _LineBrowser(_KeyboardBrowser):
    async def new_line(self):
        self.events.append(("newline",))

    async def evaluate(self, script):
        if (
            "return waLastMessage(document" in script
            and ("key", "Enter") in self.events
        ):
            # WhatsApp shows *bold* without the asterisks.
            return {
                "message": {
                    "meta": "[m] Jeevan: ",
                    "text": "Brief Today - Standup",
                    "outgoing": True,
                }
            }
        return await super().evaluate(script)


async def test_multi_line_message_is_one_message_not_several() -> None:
    browser = _LineBrowser(chats=[_chat("+91 96405 20634")])
    result = await WhatsAppClient(browser=browser).send_message(
        "+91 96405 20634", "Brief\n\n*Today*\n- Standup"
    )

    assert result["sent"] is True
    kinds = [e[0] for e in browser.events if e[0] in ("type", "newline", "key")]
    # Lines joined with Shift+Enter; only the final Enter sends.
    assert kinds.count("key") == 1 and kinds[-1] == "key"
    assert kinds.count("newline") == 3


async def test_unknown_option_never_runs_a_digest(monkeypatch, capsys) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import digest_run

    def forbidden(*args, **kwargs):
        raise AssertionError("a digest must not start")

    monkeypatch.setattr(digest_run, "BrowserManager", forbidden)
    monkeypatch.setattr(sys, "argv", ["digest_run.py", "--plann"])

    assert await digest_run.main() == 2
    assert "Unknown option --plann" in capsys.readouterr().out


if __name__ == "__main__":
    pytest.main([__file__])
