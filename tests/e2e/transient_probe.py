"""GTK3 transient chain for window_set_desktop guard tests.

``python3 transient_probe.py [--modal] [--nested]`` maps a normal parent window
and a dialog transient for it (``set_transient_for``); ``--modal`` marks the
dialog modal. ``--nested`` adds a nonmodal grandchild transient for the dialog.
Tests read the resulting KWin relationships back instead of assuming that the
toolkit tells the compositor every hint.
"""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

PARENT_TITLE = "Transient Probe Parent"
DIALOG_TITLE = "Transient Probe Dialog"
GRANDCHILD_TITLE = "Transient Probe Grandchild"


class TransientProbeApplication(Gtk.Application):
    """Shows the parent, its transient dialog and an optional grandchild."""

    def __init__(self, modal: bool, nested: bool = False) -> None:
        super().__init__(application_id="io.github.kwinmcp.TransientProbe")
        self._modal = modal
        self._nested = nested
        self._parent: Gtk.ApplicationWindow | None = None
        self._dialog: Gtk.Window | None = None
        self._grandchild: Gtk.Window | None = None

    def do_activate(self) -> None:
        if self._parent is not None:
            return
        parent = Gtk.ApplicationWindow(application=self, title=PARENT_TITLE)
        parent.set_default_size(480, 320)
        parent.show_all()
        dialog = Gtk.Window(title=DIALOG_TITLE)
        dialog.set_default_size(240, 120)
        dialog.set_transient_for(parent)
        dialog.set_modal(self._modal)
        self.add_window(dialog)
        dialog.show_all()
        self._parent = parent
        self._dialog = dialog
        if self._nested:
            grandchild = Gtk.Window(title=GRANDCHILD_TITLE)
            grandchild.set_default_size(200, 100)
            grandchild.set_transient_for(dialog)
            grandchild.set_modal(False)
            self.add_window(grandchild)
            grandchild.show_all()
            self._grandchild = grandchild


def main() -> int:
    GLib.set_application_name("Transient Probe")
    application = TransientProbeApplication(
        modal="--modal" in sys.argv[1:], nested="--nested" in sys.argv[1:]
    )
    # Gtk.Application would reject the fixture options as unknown.
    return application.run(sys.argv[:1])


if __name__ == "__main__":
    raise SystemExit(main())
