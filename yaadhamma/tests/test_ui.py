"""Tests for the menu-bar UI's testable core (src/ui_state.py, src/ui.py).

Rendering itself needs macOS and cannot be tested headless; the state ->
visual mapping, the menu actions, and the never-crash-the-daemon wrapper
can, and are tested here.
"""

from ui import run_with_optional_ui, ui_enabled
from ui_state import (
    ACCENT_FROM,
    ACCENT_TO,
    BASE,
    VISUALS,
    Actions,
    UICaptions,
    UIController,
    UIState,
    map_wake_to_ui,
)


def test_visuals_cover_every_state() -> None:
    assert set(VISUALS) == set(UIState)
    for spec in VISUALS.values():
        assert spec.animation and spec.icon


def test_dark_palette() -> None:
    assert BASE == "#0B0B10"
    assert ACCENT_FROM == "#4C3CE0"
    assert ACCENT_TO == "#2A1F6B"


def test_captions_off_by_default() -> None:
    assert UICaptions().enabled is False


def test_map_idle_with_mic_open_is_listening() -> None:
    assert (
        map_wake_to_ui("idle", mic_open=True, agent_speaking=False, user_speaking=False)
        is UIState.LISTENING
    )


def test_map_idle_with_mic_closed_is_idle() -> None:
    assert (
        map_wake_to_ui(
            "idle", mic_open=False, agent_speaking=False, user_speaking=False
        )
        is UIState.IDLE
    )


def test_map_conversation_states() -> None:
    assert (
        map_wake_to_ui("conversation", True, agent_speaking=True, user_speaking=False)
        is UIState.SPEAKING
    )
    assert (
        map_wake_to_ui("conversation", True, agent_speaking=False, user_speaking=True)
        is UIState.LISTENING
    )
    assert (
        map_wake_to_ui("conversation", True, agent_speaking=False, user_speaking=False)
        is UIState.THINKING
    )


def test_map_muted() -> None:
    assert (
        map_wake_to_ui(
            "muted", mic_open=False, agent_speaking=False, user_speaking=False
        )
        is UIState.MUTED
    )


def _fake_actions(calls: list):
    return Actions(
        set_muted=lambda m: calls.append(("muted", m)),
        set_listening=lambda on: calls.append(("listening", on)),
        set_jobs_paused=lambda p: calls.append(("jobs", p)),
        get_today_cost=lambda: "$1.23 today",
        get_whatsapp_status=lambda: "WhatsApp: paired",
        open_plans=lambda: calls.append(("plans",)),
        open_settings=lambda: calls.append(("settings",)),
        quit=lambda: calls.append(("quit",)),
    )


def test_controller_mute_toggle() -> None:
    calls: list = []
    ctl = UIController(actions=_fake_actions(calls))
    ctl.toggle_mute()
    assert calls == [("muted", True)]
    assert ctl.muted is True
    ctl.toggle_mute()
    assert calls[-1] == ("muted", False)


def test_controller_listening_toggle() -> None:
    calls: list = []
    ctl = UIController(actions=_fake_actions(calls))
    ctl.toggle_listening()
    assert calls == [("listening", False)]
    assert ctl.listening is False


def test_controller_jobs_toggle() -> None:
    calls: list = []
    ctl = UIController(actions=_fake_actions(calls))
    ctl.toggle_jobs_paused()
    assert calls == [("jobs", True)]
    assert ctl.jobs_paused is True


def test_controller_status_lines() -> None:
    calls: list = []
    ctl = UIController(actions=_fake_actions(calls))
    lines = ctl.status_lines()
    assert lines["cost"] == "Today's cost: $1.23 today"
    assert lines["whatsapp"] == "WhatsApp: paired"


def test_controller_caption_respects_setting() -> None:
    calls: list = []
    ctl = UIController(actions=_fake_actions(calls), captions=UICaptions(enabled=False))
    ctl.note_caption("hello")
    assert ctl.caption == ""
    ctl2 = UIController(actions=_fake_actions(calls), captions=UICaptions(enabled=True))
    ctl2.note_caption("hello")
    assert ctl2.caption == "hello"


def test_menu_items_cover_the_brief() -> None:
    calls: list = []
    ctl = UIController(actions=_fake_actions(calls))
    titles = [title for title, _ in ctl.menu_items()]
    for expected in (
        "listening",
        "mute",
        "pause background jobs",
        "cost",
        "whatsapp",
        "plans",
        "settings",
        "quit",
    ):
        assert any(expected in t.lower() for t in titles), titles


def test_ui_disabled_runs_wake_loop_directly(monkeypatch) -> None:
    monkeypatch.setenv("YAADHAMMA_UI", "off")
    ran = []
    run_with_optional_ui(
        lambda: ran.append("wake"), UIController(actions=_fake_actions([]))
    )
    assert ran == ["wake"]
    assert ui_enabled() is False


def test_ui_import_failure_falls_back_to_headless(monkeypatch) -> None:
    monkeypatch.setenv("YAADHAMMA_UI", "on")
    monkeypatch.setitem(__import__("sys").modules, "ui_macos", None)
    ran = []
    # A broken/absent ui_macos must never take the voice loop down.
    run_with_optional_ui(
        lambda: ran.append("wake"), UIController(actions=_fake_actions([]))
    )
    assert ran == ["wake"]


# --------------------------------- v2 Stage 2: orb click + session display

from ui_state import default_session_info, format_session_line  # noqa: E402


class RecordingActions(Actions):
    def __init__(self) -> None:
        super().__init__(
            set_muted=lambda muted: calls.append(("set_muted", muted)),
            set_listening=lambda listening: calls.append(("set_listening", listening)),
            set_jobs_paused=lambda paused: (
                calls.append(("set_jobs_paused", paused)) or "paused"
            ),
            toggle_session=lambda: calls.append(("toggle_session",)) or "toggled",
            get_today_cost=lambda: "$0.00",
            get_whatsapp_status=lambda: "WhatsApp: paired",
            open_plans=lambda: calls.append(("open_plans",)),
            open_settings=lambda: calls.append(("open_settings",)),
            quit=lambda: calls.append(("quit",)),
        )


calls: list = []


def _recording_controller(**kwargs) -> UIController:
    calls.clear()
    return UIController(actions=RecordingActions(), **kwargs)


def test_construction_calls_no_actions() -> None:
    """The startup bug: building the controller must never change detection
    state — no action may fire at construction."""
    _recording_controller()
    assert calls == []


def test_menu_offers_start_session_when_idle() -> None:
    controller = _recording_controller()
    titles = [title for title, _ in controller.menu_items()]
    assert titles[0] == "Start session"
    assert titles[1] == "session"  # the live elapsed/cost line from status_lines()


def test_menu_offers_finish_input_when_session_open() -> None:
    controller = _recording_controller(
        session_info=lambda: {"active": True, "elapsed_s": 65.0, "cost_usd": 0.03}
    )
    titles = [title for title, _ in controller.menu_items()]
    assert titles[0] == "Finish input"


def test_menu_session_item_triggers_toggle() -> None:
    controller = _recording_controller()
    _, action = controller.menu_items()[0]
    action()
    assert ("toggle_session",) in calls


def test_toggle_session_returns_the_action_message() -> None:
    controller = _recording_controller()
    assert controller.toggle_session() == "toggled"


def test_status_lines_show_no_session_when_idle() -> None:
    controller = _recording_controller()
    assert controller.status_lines()["session"] == "Session: none open"


def test_status_lines_show_session_elapsed_and_cost() -> None:
    controller = _recording_controller(
        session_info=lambda: {"active": True, "elapsed_s": 192.0, "cost_usd": 0.041}
    )
    assert controller.status_lines()["session"] == "Session: 3:12 · ~$0.04"


def test_format_session_line_without_cost() -> None:
    assert (
        format_session_line({"active": True, "elapsed_s": 9.0, "cost_usd": None})
        == "Session: 0:09 · cost n/a"
    )


def test_format_session_line_never_raises() -> None:
    assert format_session_line({}) == "Session: none open"
    assert format_session_line({"active": True}) == "Session: 0:00 · cost n/a"


def test_default_session_info_is_idle() -> None:
    assert default_session_info()["active"] is False
