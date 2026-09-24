import subprocess

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError


class MacTools:
    """Tools for interacting with native macOS applications."""

    @property
    def tools(self) -> list:
        return [
            self.list_running_apps,
            self.open_application,
            self.quit_application,
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
            raise ToolError(
                "Checking running applications timed out."
            ) from exc

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
            raise ToolError(
                f"Opening {application!r} timed out."
            ) from exc

        if result.returncode != 0:
            error = result.stderr.strip() or (
                "macOS could not open the application."
            )
            raise ToolError(
                f"Could not open {application!r}: {error}"
            )

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
            raise ToolError(
                f"Quitting {application!r} timed out."
            ) from exc

        if result.returncode != 0:
            error = result.stderr.strip() or (
                "macOS could not quit the application."
            )
            raise ToolError(
                f"Could not quit {application!r}: {error}"
            )

        return f"Quit {application!r}."