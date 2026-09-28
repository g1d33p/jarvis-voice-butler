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
