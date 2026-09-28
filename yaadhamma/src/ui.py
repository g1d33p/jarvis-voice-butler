"""Menu-bar UI entry point: the wrapper that can never crash the daemon.

- YAADHAMMA_UI=off disables the UI entirely (headless daemon).
- Any failure importing or starting the macOS UI logs a warning and the
  daemon continues headless. A UI failure must never take the voice loop
  down with it.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable

log = logging.getLogger("yaadhamma.ui")


def ui_enabled() -> bool:
    return os.environ.get("YAADHAMMA_UI", "on").strip().lower() != "off"


def run_with_optional_ui(
    wake_loop: Callable[[], None], controller, state_provider=None
) -> None:
    """Run the wake loop; put the menu-bar UI on the main thread when it
    is available. Never raises: the voice loop always runs."""
    if not ui_enabled():
        log.info("menu-bar UI disabled (YAADHAMMA_UI=off); running headless")
        wake_loop()
        return
    try:
        from ui_macos import run_macos_ui
    except Exception as exc:
        log.warning("menu-bar UI unavailable (%s); running headless", exc)
        wake_loop()
        return
    try:
        thread = threading.Thread(target=wake_loop, name="wake-loop", daemon=True)
        thread.start()
        run_macos_ui(controller, state_provider or (lambda: None))
    except Exception as exc:
        log.warning("menu-bar UI failed (%s); continuing headless", exc)
        wake_loop()
