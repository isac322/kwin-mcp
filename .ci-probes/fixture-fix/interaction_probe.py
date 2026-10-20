"""Deterministic GTK3 interaction probe for containerized end-to-end tests.

Besides the per-target status labels, the probe records every raw pointer, key,
scroll and touch event before GTK dispatches it (``Gdk.event_handler_set``):

- ``hold_status:`` left/middle/right press/release counts, the buttons held now
  and the motions seen while any button was held;
- ``key_log:`` press/release counts per key name plus the auto-repeat count (a
  press of a key that is already down);
- ``raw_events:`` raw ``bp``/``br``/``kp``/``kr`` counts, multi-press counts, the
  last event and the modifiers held after it.

All three are exposed as AT-SPI label names like ``click_status:``. When
``KWIN_MCP_PROBE_EVENT_LOG`` names a file, every raw event is also appended to it
as one JSON line, flushed per line, stamped with CLOCK_MONOTONIC and
CLOCK_REALTIME nanoseconds so frame capture times can be anchored to the last
injected event. Coordinates in the log are surface-local to the probe window.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import TYPE_CHECKING, Final

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

if TYPE_CHECKING:
    from typing import IO

APP_NAME: Final = "Interaction Probe"
WINDOW_WIDTH: Final = 920
WINDOW_HEIGHT: Final = 700
TARGET_HEIGHT: Final = 90
SCROLL_STEP: Final = 48.0
EVENT_LOG_ENV: Final = "KWIN_MCP_PROBE_EVENT_LOG"

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
_MODIFIER_ORDER: Final = tuple(name for _, name in _MODIFIERS)
# The modifiers the 0.11 goldens record in ``mods`` (tests/e2e/golden/README.md).
_GOLDEN_MODIFIERS: Final = frozenset({"shift", "ctrl", "alt", "super", "meta"})
# Width cap (characters) of the raw-event labels; shorter than any of their texts.
_RAW_LABEL_CHARS: Final = 20
# Key names whose press/release changes a modifier before the next event's state.
_MODIFIER_KEYS: Final = {
    "Control_L": "ctrl",
    "Control_R": "ctrl",
    "Shift_L": "shift",
    "Shift_R": "shift",
    "Alt_L": "alt",
    "Alt_R": "alt",
    "Super_L": "super",
    "Super_R": "super",
    "Meta_L": "meta",
    "Meta_R": "meta",
    "Hyper_L": "hyper",
    "Hyper_R": "hyper",
}
_TOUCH_KINDS: Final = {
    Gdk.EventType.TOUCH_BEGIN: "touch_begin",
    Gdk.EventType.TOUCH_UPDATE: "touch_update",
    Gdk.EventType.TOUCH_END: "touch_end",
    Gdk.EventType.TOUCH_CANCEL: "touch_cancel",
}
_PRESS_KINDS: Final = {1: "button_press", 2: "gtk_double_press", 3: "gtk_triple_press"}
_BUTTON_MASKS: Final = (
    (Gdk.ModifierType.BUTTON1_MASK, "left"),
    (Gdk.ModifierType.BUTTON2_MASK, "middle"),
    (Gdk.ModifierType.BUTTON3_MASK, "right"),
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
label.raw-status {
    color: #202938;
    font-family: monospace;
    font-size: 10px;
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
    # Reserve the full wrapping width so status updates cannot resize the window.
    label.set_width_chars(64)
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


def _raw_status_label(text: str) -> Gtk.Label:
    """Create a one-line raw-event status label; its full text stays in the AT-SPI name.

    The window is not resizable, so GTK sizes it to its natural request. An
    ellipsized label's natural width is its whole text unless capped, and these
    texts grow with every event: without the cap the window widened mid-drag and
    moved the targets under the pointer. Every text is longer than the cap, so
    the natural width never changes.
    """
    label = _named_label(text, "raw-status")
    label.set_ellipsize(Pango.EllipsizeMode.END)
    label.set_width_chars(_RAW_LABEL_CHARS)
    label.set_max_width_chars(_RAW_LABEL_CHARS)
    return label


def _surface_coords(event: Gdk.Event) -> tuple[float, float] | None:
    """Event coordinates translated into the probe window's surface, if it has any."""
    has_coords, x, y = event.get_coords()
    window = event.get_window()
    if not has_coords or window is None:
        return None
    while window.get_window_type() == Gdk.WindowType.CHILD:
        parent = window.get_effective_parent()
        if parent is None:
            break
        x, y = window.coords_to_parent(x, y)
        window = parent
    return x, y


class RawEventRecorder:
    """Counts and logs every raw input event before GTK dispatches it."""

    def __init__(self, log_path: str | None) -> None:
        self._seq = 0
        self._button_counts = {name: [0, 0] for name in ("left", "middle", "right")}
        self._held_buttons: set[str] = set()
        self._motions_held = 0
        self._key_counts: dict[str, list[int]] = {}
        self._held_keys: set[str] = set()
        self._repeats = 0
        self._raw = {"bp": 0, "br": 0, "kp": 0, "kr": 0, "bp2": 0, "bp3": 0}
        self._last = "none"
        self._held_mods = "none"
        self._touch_slots: dict[object, int] = {}
        self._log: IO[str] | None = (
            open(log_path, "a", encoding="utf-8", buffering=1) if log_path else None  # noqa: SIM115
        )
        self.hold_status = _raw_status_label(self._hold_text())
        self.key_log = _raw_status_label("key_log: none repeats=0")
        self.raw_events = _raw_status_label(
            "raw_events: bp=0 br=0 kp=0 kr=0 bp2=0 bp3=0 last=none held_mods=none"
        )

    def handle(self, event: Gdk.Event, *_data: object) -> None:
        """``Gdk.event_handler_set`` callback: record, then let GTK dispatch the event."""
        try:
            self._record(event)
        finally:
            Gtk.main_do_event(event)

    def _hold_text(self) -> str:
        held = ",".join(sorted(self._held_buttons)) or "none"
        parts = " ".join(
            f"{name}={counts[0]}/{counts[1]}" for name, counts in self._button_counts.items()
        )
        return f"hold_status: {parts} held={held} motions_held={self._motions_held}"

    def _held_modifiers(self, state: Gdk.ModifierType, key: str | None, pressed: bool) -> str:
        held = {name for mask, name in _MODIFIERS if state & mask}
        modifier = _MODIFIER_KEYS.get(key or "")
        if modifier is not None:
            if pressed:
                held.add(modifier)
            else:
                held.discard(modifier)
        ordered = [name for name in _MODIFIER_ORDER if name in held]
        return "+".join(ordered) if ordered else "none"

    def _touch_slot(self, event: Gdk.Event, kind: str) -> int:
        """Finger slot of a touch sequence; slots restart at 0 for each gesture."""
        sequence = event.get_event_sequence()
        if kind == "touch_begin" and sequence not in self._touch_slots:
            used = set(self._touch_slots.values())
            self._touch_slots[sequence] = min(i for i in range(len(used) + 1) if i not in used)
        slot = self._touch_slots.get(sequence, -1)
        if kind in {"touch_end", "touch_cancel"}:
            self._touch_slots.pop(sequence, None)
        return slot

    def _record(self, event: Gdk.Event) -> None:
        """Log one event in the schema of tests/e2e/golden/README.md plus timing extras.

        Core fields per kind match the 0.11 goldens: ``kind``, ``button``/``key``,
        probe-local rounded ``x``/``y``, ``mods`` (modifiers held before the
        event, sorted), ``held`` (pointer buttons held before the event, from
        its state), scroll ``direction``/``dx``/``dy``/``is_stop`` and touch
        ``touch``. Extras: ``seq``, ``mono_us`` (g_get_monotonic_time at
        dispatch), ``event_time`` (compositor ms), ``mono_ns``/``real_ns``
        (CLOCK_MONOTONIC/CLOCK_REALTIME), ``held_mods`` (after the event),
        ``keycode`` and ``repeat`` for keys.
        """
        event_type = event.get_event_type()
        has_state, state = event.get_state()
        if not has_state:
            state = Gdk.ModifierType(0)
        coords = _surface_coords(event)
        at = "" if coords is None else f"@({round(coords[0])},{round(coords[1])})"
        mods = sorted(
            name for mask, name in _MODIFIERS if state & mask and name in _GOLDEN_MODIFIERS
        )
        held = sorted(name for mask, name in _BUTTON_MASKS if state & mask)
        position: dict[str, object] = (
            {} if coords is None else {"x": round(coords[0]), "y": round(coords[1])}
        )
        key: str | None = None
        pressed = False
        entry: dict[str, object]

        if event_type in _CLICK_COUNTS:
            _has_button, button = event.get_button()
            name = _button_name(button)
            count = _CLICK_COUNTS[event_type]
            kind = _PRESS_KINDS[count]
            counter = "bp" if count == 1 else f"bp{count}"
            self._raw[counter] += 1
            if count == 1:
                self._button_counts.setdefault(name, [0, 0])[0] += 1
                self._held_buttons.add(name)
            entry = {"button": name, **position, "mods": mods, "held": held}
            self._last = f"{counter}:{name}{at}"
        elif event_type == Gdk.EventType.BUTTON_RELEASE:
            _has_button, button = event.get_button()
            name = _button_name(button)
            kind = "button_release"
            self._raw["br"] += 1
            self._button_counts.setdefault(name, [0, 0])[1] += 1
            self._held_buttons.discard(name)
            entry = {"button": name, **position, "mods": mods, "held": held}
            self._last = f"br:{name}{at}"
        elif event_type in (Gdk.EventType.KEY_PRESS, Gdk.EventType.KEY_RELEASE):
            _has_keyval, keyval = event.get_keyval()
            _has_keycode, keycode = event.get_keycode()
            key = Gdk.keyval_name(keyval) or f"keyval-{keyval}"
            pressed = event_type == Gdk.EventType.KEY_PRESS
            counts = self._key_counts.setdefault(key, [0, 0])
            repeat = False
            if pressed:
                kind, counter = "key_press", "kp"
                repeat = key in self._held_keys
                if repeat:
                    self._repeats += 1
                else:
                    counts[0] += 1
                self._held_keys.add(key)
            else:
                kind, counter = "key_release", "kr"
                counts[1] += 1
                self._held_keys.discard(key)
            self._raw[counter] += 1
            entry = {"key": key, "mods": mods, "keycode": keycode, "repeat": repeat}
            self._last = f"{counter}:{key}"
        elif event_type == Gdk.EventType.MOTION_NOTIFY:
            kind = "motion"
            if self._held_buttons:
                self._motions_held += 1
            entry = {**position, "mods": mods, "held": held}
            self._last = f"motion{at}"
        elif event_type == Gdk.EventType.SCROLL:
            kind = "scroll"
            # get_scroll_direction() reports nothing for smooth events; the field does.
            direction = event.direction
            entry = {
                **position,
                "mods": mods,
                "direction": direction.value_nick,
                "dx": float(event.delta_x),
                "dy": float(event.delta_y),
                "is_stop": bool(event.is_stop),
            }
            self._last = f"scroll:{direction.value_nick}{at}"
        elif event_type in _TOUCH_KINDS:
            kind = _TOUCH_KINDS[event_type]
            entry = {**position, "touch": self._touch_slot(event, kind)}
            self._last = f"{kind}{at}"
        else:
            return

        self._held_mods = self._held_modifiers(state, key, pressed)
        self._seq += 1
        if self._log is not None:
            record = {
                "kind": kind,
                **entry,
                "seq": self._seq,
                "mono_us": GLib.get_monotonic_time(),
                "event_time": event.get_time(),
                "mono_ns": time.monotonic_ns(),
                "real_ns": time.clock_gettime_ns(time.CLOCK_REALTIME),
                "held_mods": self._held_mods,
            }
            self._log.write(json.dumps(record) + "\n")
        self._publish(kind)

    def _publish(self, kind: str) -> None:
        # Motion and touch-update streams are only counted: relabelling (and the
        # AT-SPI name-change signal) on every one of them slowed the probe enough
        # that fewer drag motions reached the widgets. The labels catch up at the
        # next button, key, scroll or touch begin/end event, e.g. the release that
        # ends a drag.
        if kind in {"motion", "touch_update"}:
            return
        if kind in {"button_press", "button_release"}:
            _set_status_text(self.hold_status, self._hold_text())
        if kind in {"key_press", "key_release"}:
            keys = " ".join(
                f"{key}={counts[0]}/{counts[1]}" for key, counts in sorted(self._key_counts.items())
            )
            _set_status_text(self.key_log, f"key_log: {keys or 'none'} repeats={self._repeats}")
        raw = " ".join(f"{name}={value}" for name, value in self._raw.items())
        _set_status_text(
            self.raw_events, f"raw_events: {raw} last={self._last} held_mods={self._held_mods}"
        )


def _set_status_text(label: Gtk.Label, text: str) -> None:
    label.set_text(text)
    _set_accessible_name(label, text)


class InteractionProbeWindow(Gtk.ApplicationWindow):
    """Fixed-size window exposing observable input state to users and AT-SPI."""

    def __init__(self, application: Gtk.Application) -> None:
        super().__init__(application=application, title=APP_NAME)
        self.set_default_size(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.set_resizable(False)
        _set_accessible_name(self, APP_NAME)
        self.raw_recorder = RawEventRecorder(os.environ.get(EVENT_LOG_ENV) or None)
        # Every wl_pointer motion frame becomes one event, as in the 0.11 golden capture
        # (see disable_event_compression; applied once the widget windows exist).
        # The toplevel takes every pointer/key/scroll/touch event no child window
        # selects, so the raw recorder sees motion and touches over any widget (as
        # the golden capture probe did), not only over the Drag/Zoom Targets.
        self.add_events(
            Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.SCROLL_MASK
            | Gdk.EventMask.SMOOTH_SCROLL_MASK
            | Gdk.EventMask.KEY_PRESS_MASK
            | Gdk.EventMask.KEY_RELEASE_MASK
            | Gdk.EventMask.TOUCH_MASK
        )

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

        raw_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        for label in (
            self.raw_recorder.hold_status,
            self.raw_recorder.key_log,
            self.raw_recorder.raw_events,
        ):
            raw_row.pack_start(label, True, True, 0)
        root.pack_start(raw_row, False, False, 0)

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

    def disable_event_compression(self) -> None:
        """Turn GDK motion compression off on every window of the probe.

        During a press GDK delivers motion to the grabbing widget's child window, not
        the toplevel, and newer GTK (3.24.52) merges queued motions there: a drag
        captured on a loaded host then lost interpolated steps that KWin did send.
        Child windows exist only after ``show_all`` realizes the widgets.
        """
        pending = [self.get_window()]
        while pending:
            window = pending.pop()
            if window is not None:
                window.set_event_compression(False)
                pending.extend(window.get_children())

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
            # Every event passes the recorder before GTK dispatches it.
            Gdk.event_handler_set(self._window.raw_recorder.handle)
        self._window.show_all()
        self._window.disable_event_compression()
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
