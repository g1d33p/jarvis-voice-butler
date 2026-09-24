"""Tests for scripts/whatsapp_signin.py — the one-time QR pairing script.

The script's browser holds an exclusive lock on ~/.yaadhamma/chrome-profile
while open. On 2026-09-24 the sign-in window lingered after pairing, which
can block the agent's own browser from starting (and surfaces as a confusing
"not paired"). So after login is confirmed the script must quit the browser
itself after a short grace period. No real browser here — fakes only.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "whatsapp_signin.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("whatsapp_signin", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["whatsapp_signin"] = module
    spec.loader.exec_module(module)
    return module


class FakeClient:
    def __init__(self, behavior="paired"):
        self.behavior = behavior

    async def wait_for_login(self, timeout_s):
        if self.behavior == "timeout":
            from whatsapp import WhatsAppNotPairedError

            raise WhatsAppNotPairedError("timed out")


class FakeBrowser:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


async def test_pair_then_release_closes_browser_after_pairing(capsys):
    mod = _load_script()
    browser = FakeBrowser()
    await mod.pair_then_release(FakeClient(), browser, grace_s=0)
    assert browser.closed is True
    out = capsys.readouterr().out
    assert "Paired!" in out
    assert "close" in out.lower()


async def test_pair_then_release_tells_user_agent_takes_over(capsys):
    mod = _load_script()
    await mod.pair_then_release(FakeClient(), FakeBrowser(), grace_s=0)
    out = capsys.readouterr().out
    assert "Yaadhamma" in out


async def test_pair_then_release_propagates_timeout_without_closing():
    mod = _load_script()
    browser = FakeBrowser()
    from whatsapp import WhatsAppNotPairedError

    with pytest.raises(WhatsAppNotPairedError):
        await mod.pair_then_release(FakeClient("timeout"), browser, grace_s=0)
    # The caller (main) closes the browser on the timeout path.
    assert browser.closed is False
