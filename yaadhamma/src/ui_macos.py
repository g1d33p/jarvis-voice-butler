"""macOS menu-bar item and floating orb (PyObjC/rumps).

This module only runs on macOS. Importing it anywhere else raises
ImportError, which src/ui.py treats as "run headless". Rendering cannot be
tested without a Mac; the state mapping and menu actions it draws live in
src/ui_state.py and are fully tested.

Technology note (for docs/BUILD_REPORT.md): rumps + a borderless NSWindow
with Core Animation layers — no web stack, no extra runtime, no network
listener. The orb is click-through except on its own area, draggable, and
remembers its position in ~/.yaadhamma/orb-position.json.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("yaadhamma.ui.macos")

ORB_POSITION = Path.home() / ".yaadhamma" / "orb-position.json"
ORB_SIZE = 72  # points


def _load_orb_position() -> tuple[float, float] | None:
    try:
        data = json.loads(ORB_POSITION.read_text())
        return float(data["x"]), float(data["y"])
    except Exception:
        return None


def _save_orb_position(x: float, y: float) -> None:
    try:
        ORB_POSITION.parent.mkdir(parents=True, exist_ok=True)
        ORB_POSITION.write_text(json.dumps({"x": x, "y": y}))
    except Exception:
        pass


def run_macos_ui(controller, state_provider) -> None:
    """Run the menu bar (and orb) on the calling thread. Blocking."""
    # Note: pyobjc exposes CoreAnimation as Quartz.QuartzCore; there is no
    # top-level QuartzCore module, so a bare `import QuartzCore` fails.
    import rumps
    from AppKit import (
        NSBackingStoreBuffered,
        NSColor,
        NSMakeRect,
        NSScreen,
        NSView,
        NSWindow,
        NSWindowStyleMaskBorderless,
    )
    from Foundation import NSTimer
    from Quartz import QuartzCore

    from ui_state import BASE, VISUALS, UIState

    def hex_color(value: str, alpha: float = 1.0) -> NSColor:
        value = value.lstrip("#")
        r, g, b = (int(value[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, alpha)

    class OrbWindow(NSWindow):
        def init(self):
            frame = NSMakeRect(0, 0, ORB_SIZE, ORB_SIZE)
            self = self.initWithContentRect_styleMask_backing_defer_(
                frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
            )
            self.setOpaque_(False)
            self.setBackgroundColor_(NSColor.clearColor())
            self.setLevel_(3)  # floating, always on top
            self.setIgnoresMouseEvents_(False)
            self.setMovable_(True)
            self.setHasShadow_(False)
            content = NSView.alloc().initWithFrame_(frame)
            content.setWantsLayer_(True)
            layer = QuartzCore.CALayer.layer()
            layer.setCornerRadius_(ORB_SIZE / 2)
            layer.setBackgroundColor_(hex_color(BASE, 0.82).CGColor())
            layer.setBorderWidth_(1.5)
            layer.setBorderColor_(hex_color("#4C3CE0", 0.9).CGColor())
            content.setLayer_(layer)
            self.setContentView_(content)
            self._orb_layer = layer
            return self

        def set_state(self, state: UIState, level: float) -> None:
            spec = VISUALS[state]
            # Animations are deliberately gentle: pulse/rotate/ripple map to
            # Core Animation on opacity/transform; idle and muted stay still.
            layer = self._orb_layer
            layer.removeAllAnimations()
            if spec.animation == "pulse":
                anim = QuartzCore.CABasicAnimation.animationWithKeyPath_("opacity")
                anim.setFromValue_(0.55)
                anim.setToValue_(0.95)
                anim.setDuration_(1.6)
                anim.setAutoreverses_(True)
                anim.setRepeatCount_(1e9)
                layer.addAnimation_forKey_(anim, "pulse")
            elif spec.animation == "rotate":
                anim = QuartzCore.CABasicAnimation.animationWithKeyPath_(
                    "transform.rotation"
                )
                anim.setFromValue_(0)
                anim.setToValue_(6.2832)
                anim.setDuration_(4.0)
                anim.setRepeatCount_(1e9)
                layer.addAnimation_forKey_(anim, "rotate")
            elif spec.animation == "ripple":
                scale = 1.0 + min(0.25, level * 0.25)
                layer.setTransform_(
                    QuartzCore.CATransform3DMakeScale(scale, scale, 1.0)
                )

    class YaadhammaBar(rumps.App):
        def __init__(self):
            super().__init__("Yaadhamma", quit_button=None)
            self.controller = controller
            self._orb = OrbWindow.alloc().init()
            pos = _load_orb_position()
            if pos is None:
                screen = NSScreen.mainScreen().frame()
                pos = (screen.size.width - ORB_SIZE - 40, 120)
            self._orb.setFrameOrigin_(pos)
            self._orb.orderFrontRegardless()
            self._caption_item = None
            self._rebuild_menu()
            self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0, self, "refresh:", None, True
            )

        def _rebuild_menu(self):
            self.menu.clear()
            lines = self.controller.status_lines()
            for title, action in self.controller.menu_items():
                if title == "cost":
                    item = rumps.MenuItem(lines["cost"])
                    item.setcallback(lambda _: self.refresh_(None))
                    self.menu.add(item)
                elif title == "whatsapp":
                    item = rumps.MenuItem(lines["whatsapp"])
                    item.setcallback(lambda _: self.refresh_(None))
                    self.menu.add(item)
                elif title == "Quit":
                    self.menu.add(
                        rumps.MenuItem(title, callback=lambda _: self._quit_app())
                    )
                else:
                    self.menu.add(
                        rumps.MenuItem(title, callback=lambda _, a=action: a())
                    )
            if self.controller.captions.enabled:
                self._caption_item = rumps.MenuItem("")
                self.menu.add(self._caption_item)
            self._menu_flags = (
                self.controller.listening,
                self.controller.muted,
                self.controller.jobs_paused,
            )

        def _quit_app(self):
            _save_orb_position(*self._orb.frame().origin)
            self.controller.actions.quit()

        def refresh_(self, _timer):
            try:
                state = state_provider()
            except Exception:
                state = UIState.ERROR
            # Toggle titles go stale after a tap; rebuild when flags change.
            flags = (
                self.controller.listening,
                self.controller.muted,
                self.controller.jobs_paused,
            )
            if flags != self._menu_flags:
                self._rebuild_menu()
            spec = VISUALS.get(state, VISUALS[UIState.ERROR])
            self.title = {"idle": "○", "listening": "◉"}.get(spec.icon, "●")
            level = 0.0  # output level drives the ripple; wired in stage 4+
            self._orb.set_state(state, level)
            lines = self.controller.status_lines()
            for item in self.menu.values():
                if item.title.startswith("Today's cost"):
                    item.title = lines["cost"]
                elif item.title.startswith("WhatsApp"):
                    item.title = lines["whatsapp"]
            if self._caption_item is not None:
                self._caption_item.title = self.controller.caption

    # rumps takes the main thread; the daemon's wake loop already runs on a
    # background thread (see src/ui.py).
    YaadhammaBar().run()
