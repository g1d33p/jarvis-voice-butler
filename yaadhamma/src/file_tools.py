import shutil
import subprocess
from pathlib import Path

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from tools import _latest_user_text, is_clear_approval

# Folders that are never moved to the Trash as a whole, even with approval.
_PROTECTED_NAMES = {
    "Desktop",
    "Documents",
    "Downloads",
    "Library",
    "Applications",
    "Movies",
    "Music",
    "Pictures",
    "Public",
    "Projects",
}


# Opening these would run a program or installer, so open_path only reveals them.
_RUNNABLE_SUFFIXES = {
    ".app",
    ".command",
    ".sh",
    ".zsh",
    ".bash",
    ".tool",
    ".pkg",
    ".mpkg",
    ".dmg",
    ".scpt",
    ".applescript",
    ".workflow",
    ".terminal",
    ".jar",
    ".py",
}


def _run_open(args: list[str]) -> subprocess.CompletedProcess:
    """Run macOS `open`. Kept separate so tests can replace it."""
    return subprocess.run(["open", *args], capture_output=True, text=True, timeout=15)


def _run_osascript(script: str) -> subprocess.CompletedProcess:
    """Run AppleScript. Kept separate so tests can replace it."""
    return subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=30
    )


def check_trashable(path: Path, home: Path | None = None) -> Path:
    """Validate that `path` may be moved to the Trash; return it resolved.

    Allowed: existing files or folders inside the home folder. Refused: the
    home folder itself, the standard top-level folders, anything under
    ~/Library, and anything under a hidden folder (for example ~/.yaadhamma).
    """
    home = (home or Path.home()).resolve()
    target = path.expanduser().resolve()

    if not target.exists():
        raise ToolError(f"Nothing exists at {target}.")
    if target == home or home not in target.parents:
        raise ToolError("I can only move items inside your home folder to the Trash.")

    relative = target.relative_to(home)
    if len(relative.parts) == 1 and relative.parts[0] in _PROTECTED_NAMES:
        raise ToolError(
            f"{target.name} is a main system folder, so I will not trash it."
        )
    if relative.parts[0] == "Library":
        raise ToolError(
            "Items inside Library are used by apps, so I will not trash them."
        )
    if any(part.startswith(".") for part in relative.parts[:-1]) or (
        len(relative.parts) == 1 and relative.parts[0].startswith(".")
    ):
        raise ToolError("Hidden configuration folders are off limits for the Trash.")
    return target


def describe_path(target: Path) -> dict[str, object]:
    if target.is_dir():
        count = sum(1 for _ in target.rglob("*"))
        return {"kind": "folder", "items_inside": count}
    return {"kind": "file", "bytes": target.stat().st_size}


class FileTools:
    """Tools for interacting with files and folders on the Mac."""

    @property
    def tools(self) -> list:
        return [
            self.get_home_directory,
            self.list_directory,
            self.search_files,
            self.inspect_path,
            self.create_folder,
            self.create_file,
            self.rename_path,
            self.move_path,
            self.copy_path,
            self.move_to_trash,
            self.open_path,
        ]

    @function_tool()
    async def get_home_directory(
        self,
        context: RunContext,
    ) -> str:
        """Return the user's Mac home directory."""

        return str(Path.home())

    @function_tool()
    async def list_directory(
        self,
        context: RunContext,
        path: str,
    ) -> str:
        """List files and folders inside a directory."""

        target = Path(path).expanduser()

        if not target.exists():
            raise ToolError(f"The path does not exist: {target}")

        if not target.is_dir():
            raise ToolError(f"The path is not a directory: {target}")

        try:
            items = sorted(
                target.iterdir(),
                key=lambda item: (not item.is_dir(), item.name.lower()),
            )
        except PermissionError as exc:
            raise ToolError(f"Permission denied while reading {target}.") from exc

        if not items:
            return f"{target} is empty."

        lines = []

        for item in items:
            item_type = "DIR" if item.is_dir() else "FILE"
            lines.append(f"[{item_type}] {item.name}")

        return "\n".join(lines)

    @function_tool()
    async def search_files(
        self,
        context: RunContext,
        directory: str,
        query: str,
    ) -> str:
        """Search for files and folders whose names contain the query."""

        root = Path(directory).expanduser()
        query = query.strip().casefold()

        if not root.exists():
            raise ToolError(f"The directory does not exist: {root}")

        if not root.is_dir():
            raise ToolError(f"The path is not a directory: {root}")

        if not query:
            raise ToolError("The search query cannot be empty.")

        matches = []

        try:
            for item in root.rglob("*"):
                if query in item.name.casefold():
                    matches.append(str(item))

                if len(matches) >= 100:
                    break

        except PermissionError:
            pass

        if not matches:
            return f"No files or folders matching {query!r} were found."

        return "\n".join(matches)

    @function_tool()
    async def inspect_path(
        self,
        context: RunContext,
        path: str,
    ) -> str:
        """Inspect basic information about a file or folder."""

        target = Path(path).expanduser()

        if not target.exists():
            raise ToolError(f"The path does not exist: {target}")

        try:
            stat = target.stat()
        except PermissionError as exc:
            raise ToolError(f"Permission denied while inspecting {target}.") from exc

        path_type = "directory" if target.is_dir() else "file"

        return (
            f"Path: {target}\n"
            f"Type: {path_type}\n"
            f"Size: {stat.st_size} bytes\n"
            f"Modified: {stat.st_mtime}"
        )

    @function_tool()
    async def create_folder(
        self,
        context: RunContext,
        path: str,
    ) -> str:
        """Create a folder, including any missing parent folders."""

        target = Path(path).expanduser()

        if target.exists():
            if target.is_dir():
                return f"The folder already exists: {target}"

            raise ToolError(f"A file already exists at this path: {target}")

        try:
            target.mkdir(parents=True, exist_ok=False)
        except PermissionError as exc:
            raise ToolError(f"Permission denied while creating {target}.") from exc

        return f"Created folder: {target}"

    @function_tool()
    async def create_file(
        self,
        context: RunContext,
        path: str,
        content: str = "",
    ) -> str:
        """Create a text file with the supplied content."""

        target = Path(path).expanduser()

        if target.exists():
            raise ToolError(f"A file or folder already exists at: {target}")

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except PermissionError as exc:
            raise ToolError(f"Permission denied while creating {target}.") from exc

        return f"Created file: {target}"

    @function_tool()
    async def rename_path(
        self,
        context: RunContext,
        source: str,
        new_name: str,
    ) -> str:
        """Rename a file or folder within its current directory."""

        source_path = Path(source).expanduser()
        new_name = new_name.strip()

        if not source_path.exists():
            raise ToolError(f"The source does not exist: {source_path}")

        if not new_name:
            raise ToolError("The new name cannot be empty.")

        destination = source_path.parent / new_name

        if destination.exists():
            raise ToolError(f"A file or folder already exists at: {destination}")

        try:
            source_path.rename(destination)
        except PermissionError as exc:
            raise ToolError(f"Permission denied while renaming {source_path}.") from exc

        return f"Renamed {source_path} to {destination}"

    @function_tool()
    async def move_path(
        self,
        context: RunContext,
        source: str,
        destination: str,
    ) -> str:
        """Move a file or folder to another location."""

        source_path = Path(source).expanduser()
        destination_path = Path(destination).expanduser()

        if not source_path.exists():
            raise ToolError(f"The source does not exist: {source_path}")

        if destination_path.exists() and destination_path.is_dir():
            final_destination = destination_path / source_path.name
        else:
            final_destination = destination_path

        if final_destination.exists():
            raise ToolError(f"The destination already exists: {final_destination}")

        try:
            shutil.move(str(source_path), str(final_destination))
        except PermissionError as exc:
            raise ToolError(f"Permission denied while moving {source_path}.") from exc

        return f"Moved {source_path} to {final_destination}"

    @function_tool()
    async def copy_path(
        self,
        context: RunContext,
        source: str,
        destination: str,
    ) -> str:
        """Copy a file or folder to another location."""

        source_path = Path(source).expanduser()
        destination_path = Path(destination).expanduser()

        if not source_path.exists():
            raise ToolError(f"The source does not exist: {source_path}")

        if destination_path.exists() and destination_path.is_dir():
            final_destination = destination_path / source_path.name
        else:
            final_destination = destination_path

        if final_destination.exists():
            raise ToolError(f"The destination already exists: {final_destination}")

        try:
            if source_path.is_dir():
                shutil.copytree(source_path, final_destination)
            else:
                shutil.copy2(source_path, final_destination)
        except PermissionError as exc:
            raise ToolError(f"Permission denied while copying {source_path}.") from exc

        return f"Copied {source_path} to {final_destination}"

    @function_tool()
    async def move_to_trash(
        self,
        context: RunContext,
        path: str,
        user_confirmed: bool = False,
    ) -> dict[str, object]:
        """Move a file or folder to the macOS Trash, where it can be restored.

        Never deletes permanently. First call with user_confirmed false: the
        result describes the item. Tell the user exactly what will be moved to
        the Trash (and how many items a folder contains), ask for confirmation,
        and only call again with user_confirmed true after a clear yes.

        Args:
            path: Full path of the file or folder, e.g. ~/Downloads/old.pdf.
            user_confirmed: True only after the user clearly approved this item.
        """
        target = check_trashable(Path(path))
        details = {"path": str(target), **describe_path(target)}

        if not user_confirmed:
            return {"moved": False, "needs_confirmation": True, **details}

        heard = _latest_user_text(context)
        if heard is not None and not is_clear_approval(heard):
            raise ToolError(
                "The user's reply was not a clear yes, so nothing was moved. Ask again."
            )

        safe = str(target).replace("\\", "\\\\").replace('"', '\\"')
        try:
            result = _run_osascript(
                f'tell application "Finder" to delete POSIX file "{safe}"'
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError("Moving the item to the Trash timed out.") from exc

        if result.returncode != 0:
            raise ToolError(
                "Finder could not move it to the Trash. If macOS asked for "
                "permission to control Finder, allow it and try again. "
                f"Details: {result.stderr.strip()}"
            )
        if target.exists():
            raise ToolError("Finder reported success, but the item is still there.")

        return {"moved": True, "restorable": True, **details}

    @function_tool()
    async def open_path(
        self,
        context: RunContext,
        path: str,
        reveal_in_finder: bool = False,
    ) -> dict[str, object]:
        """Open a folder in Finder, or open a file in its usual app.

        Use this when the user says "open that folder" or "show me the file".
        With reveal_in_finder true, Finder opens the containing folder with the
        item selected instead. Programs and scripts are only revealed, never run.

        Args:
            path: Full path of the file or folder, e.g. ~/Documents/Yaadhamma.
            reveal_in_finder: True to show the item in Finder rather than open it.
        """
        target = Path(path).expanduser()
        if not target.exists():
            raise ToolError(f"Nothing exists at {target}.")

        runnable = target.suffix.lower() in _RUNNABLE_SUFFIXES
        reveal = reveal_in_finder or runnable
        args = ["-R", str(target)] if reveal else [str(target)]
        try:
            result = _run_open(args)
        except subprocess.TimeoutExpired as exc:
            raise ToolError("Opening that timed out.") from exc
        if result.returncode != 0:
            raise ToolError(result.stderr.strip() or "macOS could not open that.")

        opened = {
            "opened": True,
            "path": str(target),
            "shown_in_finder": reveal,
        }
        if runnable and not reveal_in_finder:
            opened["note"] = (
                "This is a program or script, so it was only shown in Finder."
            )
        return opened
