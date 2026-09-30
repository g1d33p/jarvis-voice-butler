"""Stage 1 (v3): one long-lived, invisible WhatsApp poller.

Covers: a single browser reused across polls; waiting for the chat list to
settle; the historical best chat count; refusing a partial list loudly
instead of acting on it; the digest profile never raising its window; lost
pairing reported loudly (health check, morning brief, daemon status);
cheap polling (a chat is opened only when its row shows unread); the digest
refusing a partial list; polling-hours configuration.
"""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

import config
from browser import BrowserManager
from remote import (
    DigestLock,
    RemotePoller,
    ResidentPoller,
    _match_self_chats,
    in_window,
)
from whatsapp import WhatsAppClient, WhatsAppError, WhatsAppNotPairedError
from whatsapp_health import (
    ChatCountLog,
    guarded_chat_list,
    partial_list_report,
    run_health_check,
)

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SELF = "19408438446"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("YAADHAMMA_REMOTE", "on")
    monkeypatch.setenv("YAADHAMMA_SELF_CHATS", SELF)
    monkeypatch.setenv("YAADHAMMA_REMOTE_HOURS", "0-24")
    monkeypatch.setenv("HOME", str(tmp_path))
    # ChatCountLog defaults to the real DB path (import-time constant), so
    # point it at scratch: tests must never touch ~/.yaadhamma.
    monkeypatch.setattr("whatsapp_health.DEFAULT_DB", tmp_path / "yaadhamma.db")


def _poller(**kwargs):
    kwargs.setdefault("orchestrator_factory", lambda: object())
    return RemotePoller(**kwargs)


class PollFakeClient:
    """A self chat with scripted rows, reads and pairing state."""

    def __init__(self, rows=None, paired=True):
        self._rows = rows if rows is not None else []
        self.paired = paired
        self.read = []
        self.sent = []
        self.closed = False
        self.find_result = "Jeevan (You)"

    async def list_chats_settled(self, timeout_s=20.0, poll_s=1.0):
        if not self.paired:
            raise WhatsAppNotPairedError("not paired")
        return self._rows, True

    async def read_messages(self, chat, limit=15, exact=False):
        self.read.append(chat)
        return {"chat": chat, "messages": []}

    async def find_chat(self, number):
        return self.find_result

    async def send_message(self, chat, text):
        self.sent.append((chat, text))
        return {"chat": chat, "verified": True}

    async def close(self):
        self.closed = True


def _self_row(name="Jeevan (You)", unread=0):
    return {"name": name, "unread": unread, "preview": "", "time": ""}


# ---------------------------------------------------------- browser reuse


async def test_one_browser_across_several_polls(tmp_path) -> None:
    """The resident poller opens one browser and reuses it across polls."""
    created = []

    async def factory():
        client = PollFakeClient([_self_row()])
        created.append(client)
        return client

    poller = _poller()
    resident = ResidentPoller(
        poller=poller,
        client_factory=factory,
        lock=DigestLock(tmp_path / "digest.lock"),
    )
    for _ in range(3):
        await resident.run_one_cycle(datetime(2026, 9, 29, 12, 0, 0))
    assert len(created) == 1
    assert created[0].closed is False


async def test_browser_relaunches_after_a_poll_error(tmp_path, monkeypatch) -> None:
    """A failed poll parks the browser; the next cycle opens a fresh one."""
    created = []

    class FlakyClient(PollFakeClient):
        async def list_chats_settled(self, timeout_s=20.0, poll_s=1.0):
            raise WhatsAppError("boom")

    async def factory():
        client = FlakyClient([_self_row()])
        created.append(client)
        return client

    async def ok_factory():
        client = PollFakeClient([_self_row()])
        created.append(client)
        return client

    poller = _poller()
    resident = ResidentPoller(
        poller=poller,
        client_factory=factory,
        lock=DigestLock(tmp_path / "digest.lock"),
    )
    await resident.run_one_cycle(datetime(2026, 9, 29, 12, 0, 0))
    assert created[0].closed is True  # parked after the error
    resident.client_factory = ok_factory
    await resident.run_one_cycle(datetime(2026, 9, 29, 12, 2, 0))
    assert len(created) == 2


async def test_poll_skipped_while_digest_holds_the_lock(tmp_path) -> None:
    """While a digest holds the lock, the poller waits and opens nothing."""
    digest_lock = DigestLock(tmp_path / "digest.lock")
    assert digest_lock.acquire()  # the digest is running
    created = []

    async def factory():
        created.append(1)
        return PollFakeClient([_self_row()])

    resident = ResidentPoller(
        poller=_poller(),
        client_factory=factory,
        lock=DigestLock(tmp_path / "digest.lock"),
    )
    try:
        delay = await resident.run_one_cycle(datetime(2026, 9, 29, 12, 0, 0))
    finally:
        digest_lock.release()
    assert delay == resident.parked_retry_s
    assert created == []


async def test_digest_request_hands_over_the_profile(tmp_path) -> None:
    """The resident holds the lock for its browser's lifetime; a digest's
    request makes it park and release, so the profile is never driven
    twice at once."""
    lock_path = tmp_path / "digest.lock"
    created = []

    async def factory():
        client = PollFakeClient([_self_row()])
        created.append(client)
        return client

    resident = ResidentPoller(
        poller=_poller(), client_factory=factory, lock=DigestLock(lock_path)
    )
    now = datetime(2026, 9, 29, 12, 0, 0)
    await resident.run_one_cycle(now)
    assert resident._client is not None
    assert resident.lock.held

    digest_lock = DigestLock(lock_path)
    digest_lock.request()
    assert not digest_lock.acquire()  # the resident still holds it

    await resident.run_one_cycle(now)  # sees the request, parks, releases
    assert resident._client is None
    assert not resident.lock.held
    assert digest_lock.acquire()  # handoff complete: the digest gets it
    digest_lock.clear_request()
    digest_lock.release()


async def test_sleep_between_polls_yields_to_a_request(tmp_path) -> None:
    """A digest that asks mid-sleep does not wait out the full 2 minutes."""
    lock_path = tmp_path / "digest.lock"
    created = []

    async def _new_client():
        client = PollFakeClient([_self_row()])
        created.append(client)
        return client

    resident = ResidentPoller(
        poller=_poller(), client_factory=_new_client, lock=DigestLock(lock_path)
    )
    await resident.run_one_cycle(datetime(2026, 9, 29, 12, 0, 0))
    assert resident._client is not None
    DigestLock(lock_path).request()
    await resident._sleep_between_polls(120)
    assert resident._client is None
    assert not resident.lock.held


async def test_shutdown_parks_and_releases(tmp_path) -> None:
    lock_path = tmp_path / "digest.lock"
    clients = []

    async def _new_client():
        client = PollFakeClient([_self_row()])
        clients.append(client)
        return client

    resident = ResidentPoller(
        poller=_poller(), client_factory=_new_client, lock=DigestLock(lock_path)
    )
    await resident.run_one_cycle(datetime(2026, 9, 29, 12, 0, 0))
    assert resident.lock.held
    await resident.shutdown()
    assert resident._client is None
    assert not resident.lock.held
    assert clients[0].closed is True
    # ... and never raises when there is nothing to shut down.
    await resident.shutdown()
    assert DigestLock(lock_path).acquire()  # the profile is free for others


def test_stale_and_dead_requests_are_ignored(tmp_path) -> None:
    import json
    import subprocess
    import time as time_mod

    lock = DigestLock(tmp_path / "d.lock")
    assert not lock.requested()  # no marker at all

    # A marker older than the TTL is stale (a crashed digest).
    lock.request_path.write_text(json.dumps({"at": time_mod.time() - 3600, "pid": 1}))
    assert not lock.requested()

    # A marker from a dead process is ignored even when fresh.
    proc = subprocess.Popen(["true"])
    dead_pid = proc.pid
    proc.wait()
    lock.request_path.write_text(json.dumps({"at": time_mod.time(), "pid": dead_pid}))
    assert not lock.requested()

    # A live requester counts.
    lock.request()
    assert lock.requested()
    lock.clear_request()
    assert not lock.requested()


def test_clear_request_keeps_a_foreign_marker(tmp_path) -> None:
    """clear_request only removes the marker this process wrote: digest A
    finishing must not cancel digest B's pending handoff."""
    import json
    import subprocess
    import time as time_mod

    lock = DigestLock(tmp_path / "d3.lock")
    proc = subprocess.Popen(["sleep", "30"])
    try:
        lock.request_path.write_text(
            json.dumps({"at": time_mod.time(), "pid": proc.pid})
        )
        assert lock.requested()  # foreign but live: a real request
        lock.clear_request()
        assert lock.requested()  # untouched: not our marker to remove
    finally:
        proc.terminate()
        proc.wait()
    # Our own marker is removed as before.
    lock.request()
    lock.clear_request()
    assert not lock.requested()


async def test_unpaired_parks_the_browser_and_backs_off(tmp_path) -> None:
    """Lost pairing parks the browser (profile free for re-pairing) and the
    cycle backs off instead of hammering every 2 minutes."""
    created = []

    async def factory():
        client = PollFakeClient(paired=False)
        created.append(client)
        return client

    health_db = tmp_path / "health.db"
    resident = ResidentPoller(
        poller=_poller(),
        client_factory=factory,
        lock=DigestLock(tmp_path / "digest.lock"),
        health_log_path=health_db,
    )
    delay = await resident.run_one_cycle(datetime(2026, 9, 29, 12, 0, 0))
    assert delay == resident.unpaired_retry_s
    assert created[0].closed is True

    # The failure is persisted where the morning brief and daemon status
    # look: a failed "paired" check in the health log.
    from whatsapp_health import HealthLog

    latest = HealthLog(path=health_db).latest()
    assert latest is not None
    paired = next(c for c in latest["checks"] if c["name"] == "paired")
    assert paired["ok"] is False

    # A second failure during the same outage is not recorded twice.
    before = health_db.stat().st_mtime_ns
    await resident.run_one_cycle(datetime(2026, 9, 29, 12, 16, 0))
    assert health_db.stat().st_mtime_ns == before


# ------------------------------------------------------------ settle+best


class ScriptedClient:
    """Drives the REAL WhatsAppClient.list_chats_settled with scripted data."""

    list_chats_settled = WhatsAppClient.list_chats_settled

    def __init__(self, counts, chats):
        self._counts = list(counts)
        self._chats = chats

    async def _require_login(self):
        pass

    async def list_chats(self, limit=30):
        n = self._counts[0]
        if len(self._counts) > 1:
            self._counts.pop(0)
        return [{"name": f"chat {i}"} for i in range(n)]

    async def list_all_chats(self, max_rounds=25):
        return self._chats


async def test_settle_waits_for_growth_then_returns_everything() -> None:
    """18 -> 60 -> 135 settles at 135: the full list, not the first glimpse."""
    chats = [_self_row(f"chat {i}") for i in range(135)]
    client = ScriptedClient([18, 60, 135, 135, 135], chats)
    listed, settled = await client.list_chats_settled(poll_s=0)
    assert settled is True
    assert len(listed) == 135
    # Five list_chats calls (one count scripted per call): the count was
    # genuinely re-polled until it held still twice, not eyeballed once.
    assert client._counts == [135]


async def test_unsettled_list_reports_not_settled() -> None:
    """A list still moving after the timeout reports settled=False."""
    chats = [_self_row(f"chat {i}") for i in range(40)]
    client = ScriptedClient([10, 20, 30, 40], chats)
    listed, settled = await client.list_chats_settled(timeout_s=0, poll_s=0)
    assert settled is False
    assert len(listed) == 40  # the best available, honestly flagged


async def test_best_count_only_grows(tmp_path) -> None:
    stats = ChatCountLog(tmp_path / "counts.db")
    assert stats.best() == 0
    assert stats.record(18) == 18
    assert stats.record(135) == 135
    assert stats.record(60) == 135  # a partial re-sync never lowers best
    assert stats.best() == 135


async def test_partial_list_is_refused_after_retry(monkeypatch) -> None:
    """Far below the historical best: wait, retry once, then refuse loudly."""
    monkeypatch.setattr("whatsapp_health.asyncio.sleep", _no_sleep)
    stats = ChatCountLog()
    assert stats.record(135) == 135
    rows = [_self_row(f"chat {i}") for i in range(18)]
    client = PollFakeClient(rows)
    chats, best, report = await guarded_chat_list(client, stats)
    assert chats == []
    assert best == 135
    assert report == "WhatsApp is still syncing (18 of 135 chats)"


async def test_partial_list_recovers_on_retry(monkeypatch) -> None:
    """If the retry sees the full list, the poll goes ahead."""
    monkeypatch.setattr("whatsapp_health.asyncio.sleep", _no_sleep)
    stats = ChatCountLog()
    stats.record(135)

    class RecoveringClient(PollFakeClient):
        def __init__(self):
            super().__init__([_self_row(f"chat {i}") for i in range(18)])
            self.calls = 0

        async def list_chats_settled(self, timeout_s=20.0, poll_s=1.0):
            self.calls += 1
            if self.calls == 1:
                return self._rows, True
            return [_self_row(f"chat {i}") for i in range(135)], True

    chats, best, report = await guarded_chat_list(RecoveringClient(), stats)
    assert report is None
    assert len(chats) == 135
    assert best == 135


async def test_partial_list_skips_the_poll_loudly(monkeypatch) -> None:
    """A poll on a partial list touches nothing and says why."""
    monkeypatch.setattr("whatsapp_health.asyncio.sleep", _no_sleep)
    ChatCountLog().record(135)
    client = PollFakeClient([_self_row(f"chat {i}") for i in range(18)])
    summary = await _poller().poll_with_client(client, datetime(2026, 9, 29, 12, 0, 0))
    assert "still syncing" in summary.note
    assert "poll skipped" in summary.note
    assert "18 of 135" in summary.note
    assert summary.commands_found == 0
    assert client.read == []


async def _no_sleep(delay):
    return None


def test_partial_list_report_threshold() -> None:
    assert partial_list_report(135, 135) is None
    assert partial_list_report(68, 135) is None  # just above half: usable
    assert partial_list_report(67, 135) == "WhatsApp is still syncing (67 of 135 chats)"
    assert partial_list_report(0, 0) is None  # no history yet: nothing to judge


# ------------------------------------------------------- cheap polling


async def test_chat_without_unread_is_never_opened() -> None:
    """A poll reads the chat list; a quiet self chat is not opened."""
    client = PollFakeClient([_self_row(unread=0)])
    poller = _poller()
    now = datetime(2026, 9, 29, 12, 0, 0)
    await poller.poll_with_client(client, now)  # first sight: primes
    assert client.read == ["Jeevan (You)"]
    summary = await poller.poll_with_client(client, now)
    assert client.read == ["Jeevan (You)"]  # no second open: nothing unread
    assert summary.self_chats_matched == 1
    assert summary.commands_found == 0


async def test_chat_with_unread_is_opened() -> None:
    client = PollFakeClient([_self_row(unread=0)])
    poller = _poller()
    now = datetime(2026, 9, 29, 12, 0, 0)
    await poller.poll_with_client(client, now)  # prime
    client._rows = [_self_row(unread=3)]
    await poller.poll_with_client(client, now)
    assert client.read == ["Jeevan (You)", "Jeevan (You)"]


async def test_missing_self_chat_falls_back_to_search() -> None:
    """Not in the settled list: one targeted resolve, then poll as normal."""
    client = PollFakeClient([])  # settled list has no self chat
    client.find_result = "Jeevan (You)"
    summary = await _poller().poll_with_client(client, datetime(2026, 9, 29, 12, 0, 0))
    assert client.read == ["Jeevan (You)"]  # resolved, then primed
    assert summary.self_chats_matched == 0
    assert summary.outcomes  # the first-poll priming line


async def test_unresolvable_self_chat_is_loud() -> None:
    """Neither the list nor search finds his chat: the outcome says so."""

    class LostClient(PollFakeClient):
        async def find_chat(self, number):
            raise WhatsAppError("no chat found")

    client = LostClient([])
    summary = await _poller().poll_with_client(client, datetime(2026, 9, 29, 12, 0, 0))
    assert summary.self_chats_matched == 0
    assert any("could not resolve self chat" in o for o in summary.outcomes)


def test_match_self_chats_dedupes_across_numbers() -> None:
    """One "(You)" chat is claimed by the first number only; the second
    number falls back to a targeted resolve instead of polling it twice."""
    rows = [_self_row("Jeevan (You)"), _self_row("Ravi")]
    matched = _match_self_chats(rows, [SELF, "919640520634"])
    assert matched[SELF]["name"] == "Jeevan (You)"
    assert "919640520634" not in matched


def test_digit_match_beats_you_marker() -> None:
    rows = [_self_row("+1 940 843 8446"), _self_row("Jeevan (You)")]
    matched = _match_self_chats(rows, [SELF])
    assert matched[SELF]["name"] == "+1 940 843 8446"


def test_no_match_when_nothing_fits() -> None:
    assert _match_self_chats([_self_row("Ravi")], [SELF]) == {}


# ------------------------------------------------- never raise the window


class _SpyPage:
    async def bring_to_front(self) -> None:  # pragma: no cover - spy only
        raise AssertionError("the digest profile must never take focus")


async def test_digest_profile_never_invokes_bring_to_front(monkeypatch) -> None:
    """The digest profile never asks the OS for window focus — proven by
    spying on the actual _bring_page_window_to_front invocation, not the
    policy boolean. The caller here deliberately forgets never_raise=True:
    the profile path alone must be enough."""
    import browser as browser_module

    calls = []

    async def spy(page):
        calls.append(page)

    monkeypatch.setattr(browser_module, "_bring_page_window_to_front", spy)
    mgr = BrowserManager(headless=False, profile_dir=browser_module.DIGEST_PROFILE_DIR)
    assert mgr._raise_window_allowed() is False
    await mgr._maybe_bring_to_front(_SpyPage())
    assert calls == []


async def test_default_browser_still_invokes_bring_to_front(monkeypatch) -> None:
    """Positive control: the voice/tool browsers keep raising their window."""
    import browser as browser_module

    calls = []

    async def spy(page):
        calls.append(page)

    monkeypatch.setattr(browser_module, "_bring_page_window_to_front", spy)
    page = _SpyPage()
    await BrowserManager(headless=False)._maybe_bring_to_front(page)
    assert calls == [page]


def test_headless_never_raises_its_window() -> None:
    assert BrowserManager(headless=True)._raise_window_allowed() is False


# ------------------------------------------------------- pairing loudly


async def test_health_check_reports_unpaired(tmp_path) -> None:
    report = await run_health_check(
        PollFakeClient(paired=False), stats=ChatCountLog(tmp_path / "c.db")
    )
    assert not report.ok
    assert report.failed[0].name == "paired"


async def test_health_check_has_settled_step(tmp_path) -> None:
    rows = [_self_row(f"chat {i}") for i in range(135)]
    report = await run_health_check(
        PollFakeClient(rows), stats=ChatCountLog(tmp_path / "c.db")
    )
    settled = next(c for c in report.checks if c.name == "chat list settled")
    assert settled.ok
    assert settled.detail == "135 chats, best 135"


async def test_poll_propagates_unpaired() -> None:
    """The resident poller catches it; poll_with_client lets it through."""
    with pytest.raises(WhatsAppNotPairedError):
        await _poller().poll_with_client(
            PollFakeClient(paired=False), datetime(2026, 9, 29, 12, 0, 0)
        )


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_daemon_status_pairing_lines() -> None:
    control = _load_script("daemon_control")
    assert control.format_whatsapp_pairing(None) == (
        "WhatsApp: no health check recorded yet."
    )
    assert control.format_whatsapp_pairing(
        {
            "time": "2026-09-29T12:00:00",
            "ok": True,
            "checks": [{"name": "paired", "ok": True, "detail": ""}],
        }
    ).startswith("WhatsApp: paired")
    unpaired = control.format_whatsapp_pairing(
        {
            "time": "2026-09-29T12:00:00",
            "ok": False,
            "checks": [{"name": "paired", "ok": False, "detail": "not paired"}],
        }
    )
    assert "NOT PAIRED" in unpaired
    assert "re-paired" in unpaired


def test_morning_brief_warns_about_pairing(monkeypatch) -> None:
    import digest

    class FakeLog:
        def latest(self):
            return {
                "time": "2026-09-29T12:00:00",
                "ok": False,
                "checks": [{"name": "paired", "ok": False, "detail": "not paired"}],
            }

    monkeypatch.setattr("whatsapp_health.HealthLog", lambda *a, **k: FakeLog())
    warning = digest._health_warning()
    assert "not paired" in warning.lower()
    assert "Linked devices" in warning


# ------------------------------------------------------- digest guard


async def test_digest_refuses_a_partial_list(tmp_path, monkeypatch) -> None:
    """A digest never summarises a fraction of his chats as all of them."""
    from digest import DigestStore, run_digest

    monkeypatch.setattr(config, "WHATSAPP_WATCHLIST", ["SC1"])
    monkeypatch.setattr("whatsapp_health.asyncio.sleep", _no_sleep)
    ChatCountLog().record(135)

    class FakeBrain:
        async def generate(self, model, messages, tools, **kwargs):
            raise AssertionError("the brain must not run on a partial list")

    rows = [_self_row("SC1-Executives", unread=2)] + [
        _self_row(f"chat {i}") for i in range(17)
    ]
    result = await run_digest(
        PollFakeClient(rows), FakeBrain(), DigestStore(tmp_path / "db.sqlite")
    )
    assert result.status == "failed"
    assert "still syncing" in result.error


# ------------------------------------------------------- hours config


def test_in_window_overnight_wrap() -> None:
    assert in_window(datetime(2026, 9, 29, 23, 0), (22, 6)) is True
    assert in_window(datetime(2026, 9, 29, 3, 0), (22, 6)) is True
    assert in_window(datetime(2026, 9, 29, 12, 0), (22, 6)) is False


def test_parse_hours_defaults_and_typos(monkeypatch) -> None:
    # No env var: Stage 2 polls all day by default.
    monkeypatch.delenv("YAADHAMMA_REMOTE_HOURS", raising=False)
    assert config.remote_settings()["hours"] == (0, 24)
    monkeypatch.setenv("YAADHAMMA_REMOTE_HOURS", "8-23")
    assert config.remote_settings()["hours"] == (8, 23)
    monkeypatch.setenv("YAADHAMMA_REMOTE_HOURS", "nonsense")
    assert config.remote_settings()["hours"] == (0, 24)
    monkeypatch.setenv("YAADHAMMA_REMOTE_HOURS", "9-9")
    assert config.remote_settings()["hours"] == (0, 24)


def test_poller_headless_defaults_on() -> None:
    assert config.remote_settings()["poller_headless"] is True


def test_remote_log_rotated_once(monkeypatch, tmp_path) -> None:
    """Stage 2: the old untimestamped remote.log moves aside exactly once.
    Copy-then-truncate (not rename): launchd keeps the log file open, so
    the path must keep pointing at the fresh file."""
    monkeypatch.setenv("HOME", str(tmp_path))
    script = _load_script("remote_poller")
    log_dir = tmp_path / ".yaadhamma"
    log_dir.mkdir(parents=True)
    old = log_dir / "remote.log"
    old.write_text("untimestamped old line\n")

    script.rotate_old_log_once()
    assert old.read_text() == ""  # truncated in place, same inode
    assert (log_dir / "remote.log.1").read_text() == "untimestamped old line\n"
    assert (log_dir / "remote.log.rotated").exists()

    # A second start (e.g. launchd KeepAlive restart) rotates nothing.
    old.write_text("2026-09-30T01:00:00 [poller] new line\n")
    script.rotate_old_log_once()
    assert old.read_text() == "2026-09-30T01:00:00 [poller] new line\n"
    assert (log_dir / "remote.log.1").read_text() == "untimestamped old line\n"


def test_remote_log_rotation_without_old_log(monkeypatch, tmp_path) -> None:
    """Stage 2: rotation is a no-op when there is no old log, but the
    marker is still written so the check stays once-only."""
    monkeypatch.setenv("HOME", str(tmp_path))
    script = _load_script("remote_poller")
    script.rotate_old_log_once()
    log_dir = tmp_path / ".yaadhamma"
    assert not (log_dir / "remote.log").exists()
    assert not (log_dir / "remote.log.1").exists()
    assert (log_dir / "remote.log.rotated").exists()
