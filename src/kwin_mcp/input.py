"""Input injection via KWin's EIS (Emulated Input Server) D-Bus interface.

Uses KWin's private org.kde.KWin.EIS.RemoteDesktop D-Bus interface to get
a direct EIS file descriptor, then uses libei to inject mouse/keyboard
events into the isolated kwin_wayland --virtual session.

This bypasses the XDG RemoteDesktop portal (which requires user authorization)
and communicates directly with the KWin compositor.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import select
import shutil
import subprocess
import time
from enum import Enum

import dbus
from dbus.mainloop.glib import DBusGMainLoop


class MouseButton(Enum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


# Linux input event codes for mouse buttons
_BTN_CODES: dict[MouseButton, int] = {
    MouseButton.LEFT: 0x110,  # BTN_LEFT
    MouseButton.RIGHT: 0x111,  # BTN_RIGHT
    MouseButton.MIDDLE: 0x112,  # BTN_MIDDLE
}

# Linux evdev keycodes for special keys
_EVDEV_KEY_MAP: dict[str, int] = {
    "return": 28,
    "enter": 28,
    "tab": 15,
    "escape": 1,
    "backspace": 14,
    "delete": 111,
    "space": 57,
    "up": 103,
    "down": 108,
    "left": 105,
    "right": 106,
    "home": 102,
    "end": 107,
    "page_up": 104,
    "pageup": 104,
    "page_down": 109,
    "pagedown": 109,
    "insert": 110,
    "f1": 59,
    "f2": 60,
    "f3": 61,
    "f4": 62,
    "f5": 63,
    "f6": 64,
    "f7": 65,
    "f8": 66,
    "f9": 67,
    "f10": 68,
    "f11": 87,
    "f12": 88,
    "print": 99,
    "scroll_lock": 70,
    "pause": 119,
    "caps_lock": 58,
    "num_lock": 69,
    "menu": 127,
}

# Modifier evdev keycodes
_MODIFIER_KEYS: dict[str, int] = {
    "shift": 42,  # KEY_LEFTSHIFT
    "ctrl": 29,  # KEY_LEFTCTRL
    "control": 29,
    "alt": 56,  # KEY_LEFTALT
    "super": 125,  # KEY_LEFTMETA
    "meta": 125,
}

# Character to evdev keycode mapping (US QWERTY layout)
_CHAR_KEY_MAP: dict[str, tuple[int, bool]] = {}

# Build character → (keycode, needs_shift) mapping
_QWERTY_ROWS = [
    # (normal_chars, shifted_chars, keycodes)
    ("`1234567890-=", "~!@#$%^&*()_+", [41, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]),
    ("qwertyuiop[]\\", "QWERTYUIOP{}|", [16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 43]),
    ("asdfghjkl;'", 'ASDFGHJKL:"', [30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40]),
    ("zxcvbnm,./", "ZXCVBNM<>?", [44, 45, 46, 47, 48, 49, 50, 51, 52, 53]),
]

for normal, shifted, codes in _QWERTY_ROWS:
    for char, code in zip(normal, codes, strict=True):
        _CHAR_KEY_MAP[char] = (code, False)
    for char, code in zip(shifted, codes, strict=True):
        _CHAR_KEY_MAP[char] = (code, True)

_CHAR_KEY_MAP[" "] = (57, False)  # space
_CHAR_KEY_MAP["\t"] = (15, False)  # tab
_CHAR_KEY_MAP["\n"] = (28, False)  # enter

# Button states
_PRESSED = 1
_RELEASED = 0

# EI device capabilities (bitfield)
_EI_CAP_POINTER = 1 << 0
_EI_CAP_POINTER_ABSOLUTE = 1 << 1
_EI_CAP_KEYBOARD = 1 << 2
_EI_CAP_TOUCH = 1 << 3
_EI_CAP_SCROLL = 1 << 4
_EI_CAP_BUTTON = 1 << 5

# EI event types
_EI_EVENT_CONNECT = 1
_EI_EVENT_DISCONNECT = 2
_EI_EVENT_SEAT_ADDED = 3
_EI_EVENT_DEVICE_ADDED = 5
_EI_EVENT_DEVICE_RESUMED = 8

# Scroll axis values (in libei, scroll is in pixels)
_SCROLL_STEP_PIXELS = 15.0


def _load_libei() -> ctypes.CDLL:
    """Load libei shared library and set up function prototypes."""
    lib = ctypes.CDLL("libei.so.1")

    # Context management
    lib.ei_new_sender.restype = ctypes.c_void_p
    lib.ei_new_sender.argtypes = [ctypes.c_void_p]
    lib.ei_configure_name.restype = None
    lib.ei_configure_name.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.ei_setup_backend_fd.restype = ctypes.c_int
    lib.ei_setup_backend_fd.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.ei_dispatch.restype = ctypes.c_int
    lib.ei_dispatch.argtypes = [ctypes.c_void_p]
    lib.ei_get_event.restype = ctypes.c_void_p
    lib.ei_get_event.argtypes = [ctypes.c_void_p]
    lib.ei_event_get_type.restype = ctypes.c_int
    lib.ei_event_get_type.argtypes = [ctypes.c_void_p]
    lib.ei_event_unref.restype = ctypes.c_void_p
    lib.ei_event_unref.argtypes = [ctypes.c_void_p]
    lib.ei_unref.restype = ctypes.c_void_p
    lib.ei_unref.argtypes = [ctypes.c_void_p]
    lib.ei_get_fd.restype = ctypes.c_int
    lib.ei_get_fd.argtypes = [ctypes.c_void_p]

    # Seat functions
    lib.ei_event_get_seat.restype = ctypes.c_void_p
    lib.ei_event_get_seat.argtypes = [ctypes.c_void_p]
    lib.ei_seat_has_capability.restype = ctypes.c_int
    lib.ei_seat_has_capability.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    lib.ei_seat_ref.restype = ctypes.c_void_p
    lib.ei_seat_ref.argtypes = [ctypes.c_void_p]
    # ei_seat_bind_capabilities is variadic, argtypes not set

    # Device functions
    lib.ei_event_get_device.restype = ctypes.c_void_p
    lib.ei_event_get_device.argtypes = [ctypes.c_void_p]
    lib.ei_device_get_name.restype = ctypes.c_char_p
    lib.ei_device_get_name.argtypes = [ctypes.c_void_p]
    lib.ei_device_has_capability.restype = ctypes.c_int
    lib.ei_device_has_capability.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    lib.ei_device_ref.restype = ctypes.c_void_p
    lib.ei_device_ref.argtypes = [ctypes.c_void_p]
    lib.ei_device_unref.restype = ctypes.c_void_p
    lib.ei_device_unref.argtypes = [ctypes.c_void_p]

    # Input injection
    lib.ei_device_pointer_motion.restype = None
    lib.ei_device_pointer_motion.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
    lib.ei_device_pointer_motion_absolute.restype = None
    lib.ei_device_pointer_motion_absolute.argtypes = [
        ctypes.c_void_p,
        ctypes.c_double,
        ctypes.c_double,
    ]
    lib.ei_device_button_button.restype = None
    lib.ei_device_button_button.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int]
    lib.ei_device_scroll_delta.restype = None
    lib.ei_device_scroll_delta.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
    lib.ei_device_scroll_discrete.restype = None
    lib.ei_device_scroll_discrete.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32]
    lib.ei_device_scroll_stop.restype = None
    lib.ei_device_scroll_stop.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    lib.ei_device_keyboard_key.restype = None
    lib.ei_device_keyboard_key.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int]
    lib.ei_device_frame.restype = None
    lib.ei_device_frame.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    lib.ei_device_start_emulating.restype = None
    lib.ei_device_start_emulating.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.ei_device_stop_emulating.restype = None
    lib.ei_device_stop_emulating.argtypes = [ctypes.c_void_p]

    # Touch functions
    lib.ei_device_touch_new.restype = ctypes.c_void_p
    lib.ei_device_touch_new.argtypes = [ctypes.c_void_p]
    lib.ei_touch_down.restype = None
    lib.ei_touch_down.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
    lib.ei_touch_motion.restype = None
    lib.ei_touch_motion.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
    lib.ei_touch_up.restype = None
    lib.ei_touch_up.argtypes = [ctypes.c_void_p]
    lib.ei_touch_unref.restype = ctypes.c_void_p
    lib.ei_touch_unref.argtypes = [ctypes.c_void_p]

    return lib


# Module-level libei instance (loaded once)
_libei = _load_libei()


class EISClient:
    """Low-level EIS client using KWin's direct D-Bus interface + libei.

    Connects to KWin's org.kde.KWin.EIS.RemoteDesktop D-Bus interface
    to get an EIS file descriptor, then uses libei to negotiate devices
    and inject input events.
    """

    def __init__(self, dbus_address: str) -> None:
        DBusGMainLoop(set_as_default=True)
        self._bus = dbus.bus.BusConnection(dbus_address)
        self._ei: int = 0  # ctypes void pointer (int representation)
        self._cookie: int = 0
        self._pointer: int = 0  # absolute pointer device
        self._keyboard: int = 0  # keyboard device
        self._touch_device: int = 0  # touch-capable device
        self._eis_iface: dbus.Interface | None = None
        self._next_touch_id: int = 0  # auto-increment touch ID
        self._active_touches: dict[int, int] = {}  # touch_id -> ctypes pointer
        self._setup()

    def _setup(self) -> None:
        """Connect to KWin EIS and negotiate devices."""
        eis_obj = self._bus.get_object("org.kde.KWin", "/org/kde/KWin/EIS/RemoteDesktop")
        self._eis_iface = dbus.Interface(eis_obj, "org.kde.KWin.EIS.RemoteDesktop")

        # Request all relevant capabilities
        caps = (
            _EI_CAP_POINTER
            | _EI_CAP_POINTER_ABSOLUTE
            | _EI_CAP_KEYBOARD
            | _EI_CAP_TOUCH
            | _EI_CAP_BUTTON
            | _EI_CAP_SCROLL
        )
        result = self._eis_iface.connectToEIS(dbus.Int32(caps))
        fd = result[0].take()
        self._cookie = int(result[1])

        # Create libei sender context
        self._ei = _libei.ei_new_sender(None)
        if not self._ei:
            msg = "Failed to create EI context"
            raise RuntimeError(msg)

        _libei.ei_configure_name(self._ei, b"kwin-mcp")

        ret = _libei.ei_setup_backend_fd(self._ei, fd)
        if ret != 0:
            _libei.ei_unref(self._ei)
            self._ei = 0
            msg = f"ei_setup_backend_fd failed: {ret}"
            raise RuntimeError(msg)

        # Process handshake events to get devices
        self._negotiate_devices()

    def _negotiate_devices(self, timeout: float = 5.0) -> None:
        """Process EIS handshake events until we have pointer + keyboard."""
        ei_fd = _libei.ei_get_fd(self._ei)
        start = time.monotonic()

        while time.monotonic() - start < timeout:
            readable, _, _ = select.select([ei_fd], [], [], 0.3)
            if readable:
                ret = _libei.ei_dispatch(self._ei)
                if ret < 0:
                    break

            while True:
                event = _libei.ei_get_event(self._ei)
                if not event:
                    break

                etype = _libei.ei_event_get_type(event)

                if etype == _EI_EVENT_DISCONNECT:
                    _libei.ei_event_unref(event)
                    msg = "EIS server disconnected during handshake"
                    raise RuntimeError(msg)

                if etype == _EI_EVENT_SEAT_ADDED:
                    self._bind_seat_capabilities(event)

                elif etype == _EI_EVENT_DEVICE_ADDED:
                    self._register_device(event)

                elif etype == _EI_EVENT_DEVICE_RESUMED:
                    pass  # Device ready for input

                _libei.ei_event_unref(event)

            if self._pointer and self._keyboard:
                break

        if not self._pointer:
            msg = "No pointer device available from EIS"
            raise RuntimeError(msg)
        if not self._keyboard:
            msg = "No keyboard device available from EIS"
            raise RuntimeError(msg)

        # Start emulating on all devices
        _libei.ei_device_start_emulating(self._pointer, 0)
        if self._keyboard != self._pointer:
            _libei.ei_device_start_emulating(self._keyboard, 0)
        if self._touch_device and self._touch_device not in (self._pointer, self._keyboard):
            _libei.ei_device_start_emulating(self._touch_device, 0)

    def _bind_seat_capabilities(self, event: int) -> None:
        """Bind to all available capabilities on the seat."""
        seat = _libei.ei_event_get_seat(event)

        bind_list: list[int] = []
        for cap in [
            _EI_CAP_POINTER,
            _EI_CAP_POINTER_ABSOLUTE,
            _EI_CAP_KEYBOARD,
            _EI_CAP_TOUCH,
            _EI_CAP_BUTTON,
            _EI_CAP_SCROLL,
        ]:
            if _libei.ei_seat_has_capability(seat, cap):
                bind_list.append(cap)

        # Call variadic ei_seat_bind_capabilities(seat, cap1, ..., NULL)
        func = _libei.ei_seat_bind_capabilities
        func.restype = None
        args: list[ctypes.c_uint | ctypes.c_void_p] = [ctypes.c_uint(c) for c in bind_list]
        args.append(ctypes.c_void_p(None))  # NULL sentinel
        func(seat, *args)

    def _register_device(self, event: int) -> None:
        """Register a device from a DEVICE_ADDED event."""
        device = _libei.ei_event_get_device(event)

        has_abs = _libei.ei_device_has_capability(device, _EI_CAP_POINTER_ABSOLUTE)
        has_kbd = _libei.ei_device_has_capability(device, _EI_CAP_KEYBOARD)
        has_touch = _libei.ei_device_has_capability(device, _EI_CAP_TOUCH)

        # Prefer absolute pointer device
        if has_abs and not self._pointer:
            self._pointer = _libei.ei_device_ref(device)
        if has_kbd and not self._keyboard:
            self._keyboard = _libei.ei_device_ref(device)
        if has_touch and not self._touch_device:
            self._touch_device = _libei.ei_device_ref(device)

    def _now_us(self) -> int:
        """Current time in microseconds."""
        return int(time.monotonic() * 1_000_000)

    def _flush(self) -> None:
        """Dispatch pending events to send data to KWin."""
        _libei.ei_dispatch(self._ei)

    def pointer_move_absolute(self, x: float, y: float) -> None:
        """Move pointer to absolute coordinates."""
        _libei.ei_device_pointer_motion_absolute(self._pointer, x, y)
        _libei.ei_device_frame(self._pointer, self._now_us())
        self._flush()

    def pointer_button(self, button: int, state: int) -> None:
        """Press/release a mouse button (evdev button code)."""
        _libei.ei_device_button_button(self._pointer, button, state)
        _libei.ei_device_frame(self._pointer, self._now_us())
        self._flush()

    def pointer_scroll(self, dx: float, dy: float) -> None:
        """Scroll by pixel delta."""
        _libei.ei_device_scroll_delta(self._pointer, dx, dy)
        _libei.ei_device_frame(self._pointer, self._now_us())
        self._flush()

    def pointer_scroll_discrete(self, dx: int, dy: int) -> None:
        """Scroll by discrete steps (wheel ticks)."""
        _libei.ei_device_scroll_discrete(self._pointer, dx, dy)
        _libei.ei_device_frame(self._pointer, self._now_us())
        self._flush()

    def pointer_scroll_stop(self) -> None:
        """Signal end of scroll."""
        _libei.ei_device_scroll_stop(self._pointer, 1, 1)
        _libei.ei_device_frame(self._pointer, self._now_us())
        self._flush()

    def keyboard_key(self, keycode: int, state: int) -> None:
        """Press/release a key (evdev keycode)."""
        _libei.ei_device_keyboard_key(self._keyboard, keycode, state)
        _libei.ei_device_frame(self._keyboard, self._now_us())
        self._flush()

    def touch_down(self, x: float, y: float) -> int:
        """Start a new touch at (x, y). Returns a touch ID."""
        device = self._touch_device or self._pointer
        touch = _libei.ei_device_touch_new(device)
        if not touch:
            msg = "Failed to create touch object"
            raise RuntimeError(msg)
        _libei.ei_touch_down(touch, x, y)
        _libei.ei_device_frame(device, self._now_us())
        self._flush()

        touch_id = self._next_touch_id
        self._next_touch_id += 1
        self._active_touches[touch_id] = touch
        return touch_id

    def touch_move(self, touch_id: int, x: float, y: float) -> None:
        """Move an active touch to (x, y)."""
        touch = self._active_touches.get(touch_id)
        if touch is None:
            msg = f"No active touch with ID {touch_id}"
            raise ValueError(msg)
        device = self._touch_device or self._pointer
        _libei.ei_touch_motion(touch, x, y)
        _libei.ei_device_frame(device, self._now_us())
        self._flush()

    def touch_up(self, touch_id: int) -> None:
        """End an active touch."""
        touch = self._active_touches.pop(touch_id, None)
        if touch is None:
            msg = f"No active touch with ID {touch_id}"
            raise ValueError(msg)
        device = self._touch_device or self._pointer
        _libei.ei_touch_up(touch)
        _libei.ei_device_frame(device, self._now_us())
        _libei.ei_touch_unref(touch)
        self._flush()

    def close(self) -> None:
        """Clean up EIS connection."""
        # Release any lingering touches
        for touch in self._active_touches.values():
            _libei.ei_touch_up(touch)
            _libei.ei_touch_unref(touch)
        self._active_touches.clear()

        if self._touch_device and self._touch_device not in (self._pointer, self._keyboard):
            _libei.ei_device_stop_emulating(self._touch_device)
            _libei.ei_device_unref(self._touch_device)
            self._touch_device = 0
        if self._pointer:
            _libei.ei_device_stop_emulating(self._pointer)
            _libei.ei_device_unref(self._pointer)
            self._pointer = 0
        if self._keyboard and self._keyboard != self._pointer:
            _libei.ei_device_stop_emulating(self._keyboard)
            _libei.ei_device_unref(self._keyboard)
            self._keyboard = 0

        if self._eis_iface and self._cookie:
            with contextlib.suppress(dbus.DBusException):
                self._eis_iface.disconnect(dbus.Int32(self._cookie))

        if self._ei:
            _libei.ei_unref(self._ei)
            self._ei = 0


class InputBackend:
    """High-level input injection for an isolated KWin session.

    Wraps EISClient with convenient methods for mouse and keyboard
    operations including click, drag, scroll, and key combos.
    """

    def __init__(self, dbus_address: str) -> None:
        self._client = EISClient(dbus_address)

    def mouse_move(self, x: int, y: int) -> None:
        """Move mouse to absolute coordinates (hover)."""
        self._client.pointer_move_absolute(float(x), float(y))

    def mouse_click(
        self,
        x: int,
        y: int,
        button: MouseButton = MouseButton.LEFT,
        *,
        double: bool = False,
        click_count: int = 1,
        modifiers: list[str] | None = None,
        hold_ms: int = 0,
    ) -> None:
        """Click at the given coordinates.

        Args:
            x, y: Coordinates to click at.
            button: Mouse button to use.
            double: If True, double-click (shorthand for click_count=2).
            click_count: Number of consecutive clicks (1=single, 2=double, 3=triple).
            modifiers: Modifier keys to hold during click (e.g. ["ctrl"], ["shift", "alt"]).
            hold_ms: Duration to hold button pressed before release (for long-press).
        """
        if double and click_count == 1:
            click_count = 2

        btn_code = _BTN_CODES[button]
        mod_codes = _resolve_modifiers(modifiers)

        self.mouse_move(x, y)
        time.sleep(0.02)

        # Press modifier keys
        for mod in mod_codes:
            self._client.keyboard_key(mod, _PRESSED)
            time.sleep(0.01)

        for i in range(click_count):
            if i > 0:
                time.sleep(0.05)
            self._client.pointer_button(btn_code, _PRESSED)
            if hold_ms > 0 and i == click_count - 1:
                time.sleep(max(0.01, hold_ms / 1000.0))
            else:
                time.sleep(0.01)
            self._client.pointer_button(btn_code, _RELEASED)

        # Release modifier keys in reverse order
        for mod in reversed(mod_codes):
            time.sleep(0.01)
            self._client.keyboard_key(mod, _RELEASED)

    def mouse_scroll(
        self,
        x: int,
        y: int,
        delta: int,
        *,
        horizontal: bool = False,
        discrete: bool = False,
        steps: int = 1,
    ) -> None:
        """Scroll at the given coordinates.

        Args:
            x, y: Coordinates to scroll at.
            delta: Scroll amount (positive = down/right, negative = up/left).
            horizontal: If True, scroll horizontally.
            discrete: If True, use discrete scroll (wheel ticks) instead of smooth pixels.
            steps: Split total delta into this many increments with 10ms intervals.
        """
        self.mouse_move(x, y)
        time.sleep(0.02)

        if discrete:
            dx = delta if horizontal else 0
            dy = delta if not horizontal else 0
            if steps > 1:
                for i in range(steps):
                    frac_dx = dx // steps + (1 if i < dx % steps else 0) if dx else 0
                    frac_dy = dy // steps + (1 if i < dy % steps else 0) if dy else 0
                    if frac_dx or frac_dy:
                        self._client.pointer_scroll_discrete(frac_dx, frac_dy)
                    time.sleep(0.01)
            else:
                self._client.pointer_scroll_discrete(dx, dy)
            self._client.pointer_scroll_stop()
        else:
            total_dx = float(delta) * _SCROLL_STEP_PIXELS if horizontal else 0.0
            total_dy = float(delta) * _SCROLL_STEP_PIXELS if not horizontal else 0.0
            if steps > 1:
                step_dx = total_dx / steps
                step_dy = total_dy / steps
                for _ in range(steps):
                    self._client.pointer_scroll(step_dx, step_dy)
                    time.sleep(0.01)
            else:
                self._client.pointer_scroll(total_dx, total_dy)
            self._client.pointer_scroll_stop()

    def mouse_drag(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        button: MouseButton = MouseButton.LEFT,
        modifiers: list[str] | None = None,
        waypoints: list[tuple[int, int, int]] | None = None,
    ) -> None:
        """Drag from one point to another.

        Args:
            from_x, from_y: Starting coordinates.
            to_x, to_y: Ending coordinates.
            button: Mouse button to use for dragging.
            modifiers: Modifier keys to hold during drag (e.g. ["alt"], ["ctrl"]).
            waypoints: Intermediate points as (x, y, dwell_ms) tuples.
        """
        btn_code = _BTN_CODES[button]
        mod_codes = _resolve_modifiers(modifiers)

        self.mouse_move(from_x, from_y)
        time.sleep(0.05)

        # Press modifier keys
        for mod in mod_codes:
            self._client.keyboard_key(mod, _PRESSED)
            time.sleep(0.01)

        self._client.pointer_button(btn_code, _PRESSED)
        time.sleep(0.02)

        # Build full path: start -> waypoints -> end
        segments: list[tuple[int, int, int, int, int]] = []  # (fx, fy, tx, ty, dwell_ms)
        prev_x, prev_y = from_x, from_y
        if waypoints:
            for wx, wy, dwell_ms in waypoints:
                segments.append((prev_x, prev_y, wx, wy, dwell_ms))
                prev_x, prev_y = wx, wy
        segments.append((prev_x, prev_y, to_x, to_y, 0))

        for seg_fx, seg_fy, seg_tx, seg_ty, dwell_ms in segments:
            dx = seg_tx - seg_fx
            dy = seg_ty - seg_fy
            steps = max(10, int((dx**2 + dy**2) ** 0.5 / 10))
            for i in range(1, steps + 1):
                frac = i / steps
                cx = seg_fx + dx * frac
                cy = seg_fy + dy * frac
                self._client.pointer_move_absolute(cx, cy)
                time.sleep(0.01)
            if dwell_ms > 0:
                time.sleep(dwell_ms / 1000.0)

        time.sleep(0.02)
        self._client.pointer_button(btn_code, _RELEASED)

        # Release modifier keys in reverse order
        for mod in reversed(mod_codes):
            time.sleep(0.01)
            self._client.keyboard_key(mod, _RELEASED)

    def mouse_button_down(self, x: int, y: int, button: MouseButton = MouseButton.LEFT) -> None:
        """Move to coordinates and press a mouse button without releasing.

        Args:
            x, y: Coordinates.
            button: Mouse button to press.
        """
        btn_code = _BTN_CODES[button]
        self.mouse_move(x, y)
        time.sleep(0.02)
        self._client.pointer_button(btn_code, _PRESSED)

    def mouse_button_up(self, x: int, y: int, button: MouseButton = MouseButton.LEFT) -> None:
        """Move to coordinates and release a mouse button.

        Args:
            x, y: Coordinates.
            button: Mouse button to release.
        """
        btn_code = _BTN_CODES[button]
        self.mouse_move(x, y)
        time.sleep(0.02)
        self._client.pointer_button(btn_code, _RELEASED)

    def keyboard_type(self, text: str) -> None:
        """Type a string of text character by character."""
        for char in text:
            entry = _CHAR_KEY_MAP.get(char)
            if entry is None:
                continue

            keycode, needs_shift = entry
            if needs_shift:
                self._client.keyboard_key(_MODIFIER_KEYS["shift"], _PRESSED)
                time.sleep(0.01)

            self._client.keyboard_key(keycode, _PRESSED)
            time.sleep(0.01)
            self._client.keyboard_key(keycode, _RELEASED)

            if needs_shift:
                time.sleep(0.01)
                self._client.keyboard_key(_MODIFIER_KEYS["shift"], _RELEASED)

            time.sleep(0.02)

    def keyboard_key(self, key: str) -> None:
        """Press a key combination (e.g., 'ctrl+c', 'Return', 'alt+F4').

        Supports modifier combinations with '+' separator.
        """
        modifiers, keycode = _parse_key_combo(key)
        if keycode is None:
            return

        # Press modifiers
        for mod in modifiers:
            self._client.keyboard_key(mod, _PRESSED)
            time.sleep(0.01)

        # Press and release main key
        self._client.keyboard_key(keycode, _PRESSED)
        time.sleep(0.01)
        self._client.keyboard_key(keycode, _RELEASED)

        # Release modifiers in reverse order
        for mod in reversed(modifiers):
            time.sleep(0.01)
            self._client.keyboard_key(mod, _RELEASED)

    def keyboard_key_down(self, key: str) -> None:
        """Press (and hold) a key combination without releasing.

        Useful for holding modifier keys across multiple actions.

        Args:
            key: Key to press (e.g., "ctrl", "shift+a", "alt").
        """
        modifiers, keycode = _parse_key_combo(key)

        for mod in modifiers:
            self._client.keyboard_key(mod, _PRESSED)
            time.sleep(0.01)

        if keycode is not None:
            self._client.keyboard_key(keycode, _PRESSED)
            time.sleep(0.01)

    def keyboard_key_up(self, key: str) -> None:
        """Release a previously pressed key combination.

        Releases in reverse order (main key first, then modifiers).

        Args:
            key: Key to release (e.g., "ctrl", "shift+a", "alt").
        """
        modifiers, keycode = _parse_key_combo(key)

        if keycode is not None:
            self._client.keyboard_key(keycode, _RELEASED)
            time.sleep(0.01)

        for mod in reversed(modifiers):
            self._client.keyboard_key(mod, _RELEASED)
            time.sleep(0.01)

    def touch_tap(self, x: int, y: int, hold_ms: int = 0) -> None:
        """Tap at the given coordinates.

        Args:
            x, y: Coordinates to tap at.
            hold_ms: Duration to hold before lifting (for long-press).
        """
        tid = self._client.touch_down(float(x), float(y))
        if hold_ms > 0:
            time.sleep(max(0.01, hold_ms / 1000.0))
        else:
            time.sleep(0.01)
        self._client.touch_up(tid)

    def touch_swipe(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        duration_ms: int = 300,
    ) -> None:
        """Swipe from one point to another.

        Args:
            from_x, from_y: Starting coordinates.
            to_x, to_y: Ending coordinates.
            duration_ms: Duration of the swipe in milliseconds.
        """
        steps = max(10, duration_ms // 10)
        dx = to_x - from_x
        dy = to_y - from_y

        tid = self._client.touch_down(float(from_x), float(from_y))
        step_delay = max(0.001, duration_ms / 1000.0 / steps)

        for i in range(1, steps + 1):
            frac = i / steps
            cx = from_x + dx * frac
            cy = from_y + dy * frac
            self._client.touch_move(tid, cx, cy)
            time.sleep(step_delay)

        self._client.touch_up(tid)

    def touch_pinch(
        self,
        center_x: int,
        center_y: int,
        start_distance: int,
        end_distance: int,
        duration_ms: int = 500,
    ) -> None:
        """Pinch gesture with two fingers.

        Args:
            center_x, center_y: Center point of the pinch.
            start_distance: Initial distance between fingers (pixels).
            end_distance: Final distance between fingers (pixels).
            duration_ms: Duration of the gesture.
        """
        steps = max(10, duration_ms // 10)
        step_delay = max(0.001, duration_ms / 1000.0 / steps)

        # Two fingers start symmetrically on the x-axis
        half_start = start_distance / 2.0
        tid1 = self._client.touch_down(float(center_x - half_start), float(center_y))
        tid2 = self._client.touch_down(float(center_x + half_start), float(center_y))

        for i in range(1, steps + 1):
            frac = i / steps
            half = half_start + (end_distance / 2.0 - half_start) * frac
            self._client.touch_move(tid1, float(center_x - half), float(center_y))
            self._client.touch_move(tid2, float(center_x + half), float(center_y))
            time.sleep(step_delay)

        self._client.touch_up(tid1)
        self._client.touch_up(tid2)

    def touch_multi_swipe(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        fingers: int = 3,
        duration_ms: int = 300,
    ) -> None:
        """Multi-finger swipe gesture.

        Args:
            from_x, from_y: Starting coordinates (center of finger group).
            to_x, to_y: Ending coordinates.
            fingers: Number of fingers (2-5).
            duration_ms: Duration of the swipe.
        """
        steps = max(10, duration_ms // 10)
        dx = to_x - from_x
        dy = to_y - from_y
        step_delay = max(0.001, duration_ms / 1000.0 / steps)
        finger_spacing = 20  # pixels between fingers

        # Start touches spread vertically around center
        tids: list[int] = []
        for f in range(fingers):
            offset = (f - (fingers - 1) / 2.0) * finger_spacing
            tid = self._client.touch_down(float(from_x), float(from_y + offset))
            tids.append(tid)

        for i in range(1, steps + 1):
            frac = i / steps
            cx = from_x + dx * frac
            cy = from_y + dy * frac
            for f, tid in enumerate(tids):
                offset = (f - (fingers - 1) / 2.0) * finger_spacing
                self._client.touch_move(tid, cx, cy + offset)
            time.sleep(step_delay)

        for tid in tids:
            self._client.touch_up(tid)

    def keyboard_type_unicode(self, text: str, dbus_address: str | None = None) -> bool:
        """Type arbitrary Unicode text using wtype or clipboard fallback.

        Args:
            text: Text to type (supports non-ASCII, e.g. Korean, CJK).
            dbus_address: D-Bus address for the session (needed for wl-copy fallback).

        Returns:
            True if text was typed successfully.
        """
        env = dict(__import__("os").environ)
        if dbus_address:
            env["DBUS_SESSION_BUS_ADDRESS"] = dbus_address

        # Try wtype first
        if shutil.which("wtype"):
            result = subprocess.run(
                ["wtype", "--", text],
                env=env,
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0

        # Fallback: clipboard paste via wl-copy + Ctrl+V
        # Use Popen + DEVNULL to avoid pipe-blocking from wl-copy's forked child
        if shutil.which("wl-copy"):
            cp = subprocess.Popen(
                ["wl-copy", "--", text],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.1)  # Wait for fork to complete
            if cp.poll() is None or cp.returncode == 0:
                self.keyboard_key("ctrl+v")
                return True

        return False

    def close(self) -> None:
        """Close the EIS connection."""
        self._client.close()


def _key_name_to_evdev(name: str) -> int | None:
    """Convert a key name to its Linux evdev keycode."""
    lower = name.lower()

    if lower in _EVDEV_KEY_MAP:
        return _EVDEV_KEY_MAP[lower]

    # Single character → look up in character map
    if len(name) == 1:
        entry = _CHAR_KEY_MAP.get(name)
        if entry:
            return entry[0]

    return None


def _resolve_modifiers(modifiers: list[str] | None) -> list[int]:
    """Resolve modifier key names to evdev keycodes."""
    if not modifiers:
        return []
    codes: list[int] = []
    for mod in modifiers:
        code = _MODIFIER_KEYS.get(mod.lower())
        if code is not None:
            codes.append(code)
    return codes


def _parse_key_combo(key: str) -> tuple[list[int], int | None]:
    """Parse a key combo string into (modifier_codes, main_keycode).

    Returns a tuple of (list of modifier evdev keycodes, main key evdev keycode or None).
    """
    parts = key.split("+")
    modifiers: list[int] = []
    main_key: str | None = None

    for part in parts:
        part_lower = part.strip().lower()
        if part_lower in _MODIFIER_KEYS:
            modifiers.append(_MODIFIER_KEYS[part_lower])
        else:
            main_key = part.strip()

    keycode: int | None = None
    if main_key is not None:
        keycode = _key_name_to_evdev(main_key)

    return modifiers, keycode
