from types import SimpleNamespace

from livekit.agents.llm import ToolError

import observation
from observation import Observation, ObservationTools, diff, observe


def _obs(app="Finder", title="Desktop", browser=None) -> Observation:
    return Observation(at="10:00:00", app=app, window_title=title, browser=browser)


def _browser(*tabs, active=0) -> dict:
    listed = [
        {"number": i + 1, "title": title, "url": url}
        for i, (title, url) in enumerate(tabs)
    ]
    return {"tab_count": len(listed), "active_tab": listed[active], "tabs": listed}


class _FakeBrowser:
    def __init__(self, snapshot=None) -> None:
        self.snap = snapshot

    async def snapshot(self):
        return self.snap


def test_diff_reports_app_and_tab_changes() -> None:
    before = _obs(browser=_browser(("WhatsApp", "https://web.whatsapp.com/")))
    after = _obs(
        app="Yaadhamma's browser",
        title="YouTube",
        browser=_browser(
            ("WhatsApp", "https://web.whatsapp.com/"),
            ("YouTube", "https://www.youtube.com/"),
            active=1,
        ),
    )

    changes = diff(before, after)

    assert "Front app changed from Finder to Yaadhamma's browser." in changes
    assert "Page opened: YouTube." in changes
    assert "Active tab is now YouTube." in changes


def test_diff_reports_browser_open_and_close() -> None:
    open_browser = _obs(browser=_browser(("WhatsApp", "https://web.whatsapp.com/")))

    assert diff(_obs(), open_browser) == ["Yaadhamma's browser was opened."]
    assert diff(open_browser, _obs()) == ["Yaadhamma's browser was closed."]


def test_no_changes_means_empty_diff() -> None:
    assert diff(_obs(), _obs()) == []


async def test_observe_survives_missing_permission() -> None:
    def no_permission():
        raise ToolError("Accessibility permission is needed.")

    obs = await observe(_FakeBrowser(), read_app=no_permission)

    assert obs.app == ""
    assert "Accessibility" in obs.note
    assert obs.to_dict()["yaadhamma_browser"] == "not open"


async def test_observe_state_tracks_changes_between_looks(monkeypatch) -> None:
    apps = iter(
        [
            {"app": "Finder", "window_title": "Desktop"},
            {"app": "Mail", "window_title": "Inbox"},
        ]
    )
    monkeypatch.setattr(observation, "read_active_app", lambda: next(apps))
    tools = ObservationTools(_FakeBrowser())

    first = await tools.observe_state(SimpleNamespace())
    second = await tools.observe_state(SimpleNamespace())

    assert first["front_app"] == "Finder"
    assert first["changes_since_last_look"].startswith("This is the first look")
    assert second["changes_since_last_look"] == [
        "Front app changed from Finder to Mail."
    ]


def test_observation_tool_is_registered() -> None:
    ids = [tool.id for tool in ObservationTools(_FakeBrowser()).tools]
    assert ids == ["observe_state"]
