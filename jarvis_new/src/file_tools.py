from pathlib import Path
import shutil

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError


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
            raise ToolError(
                f"Permission denied while reading {target}."
            ) from exc

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
            raise ToolError(
                f"Permission denied while inspecting {target}."
            ) from exc

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

            raise ToolError(
                f"A file already exists at this path: {target}"
            )

        try:
            target.mkdir(parents=True, exist_ok=False)
        except PermissionError as exc:
            raise ToolError(
                f"Permission denied while creating {target}."
            ) from exc

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
            raise ToolError(
                f"A file or folder already exists at: {target}"
            )

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except PermissionError as exc:
            raise ToolError(
                f"Permission denied while creating {target}."
            ) from exc

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
            raise ToolError(
                f"A file or folder already exists at: {destination}"
            )

        try:
            source_path.rename(destination)
        except PermissionError as exc:
            raise ToolError(
                f"Permission denied while renaming {source_path}."
            ) from exc

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
            raise ToolError(
                f"The destination already exists: {final_destination}"
            )

        try:
            shutil.move(str(source_path), str(final_destination))
        except PermissionError as exc:
            raise ToolError(
                f"Permission denied while moving {source_path}."
            ) from exc

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
            raise ToolError(
                f"The destination already exists: {final_destination}"
            )

        try:
            if source_path.is_dir():
                shutil.copytree(source_path, final_destination)
            else:
                shutil.copy2(source_path, final_destination)
        except PermissionError as exc:
            raise ToolError(
                f"Permission denied while copying {source_path}."
            ) from exc

        return f"Copied {source_path} to {final_destination}"