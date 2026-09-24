import subprocess

import pytest
from livekit.agents.llm import ToolError

import mac_tools
from mac_tools import MacTools, capture_screen_to_file


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


@pytest.mark.asyncio
async def test_active_app_with_window_title(monkeypatch) -> None:
    monkeypatch.setattr(
        mac_tools,
        "_run",
        lambda *a, **k: _completed("Safari\nok\nApple — Start Page\n"),
    )

    info = await MacTools().get_active_app(None)

    assert info == {"app": "Safari", "window_title": "Apple — Start Page"}


@pytest.mark.asyncio
async def test_active_app_without_accessibility_permission(monkeypatch) -> None:
    monkeypatch.setattr(
        mac_tools, "_run", lambda *a, **k: _completed("Finder\nunavailable\n\n")
    )

    info = await MacTools().get_active_app(None)

    assert info["app"] == "Finder"
    assert info["window_title"] == ""
    assert "Accessibility" in info["note"]


@pytest.mark.asyncio
async def test_read_clipboard_truncates_long_text(monkeypatch) -> None:
    monkeypatch.setattr(mac_tools, "_run", lambda *a, **k: _completed("x" * 5000))

    result = await MacTools().read_clipboard(None)

    assert result["length"] == 5000
    assert result["truncated"] is True
    assert len(result["text"]) == mac_tools.CLIPBOARD_MAX_CHARS


@pytest.mark.asyncio
async def test_write_clipboard_sends_text_on_stdin(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs.get("stdin")))
        return _completed()

    monkeypatch.setattr(mac_tools, "_run", fake_run)

    message = await MacTools().write_clipboard(None, "hello there")

    assert calls == [(["pbcopy"], "hello there")]
    assert "11 characters" in message


def test_screenshot_is_saved_and_old_ones_pruned(tmp_path, monkeypatch) -> None:
    def fake_screencapture(command, **kwargs):
        from pathlib import Path

        Path(command[-1]).write_bytes(b"\x89PNG fake")
        return _completed()

    monkeypatch.setattr(mac_tools, "_run", fake_screencapture)
    monkeypatch.setattr(mac_tools, "KEEP_SCREENSHOTS", 3)

    for _ in range(5):
        result = capture_screen_to_file(tmp_path)

    assert result["saved"] is True
    assert result["path"].endswith(".png")
    assert len(list(tmp_path.glob("screen-*.png"))) == 3


def test_screenshot_failure_mentions_permission(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mac_tools, "_run", lambda *a, **k: _completed(returncode=1))

    with pytest.raises(ToolError, match="Screen Recording"):
        capture_screen_to_file(tmp_path)


def test_new_mac_tools_are_registered() -> None:
    ids = [tool.id for tool in MacTools().tools]
    for name in (
        "get_active_app",
        "read_clipboard",
        "write_clipboard",
        "capture_screen",
    ):
        assert name in ids
