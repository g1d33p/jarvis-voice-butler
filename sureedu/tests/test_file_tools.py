import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from livekit.agents.llm import ToolError

import file_tools
from file_tools import FileTools, check_trashable


def _context_saying(text: str) -> SimpleNamespace:
    message = SimpleNamespace(type="message", role="user", text_content=text)
    return SimpleNamespace(
        session=SimpleNamespace(history=SimpleNamespace(items=[message]))
    )


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    fake_home = tmp_path / "home"
    (fake_home / "Downloads" / "old-stuff").mkdir(parents=True)
    (fake_home / "Downloads" / "old-stuff" / "a.txt").write_text("a")
    (fake_home / "Downloads" / "report.pdf").write_text("pdf")
    (fake_home / "Library" / "Prefs").mkdir(parents=True)
    (fake_home / ".sureedu" / "chrome-profile").mkdir(parents=True)
    monkeypatch.setattr(file_tools.Path, "home", classmethod(lambda cls: fake_home))
    return fake_home


def test_regular_file_in_home_is_trashable(home) -> None:
    assert (
        check_trashable(home / "Downloads" / "report.pdf")
        == (home / "Downloads" / "report.pdf").resolve()
    )


@pytest.mark.parametrize(
    ("relative", "message"),
    [
        ("", "inside your home folder"),
        ("Downloads", "main system folder"),
        ("Library/Prefs", "Library"),
        (".sureedu/chrome-profile", "Hidden configuration"),
        ("does-not-exist.txt", "Nothing exists"),
    ],
)
def test_protected_paths_are_refused(home, relative: str, message: str) -> None:
    with pytest.raises(ToolError, match=message):
        check_trashable(home / relative if relative else home)


def test_paths_outside_home_are_refused(home, tmp_path) -> None:
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x")

    with pytest.raises(ToolError, match="inside your home folder"):
        check_trashable(outside)


@pytest.mark.asyncio
async def test_first_call_only_describes_the_item(home, monkeypatch) -> None:
    monkeypatch.setattr(
        file_tools, "_run_osascript", lambda script: pytest.fail("must not trash yet")
    )

    result = await FileTools().move_to_trash(
        None, str(home / "Downloads" / "old-stuff")
    )

    assert result["needs_confirmation"] is True
    assert result["moved"] is False
    assert result["kind"] == "folder"
    assert result["items_inside"] == 1


@pytest.mark.asyncio
async def test_unclear_reply_does_not_trash(home, monkeypatch) -> None:
    monkeypatch.setattr(
        file_tools, "_run_osascript", lambda script: pytest.fail("must not trash")
    )

    with pytest.raises(ToolError, match="not a clear yes"):
        await FileTools().move_to_trash(
            _context_saying("hmm"), str(home / "Downloads" / "report.pdf"), True
        )


@pytest.mark.asyncio
async def test_confirmed_item_goes_to_trash_via_finder(home, monkeypatch) -> None:
    target = home / "Downloads" / "report.pdf"
    scripts = []

    def fake_finder(script: str):
        scripts.append(script)
        target.unlink()  # what Finder would do
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(file_tools, "_run_osascript", fake_finder)

    result = await FileTools().move_to_trash(_context_saying("yes"), str(target), True)

    assert result["moved"] is True
    assert result["restorable"] is True
    assert 'tell application "Finder" to delete POSIX file' in scripts[0]


@pytest.mark.asyncio
async def test_quotes_in_file_names_are_escaped(home, monkeypatch) -> None:
    target = home / "Downloads" / 'odd "name".txt'
    target.write_text("x")
    scripts = []

    def fake_finder(script: str):
        scripts.append(script)
        target.unlink()
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(file_tools, "_run_osascript", fake_finder)
    await FileTools().move_to_trash(_context_saying("yes"), str(target), True)

    assert 'odd \\"name\\".txt' in scripts[0]


@pytest.mark.asyncio
async def test_success_is_verified_not_assumed(home, monkeypatch) -> None:
    monkeypatch.setattr(
        file_tools,
        "_run_osascript",
        lambda script: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )

    with pytest.raises(ToolError, match="still there"):
        await FileTools().move_to_trash(
            _context_saying("yes"), str(home / "Downloads" / "report.pdf"), True
        )


def test_no_permanent_delete_tool_exists() -> None:
    ids = [tool.id for tool in FileTools().tools]
    assert "move_to_trash" in ids
    assert not any("delete" in tool_id or "empty" in tool_id for tool_id in ids)
