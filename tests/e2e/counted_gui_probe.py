"""GUI probe variant with a persistent click counter for pointer-delivery tests.

``animation_status`` restarts on every click, so it cannot tell one delivered
click from two. ``animation_clicks`` only ever grows, which lets a test prove
that an input API call actually reached the GTK ``clicked`` handler.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gui_probe

Gtk = gui_probe.Gtk
CLICKS_PREFIX = "animation_clicks"


class CountedWindow(gui_probe.GuiProbeWindow):
    """Probe window that also counts Animation Target activations."""

    def __init__(self, application: Gtk.Application) -> None:
        self._animation_clicks = 0
        self._clicks_status: Gtk.Label | None = None
        super().__init__(application)
        clicks = gui_probe._named_label(f"{CLICKS_PREFIX}: 0", "status")
        root = self.get_child()
        root.pack_start(clicks, False, False, 0)
        root.reorder_child(clicks, 3)
        self._clicks_status = clicks

    def _on_animation_clicked(self, _button: Gtk.Button) -> None:
        self._animation_clicks += 1
        if self._clicks_status is not None:
            self._set_status(self._clicks_status, f"{CLICKS_PREFIX}: {self._animation_clicks}")
        super()._on_animation_clicked(_button)


class CountedApplication(gui_probe.GuiProbeApplication):
    """Application wrapper that creates the counting window."""

    def do_activate(self) -> None:
        if self._window is None:
            self._window = CountedWindow(self)
        self._window.show_all()
        self._window.present()


def main() -> int:
    """Run the counted probe until its window is closed."""
    return gui_probe.main(CountedApplication)


if __name__ == "__main__":
    raise SystemExit(main())
