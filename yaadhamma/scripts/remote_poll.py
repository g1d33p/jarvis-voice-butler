#!/usr/bin/env python3
"""Poll his own WhatsApp chats for commands, once (the schedule repeats it).

Usage:
    uv run scripts/remote_poll.py

Real WhatsApp reading/sending happens here; there are no side effects
beyond polling his own chats and replying in them.
"""

import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from remote import RemotePoller, real_client_factory, real_orchestrator_factory


async def main_async() -> int:
    poller = RemotePoller(
        client_factory=real_client_factory,
        orchestrator_factory=real_orchestrator_factory,
    )
    try:
        outcomes = await poller.poll_once(datetime.now())
    except Exception as exc:  # the poll must never crash the launchd job
        print(f"remote poll failed: {exc}")
        return 1
    for outcome in outcomes:
        print(outcome)
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
