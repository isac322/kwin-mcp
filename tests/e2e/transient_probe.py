"""GTK3 parent window with one transient dialog, for window_set_desktop guard tests.

``python3 transient_probe.py [--modal]`` maps a normal parent window and a dialog
that is transient for it (``set_transient_for``); ``--modal`` also marks the
dialog modal. Tests read the resulting KWin relationship back instead of
assuming it, since a toolkit may not tell the compositor every hint.
"""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

PARENT_TITLE = "Transient Probe Parent"
DIALOG_TITLE = "Transient Probe Dialog"


class TransientProbeApplication(Gtk.Application):
    """Shows the parent, then the dialog transient for it."""

    def __init__(self, modal: bool) -> None:
        super().__init__(application_id="io.github.kwinmcp.TransientProbe")
        self._modal = modal
        self._parent: Gtk.ApplicationWindow | None = None

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


def main() -> int:
    GLib.set_application_name("Transient Probe")
    application = TransientProbeApplication(modal="--modal" in sys.argv[1:])
    # Gtk.Application would reject --modal as an unknown option.
    return application.run(sys.argv[:1])


if __name__ == "__main__":
    raise SystemExit(main())
