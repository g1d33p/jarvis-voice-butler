"""Menu-bar UI: the testable core.

Rendering needs macOS (src/ui_macos.py) and cannot run headless. Everything
here is pure: the state -> visual mapping, the menu actions, and the caption
rule. The daemon drives a UIController; ui_macos only draws it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

# Dark palette (Jeevan's choice): near-black base, violet-blue accent.
BASE = "#0B0B10"
ACCENT_FROM = "#4C3CE0"
ACCENT_TO = "#2A1F6B"


class UIState(Enum):
    IDLE = "idle"  # daemon up, mic closed (listening stopped)
    LISTENING = "listening"  # mic open, waiting for the wake word / him
    THINKING = "thinking"  # in conversation, neither side speaking
    SPEAKING = "speaking"  # she is speaking
    MUTED = "muted"  # detection stopped from the menu
    ERROR = "error"  # something needs his attention


@dataclass(frozen=True)
class VisualSpec:
    icon: str  # menu-bar icon name
    animation: str  # orb animation: glow | pulse | rotate | ripple | still
    note: str


VISUALS: dict[UIState, VisualSpec] = {
    UIState.IDLE: VisualSpec("idle", "still", "dim, waiting"),
    UIState.LISTENING: VisualSpec("listening", "pulse", "soft slow pulse"),
    UIState.THINKING: VisualSpec("thinking", "rotate", "gentle rotation"),
    UIState.SPEAKING: VisualSpec("speaking", "ripple", "ripple with output level"),
    UIState.MUTED: VisualSpec("muted", "still", "dim, crossed"),
    UIState.ERROR: VisualSpec("error", "still", "amber dot, steady"),
}


def map_wake_to_ui(
    wake_state: str,
    mic_open: bool,
    agent_speaking: bool,
    user_speaking: bool,
) -> UIState:
    """Daemon state -> menu-bar/orb state. Pure; the daemon polls it."""
    if wake_state == "muted":
        return UIState.MUTED
    if wake_state == "conversation":
        if agent_speaking:
            return UIState.SPEAKING
        if user_speaking:
            return UIState.LISTENING
        return UIState.THINKING
    return UIState.LISTENING if mic_open else UIState.IDLE


def control_path() -> Path:
    return Path.home() / ".yaadhamma" / "wake-control.json"


def read_control() -> dict:
    """The daemon polls this; the menu bar writes it. Never raises."""
    try:
        data = json.loads(control_path().read_text())
    except Exception:
        data = {}
    return {
        "muted": bool(data.get("muted", False)),
        "listening": bool(data.get("listening", True)),
    }


def write_control(muted: bool | None = None, listening: bool | None = None) -> None:
    """Merge flags into the control file. Never raises."""
    try:
        current = read_control()
        if muted is not None:
            current["muted"] = muted
        if listening is not None:
            current["listening"] = listening
        control_path().parent.mkdir(parents=True, exist_ok=True)
        control_path().write_text(json.dumps(current))
    except Exception:
        pass


# ------------------------------------------------------------ menu actions


def default_set_muted(muted: bool) -> None:
    write_control(muted=muted)


def default_set_listening(listening: bool) -> None:
    write_control(listening=listening)


JOB_LABELS = (
    "com.yaadhamma.learn",
    "com.yaadhamma.tidy",
    "com.yaadhamma.morning",
    "com.yaadhamma.weekly",
    "com.yaadhamma.digest",
)


def default_set_jobs_paused(paused: bool) -> str:
    """Pause/resume the background launchd jobs. Returns a status message."""
    if sys.platform != "darwin":
        return "background jobs need macOS"
    action = "unload" if paused else "load"
    agents = Path.home() / "Library" / "LaunchAgents"
    failed = []
    for label in JOB_LABELS:
        plist = agents / f"{label}.plist"
        if not plist.exists():
            continue
        result = subprocess.run(
            ["launchctl", action, "-w", str(plist)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            failed.append(label)
    if failed:
        return f"some jobs would not {action}: {', '.join(failed)}"
    return "background jobs paused" if paused else "background jobs resumed"


def default_today_cost() -> str:
    """Today's model spend, from the existing cost store. Never raises."""
    try:
        from costs import CostStore

        return f"${CostStore().today_usd():.2f} today"
    except Exception:
        return "cost unavailable"


def default_whatsapp_status() -> str:
    """Last WhatsApp self-check, from the existing health log. Never raises."""
    try:
        from whatsapp_health import HealthLog

        latest = HealthLog().latest()
    except Exception:
        latest = None
    if not latest:
        return "WhatsApp: no check yet"
    ok = latest.get("ok", False)
    return "WhatsApp: paired" if ok else "WhatsApp: needs attention"


def default_open_plans() -> None:
    plans = Path.home() / "Documents" / "Yaadhamma"
    plans.mkdir(parents=True, exist_ok=True)
    _reveal(plans)


def default_open_settings() -> None:
    env_local = Path(__file__).resolve().parents[1] / ".env.local"
    if not env_local.exists():
        env_local.write_text("# Yaadhamma settings (see .env.example)\n")
    _reveal(env_local)


def _reveal(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)


def default_quit(project_dir: Path) -> None:
    """Stop the launchd daemon and exit. Only the real menu calls this."""
    subprocess.run(
        [sys.executable, str(project_dir / "scripts" / "daemon_control.py"), "stop"],
        check=False,
    )
    raise SystemExit(0)


def default_toggle_session() -> str:
    return "session control is not wired up"


@dataclass
class Actions:
    """Every menu action, injectable for tests."""

    set_muted: Callable[[bool], None] = default_set_muted
    set_listening: Callable[[bool], None] = default_set_listening
    set_jobs_paused: Callable[[bool], str] = default_set_jobs_paused
    toggle_session: Callable[[], str] = default_toggle_session
    get_today_cost: Callable[[], str] = default_today_cost
    get_whatsapp_status: Callable[[], str] = default_whatsapp_status
    open_plans: Callable[[], None] = default_open_plans
    open_settings: Callable[[], None] = default_open_settings
    quit: Callable[[], None] = lambda: default_quit(Path(__file__).resolve().parents[1])


def format_session_line(info: dict) -> str:
    """Menu line for the current voice session. Never raises.

    info: {"active": bool, "elapsed_s": float, "cost_usd": float | None}.
    """
    try:
        if not info.get("active"):
            return "Session: none open"
        elapsed = float(info.get("elapsed_s", 0.0) or 0.0)
        minutes, seconds = divmod(int(max(0.0, elapsed)), 60)
        cost = info.get("cost_usd")
        cost_text = f"~${cost:.2f}" if isinstance(cost, (int, float)) else "cost n/a"
        return f"Session: {minutes}:{seconds:02d} · {cost_text}"
    except Exception:
        return "Session: n/a"


@dataclass
class UICaptions:
    enabled: bool = False


def default_session_info() -> dict:
    return {"active": False, "elapsed_s": 0.0, "cost_usd": None}


@dataclass
class UIController:
    """What the menu bar draws and what its items do."""

    actions: Actions = field(default_factory=Actions)
    captions: UICaptions = field(
        default_factory=lambda: UICaptions(
            enabled=os.environ.get("YAADHAMMA_UI_CAPTIONS", "off").strip().lower()
            not in {"", "off", "0", "no"}
        )
    )
    muted: bool = False
    listening: bool = True
    jobs_paused: bool = False
    caption: str = ""
    # Session facts for the menu (elapsed time, estimated cost). The daemon
    # injects the real provider; the default reports no session.
    session_info: Callable[[], dict] = default_session_info

    def toggle_session(self) -> str:
        """Orb click / menu item: start a session, or finish input in the
        open one (same as releasing the push-to-talk key)."""
        return self.actions.toggle_session()

    def toggle_mute(self) -> None:
        self.muted = not self.muted
        self.actions.set_muted(self.muted)

    def toggle_listening(self) -> None:
        self.listening = not self.listening
        self.actions.set_listening(self.listening)

    def toggle_jobs_paused(self) -> str:
        self.jobs_paused = not self.jobs_paused
        return self.actions.set_jobs_paused(self.jobs_paused)

    def note_caption(self, text: str) -> None:
        """Last utterance/reply, shown under the orb only when enabled."""
        self.caption = text if self.captions.enabled else ""

    def status_lines(self) -> dict[str, str]:
        return {
            "cost": f"Today's cost: {self.actions.get_today_cost()}",
            "whatsapp": self.actions.get_whatsapp_status(),
            "session": format_session_line(self.session_info()),
        }

    def menu_items(self) -> list[tuple[str, Callable[[], None]]]:
        """(title, action) in menu order. ui_macos renders these."""
        session_open = bool(self.session_info().get("active"))
        return [
            (
                "Finish input" if session_open else "Start session",
                self.toggle_session,
            ),
            ("session", lambda: None),
            (
                f"{'Stop' if self.listening else 'Start'} listening",
                self.toggle_listening,
            ),
            (f"{'Unmute' if self.muted else 'Mute'}", self.toggle_mute),
            (
                f"{'Resume' if self.jobs_paused else 'Pause'} background jobs",
                self.toggle_jobs_paused,
            ),
            ("cost", lambda: None),
            ("whatsapp", lambda: None),
            ("Open plans folder", self.actions.open_plans),
            ("Settings", self.actions.open_settings),
            ("Quit", self.actions.quit),
        ]
