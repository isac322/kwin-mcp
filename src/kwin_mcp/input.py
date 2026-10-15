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
import os
import select
import shutil
import subprocess
import sys
import threading
import time
from enum import Enum
from typing import TYPE_CHECKING

import dbus
import dbus.bus
from dbus.mainloop.glib import DBusGMainLoop

from kwin_mcp import progress

# Protocol constants only; importing the helper module loads no C library.
from kwin_mcp.clipboard import MAX_COPY_BYTES, READY_BUDGET_S, RESTORE_ROUNDTRIP_S
from kwin_mcp.models import HeldInput, TypeRoute

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

# Longest uninterrupted sleep while holding a button or touch point, so a
# long-press can report progress; the total hold duration is unchanged.
_HOLD_SLICE_S = 0.5

# keyboard_type reports progress at most this often.
_TYPE_REPORT_INTERVAL_S = 0.5


# Set once the server is exiting: every long wait inside a gesture (holds,
# dwells, swipe and pinch steps) then stops at once, and the gesture's own
# cleanup releases what it pressed, so the exit path can run session_stop.
_ABORT = threading.Event()


class InputAbortedError(RuntimeError):
    """A gesture stopped early because kwin-mcp is shutting down."""


def abort_waits() -> None:
    """Make every current and later gesture wait end at once (server exit only)."""
    _ABORT.set()


def _wait(seconds: float) -> None:
    """Sleep ``seconds`` inside a gesture; raise InputAbortedError once exiting."""
    if _ABORT.wait(max(seconds, 0.0)):
        msg = "kwin-mcp is shutting down; the gesture stopped and released its input"
        raise InputAbortedError(msg)


def _hold(seconds: float, message: str) -> None:
    """Sleep for ``seconds`` in slices of at most ``_HOLD_SLICE_S``, reporting progress."""
    start = time.monotonic()
    deadline = start + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        _wait(min(remaining, _HOLD_SLICE_S))
        progress.report(min(time.monotonic() - start, seconds), seconds, message)


def _report_step(done: int, total: int, message: str) -> None:
    """Report step-loop progress about every 10% of ``total`` steps and at the end."""
    if done == total or done % max(1, total // 10) == 0:
        progress.report(done, total, message)


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

# Give KWin enough time to dispatch each button transition while keeping
# consecutive presses comfortably inside GTK's multi-click recognition window.
_CLICK_PRESS_SECONDS = 0.025
_MULTI_CLICK_GAP_SECONDS = 0.06

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


def check_typeable(text: str) -> None:
    """Raise ValueError naming every character the US QWERTY key map cannot produce.

    The single source of truth for what ``keyboard_type`` accepts, so callers can
    reject unsupported text before touching any input backend.
    """
    unsupported = dict.fromkeys(char for char in text if char not in _CHAR_KEY_MAP)
    if unsupported:
        chars = ", ".join(repr(char) for char in unsupported)
        msg = f'keyboard_type cannot type {chars}.\nRetry with method="unicode".'
        raise ValueError(msg)


class HeldControlError(RuntimeError):
    """A call conflicts with a mouse button or key kwin-mcp holds pressed."""


# Canonical name of every keycode a KeyChord or typed text can produce: the
# modifier names, then the first special-key name, then the unshifted character.
_KEY_NAMES: dict[int, str] = {42: "shift", 29: "ctrl", 56: "alt", 125: "super"}
for _name, _code in _EVDEV_KEY_MAP.items():
    _KEY_NAMES.setdefault(_code, _name)
for _char, (_code, _shifted) in _CHAR_KEY_MAP.items():
    if not _shifted:
        _KEY_NAMES.setdefault(_code, _char)

_SHIFT_KEYCODE = _MODIFIER_KEYS["shift"]


def key_name(keycode: int) -> str:
    """Return the canonical lowercase name of an evdev keycode (``shift``, ``return``, ``a``)."""
    return _KEY_NAMES.get(keycode, f"keycode {keycode}")


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
    """Resolve modifier key names to evdev keycodes.

    Raises ValueError for an unknown or empty name, before any input is sent.
    """
    if not modifiers:
        return []
    codes: list[int] = []
    for mod in modifiers:
        code = _MODIFIER_KEYS.get(mod.lower())
        if code is None:
            msg = f"Unknown modifier: {mod!r}"
            raise ValueError(msg)
        codes.append(code)
    return codes


def chord_keycodes(chord: str) -> list[int]:
    """Parse a KeyChord into evdev keycodes: modifiers in chord order, then the main key.

    Names are ``+``-joined and case-insensitive (``ctrl``/``control``,
    ``super``/``meta``, ``return``, ``f4``, ``a``). A chord of modifiers only
    uses its last modifier as the main key (``ctrl+shift``). When several
    non-modifier names are given, the last one is the main key, as before.
    Raises ValueError ``Unknown key: <chord>`` for any unknown or empty name,
    before any input is sent.
    """
    modifiers: list[int] = []
    keycode: int | None = None
    for part in chord.split("+"):
        name = part.strip()
        modifier = _MODIFIER_KEYS.get(name.lower())
        if modifier is not None:
            modifiers.append(modifier)
            continue
        keycode = _key_name_to_evdev(name)
        if keycode is None:
            msg = f"Unknown key: {chord}"
            raise ValueError(msg)
    if keycode is None:
        # Every name was a modifier (split always yields at least one part).
        keycode = modifiers.pop()
    return [*modifiers, keycode]


def modifier_keycodes(chord: str | None) -> list[int]:
    """Resolve the ``modifiers`` KeyChord of a pointer gesture to evdev keycodes.

    Raises ValueError ``Unknown modifier: '<m>'`` for a name that is not a modifier.
    """
    if chord is None:
        return []
    return _resolve_modifiers(chord.split("+"))


def text_keycodes(text: str) -> set[int]:
    """Return every keycode ``keyboard_type`` presses for ``text``, incl. shift when needed.

    Characters the key map cannot produce are skipped; ``check_typeable`` rejects them.
    """
    codes: set[int] = set()
    for char in text:
        entry = _CHAR_KEY_MAP.get(char)
        if entry is None:
            continue
        code, needs_shift = entry
        codes.add(code)
        if needs_shift:
            codes.add(_SHIFT_KEYCODE)
    return codes


# InputBackend.revalidate() reports this once the EIS connection is gone. kwin-mcp can
# no longer release anything, and KWin 6.3.6 and 6.7.5 keep an emulated button down.
EIS_DISCONNECTED_WARNING = (
    "EIS disconnected: held input is no longer tracked; KWin may keep it pressed"
)

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
_EI_EVENT_DEVICE_REMOVED = 6
_EI_EVENT_DEVICE_PAUSED = 7
_EI_EVENT_DEVICE_RESUMED = 8
_EI_EVENT_PONG = 90

# How long sync() waits for KWin to answer a ping before giving up.
_SYNC_TIMEOUT = 1.0

# How long a sender waits for KWin to replace a removed or paused device. KWin
# removes the old device, adds the replacement, and resumes it synchronously
# inside one EisDevice change (keyboard layout reconfigure, output change); the
# measured REMOVED -> RESUMED gap was at most 3 ms, so 1 s leaves a wide margin
# on slow hosts while a truly lost device still fails the tool call quickly.
_DEVICE_REPLACEMENT_TIMEOUT = 1.0
# Upper bound on consecutive ei_dispatch rounds per drain while the socket
# stays readable, so a flooding or EOF socket cannot spin forever.
_MAX_DISPATCH_ROUNDS = 64

# Scroll axis values (in libei, scroll is in pixels)
_SCROLL_STEP_PIXELS = 15.0
# libei expresses discrete scrolling in 120ths of a wheel detent.
_SCROLL_DISCRETE_UNIT = 120

_X11_POINTER_MIRROR_TIMEOUT_SECONDS = 5


def _mirror_x11_pointer(dbus_address: str, x: int, y: int) -> None:
    """Mirror an absolute EIS move to the X display used for scrot screenshots.

    EIS takes global logical coordinates while the X display holds each KWin
    output at its physical size, so the point is mapped through the current
    output topology. An unmappable point raises rather than moving the X
    cursor to a wrong pixel.
    """
    if os.environ.get("KWIN_MCP_X11_SCREENSHOT") != "1":
        return

    display = os.environ.get("DISPLAY")
    if not display:
        return

    from kwin_mcp.screenshot import x11_physical_point

    x, y = x11_physical_point(dbus_address, display, x, y)

    xdotool = shutil.which("xdotool")
    if xdotool is None:
        raise RuntimeError(
            "KWIN_MCP_X11_SCREENSHOT=1 requires xdotool to mirror the screenshot cursor"
        )

    env = {"DISPLAY": display}
    for name in ("HOME", "XAUTHORITY"):
        if value := os.environ.get(name):
            env[name] = value

    try:
        result = subprocess.run(
            [xdotool, "mousemove", str(x), str(y)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_X11_POINTER_MIRROR_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "KWIN_MCP_X11_SCREENSHOT=1 requires xdotool to mirror the screenshot cursor"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "xdotool timed out while mirroring the screenshot cursor "
            f"to ({x}, {y}) on DISPLAY={display}"
        ) from exc

    if result.returncode != 0:
        detail = result.stderr.strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            "xdotool failed to mirror the screenshot cursor "
            f"to ({x}, {y}) on DISPLAY={display} (exit {result.returncode}){suffix}"
        )


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
    # libei declares `void ei_dispatch(struct ei *ei)`. With a c_int restype
    # ctypes returns whatever the return register held, which is negative on
    # some builds (Fedora 44, Tumbleweed aarch64) and used to abort the
    # handshake before DEVICE_ADDED. Errors arrive as EI_EVENT_DISCONNECT.
    lib.ei_dispatch.restype = None
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

    # Round trips: ei_ping() is answered by EI_EVENT_PONG once KWin has handled
    # every request sent before it.
    lib.ei_new_ping.restype = ctypes.c_void_p
    lib.ei_new_ping.argtypes = [ctypes.c_void_p]
    lib.ei_ping.restype = None
    lib.ei_ping.argtypes = [ctypes.c_void_p]
    lib.ei_ping_get_id.restype = ctypes.c_uint64
    lib.ei_ping_get_id.argtypes = [ctypes.c_void_p]
    lib.ei_ping_unref.restype = ctypes.c_void_p
    lib.ei_ping_unref.argtypes = [ctypes.c_void_p]
    lib.ei_event_pong_get_ping.restype = ctypes.c_void_p
    lib.ei_event_pong_get_ping.argtypes = [ctypes.c_void_p]

    # Seat functions
    lib.ei_event_get_seat.restype = ctypes.c_void_p
    lib.ei_event_get_seat.argtypes = [ctypes.c_void_p]
    lib.ei_seat_has_capability.restype = ctypes.c_int
    lib.ei_seat_has_capability.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    lib.ei_seat_ref.restype = ctypes.c_void_p
    lib.ei_seat_ref.argtypes = [ctypes.c_void_p]
    lib.ei_seat_bind_capabilities.restype = None
    lib.ei_seat_bind_capabilities.argtypes = [ctypes.c_void_p]  # variadic: fixed param only

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


# Module-level libei instance, loaded lazily on first use. Loading eagerly at
# import time makes importing this module fail on systems without libei
# installed (e.g. CI jobs that only lint or type-check), even though no EIS
# input path is touched there.
_libei: ctypes.CDLL | None = None


def _get_libei() -> ctypes.CDLL:
    """Return the process-wide libei handle, loading it on first call.

    A library that is absent or fails to load raises RuntimeError so the
    input backend stays optional (see AutomationEngine.session_start);
    the handle stays unset, so a later call retries the load.
    """
    global _libei
    if _libei is None:
        try:
            _libei = _load_libei()
        except OSError as exc:
            msg = (
                f"libei could not be loaded ({exc}); install libei "
                "(e.g. 'sudo apt install libei1' or 'sudo pacman -S libei')"
            )
            raise RuntimeError(msg) from exc
    return _libei


class EISClient:
    """Low-level EIS client using KWin's direct D-Bus interface + libei.

    Connects to KWin's org.kde.KWin.EIS.RemoteDesktop D-Bus interface
    to get an EIS file descriptor, then uses libei to negotiate devices
    and inject input events.
    """

    # Device slots per role: (role name used in errors, attribute holding the handle).
    _DEVICE_SLOTS: tuple[tuple[str, str], ...] = (
        ("pointer", "_pointer"),
        ("keyboard", "_keyboard"),
        ("touch", "_touch_device"),
    )

    def __init__(self, dbus_address: str) -> None:
        DBusGMainLoop(set_as_default=True)
        self._bus = dbus.bus.BusConnection(dbus_address)
        self._ei: int = 0  # ctypes void pointer (int representation)
        self._cookie: int = 0
        # Each slot holds one ei_device_ref() on its device; one device may fill several slots.
        self._pointer: int = 0  # absolute pointer device
        self._keyboard: int = 0  # keyboard device
        self._touch_device: int = 0  # touch-capable device
        self._eis_iface: dbus.Interface | None = None
        self._next_touch_id: int = 0  # auto-increment touch ID
        # touch_id -> (ei_touch pointer, device the touch was created on)
        self._active_touches: dict[int, tuple[int, int]] = {}
        # Touch IDs whose device KWin removed while the touch was down.
        self._abandoned_touches: set[int] = set()
        # role -> monotonic time its device was removed or paused, until a resume.
        self._lost_at: dict[str, float] = {}
        # Devices this client has called ei_device_start_emulating() on.
        self._emulating: set[int] = set()
        # Roles whose device saw REMOVED or PAUSED since the last take_lost_roles().
        self._lost_roles: set[str] = set()
        self._disconnected: bool = False
        # Ids of the pings KWin has answered (see sync()).
        self._pongs: set[int] = set()
        self._setup()

    def _setup(self) -> None:
        """Connect to KWin EIS and negotiate devices."""
        # Resolve libei before asking KWin for an EIS connection: a missing
        # library must fail before connectToEIS hands us a socket fd and a
        # cookie that nothing would close or disconnect.
        lib = _get_libei()

        # KWin only exposes the EIS interface when it supports remote input;
        # translate the D-Bus failure so callers can treat the input backend as
        # optional (see AutomationEngine.session_start).
        try:
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
        except dbus.DBusException as exc:
            msg = f"KWin EIS interface unavailable: {exc}"
            raise RuntimeError(msg) from exc
        fd: int | None = result[0].take()
        self._cookie = int(result[1])

        # From here on every failure must release what was acquired so far: the
        # socket fd until libei takes it, the ei context and devices, and the
        # KWin-side connection behind the cookie.
        try:
            # Create libei sender context
            self._ei = lib.ei_new_sender(None)
            if not self._ei:
                msg = "Failed to create EI context"
                raise RuntimeError(msg)

            lib.ei_configure_name(self._ei, b"kwin-mcp")

            # libei owns the fd once ei_setup_backend_fd is called, even when it fails.
            backend_fd, fd = fd, None
            ret = lib.ei_setup_backend_fd(self._ei, backend_fd)
            if ret != 0:
                msg = f"ei_setup_backend_fd failed: {ret}"
                raise RuntimeError(msg)

            # Process handshake events to get devices
            self._negotiate_devices()
        except BaseException:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            self.close()
            raise

    def _negotiate_devices(self, timeout: float = 5.0) -> None:
        """Process EIS handshake events until pointer + keyboard are usable.

        A libei device may only emulate once the server has resumed it. Calling
        ei_device_start_emulating() earlier is rejected ("device is not
        emulating") and every event sent afterwards is silently dropped, so
        emulation starts when EI_EVENT_DEVICE_RESUMED arrives for a held device
        (see _handle_event). A device removed during the handshake is replaced
        like any later removal.
        """
        ei_fd = _get_libei().ei_get_fd(self._ei)
        start = time.monotonic()

        while time.monotonic() - start < timeout:
            readable, _, _ = select.select([ei_fd], [], [], 0.3)
            if readable:
                _get_libei().ei_dispatch(self._ei)
            self._process_queued_events()

            if self._disconnected:
                msg = "EIS server disconnected during handshake"
                raise RuntimeError(msg)
            if self._pointer in self._emulating and self._keyboard in self._emulating:
                break

        if not self._pointer:
            msg = "No pointer device available from EIS"
            raise RuntimeError(msg)
        if not self._keyboard:
            msg = "No keyboard device available from EIS"
            raise RuntimeError(msg)

        # Devices that never announced a resume are still started: emulating a
        # paused device is no worse than the unconditional start this wait
        # replaced, and losing a slow-to-resume device would drop input that
        # used to work.
        for device in {self._pointer, self._keyboard, self._touch_device}:
            if device and device not in self._emulating:
                _get_libei().ei_device_start_emulating(device, 0)
                self._emulating.add(device)
        # Replacements during the handshake cannot have dropped held input.
        self._lost_roles.clear()

    def _process_queued_events(self) -> None:
        """Handle every event libei has already queued."""
        while True:
            event = _get_libei().ei_get_event(self._ei)
            if not event:
                return
            self._handle_event(event)

    def _handle_event(self, event: int) -> None:
        """Apply one libei event to the device state and release it.

        Shared by the handshake and by the drain every sender runs first, so a
        device KWin replaces later (a new keyboard on each layout reconfigure,
        a new absolute pointer/touch device on each output change) is tracked
        exactly like the devices of the handshake.
        """
        lib = _get_libei()
        try:
            etype = lib.ei_event_get_type(event)
            if etype == _EI_EVENT_DISCONNECT:
                self._disconnected = True
            elif etype == _EI_EVENT_SEAT_ADDED:
                self._bind_seat_capabilities(event)
            elif etype == _EI_EVENT_DEVICE_ADDED:
                self._register_device(event)
            elif etype in (
                _EI_EVENT_DEVICE_REMOVED,
                _EI_EVENT_DEVICE_PAUSED,
                _EI_EVENT_DEVICE_RESUMED,
            ):
                device = int(lib.ei_event_get_device(event) or 0)
                if device:
                    self._update_device_state(etype, device)
            elif etype == _EI_EVENT_PONG:
                ping = lib.ei_event_pong_get_ping(event)
                if ping:
                    self._pongs.add(int(lib.ei_ping_get_id(ping)))
            # Everything else (modifiers, sync, seat removal) needs no action.
        finally:
            lib.ei_event_unref(event)

    def sync(self, timeout: float = _SYNC_TIMEOUT) -> bool:
        """Wait until KWin has handled every request sent so far (a ping round trip).

        Returns False when the connection is gone or KWin did not answer within
        ``timeout``; never raises. Used before teardown, so the last releases are
        not left in libei's send buffer when the context is freed.
        """
        if not self._ei or self._disconnected:
            return False
        lib = _get_libei()
        ping = lib.ei_new_ping(self._ei)
        if not ping:
            return False
        try:
            ping_id = int(lib.ei_ping_get_id(ping))
            lib.ei_ping(ping)
            self._flush()
            deadline = time.monotonic() + timeout
            while ping_id not in self._pongs and not self._disconnected:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                with contextlib.suppress(OSError, ValueError):
                    self._drain_events(remaining)
            answered = ping_id in self._pongs
            self._pongs.discard(ping_id)
            return answered
        finally:
            lib.ei_ping_unref(ping)

    def _update_device_state(self, etype: int, device: int) -> None:
        """Apply a REMOVED, PAUSED, or RESUMED event for ``device``."""
        roles = [role for role, attr in self._DEVICE_SLOTS if getattr(self, attr) == device]
        if not roles:
            return  # a device this client never took, e.g. the relative pointer

        lib = _get_libei()
        if etype == _EI_EVENT_DEVICE_RESUMED:
            if device not in self._emulating:
                lib.ei_device_start_emulating(device, 0)
                self._emulating.add(device)
            for role in roles:
                self._lost_at.pop(role, None)
            return

        now = time.monotonic()
        for role in roles:
            self._lost_at.setdefault(role, now)
            self._lost_roles.add(role)
        self._emulating.discard(device)
        if etype == _EI_EVENT_DEVICE_PAUSED:
            return  # keep the slot and its ref until KWin resumes the device

        for role, attr in self._DEVICE_SLOTS:
            if role in roles:
                setattr(self, attr, 0)
                lib.ei_device_unref(device)
        for touch_id, (touch, touch_device) in list(self._active_touches.items()):
            if touch_device == device:
                lib.ei_touch_unref(touch)
                del self._active_touches[touch_id]
                self._abandoned_touches.add(touch_id)

    def _drain_events(self, timeout: float) -> None:
        """Handle queued EIS events, then read more, waiting up to ``timeout`` for the first.

        Events libei already queued are handled first, even when the socket is
        idle: every ei_dispatch() (including the one in _flush after each send)
        may read and queue REMOVED/ADDED/RESUMED or DISCONNECT events that no
        later select() would report. libei spreads one KWin device replacement
        over several dispatch rounds (REMOVED and the ADDED/RESUMED of its
        replacement arrive separately), so dispatch again while the socket stays
        readable, capped so a flooding or EOF socket cannot spin forever.
        """
        self._process_queued_events()
        if self._disconnected:
            return
        lib = _get_libei()
        ei_fd = lib.ei_get_fd(self._ei)
        wait = max(0.0, timeout)
        for _ in range(_MAX_DISPATCH_ROUNDS):
            readable, _, _ = select.select([ei_fd], [], [], wait)
            if not readable:
                return
            lib.ei_dispatch(self._ei)
            self._process_queued_events()
            if self._disconnected:
                return
            wait = 0.0

    @property
    def disconnected(self) -> bool:
        """Whether KWin closed the EIS connection (as of the last handled event)."""
        return self._disconnected

    def poll(self) -> None:
        """Handle every EIS event already available, without waiting."""
        if not self._ei:
            return  # closed (or never set up): nothing left to read
        self._drain_events(0.0)

    def take_lost_roles(self) -> set[str]:
        """Return the roles whose device was removed or paused since the last call, and reset."""
        lost, self._lost_roles = self._lost_roles, set()
        return lost

    def _live_device(self, role: str) -> int:
        """Return the usable device for ``role``, waiting briefly for a replacement.

        Raises RuntimeError when the EIS connection closed or when the role's
        device was removed (or paused) and nothing usable arrived within
        _DEVICE_REPLACEMENT_TIMEOUT of that event.
        """
        self._drain_events(0.0)
        while True:
            if self._disconnected:
                msg = "KWin closed the EIS connection; input is unavailable"
                raise RuntimeError(msg)
            slot_role = role
            # Without a dedicated touch device, touches go through the pointer.
            if role == "touch" and not self._touch_device and "touch" not in self._lost_at:
                slot_role = "pointer"
            device: int = getattr(self, dict(self._DEVICE_SLOTS)[slot_role])
            lost_at = self._lost_at.get(slot_role)
            if lost_at is None:
                if device:
                    return device
                msg = f"No {slot_role} device available from EIS"
                raise RuntimeError(msg)
            remaining = lost_at + _DEVICE_REPLACEMENT_TIMEOUT - time.monotonic()
            if remaining <= 0:
                msg = (
                    f"KWin removed the EIS {slot_role} device and no usable replacement "
                    f"arrived within {_DEVICE_REPLACEMENT_TIMEOUT:g} s"
                )
                raise RuntimeError(msg)
            self._drain_events(remaining)

    def _bind_seat_capabilities(self, event: int) -> None:
        """Bind to all available capabilities on the seat."""
        seat = _get_libei().ei_event_get_seat(event)

        bind_list: list[int] = []
        for cap in [
            _EI_CAP_POINTER,
            _EI_CAP_POINTER_ABSOLUTE,
            _EI_CAP_KEYBOARD,
            _EI_CAP_TOUCH,
            _EI_CAP_BUTTON,
            _EI_CAP_SCROLL,
        ]:
            if _get_libei().ei_seat_has_capability(seat, cap):
                bind_list.append(cap)

        # Call variadic ei_seat_bind_capabilities(seat, cap1, ..., NULL)
        func = _get_libei().ei_seat_bind_capabilities
        func.restype = None
        args: list[ctypes.c_uint | ctypes.c_void_p] = [ctypes.c_uint(c) for c in bind_list]
        args.append(ctypes.c_void_p(None))  # NULL sentinel
        func(seat, *args)

    def _register_device(self, event: int) -> None:
        """Register a device from a DEVICE_ADDED event into each empty matching slot."""
        device = _get_libei().ei_event_get_device(event)

        has_abs = _get_libei().ei_device_has_capability(device, _EI_CAP_POINTER_ABSOLUTE)
        has_kbd = _get_libei().ei_device_has_capability(device, _EI_CAP_KEYBOARD)
        has_touch = _get_libei().ei_device_has_capability(device, _EI_CAP_TOUCH)

        # Prefer absolute pointer device
        if has_abs and not self._pointer:
            self._pointer = _get_libei().ei_device_ref(device)
        if has_kbd and not self._keyboard:
            self._keyboard = _get_libei().ei_device_ref(device)
        if has_touch and not self._touch_device:
            self._touch_device = _get_libei().ei_device_ref(device)

    def _now_us(self) -> int:
        """Current time in microseconds."""
        return int(time.monotonic() * 1_000_000)

    def _flush(self) -> None:
        """Dispatch pending events to send data to KWin."""
        _get_libei().ei_dispatch(self._ei)

    def pointer_move_absolute(self, x: float, y: float) -> None:
        """Move pointer to absolute coordinates."""
        device = self._live_device("pointer")
        _get_libei().ei_device_pointer_motion_absolute(device, x, y)
        _get_libei().ei_device_frame(device, self._now_us())
        self._flush()

    def pointer_button(self, button: int, state: int) -> None:
        """Press/release a mouse button (evdev button code)."""
        device = self._live_device("pointer")
        _get_libei().ei_device_button_button(device, button, state)
        _get_libei().ei_device_frame(device, self._now_us())
        self._flush()

    def pointer_scroll(self, dx: float, dy: float) -> None:
        """Scroll by pixel delta."""
        device = self._live_device("pointer")
        _get_libei().ei_device_scroll_delta(device, dx, dy)
        _get_libei().ei_device_frame(device, self._now_us())
        self._flush()

    def pointer_scroll_discrete(self, dx: int, dy: int) -> None:
        """Scroll by discrete steps (wheel ticks)."""
        device = self._live_device("pointer")
        _get_libei().ei_device_scroll_discrete(device, dx, dy)
        _get_libei().ei_device_frame(device, self._now_us())
        self._flush()

    def pointer_scroll_stop(self) -> None:
        """Signal end of scroll."""
        device = self._live_device("pointer")
        _get_libei().ei_device_scroll_stop(device, 1, 1)
        _get_libei().ei_device_frame(device, self._now_us())
        self._flush()

    def keyboard_key(self, keycode: int, state: int) -> None:
        """Press/release a key (evdev keycode)."""
        device = self._live_device("keyboard")
        _get_libei().ei_device_keyboard_key(device, keycode, state)
        _get_libei().ei_device_frame(device, self._now_us())
        self._flush()

    def touch_down(self, x: float, y: float) -> int:
        """Start a new touch at (x, y). Returns a touch ID."""
        device = self._live_device("touch")
        touch = _get_libei().ei_device_touch_new(device)
        if not touch:
            msg = "Failed to create touch object"
            raise RuntimeError(msg)
        _get_libei().ei_touch_down(touch, x, y)
        _get_libei().ei_device_frame(device, self._now_us())
        self._flush()

        touch_id = self._next_touch_id
        self._next_touch_id += 1
        self._active_touches[touch_id] = (touch, device)
        return touch_id

    def _active_touch(self, touch_id: int) -> tuple[int, int]:
        """Return (touch, device) for an active touch after handling pending events."""
        self._drain_events(0.0)
        if self._disconnected:
            msg = "KWin closed the EIS connection; input is unavailable"
            raise RuntimeError(msg)
        entry = self._active_touches.get(touch_id)
        if entry is None:
            if touch_id in self._abandoned_touches:
                msg = f"KWin removed the EIS touch device while touch {touch_id} was down"
                raise RuntimeError(msg)
            msg = f"No active touch with ID {touch_id}"
            raise ValueError(msg)
        return entry

    def touch_move(self, touch_id: int, x: float, y: float) -> None:
        """Move an active touch to (x, y)."""
        touch, device = self._active_touch(touch_id)
        _get_libei().ei_touch_motion(touch, x, y)
        _get_libei().ei_device_frame(device, self._now_us())
        self._flush()

    def touch_up(self, touch_id: int) -> None:
        """End an active touch."""
        touch, device = self._active_touch(touch_id)
        del self._active_touches[touch_id]
        _get_libei().ei_touch_up(touch)
        _get_libei().ei_device_frame(device, self._now_us())
        _get_libei().ei_touch_unref(touch)
        self._flush()

    def close(self) -> None:
        """Clean up the EIS connection without waiting; never raises.

        Safe on a partially set up client: the libei context is released only
        when one exists, and the KWin-side connection is disconnected whenever a
        cookie was handed out, after libei let go of the socket.
        """
        if self._ei:
            lib = _get_libei()
            # Handle events already delivered so no call targets a removed device.
            with contextlib.suppress(OSError, ValueError):
                self._drain_events(0.0)

            # After DISCONNECT libei has already removed every device, so only
            # release references then.
            for touch, _device in self._active_touches.values():
                if not self._disconnected:
                    lib.ei_touch_up(touch)
                lib.ei_touch_unref(touch)
            self._active_touches.clear()

            if not self._disconnected:
                slot_devices = {getattr(self, attr) for _role, attr in self._DEVICE_SLOTS}
                for device in slot_devices & self._emulating:
                    lib.ei_device_stop_emulating(device)
            self._emulating.clear()
            for _role, attr in self._DEVICE_SLOTS:
                device = getattr(self, attr)
                if device:
                    lib.ei_device_unref(device)
                    setattr(self, attr, 0)

            # Release the context before KWin drops the connection: a disconnect
            # first makes libei's final message hit a closed socket ("failed to
            # send message ... Broken pipe").
            lib.ei_unref(self._ei)
            self._ei = 0

        if self._eis_iface and self._cookie:
            with contextlib.suppress(dbus.DBusException):
                self._eis_iface.disconnect(dbus.Int32(self._cookie))
        self._cookie = 0


# Bounds for the transient clipboard helper (``python -m kwin_mcp.clipboard``).
# Parent waits are the helper's own worst-case budgets plus a margin for
# interpreter startup and pipe latency, so a slow but successful step is never
# misreported. READY also covers writing the COPY request. The transfer bound
# is how long the focused app gets to request the text after the paste chord
# before the call reports failure; the prior selection is restored either way.
_CLIPBOARD_MARGIN = 2.0
_CLIPBOARD_READY_TIMEOUT = READY_BUDGET_S + _CLIPBOARD_MARGIN
_CLIPBOARD_ARM_TIMEOUT = 2.0
_CLIPBOARD_TRANSFER_TIMEOUT = 4.0
_CLIPBOARD_RESTORE_TIMEOUT = RESTORE_ROUNDTRIP_S + _CLIPBOARD_MARGIN
_CLIPBOARD_EVENTS = frozenset({"READY", "ARMED", "TRANSFERRED", "CANCELLED", "RESTORED", "ERR"})

# Paste chord selection. GUI toolkits paste on Ctrl+V and ignore it otherwise,
# but a terminal that does not bind the sent chord forwards it to the pty as ^V
# (VLNEXT, or a line editor's quoted-insert), which alters the next keystroke
# long after the call has reported failure. No follow-up key can cancel that
# state without itself being inserted literally, so the chord must be right
# before anything is pressed. Terminals listed here paste the clipboard on
# Ctrl+Shift+V by default (Konsole: ACCEL | Qt::Key_V; st: config.def.h
# TERMMOD|XK_V -> clippaste). Terminal-like classes that are not listed get no
# chord at all: xterm and urxvt have no default clipboard paste chord, and
# Shift+Insert pastes the primary selection in VTE, xterm and foot, which the
# helper does not own. Classes are compared lowercased.
_CTRL_SHIFT_V_TERMINALS = frozenset(
    {
        "alacritty",
        "com.gexperts.tilix",
        "com.mitchellh.ghostty",
        "foot",
        "footclient",
        "gnome-terminal-server",
        "kitty",
        "org.gnome.console",
        "org.gnome.terminal",
        "org.kde.konsole",
        "org.kde.yakuake",
        "org.wezfurlong.wezterm",
        "st-256color",
        "terminator",
        "xfce4-terminal",
    }
)
_UNPASTEABLE_TERMINAL_MARKERS = ("term", "rxvt")


def paste_chord(resource_class: str | None) -> str | None:
    """Return the clipboard paste chord for the focused window, or None to skip.

    ``resource_class`` is KWin's resource class of the active window ("" when
    none is active, None when it could not be determined). An unknown target
    may be a terminal, so it gets no chord: a reported failure is recoverable,
    a stray ^V in someone's shell line is not.
    """
    if resource_class is None:
        return None
    cls = resource_class.lower()
    if cls in _CTRL_SHIFT_V_TERMINALS:
        return "ctrl+shift+v"
    if any(marker in cls for marker in _UNPASTEABLE_TERMINAL_MARKERS):
        return None
    return "ctrl+v"


class _ClipboardHelper:
    """Line-protocol driver for one transient clipboard helper process.

    The helper takes the selection with the text, reports transfers, and puts
    the snapshotted prior selection back on RESTORE. It is never signalled:
    once it serves a restored selection, killing it would drop the user's
    previous clipboard, so teardown only restores and closes stdin.
    """

    def __init__(self, env: dict[str, str]) -> None:
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "kwin_mcp.clipboard"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._buffer = b""
        self._eof = False
        # True between READY and RESTORED/CANCELLED/exit: the text is the
        # current selection and the prior one only lives in the helper.
        self._owns_text = False
        # Set by RESTORED (prior selection back) or CANCELLED (a newer copy
        # replaced ours): the only outcomes that leave the clipboard correct.
        self._settled = False
        if self._proc.stdin is not None:
            # Writes go through send(), bounded by the caller's deadline.
            os.set_blocking(self._proc.stdin.fileno(), False)

    def send(self, data: bytes, deadline: float) -> bool:
        """Write ``data`` to the helper, giving up at ``deadline``."""
        stdin = self._proc.stdin
        if stdin is None:
            return False
        fd = stdin.fileno()
        view = memoryview(data)
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _, writable, _ = select.select([], [fd], [], remaining)
            if not writable:
                return False
            try:
                written = os.write(fd, view)
            except BlockingIOError:
                continue
            except OSError:
                return False
            view = view[written:]
        return True

    def next_event(self, timeout: float) -> str | None:
        """Return the next protocol event, or None on timeout or helper exit."""
        deadline = time.monotonic() + timeout
        while True:
            line = self._read_line(deadline)
            if line is None:
                if self._eof:
                    self._owns_text = False
                return None
            event = line.split(" ", 1)[0]
            if event not in _CLIPBOARD_EVENTS:
                continue
            if event == "READY":
                self._owns_text = True
            elif event in ("RESTORED", "CANCELLED"):
                self._owns_text = False
                self._settled = True
            return event

    def _read_line(self, deadline: float) -> str | None:
        stdout = self._proc.stdout
        if stdout is None:
            return None
        fd = stdout.fileno()
        while b"\n" not in self._buffer:
            if self._eof:
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                return None
            chunk = os.read(fd, 4096)
            if not chunk:
                self._eof = True
                return None
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line.decode("ascii", errors="replace").strip()

    def release(self) -> bool:
        """Put the prior selection back if needed and detach from the helper.

        Returns True unless the helper owned the text and neither RESTORED nor
        CANCELLED was confirmed (restore error, timeout, or helper exit).
        """
        if self._owns_text:
            deadline = time.monotonic() + _CLIPBOARD_RESTORE_TIMEOUT
            # A failed send may mean the helper already exited after
            # CANCELLED; still read what it reported before judging.
            self.send(b"RESTORE\n", deadline)
            while self._owns_text:
                event = self.next_event(max(0.0, deadline - time.monotonic()))
                if event is None or event == "ERR":
                    break
        settled = self._settled or (not self._owns_text and not self._eof)
        # EOF tells a helper that still owns the text to restore on its own;
        # one serving the restored selection keeps running until replaced.
        for pipe in (self._proc.stdin, self._proc.stdout):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()
        threading.Thread(target=self._proc.wait, daemon=True).start()
        return settled


def _is_are(count: int) -> str:
    return "is" if count == 1 else "are"


def _lift_after_failure(client: EISClient, touch_ids: Iterable[int]) -> None:
    """Lift touch points a failed gesture left down, keeping the original error."""
    for touch_id in touch_ids:
        with contextlib.suppress(RuntimeError, ValueError, OSError):
            client.touch_up(touch_id)


class InputBackend:
    """High-level input injection for an isolated KWin session.

    Wraps EISClient with convenient methods for mouse and keyboard
    operations including click, drag, scroll, and key combos, and tracks the
    buttons and keys the raw hold calls leave pressed.
    """

    def __init__(self, dbus_address: str) -> None:
        self._dbus_address = dbus_address
        self._client = EISClient(dbus_address)
        # Buttons pressed by mouse_button_down and not yet released, in press
        # order. While any is held, pointer motion is part of a drag path the
        # client records.
        self._held_buttons: dict[MouseButton, None] = {}
        # Keys pressed by keyboard_key_down and not yet released, in press order.
        self._held_keys: dict[int, None] = {}
        # The disconnect warning is reported once, by the first revalidation that sees it.
        self._disconnect_reported = False
        # Held controls whose EIS device KWin replaced: KWin keeps them pressed, and
        # their release must go through the replacement device (see _release_replaced).
        self._replaced_buttons: set[MouseButton] = set()
        self._replaced_keys: set[int] = set()

    # -- Held state -------------------------------------------------------

    def held(self) -> HeldInput:
        """The buttons and keys kwin-mcp holds pressed, each in press order."""
        return HeldInput(
            buttons=[button.value for button in self._held_buttons],
            keys=[key_name(code) for code in self._held_keys],
        )

    def revalidate(self) -> list[str]:
        """Apply EIS events KWin already sent to the held state, without sending input.

        A closed EIS connection clears both held sets. When KWin replaces the
        pointer or keyboard device, the app never receives a release for that
        device's pressed controls (checked on KWin 6.3.6 and 6.7.5): a held button
        stays pressed, and a held key stays down in the app although KWin resets
        its modifier state. They stay held here too and are released on an explicit
        release, ``session_stop`` or server exit. Returns one warning per such event
        (each reported once). Read-only: reporting never presses or releases anything.
        """
        self._client.poll()
        warnings: list[str] = []
        if self._client.disconnected:
            self._held_buttons.clear()
            self._held_keys.clear()
            self._replaced_buttons.clear()
            self._replaced_keys.clear()
            if not self._disconnect_reported:
                self._disconnect_reported = True
                warnings.append(EIS_DISCONNECTED_WARNING)
            return warnings
        lost = self._client.take_lost_roles()
        if "pointer" in lost and self._held_buttons:
            self._replaced_buttons.update(self._held_buttons)
            names = ", ".join(button.value for button in self._held_buttons)
            verb = "stays" if len(self._held_buttons) == 1 else "stay"
            warnings.append(f"KWin replaced the EIS pointer device: held {names} {verb} pressed")
        if "keyboard" in lost and self._held_keys:
            # The app got the press and no release, so the key stays down there until
            # released; KWin itself resets its modifier state, so new keys from the
            # replacement device are not modified by it (KWin 6.3.6 and 6.7.5).
            self._replaced_keys.update(self._held_keys)
            names = ", ".join(key_name(code) for code in self._held_keys)
            many = len(self._held_keys) > 1
            warnings.append(
                f"KWin replaced the EIS keyboard device: held {names} "
                f"{'stay' if many else 'stays'} pressed until released, but new keys no "
                f"longer see {'them as modifiers' if many else 'it as a modifier'}"
            )
        return warnings

    @staticmethod
    def _release_replaced(send: Callable[[int], None]) -> None:
        """Release a control KWin kept pressed after replacing the device that pressed it.

        Newer KWin (6.7.5) ignores a release from the new device for a control that
        device never pressed. A press first is absorbed (the control is already
        down, so no event reaches the app) and makes the following release count
        on every version.
        """
        send(_PRESSED)
        time.sleep(0.01)
        send(_RELEASED)

    def _release_button(self, button: MouseButton) -> None:
        """Send the release of ``button``, through the replacement device if needed."""
        code = _BTN_CODES[button]
        if button in self._replaced_buttons:
            self._release_replaced(lambda state: self._client.pointer_button(code, state))
        else:
            self._client.pointer_button(code, _RELEASED)

    def _release_key(self, code: int) -> None:
        """Send the release of key ``code``, through the replacement device if needed."""
        if code in self._replaced_keys:
            self._release_replaced(lambda state: self._client.keyboard_key(code, state))
        else:
            self._client.keyboard_key(code, _RELEASED)

    def check_not_held(
        self,
        tool: str,
        *,
        buttons: Iterable[MouseButton] = (),
        keycodes: Iterable[int] = (),
    ) -> None:
        """Refuse a gesture that would press or release a control kwin-mcp holds.

        Raises HeldControlError naming the held controls and the hold call that
        releases them, before any event is sent.
        """
        touched_buttons = set(buttons)
        touched_keys = set(keycodes)
        held_buttons = [button for button in self._held_buttons if button in touched_buttons]
        held_keys = [code for code in self._held_keys if code in touched_keys]
        if not held_buttons and not held_keys:
            return
        clauses: list[str] = []
        remedies: list[str] = []
        if held_buttons:
            names = ", ".join(button.value for button in held_buttons)
            clauses.append(f"{names} {_is_are(len(held_buttons))} held by pointer_hold")
            remedies.extend(
                f"pointer_hold(phase='release', button='{button.value}', target={{x, y}})"
                for button in held_buttons
            )
        if held_keys:
            names = ", ".join(key_name(code) for code in held_keys)
            chord = "+".join(key_name(code) for code in held_keys)
            clauses.append(f"{names} {_is_are(len(held_keys))} held by keyboard_hold")
            remedies.append(f"keyboard_hold(phase='release', keys='{chord}')")
        msg = (
            f"{tool} refused: {' and '.join(clauses)}; release it first.\n"
            f"Release with {'; '.join(remedies)}, then retry."
        )
        raise HeldControlError(msg)

    def check_move_allowed(self) -> None:
        """Refuse pointer_move while a button is held: the hover would become a drag."""
        if not self._held_buttons:
            return
        names = ", ".join(button.value for button in self._held_buttons)
        msg = (
            f"pointer_move refused: {names} {_is_are(len(self._held_buttons))} held by "
            "pointer_hold.\nTo continue: use pointer_hold(phase='move', button, target) "
            "to move a held button."
        )
        raise HeldControlError(msg)

    def moved_while_held(self, button: MouseButton | None) -> str | None:
        """Warning for a gesture that moves the pointer while another button is held."""
        others = [held for held in self._held_buttons if held is not button]
        if not others:
            return None
        verb = "was" if len(others) == 1 else "were"
        return f"moved while {', '.join(held.value for held in others)} {verb} held"

    def release_all(self) -> HeldInput:
        """Release every held key (reverse press order), then every held button.

        Buttons are released where the pointer is. Never raises: a release EIS
        refuses is skipped, and the result names everything that was attempted.
        When anything was released, the call waits for KWin to acknowledge it, so
        a teardown right after it cannot drop the last release.
        """
        released = self.held()
        for code in reversed(list(self._held_keys)):
            with contextlib.suppress(RuntimeError, OSError):
                self._release_key(code)
            time.sleep(0.01)
        for button in reversed(list(self._held_buttons)):
            with contextlib.suppress(RuntimeError, OSError):
                self._release_button(button)
            time.sleep(0.01)
        self._held_keys.clear()
        self._held_buttons.clear()
        self._replaced_keys.clear()
        self._replaced_buttons.clear()
        if released.buttons or released.keys:
            self._client.sync()
        return released

    # -- Pointer ----------------------------------------------------------

    def mouse_move(self, x: int, y: int) -> None:
        """Move mouse to absolute coordinates (hover).

        In KWin 6.3.6, ``SeatInterface::notifyPointerMotion`` (``seat.cpp``)
        returns early for an absolute motion that does not change the global
        cursor position, and no ``wl_pointer.motion`` is sent when a surface is
        mapped or moved under a stationary cursor. A client whose window
        appeared or moved beneath a parked cursor therefore keeps a stale
        surface-local pointer position, and a button sent after a same-position
        move is dispatched at that stale spot (issue #66).

        With no button held, the move first visits an adjacent pixel so the
        final move to ``(x, y)`` is a position change and the focused client
        receives a current enter/motion there. This restores coordinate sync
        only when KWin accepts the adjacent point: output clamping, edge
        barriers, or pointer confinement can pin it back onto ``(x, y)``, which
        leaves the same-position move a no-op again. While a button pressed by
        ``mouse_button_down`` is held, the move goes straight to ``(x, y)``: an
        out-and-back detour would become part of the drag path the client sees.
        """
        if not self._held_buttons:
            approach_x = x - 1 if x > 0 else x + 1
            self._client.pointer_move_absolute(float(approach_x), float(y))
        self._client.pointer_move_absolute(float(x), float(y))
        _mirror_x11_pointer(self._dbus_address, x, y)

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

        # Press modifier keys; everything pressed is released even when a
        # later event fails.
        pressed_mods: list[int] = []
        try:
            for mod in mod_codes:
                self._client.keyboard_key(mod, _PRESSED)
                pressed_mods.append(mod)
                time.sleep(0.01)

            for i in range(click_count):
                if i > 0:
                    time.sleep(_MULTI_CLICK_GAP_SECONDS)
                self._client.pointer_button(btn_code, _PRESSED)
                try:
                    if hold_ms > 0 and i == click_count - 1:
                        hold_s = max(_CLICK_PRESS_SECONDS, hold_ms / 1000.0)
                        _hold(hold_s, f"Holding {button.value} button")
                    else:
                        time.sleep(_CLICK_PRESS_SECONDS)
                finally:
                    self._client.pointer_button(btn_code, _RELEASED)
        finally:
            # Release modifier keys in reverse order
            for mod in reversed(pressed_mods):
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
        increments = max(steps, 1)

        if discrete:
            # libei counts discrete scrolling in 120ths of a wheel detent (the
            # wl_pointer axis_value120 convention). Passing the caller's click
            # count straight through makes libei reject it as a suspicious
            # fraction of a click and the compositor drops the event, so split
            # whole detents across the increments and scale each chunk. The
            # split works on the magnitude: floor division on a negative delta
            # would hand out more clicks than were asked for.
            sign = 1 if delta >= 0 else -1
            magnitude = abs(delta)
            for index in range(increments):
                clicks = magnitude // increments + (1 if index < magnitude % increments else 0)
                if not clicks:
                    continue
                ticks = sign * clicks * _SCROLL_DISCRETE_UNIT
                self._client.pointer_scroll_discrete(
                    ticks if horizontal else 0, 0 if horizontal else ticks
                )
                time.sleep(0.01)
            self._client.pointer_scroll_stop()
        else:
            total_dx = float(delta) * _SCROLL_STEP_PIXELS if horizontal else 0.0
            total_dy = float(delta) * _SCROLL_STEP_PIXELS if not horizontal else 0.0
            if increments > 1:
                step_dx = total_dx / increments
                step_dy = total_dy / increments
                for _ in range(increments):
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
        dwell_ms: int = 0,
    ) -> None:
        """Drag from one point to another.

        Args:
            from_x, from_y: Starting coordinates.
            to_x, to_y: Ending coordinates.
            button: Mouse button to use for dragging.
            modifiers: Modifier keys to hold during drag (e.g. ["alt"], ["ctrl"]).
            waypoints: Intermediate points as (x, y, dwell_ms) tuples.
            dwell_ms: Pause at ``(to_x, to_y)`` before the release (<= 0: none),
                for drop targets that need a hover.
        """
        btn_code = _BTN_CODES[button]
        mod_codes = _resolve_modifiers(modifiers)

        self.mouse_move(from_x, from_y)
        time.sleep(0.05)

        pressed_mod_codes: list[int] = []
        button_pressed = False
        try:
            # Press modifier keys
            for mod in mod_codes:
                self._client.keyboard_key(mod, _PRESSED)
                pressed_mod_codes.append(mod)
                time.sleep(0.01)

            self._client.pointer_button(btn_code, _PRESSED)
            button_pressed = True
            time.sleep(0.02)

            # Build full path: start -> waypoints -> end
            segments: list[tuple[int, int, int, int, int]] = []  # (fx, fy, tx, ty, dwell_ms)
            prev_x, prev_y = from_x, from_y
            if waypoints:
                for wx, wy, waypoint_dwell_ms in waypoints:
                    segments.append((prev_x, prev_y, wx, wy, waypoint_dwell_ms))
                    prev_x, prev_y = wx, wy
            segments.append((prev_x, prev_y, to_x, to_y, 0))

            for seg_index, (seg_fx, seg_fy, seg_tx, seg_ty, seg_dwell_ms) in enumerate(segments, 1):
                dx = seg_tx - seg_fx
                dy = seg_ty - seg_fy
                steps = max(10, int((dx**2 + dy**2) ** 0.5 / 10))
                for i in range(1, steps + 1):
                    frac = i / steps
                    cx = seg_fx + dx * frac
                    cy = seg_fy + dy * frac
                    self._client.pointer_move_absolute(cx, cy)
                    _wait(0.01)
                if seg_dwell_ms > 0:
                    _wait(seg_dwell_ms / 1000.0)
                progress.report(
                    seg_index, len(segments), f"Drag segment {seg_index}/{len(segments)}"
                )
            if dwell_ms > 0:
                _hold(dwell_ms / 1000.0, "Dwelling at the drop target")
        finally:
            try:
                if button_pressed:
                    time.sleep(0.02)
                    self._client.pointer_button(btn_code, _RELEASED)
            finally:
                # Release modifier keys in reverse order
                for mod in reversed(pressed_mod_codes):
                    time.sleep(0.01)
                    self._client.keyboard_key(mod, _RELEASED)

        _mirror_x11_pointer(self._dbus_address, to_x, to_y)

    def mouse_button_down(self, x: int, y: int, button: MouseButton = MouseButton.LEFT) -> bool:
        """Move to coordinates and press a mouse button without releasing.

        Idempotent: when kwin-mcp already holds ``button`` the pointer still
        moves to ``(x, y)`` (straight, as a drag segment) but no button event
        is sent. Returns whether the button was newly pressed.
        """
        if button in self._held_buttons:
            self.mouse_move(x, y)
            return False
        self.mouse_move(x, y)
        time.sleep(0.02)
        self._client.pointer_button(_BTN_CODES[button], _PRESSED)
        self._held_buttons[button] = None
        return True

    def mouse_hold_move(self, x: int, y: int, button: MouseButton = MouseButton.LEFT) -> None:
        """Move straight to ``(x, y)`` with ``button`` held, as one drag segment.

        Raises HeldControlError when kwin-mcp does not hold ``button``.
        """
        if button not in self._held_buttons:
            msg = (
                f"pointer_hold(phase='move') refused: {button.value} is not held.\n"
                f"Press it first with pointer_hold(phase='press', button='{button.value}', "
                "target={x, y})."
            )
            raise HeldControlError(msg)
        self.mouse_move(x, y)

    def mouse_button_up(self, x: int, y: int, button: MouseButton = MouseButton.LEFT) -> bool:
        """Move to coordinates and release a mouse button.

        The release is always injected, also for a button kwin-mcp did not
        press (e.g. one stuck by another client). Returns whether kwin-mcp
        held the button. The button leaves the held set only once its release
        was sent: an interrupted or failed release (a shutdown signal between
        the replacement device's press and release) stays held, so a retry or
        ``release_all`` at ``session_stop``/exit still sends it.
        """
        was_held = button in self._held_buttons
        self.mouse_move(x, y)
        time.sleep(0.02)
        self._release_button(button)
        self._held_buttons.pop(button, None)
        self._replaced_buttons.discard(button)
        return was_held

    def keyboard_type(self, text: str) -> None:
        """Type a string of text character by character.

        Raises ValueError before typing anything when the text holds a character
        the US QWERTY key map cannot produce (``keyboard_type_unicode`` can).
        """
        check_typeable(text)

        total = len(text)
        last_report = time.monotonic()
        reported = 0
        for index, char in enumerate(text, 1):
            keycode, needs_shift = _CHAR_KEY_MAP[char]
            if needs_shift:
                self._client.keyboard_key(_SHIFT_KEYCODE, _PRESSED)
                time.sleep(0.01)

            try:
                self._client.keyboard_key(keycode, _PRESSED)
                time.sleep(0.01)
                self._client.keyboard_key(keycode, _RELEASED)
            finally:
                if needs_shift:
                    time.sleep(0.01)
                    self._client.keyboard_key(_SHIFT_KEYCODE, _RELEASED)

            time.sleep(0.02)

            now = time.monotonic()
            if now - last_report >= _TYPE_REPORT_INTERVAL_S:
                last_report = now
                reported = index
                progress.report(index, total, f"Typed {index}/{total} characters")

        # Always end at total; a repeated equal value would open a new phase.
        if reported < total:
            progress.report(total, total, f"Typed {total}/{total} characters")

    def keyboard_key(self, key: str) -> None:
        """Press a key combination (e.g., 'ctrl+c', 'Return', 'alt+F4').

        Supports modifier combinations with '+' separator. Raises ValueError
        ``Unknown key: <key>`` before any input; modifiers pressed are released
        even when a later event fails.
        """
        *modifiers, keycode = chord_keycodes(key)

        pressed: list[int] = []
        try:
            # Press modifiers
            for mod in modifiers:
                self._client.keyboard_key(mod, _PRESSED)
                pressed.append(mod)
                time.sleep(0.01)

            # Press and release main key
            self._client.keyboard_key(keycode, _PRESSED)
            time.sleep(0.01)
            self._client.keyboard_key(keycode, _RELEASED)
        finally:
            # Release modifiers in reverse order
            for mod in reversed(pressed):
                time.sleep(0.01)
                self._client.keyboard_key(mod, _RELEASED)

    def keyboard_key_down(self, key: str) -> bool:
        """Press (and hold) the keys of a chord that kwin-mcp does not hold yet.

        Modifiers go first, 10 ms apart; already-held keys send no event.
        Returns whether any key was newly pressed. Raises ValueError for an
        unknown key before any input.
        """
        changed = False
        for code in chord_keycodes(key):
            if code in self._held_keys:
                continue
            self._client.keyboard_key(code, _PRESSED)
            self._held_keys[code] = None
            changed = True
            time.sleep(0.01)
        return changed

    def keyboard_key_up(self, key: str) -> bool:
        """Release every key of a chord in reverse order (main key first).

        The release is always injected, also for keys kwin-mcp did not press.
        Returns whether kwin-mcp held any of them. Raises ValueError for an
        unknown key before any input. Like ``mouse_button_up``, a key leaves the
        held set only once its release was sent.
        """
        codes = list(dict.fromkeys(chord_keycodes(key)))
        was_held = any(code in self._held_keys for code in codes)
        for code in reversed(codes):
            self._release_key(code)
            self._held_keys.pop(code, None)
            self._replaced_keys.discard(code)
            time.sleep(0.01)
        return was_held

    def touch_tap(self, x: int, y: int, hold_ms: int = 0) -> None:
        """Tap at the given coordinates.

        Args:
            x, y: Coordinates to tap at.
            hold_ms: Duration to hold before lifting (for long-press).
        """
        tid = self._client.touch_down(float(x), float(y))
        try:
            if hold_ms > 0:
                _hold(max(0.01, hold_ms / 1000.0), "Holding touch")
            else:
                time.sleep(0.01)
        except BaseException:
            _lift_after_failure(self._client, [tid])
            raise
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

        try:
            for i in range(1, steps + 1):
                frac = i / steps
                cx = from_x + dx * frac
                cy = from_y + dy * frac
                self._client.touch_move(tid, cx, cy)
                _wait(step_delay)
                _report_step(i, steps, "Swiping")
        except BaseException:
            _lift_after_failure(self._client, [tid])
            raise

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
        tids: list[int] = []
        try:
            tids.append(self._client.touch_down(float(center_x - half_start), float(center_y)))
            tids.append(self._client.touch_down(float(center_x + half_start), float(center_y)))
            tid1, tid2 = tids

            for i in range(1, steps + 1):
                frac = i / steps
                half = half_start + (end_distance / 2.0 - half_start) * frac
                self._client.touch_move(tid1, float(center_x - half), float(center_y))
                self._client.touch_move(tid2, float(center_x + half), float(center_y))
                _wait(step_delay)
                _report_step(i, steps, "Pinching")
        except BaseException:
            _lift_after_failure(self._client, tids)
            raise

        for tid in tids:
            self._client.touch_up(tid)

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
            fingers: Number of fingers (at least 1).
            duration_ms: Duration of the swipe.
        """
        steps = max(10, duration_ms // 10)
        dx = to_x - from_x
        dy = to_y - from_y
        step_delay = max(0.001, duration_ms / 1000.0 / steps)
        finger_spacing = 20  # pixels between fingers

        # Start touches spread vertically around center
        tids: list[int] = []
        try:
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
                _wait(step_delay)
                _report_step(i, steps, f"{fingers}-finger swipe")
        except BaseException:
            _lift_after_failure(self._client, tids)
            raise

        for tid in tids:
            self._client.touch_up(tid)

    def keyboard_type_unicode(
        self,
        text: str,
        env: dict[str, str] | None = None,
        focused_class: str | Callable[[], str | None] | None = None,
        *,
        before_paste: Callable[[str], None] | None = None,
    ) -> TypeRoute | None:
        """Type arbitrary Unicode text using wtype or a temporary clipboard paste.

        The clipboard route hands the text to ``kwin_mcp.clipboard``, which
        snapshots the current selection, offers the text (marked as a secret
        for KDE clipboard managers) and restores the snapshot after the paste.
        The paste chord is sent only after the helper owns the selection and
        has armed transfer counting, so a failed or slow copy never pastes
        stale content. The chord follows the focused window's class: Ctrl+Shift+V
        for terminals known to paste on it, none for other terminals or an
        unknown target, and Ctrl+V otherwise (see ``paste_chord``).

        Args:
            text: Text to type (supports non-ASCII, e.g. Korean, CJK).
            env: Session environment. Both routes use Wayland clients, so this
                must carry WAYLAND_DISPLAY of the target session.
            focused_class: KWin resource class of the active window ("" when
                none is active), None when it could not be determined, or a
                zero-argument callable returning one of those. A callable is
                resolved only when the clipboard path is reached, so a session
                where wtype succeeds never pays for the window query.
            before_paste: Called with the paste chord once the clipboard route
                is chosen, before the clipboard is touched; an exception it
                raises (e.g. HeldControlError for a held chord key) propagates
                with nothing touched.

        Returns:
            ``TypeRoute(kind="wtype")`` if wtype succeeded;
            ``TypeRoute(kind="paste", chord=<chord>)`` if the text was
            transferred to a client that requested it after the paste chord and
            the helper then confirmed that the prior selection was restored or
            replaced by a newer copy. None without touching the clipboard or
            pressing any key for a target with no safe paste chord and for text
            over the helper's 1 MiB limit; None when any step or the
            restoration fails or times out.
        """
        env = {**os.environ, **(env or {})}

        # Try wtype first. It needs the virtual-keyboard Wayland protocol, which
        # KWin does not implement, so a failure here is expected on Plasma and
        # must fall through to the clipboard route rather than give up. The text
        # travels in argv, so a long one fails to launch (E2BIG); that is not a
        # typing attempt either and falls through the same way.
        if shutil.which("wtype"):
            try:
                result = subprocess.run(
                    ["wtype", "--", text],
                    env=env,
                    capture_output=True,
                    timeout=5,
                )
            except OSError:
                result = None
            if result is not None and result.returncode == 0:
                return TypeRoute(kind="wtype")

        if focused_class is None or isinstance(focused_class, str):
            cls = focused_class
        else:
            cls = focused_class()
        chord = paste_chord(cls)
        if chord is None:
            return None  # no chord this target is known to paste on; touch nothing
        if before_paste is not None:
            before_paste(chord)
        if not self._paste_via_clipboard(text, env, chord):
            return None
        return TypeRoute(kind="paste", chord=chord)

    def _paste_via_clipboard(self, text: str, env: dict[str, str], chord: str) -> bool:
        try:
            payload = text.encode()
        except UnicodeEncodeError:
            return False
        if len(payload) > MAX_COPY_BYTES:
            return False  # the helper would reject it; touch nothing
        try:
            helper = _ClipboardHelper(env)
        except OSError:
            return False
        transferred = False
        try:
            # Writing the request shares the READY deadline, so a helper that
            # stops reading stdin cannot block the call indefinitely.
            deadline = time.monotonic() + _CLIPBOARD_READY_TIMEOUT
            if not helper.send(b"COPY %d\n" % len(payload) + payload, deadline):
                return False
            if helper.next_event(deadline - time.monotonic()) != "READY":
                return False
            # Transfers finishing before ARMED (e.g. a clipboard manager that
            # reads every new selection) are served but never counted.
            deadline = time.monotonic() + _CLIPBOARD_ARM_TIMEOUT
            if not helper.send(b"ARM\n", deadline):
                return False
            if helper.next_event(deadline - time.monotonic()) != "ARMED":
                return False
            self.keyboard_key(chord)
            transferred = helper.next_event(_CLIPBOARD_TRANSFER_TIMEOUT) == "TRANSFERRED"
        finally:
            restored = helper.release()
        return transferred and restored

    def close(self) -> None:
        """Close the EIS connection."""
        self._client.close()
