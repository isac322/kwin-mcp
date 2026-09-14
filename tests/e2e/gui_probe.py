"""Deterministic GTK3 application used by containerized GUI end-to-end tests."""

from __future__ import annotations

import sys

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

APP_NAME = "GUI Probe"
WINDOW_WIDTH = 920
WINDOW_HEIGHT = 700
TARGET_WIDTH = 320
TARGET_HEIGHT = 84
ANIMATION_INTERVAL_MS = 50
ANIMATION_FRAME_COUNT = 12
SCROLL_STEP = 40.0

_CSS = b"""
window {
    background-color: #f4f6fa;
}
label.section-title {
    color: #202734;
    font-size: 17px;
    font-weight: bold;
}
label.sample {
    background-color: #ffffff;
    border: 1px solid #aeb8c8;
    border-radius: 5px;
    color: #111827;
    font-size: 22px;
    padding: 10px;
}
label.status {
    color: #303846;
    font-family: monospace;
    font-size: 14px;
}
button#hover-target {
    background: #315aa8;
    border: 3px solid #18376f;
    border-radius: 8px;
    color: #ffffff;
    font-size: 18px;
    font-weight: bold;
}
button#hover-target.hovered {
    background: #d46719;
    border-color: #743006;
}
button#animation-target {
    background: #1f5f9f;
    border: 3px solid #153c66;
    border-radius: 8px;
    padding: 8px 14px;
}
button#animation-target label.animation-frame {
    color: #ffffff;
    font-size: 18px;
    font-weight: bold;
}
button#animation-target progressbar {
    min-height: 22px;
}
button#animation-target progressbar trough {
    background: #10253f;
    border: 1px solid #081629;
    border-radius: 3px;
    min-height: 22px;
}
button#animation-target progressbar progress {
    background: #f4c542;
    border: 0;
    border-radius: 2px;
    min-height: 20px;
    transition: none;
}
.scroll-cell-even {
    background-color: #dce8ff;
    border: 1px solid #5277b8;
}
.scroll-cell-odd {
    background-color: #ffe2bd;
    border: 1px solid #b66b20;
}
"""


def _set_accessible_name(widget: Gtk.Widget, name: str) -> None:
    """Set a stable AT-SPI name on a GTK widget."""
    accessible = widget.get_accessible()
    if accessible is not None:
        accessible.set_name(name)


def _named_label(
    text: str,
    style_class: str | None = None,
    *,
    accessible_name: str | None = None,
) -> Gtk.Label:
    """Create a visible label with a stable AT-SPI name."""
    label = Gtk.Label(label=text)
    label.set_xalign(0.0)
    if style_class is not None:
        label.get_style_context().add_class(style_class)
    _set_accessible_name(label, accessible_name if accessible_name is not None else text)
    return label


class GuiProbeWindow(Gtk.ApplicationWindow):
    """Fixed-layout probe exposing deterministic visual and accessibility state."""

    def __init__(self, application: Gtk.Application) -> None:
        super().__init__(application=application, title=APP_NAME)
        self.set_default_size(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.set_resizable(False)
        _set_accessible_name(self, APP_NAME)

        self._animation_frame = 0
        self._animation_source_id: int | None = None
        self._animation_progress: Gtk.ProgressBar | None = None
        self._animation_label: Gtk.Label | None = None
        self._animation_status: Gtk.Label | None = None

        self.add(self._build_content())
        self.connect("destroy", self._on_destroy)

    def _build_content(self) -> Gtk.Widget:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        root.set_border_width(18)

        heading = _named_label(APP_NAME, "section-title")
        root.pack_start(heading, False, False, 0)

        samples = Gtk.Grid(column_spacing=14, row_spacing=6)
        samples.set_column_homogeneous(True)
        samples.attach(_named_label("Unicode Sample", "section-title"), 0, 0, 1, 1)
        samples.attach(_named_label("Tofu Control", "section-title"), 1, 0, 1, 1)
        unicode_sample = _named_label(
            "GUI 검증 42 😀",
            "sample",
            accessible_name="unicode_sample: GUI 검증 42 😀",
        )
        tofu_control = _named_label(
            "□□",
            "sample",
            accessible_name="tofu_control: □□",
        )
        unicode_sample.set_size_request(410, 56)
        tofu_control.set_size_request(410, 56)
        samples.attach(unicode_sample, 0, 1, 1, 1)
        samples.attach(tofu_control, 1, 1, 1, 1)
        root.pack_start(samples, False, False, 0)

        targets = Gtk.Grid(column_spacing=18, row_spacing=7)
        targets.set_column_homogeneous(True)

        hover_target = Gtk.Button(label="Hover Target")
        hover_target.set_name("hover-target")
        hover_target.set_size_request(TARGET_WIDTH, TARGET_HEIGHT)
        hover_target.set_can_focus(False)
        _set_accessible_name(hover_target, "Hover Target")
        hover_target.connect("enter-notify-event", self._on_hover_enter)
        hover_target.connect("leave-notify-event", self._on_hover_leave)
        self._hover_status = _named_label("hover_status: idle", "status")

        animation_target = Gtk.Button()
        animation_target.set_name("animation-target")
        animation_target.set_size_request(TARGET_WIDTH, TARGET_HEIGHT)
        animation_target.set_can_focus(False)
        _set_accessible_name(animation_target, "Animation Target")
        animation_target.connect("clicked", self._on_animation_clicked)

        animation_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        animation_label = Gtk.Label(label="Animation Target 00")
        animation_label.get_style_context().add_class("animation-frame")
        animation_progress = Gtk.ProgressBar()
        animation_progress.set_size_request(TARGET_WIDTH - 36, 22)
        animation_progress.set_fraction(0.0)
        animation_content.pack_start(animation_label, False, False, 0)
        animation_content.pack_start(animation_progress, False, False, 0)
        animation_target.add(animation_content)
        self._animation_label = animation_label
        self._animation_progress = animation_progress
        self._animation_status = _named_label("animation_status: 0", "status")

        targets.attach(hover_target, 0, 0, 1, 1)
        targets.attach(animation_target, 1, 0, 1, 1)
        targets.attach(self._hover_status, 0, 1, 1, 1)
        targets.attach(self._animation_status, 1, 1, 1, 1)
        root.pack_start(targets, False, False, 0)

        scroll_grid = Gtk.Grid(column_spacing=18, row_spacing=7)
        scroll_grid.set_column_homogeneous(True)
        horizontal_region, horizontal_status = self._build_scroll_region(
            orientation=Gtk.Orientation.HORIZONTAL,
            target_name="Horizontal Scroll Region",
            status_prefix="horizontal_scroll",
        )
        vertical_region, vertical_status = self._build_scroll_region(
            orientation=Gtk.Orientation.VERTICAL,
            target_name="Vertical Scroll Region",
            status_prefix="vertical_scroll",
        )
        scroll_grid.attach(horizontal_region, 0, 0, 1, 1)
        scroll_grid.attach(vertical_region, 1, 0, 1, 1)
        scroll_grid.attach(horizontal_status, 0, 1, 1, 1)
        scroll_grid.attach(vertical_status, 1, 1, 1, 1)
        root.pack_start(scroll_grid, True, True, 0)

        return root

    def _build_scroll_region(
        self,
        *,
        orientation: Gtk.Orientation,
        target_name: str,
        status_prefix: str,
    ) -> tuple[Gtk.ScrolledWindow, Gtk.Label]:
        region = Gtk.ScrolledWindow()
        region.set_size_request(420, 180)
        region.set_shadow_type(Gtk.ShadowType.IN)
        _set_accessible_name(region, target_name)

        if orientation == Gtk.Orientation.HORIZONTAL:
            region.set_policy(Gtk.PolicyType.ALWAYS, Gtk.PolicyType.NEVER)
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        else:
            region.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.ALWAYS)
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        cell_prefix = "H" if orientation == Gtk.Orientation.HORIZONTAL else "V"
        for index in range(12):
            cell = Gtk.EventBox()
            cell.get_style_context().add_class(
                "scroll-cell-even" if index % 2 == 0 else "scroll-cell-odd"
            )
            cell_label = Gtk.Label(label=f"{cell_prefix}{index:02d}")
            cell_label.set_size_request(
                120 if orientation == Gtk.Orientation.HORIZONTAL else 380,
                115 if orientation == Gtk.Orientation.HORIZONTAL else 58,
            )
            cell.add(cell_label)
            content.pack_start(cell, False, False, 0)

        region.add(content)
        adjustment = (
            region.get_hadjustment()
            if orientation == Gtk.Orientation.HORIZONTAL
            else region.get_vadjustment()
        )
        status = _named_label(f"{status_prefix}: 0", "status")
        adjustment.connect("value-changed", self._on_scroll_value_changed, status, status_prefix)
        region.connect("scroll-event", self._on_scroll_event, adjustment, orientation)
        return region, status

    def _set_status(self, label: Gtk.Label, text: str) -> None:
        label.set_text(text)
        _set_accessible_name(label, text)

    def _on_hover_enter(self, widget: Gtk.Widget, _event: Gdk.EventCrossing) -> bool:
        widget.get_style_context().add_class("hovered")
        self._set_status(self._hover_status, "hover_status: entered")
        return False

    def _on_hover_leave(self, widget: Gtk.Widget, _event: Gdk.EventCrossing) -> bool:
        widget.get_style_context().remove_class("hovered")
        self._set_status(self._hover_status, "hover_status: idle")
        return False

    def _on_animation_clicked(self, _button: Gtk.Button) -> None:
        if self._animation_source_id is not None:
            GLib.source_remove(self._animation_source_id)
        self._animation_frame = 0
        self._publish_animation_frame()
        self._animation_source_id = GLib.timeout_add(
            ANIMATION_INTERVAL_MS,
            self._advance_animation,
        )

    def _advance_animation(self) -> bool:
        self._animation_frame += 1
        self._publish_animation_frame()
        if self._animation_frame >= ANIMATION_FRAME_COUNT:
            self._animation_source_id = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _publish_animation_frame(self) -> None:
        if self._animation_status is not None:
            self._set_status(
                self._animation_status,
                f"animation_status: {self._animation_frame}",
            )
        if self._animation_label is not None:
            self._animation_label.set_text(f"Animation Target {self._animation_frame:02d}")
        if self._animation_progress is not None:
            self._animation_progress.set_fraction(self._animation_frame / ANIMATION_FRAME_COUNT)

    def _on_scroll_event(
        self,
        _region: Gtk.ScrolledWindow,
        event: Gdk.EventScroll,
        adjustment: Gtk.Adjustment,
        orientation: Gtk.Orientation,
    ) -> bool:
        delta = 0.0
        if event.direction == Gdk.ScrollDirection.UP:
            delta = -SCROLL_STEP
        elif event.direction == Gdk.ScrollDirection.DOWN:
            delta = SCROLL_STEP
        elif event.direction == Gdk.ScrollDirection.LEFT:
            delta = -SCROLL_STEP
        elif event.direction == Gdk.ScrollDirection.RIGHT:
            delta = SCROLL_STEP
        elif event.direction == Gdk.ScrollDirection.SMOOTH:
            _available, delta_x, delta_y = event.get_scroll_deltas()
            if orientation == Gtk.Orientation.HORIZONTAL:
                delta = (delta_x if abs(delta_x) > abs(delta_y) else delta_y) * SCROLL_STEP
            else:
                delta = delta_y * SCROLL_STEP

        if delta == 0.0:
            return False

        lower = adjustment.get_lower()
        upper = adjustment.get_upper() - adjustment.get_page_size()
        adjustment.set_value(min(max(adjustment.get_value() + delta, lower), upper))
        return True

    def _on_scroll_value_changed(
        self,
        adjustment: Gtk.Adjustment,
        status: Gtk.Label,
        status_prefix: str,
    ) -> None:
        self._set_status(status, f"{status_prefix}: {round(adjustment.get_value())}")

    def _on_destroy(self, _window: Gtk.Widget) -> None:
        if self._animation_source_id is not None:
            GLib.source_remove(self._animation_source_id)
            self._animation_source_id = None
        application = self.get_application()
        if application is not None:
            application.quit()


class GuiProbeApplication(Gtk.Application):
    """GTK application wrapper for the deterministic probe window."""

    def __init__(self) -> None:
        super().__init__(application_id="io.github.kwinmcp.GuiProbe")
        self._window: GuiProbeWindow | None = None

    def do_activate(self) -> None:
        if self._window is None:
            self._window = GuiProbeWindow(self)
        self._window.show_all()
        self._window.present()


def main() -> int:
    """Run the GUI probe until its window is closed."""
    GLib.set_application_name(APP_NAME)
    style_provider = Gtk.CssProvider()
    style_provider.load_from_data(_CSS)
    screen = Gdk.Screen.get_default()
    if screen is not None:
        Gtk.StyleContext.add_provider_for_screen(
            screen,
            style_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
    application = GuiProbeApplication()
    return application.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
