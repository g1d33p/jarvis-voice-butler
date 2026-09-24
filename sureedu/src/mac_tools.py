import subprocess
from datetime import datetime
from pathlib import Path

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

SCREENSHOT_DIR = Path.home() / ".sureedu" / "screenshots"
KEEP_SCREENSHOTS = 20
CLIPBOARD_MAX_CHARS = 4_000

_ACTIVE_APP_SCRIPT = """
tell application "System Events"
    set frontApp to first application process whose frontmost is true
    set appName to name of frontApp
    set winTitle to ""
    set titleStatus to "ok"
    try
        set winTitle to name of front window of frontApp
    on error
        set titleStatus to "unavailable"
    end try
end tell
return appName & linefeed & titleStatus & linefeed & winTitle
"""


def _run(
    command: list[str], *, timeout: float = 10, stdin: str | None = None
) -> subprocess.CompletedProcess:
    """Run a macOS command-line tool. Kept separate so tests can replace it."""
    return subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, input=stdin
    )


class MacTools:
    """Tools for interacting with native macOS applications."""

    @property
    def tools(self) -> list:
        return [
            self.list_running_apps,
            self.open_application,
            self.quit_application,
            self.get_active_app,
            self.read_clipboard,
            self.write_clipboard,
            self.capture_screen,
        ]

    @function_tool()
    async def list_running_apps(
        self,
        context: RunContext,
    ) -> str:
        """List applications currently running on the Mac."""

        try:
            result = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "System Events" to get name of every process whose background only is false',
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError("Checking running applications timed out.") from exc

        if result.returncode != 0:
            error = result.stderr.strip() or (
                "macOS could not list running applications."
            )
            raise ToolError(error)

        return result.stdout.strip()

    @function_tool()
    async def open_application(
        self,
        context: RunContext,
        application: str,
    ) -> str:
        """Open a native macOS application."""

        application = application.strip()

        if not application:
            raise ToolError("The application name cannot be empty.")

        try:
            result = subprocess.run(
                ["open", "-a", application],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"Opening {application!r} timed out.") from exc

        if result.returncode != 0:
            error = result.stderr.strip() or ("macOS could not open the application.")
            raise ToolError(f"Could not open {application!r}: {error}")

        return f"Opened {application!r}."

    @function_tool()
    async def quit_application(
        self,
        context: RunContext,
        application: str,
    ) -> str:
        """Quit a native macOS application."""

        application = application.strip()

        if not application:
            raise ToolError("The application name cannot be empty.")

        safe_application = application.replace('"', '\\"')

        try:
            result = subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'tell application "{safe_application}" to quit',
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"Quitting {application!r} timed out.") from exc

        if result.returncode != 0:
            error = result.stderr.strip() or ("macOS could not quit the application.")
            raise ToolError(f"Could not quit {application!r}: {error}")

        return f"Quit {application!r}."

    @function_tool()
    async def get_active_app(self, context: RunContext) -> dict[str, object]:
        """Find out which Mac application is in front and its window title.

        Use this when the user asks what they are looking at, which app is open,
        or refers to "this window" or "this app".
        """
        try:
            result = _run(["osascript", "-e", _ACTIVE_APP_SCRIPT])
        except subprocess.TimeoutExpired as exc:
            raise ToolError("Checking the active application timed out.") from exc
        if result.returncode != 0:
            raise ToolError(
                result.stderr.strip() or "macOS could not report the active app."
            )

        app, status, title = [*result.stdout.rstrip("\n").split("\n", 2), "", ""][:3]
        info: dict[str, object] = {"app": app.strip(), "window_title": title.strip()}
        if status.strip() != "ok":
            info["window_title"] = ""
            info["note"] = (
                "The window title is unavailable. The app may have no window, or "
                "Accessibility permission is needed in System Settings, Privacy "
                "and Security, Accessibility."
            )
        return info

    @function_tool()
    async def read_clipboard(self, context: RunContext) -> dict[str, object]:
        """Read the text currently on the Mac clipboard.

        Only use this when the user asks about what they copied. The clipboard
        can hold private information, so never read it on your own initiative
        and never repeat passwords or codes aloud.
        """
        try:
            result = _run(["pbpaste"])
        except subprocess.TimeoutExpired as exc:
            raise ToolError("Reading the clipboard timed out.") from exc
        if result.returncode != 0:
            raise ToolError("macOS could not read the clipboard.")

        text = result.stdout
        return {
            "text": text[:CLIPBOARD_MAX_CHARS],
            "length": len(text),
            "truncated": len(text) > CLIPBOARD_MAX_CHARS,
            "empty": not text.strip(),
        }

    @function_tool()
    async def write_clipboard(self, context: RunContext, text: str) -> str:
        """Copy text to the Mac clipboard so the user can paste it anywhere.

        This replaces whatever the user had copied before.

        Args:
            text: The text to put on the clipboard.
        """
        try:
            result = _run(["pbcopy"], stdin=text)
        except subprocess.TimeoutExpired as exc:
            raise ToolError("Writing to the clipboard timed out.") from exc
        if result.returncode != 0:
            raise ToolError("macOS could not write to the clipboard.")
        return f"Copied {len(text)} characters to the clipboard."

    @function_tool()
    async def capture_screen(self, context: RunContext) -> dict[str, object]:
        """Take a screenshot of the whole Mac screen and save it to a file.

        Returns the file path only; the image itself is not sent anywhere. Use
        this when the user asks for a screenshot of their screen. For the
        browser page, use the browser screenshot tool instead.
        """
        return capture_screen_to_file()


def capture_screen_to_file(directory: Path | None = None) -> dict[str, object]:
    """Save a full-screen screenshot and keep only the most recent ones."""
    directory = directory or SCREENSHOT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"screen-{datetime.now():%Y%m%d-%H%M%S-%f}.png"

    try:
        # -x: no camera sound.
        result = _run(["screencapture", "-x", str(path)], timeout=15)
    except subprocess.TimeoutExpired as exc:
        raise ToolError("Taking the screenshot timed out.") from exc

    if result.returncode != 0 or not path.exists() or path.stat().st_size == 0:
        raise ToolError(
            "The screenshot could not be saved. Screen Recording permission may "
            "be needed in System Settings, Privacy and Security, Screen Recording."
        )

    old = sorted(directory.glob("screen-*.png"))[:-KEEP_SCREENSHOTS]
    for stale in old:
        stale.unlink(missing_ok=True)

    return {"saved": True, "path": str(path), "bytes": path.stat().st_size}
