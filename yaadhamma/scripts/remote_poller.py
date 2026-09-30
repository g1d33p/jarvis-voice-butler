#!/usr/bin/env python3
"""The resident WhatsApp poller: one process, one browser, polls every 2 min.

This replaces the old per-poll process (remote_poll.py on a 120-second
launchd timer), which opened a brand-new browser for every poll. Each fresh
launch made WhatsApp Web re-sync from scratch — the chat list visibly grew
18 -> 60 -> 135 over seconds, so commands could be missed — and each new
window risked stealing Jeevan's keyboard focus.

The poller opens one browser once and reuses it across polls. Between polls
it just sits on the WhatsApp tab: no navigation, no reload, no focus
requests (headless first; never_raise always for this profile).

The digest browser lock is held for the whole lifetime of that browser, so
the profile is never driven twice at once. A scheduled digest writes a
request marker first; the poller notices it between polls, parks its
browser and releases the lock, and reopens once the digest is done.

Usage:
    uv run scripts/remote_poller.py      # foreground, Ctrl+C to stop

On the Mac it runs as a launchd job (scripts/remote_schedule.py install);
stdout goes to ~/.yaadhamma/remote.log.

Real WhatsApp reading/sending happens here; there are no side effects
beyond polling his own chats and replying in them.
"""

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from remote import (
    RemotePoller,
    ResidentPoller,
    persistent_client_factory,
    real_orchestrator_factory,
)

log = logging.getLogger("yaadhamma.poller")


def rotate_old_log_once() -> None:
    """Rotate the old untimestamped remote.log aside exactly once.

    The previous per-poll regime wrote untimestamped lines to remote.log;
    the resident poller writes timestamped lines. On first start the old
    content moves to remote.log.1 so the new era starts clean. A marker
    file guards the once-ness: launchd KeepAlive restarts must not rotate
    again.

    Copy-then-truncate, not rename: launchd opens the log path once when
    the job spawns and keeps the descriptor (append mode). Renaming the
    path would send this generation's output to the renamed file;
    truncating via the path keeps the open descriptor writing to the
    fresh file.
    """
    remote_log = Path.home() / ".yaadhamma" / "remote.log"
    marker = remote_log.with_name("remote.log.rotated")
    rotated = remote_log.with_name("remote.log.1")
    try:
        if marker.exists() or rotated.exists():
            return
        remote_log.parent.mkdir(parents=True, exist_ok=True)
        if remote_log.exists():
            shutil.copyfile(remote_log, rotated)
            remote_log.write_text("")
        marker.touch()
    except OSError:
        pass  # best effort: logging must never fail startup


async def main_async() -> int:
    poller = RemotePoller(
        client_factory=persistent_client_factory,
        orchestrator_factory=real_orchestrator_factory,
    )
    resident = ResidentPoller(poller=poller)
    log.info("poller: resident poller started (one browser, every 2 minutes)")
    try:
        await resident.run_forever()
    finally:
        # Ctrl+C or a crash: park the browser and free the profile, so a
        # digest (or the next poller start) is never locked out.
        await resident.shutdown()
    return 0


def main() -> int:
    rotate_old_log_once()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [poller] %(message)s")
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        log.info("poller: stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
