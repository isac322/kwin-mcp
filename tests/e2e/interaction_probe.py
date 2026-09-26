"""Deterministic GTK3 interaction probe for containerized end-to-end tests."""

from __future__ import annotations

import sys
from typing import Final

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

APP_NAME: Final = "Interaction Probe"
WINDOW_WIDTH: Final = 920
WINDOW_HEIGHT: Final = 700
TARGET_HEIGHT: Final = 90
SCROLL_STEP: Final = 48.0

_BUTTON_NAMES: Final = {
    1: "left",
    2: "middle",
    3: "right",
}
_CLICK_COUNTS: Final = {
    Gdk.EventType.BUTTON_PRESS: 1,
    Gdk.EventType.DOUBLE_BUTTON_PRESS: 2,
    Gdk.EventType.TRIPLE_BUTTON_PRESS: 3,
}
_MODIFIERS: Final = (
    (Gdk.ModifierType.CONTROL_MASK, "ctrl"),
    (Gdk.ModifierType.SHIFT_MASK, "shift"),
    (Gdk.ModifierType.MOD1_MASK, "alt"),
    (Gdk.ModifierType.SUPER_MASK, "super"),
    (Gdk.ModifierType.META_MASK, "meta"),
    (Gdk.ModifierType.HYPER_MASK, "hyper"),
)

_CSS = b"""
window {
    background-color: #f3f5f8;
}
label.heading {
    color: #18202c;
    font-size: 20px;
    font-weight: bold;
}
label.section-title {
    color: #263244;
    font-size: 14px;
    font-weight: bold;
}
label.status {
    color: #202938;
    font-family: monospace;
    font-size: 13px;
}
button.interaction-target {
    background: #285aa6;
    border: 3px solid #15376d;
    border-radius: 7px;
    color: #ffffff;
    font-size: 18px;
    font-weight: bold;
}
button#drag-target {
    background: #8b4a16;
    border-color: #552b0b;
}
button#zoom-target {
    background: #28714a;
    border-color: #16452d;
}
entry {
    background: #ffffff;
    border: 2px solid #526985;
    color: #111827;
    font-size: 17px;
    min-height: 36px;
    padding: 4px 8px;
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


def _named_label(text: str, style_class: str | None = None) -> Gtk.Label:
    """Create a visible label whose AT-SPI name exactly matches its text."""
    label = Gtk.Label(label=text)
    label.set_xalign(0.0)
    if style_class is not None:
        label.get_style_context().add_class(style_class)
    _set_accessible_name(label, text)
    return label


def _status_label(text: str) -> Gtk.Label:
    """Create a wrapping status label with deterministic accessibility text."""
    label = _named_label(text, "status")
    label.set_line_wrap(True)
    label.set_max_width_chars(64)
    label.set_size_request(-1, 48)
    return label


def _button_name(button: int) -> str:
    """Return a stable human-readable pointer button name."""
    return _BUTTON_NAMES.get(button, f"button-{button}")


def _modifier_names(state: Gdk.ModifierType) -> str:
    """Return active keyboard modifiers in a stable order."""
    names = [name for mask, name in _MODIFIERS if state & mask]
    return "+".join(names) if names else "none"


def _coordinate(value: float) -> int:
    """Convert a GTK local coordinate to the integer exposed by the probe."""
    return round(value)


class InteractionProbeWindow(Gtk.ApplicationWindow):
    """Fixed-size window exposing observable input state to users and AT-SPI."""

    def __init__(self, application: Gtk.Application) -> None:
        super().__init__(application=application, title=APP_NAME)
        self.set_default_size(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.set_resizable(False)
        _set_accessible_name(self, APP_NAME)

        self._click_button = 0
        self._click_count = 1
        self._click_modifiers = Gdk.ModifierType(0)
        self._click_started_us: int | None = None

        self._drag_active = False
        self._drag_source = "idle"
        self._drag_press_button = 0
        self._drag_modifiers = Gdk.ModifierType(0)
        self._drag_motion_samples = 0
        self._drag_start = (0, 0)
        self._drag_end = (0, 0)
        self._drag_bounds = (0, 0, 0, 0)
        self._drag_touch_sequences: set[Gdk.EventSequence] = set()
        self._drag_max_touches = 0

        self._zoom_phase = "idle"
        self._zoom_scale = 1.0
        self._long_press_state = "idle"
        self._long_press_at = (-1, -1)
        self._scroll_mode = "idle"
        self._scroll_events = 0
        self._scroll_last = 0.0
        self._scroll_total = 0.0

        self._keyboard_target: Gtk.Entry | None = None
        self._zoom_gesture: Gtk.GestureZoom | None = None
        self._long_press_gesture: Gtk.GestureLongPress | None = None

        self.add(self._build_content())
        self.connect("destroy", self._on_destroy)

    def _build_content(self) -> Gtk.Widget:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        root.set_border_width(14)

        heading = _named_label(APP_NAME, "heading")
        root.pack_start(heading, False, False, 0)

        pointer_grid = Gtk.Grid(column_spacing=16, row_spacing=5)
        pointer_grid.set_column_homogeneous(True)

        click_target = self._target_button("Click Target", "click-target")
        click_target.connect("button-press-event", self._on_click_press)
        click_target.connect("button-release-event", self._on_click_release)
        self._click_status = _status_label(
            "click_status: button=none count=0 modifiers=none hold_ms=0"
        )

        drag_target = self._target_button("Drag Target", "drag-target")
        drag_target.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.TOUCH_MASK
        )
        drag_target.connect("button-press-event", self._on_drag_press)
        drag_target.connect("motion-notify-event", self._on_drag_motion)
        drag_target.connect("button-release-event", self._on_drag_release)
        drag_target.connect("touch-event", self._on_drag_touch)
        self._drag_status = _status_label(
            "drag_status: source=idle press=none release=none modifiers=none "
            "motions=0 fingers=0 start=(0,0) end=(0,0) bounds=(0,0,0,0)"
        )
        self._drag_status.set_size_request(-1, 66)

        pointer_grid.attach(click_target, 0, 0, 1, 1)
        pointer_grid.attach(drag_target, 1, 0, 1, 1)
        pointer_grid.attach(self._click_status, 0, 1, 1, 1)
        pointer_grid.attach(self._drag_status, 1, 1, 1, 1)
        root.pack_start(pointer_grid, False, False, 0)

        keyboard_group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        keyboard_group.pack_start(_named_label("Keyboard Input", "section-title"), False, False, 0)
        keyboard_target = Gtk.Entry()
        keyboard_target.set_placeholder_text("Type here")
        _set_accessible_name(keyboard_target, "Keyboard Target")
        keyboard_group.pack_start(keyboard_target, False, False, 0)
        self._keyboard_target = keyboard_target
        root.pack_start(keyboard_group, False, False, 0)

        zoom_target = self._target_button("Zoom Target", "zoom-target")
        zoom_target.set_size_request(-1, 96)
        zoom_target.add_events(Gdk.EventMask.TOUCH_MASK)
        self._zoom_status = _status_label(
            "zoom_status: phase=idle scale=1.000 long_press=idle at=(-1,-1)"
        )
        self._install_touch_gestures(zoom_target)
        root.pack_start(zoom_target, False, False, 0)
        root.pack_start(self._zoom_status, False, False, 0)

        scroll_group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        scroll_group.pack_start(
            _named_label("Horizontal Scroll", "section-title"),
            False,
            False,
            0,
        )
        scroll_region, scroll_status = self._build_horizontal_scroll_region()
        scroll_group.pack_start(scroll_region, True, True, 0)
        scroll_group.pack_start(scroll_status, False, False, 0)
        root.pack_start(scroll_group, True, True, 0)

        return root

    def _target_button(self, label: str, widget_name: str) -> Gtk.Button:
        button = Gtk.Button(label=label)
        button.set_name(widget_name)
        button.set_size_request(420, TARGET_HEIGHT)
        button.set_can_focus(False)
        button.get_style_context().add_class("interaction-target")
        _set_accessible_name(button, label)
        return button

    def _install_touch_gestures(self, target: Gtk.Widget) -> None:
        zoom = Gtk.GestureZoom.new(target)
        zoom.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        zoom.connect("begin", self._on_zoom_begin)
        zoom.connect("scale-changed", self._on_zoom_scale_changed)
        zoom.connect("end", self._on_zoom_end)
        zoom.connect("cancel", self._on_zoom_cancel)
        self._zoom_gesture = zoom

        long_press = Gtk.GestureLongPress.new(target)
        long_press.set_touch_only(True)
        long_press.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        long_press.connect("pressed", self._on_long_press)
        long_press.connect("cancelled", self._on_long_press_cancelled)
        self._long_press_gesture = long_press

    def _build_horizontal_scroll_region(self) -> tuple[Gtk.ScrolledWindow, Gtk.Label]:
        region = Gtk.ScrolledWindow()
        region.set_size_request(-1, 132)
        region.set_policy(Gtk.PolicyType.ALWAYS, Gtk.PolicyType.NEVER)
        region.set_overlay_scrolling(False)
        region.set_kinetic_scrolling(False)
        region.set_shadow_type(Gtk.ShadowType.IN)
        _set_accessible_name(region, "Horizontal Scroll Region")
        horizontal_scrollbar = region.get_hscrollbar()
        if horizontal_scrollbar is not None:
            _set_accessible_name(horizontal_scrollbar, "Horizontal Scrollbar")

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        for index in range(12):
            cell = Gtk.EventBox()
            cell.get_style_context().add_class(
                "scroll-cell-even" if index % 2 == 0 else "scroll-cell-odd"
            )
            cell_label = Gtk.Label(label=f"Horizontal Cell {index:02d}")
            cell_label.set_size_request(150, 82)
            _set_accessible_name(cell_label, f"Horizontal Cell {index:02d}")
            cell.add(cell_label)
            content.pack_start(cell, False, False, 0)
        region.add(content)

        adjustment = region.get_hadjustment()
        status = _status_label(
            "horizontal_scroll: mode=idle events=0 last=0.000 total=0.000 position=0"
        )
        status.set_max_width_chars(100)
        status.set_size_request(-1, 24)
        adjustment.connect("value-changed", self._on_horizontal_scroll_changed, status)
        region.connect(
            "scroll-event",
            self._on_horizontal_scroll_event,
            adjustment,
            status,
        )
        return region, status

    def _set_status(self, label: Gtk.Label, text: str) -> None:
        label.set_text(text)
        _set_accessible_name(label, text)

    def _on_click_press(self, _target: Gtk.Widget, event: Gdk.EventButton) -> bool:
        self._click_button = int(event.button)
        self._click_count = _CLICK_COUNTS.get(event.type, 1)
        self._click_modifiers = event.state
        self._click_started_us = GLib.get_monotonic_time()
        return False

    def _on_click_release(self, _target: Gtk.Widget, event: Gdk.EventButton) -> bool:
        started_us = self._click_started_us
        elapsed_us = 0 if started_us is None else GLib.get_monotonic_time() - started_us
        hold_ms = max(0, elapsed_us // 1000)
        button = self._click_button if self._click_button else int(event.button)
        modifiers = self._click_modifiers | event.state
        self._set_status(
            self._click_status,
            f"click_status: button={_button_name(button)} count={self._click_count} "
            f"modifiers={_modifier_names(modifiers)} hold_ms={hold_ms}",
        )
        self._click_started_us = None
        return False

    def _on_drag_press(self, _target: Gtk.Widget, event: Gdk.EventButton) -> bool:
        self._drag_active = True
        self._drag_source = "mouse"
        self._drag_press_button = int(event.button)
        self._drag_modifiers = event.state
        self._drag_motion_samples = 0
        self._drag_touch_sequences.clear()
        self._drag_max_touches = 0
        self._reset_drag_path(event.x, event.y)
        return False

    def _on_drag_motion(self, _target: Gtk.Widget, event: Gdk.EventMotion) -> bool:
        if not self._drag_active or self._drag_source != "mouse":
            return False
        self._drag_motion_samples += 1
        self._drag_modifiers |= event.state
        self._record_drag_point(event.x, event.y)
        return False

    def _on_drag_release(self, _target: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if not self._drag_active or self._drag_source != "mouse":
            return False
        self._drag_active = False
        self._drag_modifiers |= event.state
        self._record_drag_point(event.x, event.y)
        self._publish_drag_status(
            press=_button_name(self._drag_press_button),
            release=_button_name(int(event.button)),
        )
        return False

    def _on_drag_touch(self, _target: Gtk.Widget, event: Gdk.EventTouch) -> bool:
        sequence = event.sequence
        if event.type == Gdk.EventType.TOUCH_BEGIN:
            if not self._drag_touch_sequences:
                self._drag_active = True
                self._drag_source = "touch"
                self._drag_modifiers = event.state
                self._drag_motion_samples = 0
                self._drag_max_touches = 0
                self._reset_drag_path(event.x, event.y)
            self._drag_touch_sequences.add(sequence)
            self._drag_max_touches = max(
                self._drag_max_touches,
                len(self._drag_touch_sequences),
            )
            self._drag_modifiers |= event.state
            self._record_drag_point(event.x, event.y)
        elif event.type == Gdk.EventType.TOUCH_UPDATE:
            self._drag_motion_samples += 1
            self._drag_modifiers |= event.state
            self._record_drag_point(event.x, event.y)
        elif event.type in (Gdk.EventType.TOUCH_END, Gdk.EventType.TOUCH_CANCEL):
            self._drag_modifiers |= event.state
            self._record_drag_point(event.x, event.y)
            self._drag_touch_sequences.discard(sequence)
            if not self._drag_touch_sequences:
                self._drag_active = False
                release = "touch" if event.type == Gdk.EventType.TOUCH_END else "cancel"
                self._publish_drag_status(press="touch", release=release)
        return True

    def _reset_drag_path(self, x: float, y: float) -> None:
        point = (_coordinate(x), _coordinate(y))
        self._drag_start = point
        self._drag_end = point
        self._drag_bounds = (*point, *point)

    def _record_drag_point(self, x: float, y: float) -> None:
        point_x = _coordinate(x)
        point_y = _coordinate(y)
        min_x, min_y, max_x, max_y = self._drag_bounds
        self._drag_end = (point_x, point_y)
        self._drag_bounds = (
            min(min_x, point_x),
            min(min_y, point_y),
            max(max_x, point_x),
            max(max_y, point_y),
        )

    def _publish_drag_status(self, *, press: str, release: str) -> None:
        start_x, start_y = self._drag_start
        end_x, end_y = self._drag_end
        min_x, min_y, max_x, max_y = self._drag_bounds
        self._set_status(
            self._drag_status,
            f"drag_status: source={self._drag_source} press={press} release={release} "
            f"modifiers={_modifier_names(self._drag_modifiers)} "
            f"motions={self._drag_motion_samples} fingers={self._drag_max_touches} "
            f"start=({start_x},{start_y}) end=({end_x},{end_y}) "
            f"bounds=({min_x},{min_y},{max_x},{max_y})",
        )

    def _on_zoom_begin(self, _gesture: Gtk.Gesture, _sequence: Gdk.EventSequence) -> None:
        self._zoom_phase = "active"
        self._zoom_scale = 1.0
        self._long_press_state = "idle"
        self._long_press_at = (-1, -1)
        self._publish_zoom_status()

    def _on_zoom_scale_changed(self, _gesture: Gtk.GestureZoom, scale: float) -> None:
        self._zoom_phase = "active"
        self._zoom_scale = scale
        self._publish_zoom_status()

    def _on_zoom_end(self, _gesture: Gtk.Gesture, _sequence: Gdk.EventSequence) -> None:
        self._zoom_phase = "completed"
        self._publish_zoom_status()

    def _on_zoom_cancel(self, _gesture: Gtk.Gesture, _sequence: Gdk.EventSequence) -> None:
        self._zoom_phase = "cancelled"
        self._publish_zoom_status()

    def _on_long_press(self, _gesture: Gtk.GestureLongPress, x: float, y: float) -> None:
        self._long_press_state = "recognized"
        self._long_press_at = (_coordinate(x), _coordinate(y))
        self._publish_zoom_status()

    def _on_long_press_cancelled(self, _gesture: Gtk.GestureLongPress) -> None:
        if self._long_press_state != "recognized":
            self._long_press_state = "cancelled"
            self._publish_zoom_status()

    def _publish_zoom_status(self) -> None:
        x, y = self._long_press_at
        self._set_status(
            self._zoom_status,
            f"zoom_status: phase={self._zoom_phase} scale={self._zoom_scale:.3f} "
            f"long_press={self._long_press_state} at=({x},{y})",
        )

    def _on_horizontal_scroll_event(
        self,
        _region: Gtk.ScrolledWindow,
        event: Gdk.EventScroll,
        adjustment: Gtk.Adjustment,
        status: Gtk.Label,
    ) -> bool:
        raw_delta = 0.0
        mode = "discrete"
        if event.direction == Gdk.ScrollDirection.UP:
            raw_delta = -1.0
        elif event.direction == Gdk.ScrollDirection.DOWN:
            raw_delta = 1.0
        elif event.direction == Gdk.ScrollDirection.LEFT:
            raw_delta = -1.0
        elif event.direction == Gdk.ScrollDirection.RIGHT:
            raw_delta = 1.0
        elif event.direction == Gdk.ScrollDirection.SMOOTH:
            mode = "smooth"
            _available, delta_x, delta_y = event.get_scroll_deltas()
            raw_delta = delta_x if abs(delta_x) > abs(delta_y) else delta_y

        if raw_delta == 0.0:
            return False

        self._scroll_mode = mode
        self._scroll_events += 1
        self._scroll_last = raw_delta
        self._scroll_total += raw_delta
        pixel_delta = raw_delta * SCROLL_STEP

        lower = adjustment.get_lower()
        upper = adjustment.get_upper() - adjustment.get_page_size()
        adjustment.set_value(min(max(adjustment.get_value() + pixel_delta, lower), upper))
        self._publish_horizontal_scroll(status, adjustment)
        return True

    def _on_horizontal_scroll_changed(
        self,
        adjustment: Gtk.Adjustment,
        status: Gtk.Label,
    ) -> None:
        self._publish_horizontal_scroll(status, adjustment)

    def _publish_horizontal_scroll(
        self,
        status: Gtk.Label,
        adjustment: Gtk.Adjustment,
    ) -> None:
        self._set_status(
            status,
            f"horizontal_scroll: mode={self._scroll_mode} "
            f"events={self._scroll_events} last={self._scroll_last:.3f} "
            f"total={self._scroll_total:.3f} "
            f"position={round(adjustment.get_value())}",
        )

    def focus_keyboard_target(self) -> None:
        if self._keyboard_target is not None:
            self._keyboard_target.grab_focus()

    def _on_destroy(self, _window: Gtk.Widget) -> None:
        application = self.get_application()
        if application is not None:
            application.quit()


class InteractionProbeApplication(Gtk.Application):
    """GTK application wrapper for the deterministic interaction probe."""

    def __init__(self) -> None:
        super().__init__(application_id="io.github.kwinmcp.InteractionProbe")
        self._window: InteractionProbeWindow | None = None

    def do_activate(self) -> None:
        if self._window is None:
            self._window = InteractionProbeWindow(self)
        self._window.show_all()
        self._window.present()
        self._window.focus_keyboard_target()


def main() -> int:
    """Run the probe until its window is closed."""
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
    application = InteractionProbeApplication()
    return application.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
