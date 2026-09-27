"""EIS event handling in ``EISClient``, driven through a scripted libei.

libei declares ``void ei_dispatch(struct ei *ei)``. kwin-mcp used to bind it
with a ``c_int`` return type and abort the handshake when that "value" was
negative, but a void function leaves whatever the return register held: on
Fedora 44 and Tumbleweed aarch64 builds it was negative on every call, so the
loop exited before DEVICE_ADDED and ``session_start`` reported "No input backend
available". These tests replay the handshake KWin sends through a stand-in whose
dispatch leaves such a garbage value, so the behavior does not depend on which
libei build or architecture the suite runs on. Real KWin coverage is the rest of
the suite, which asserts "Input backend: KWin EIS" on every image in CI.

The same stand-in also scripts what KWin sends after the handshake (issue #76).
KWin replaces its EIS keyboard on every keyboard-layout reconfigure and its
absolute pointer/touch device on every output change: libei reports
DEVICE_REMOVED for the old device and DEVICE_ADDED/RESUMED for the new one,
possibly in separate dispatch rounds. Input sent to the removed device is
dropped, and a NULL device crashes libei, so the stand-in records every device
call and fails the test on a NULL handle instead of crashing the interpreter.
Its descriptor is readable only while scripted batches remain, so the client's
``select`` sees the same readiness it would on a real EIS socket. Timing races
and ordering that real KWin cannot produce on demand live here; the end-to-end
behavior against KWin is in ``test_eis_device_replacement.py``.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import pytest

from kwin_mcp import input as kwin_input

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# The value one Tumbleweed aarch64 build left in the return register.
GARBAGE_DISPATCH_VALUE = -1487655808

EI_CONTEXT = 0x1000
SEAT = 0x2000
DEVICE = 0x3000

# libei event types (enum ei_event_type in ei.h), spelled out so these tests do
# not depend on which of them kwin_mcp.input names.
EI_EVENT_CONNECT = 1
EI_EVENT_DISCONNECT = 2
EI_EVENT_SEAT_ADDED = 3
EI_EVENT_DEVICE_ADDED = 5
EI_EVENT_DEVICE_REMOVED = 6
EI_EVENT_DEVICE_PAUSED = 7
EI_EVENT_DEVICE_RESUMED = 8
EI_EVENT_KEYBOARD_MODIFIERS = 9
EI_EVENT_SYNC = 91
UNKNOWN_EVENT = 0xFFFF

# libei device capabilities (enum ei_device_capability in ei.h).
EI_DEVICE_CAP_POINTER = 1 << 0
EI_DEVICE_CAP_POINTER_ABSOLUTE = 1 << 1
EI_DEVICE_CAP_KEYBOARD = 1 << 2
EI_DEVICE_CAP_TOUCH = 1 << 3
EI_DEVICE_CAP_SCROLL = 1 << 4
EI_DEVICE_CAP_BUTTON = 1 << 5
ALL_CAPABILITIES = (
    EI_DEVICE_CAP_POINTER
    | EI_DEVICE_CAP_POINTER_ABSOLUTE
    | EI_DEVICE_CAP_KEYBOARD
    | EI_DEVICE_CAP_TOUCH
    | EI_DEVICE_CAP_SCROLL
    | EI_DEVICE_CAP_BUTTON
)

# The devices KWin creates per EIS client: an absolute pointer that also
# carries touch, a keyboard, and a relative pointer kwin-mcp does not use.
ABSOLUTE = 0x3100
KEYBOARD = 0x3200
RELATIVE = 0x3300
ABSOLUTE_REPLACEMENT = 0x3400
KEYBOARD_REPLACEMENT = 0x3500
FOREIGN = 0x3600
_ABSOLUTE_CAPABILITIES = (
    EI_DEVICE_CAP_POINTER_ABSOLUTE
    | EI_DEVICE_CAP_TOUCH
    | EI_DEVICE_CAP_BUTTON
    | EI_DEVICE_CAP_SCROLL
)
CAPABILITIES = {
    ABSOLUTE: _ABSOLUTE_CAPABILITIES,
    ABSOLUTE_REPLACEMENT: _ABSOLUTE_CAPABILITIES,
    KEYBOARD: EI_DEVICE_CAP_KEYBOARD,
    KEYBOARD_REPLACEMENT: EI_DEVICE_CAP_KEYBOARD,
    RELATIVE: EI_DEVICE_CAP_POINTER | EI_DEVICE_CAP_BUTTON | EI_DEVICE_CAP_SCROLL,
}

type _Event = int | tuple[int, int]

HANDSHAKE: list[list[_Event]] = [
    [(EI_EVENT_CONNECT, 0)],
    [(EI_EVENT_SEAT_ADDED, 0)],
    [
        (EI_EVENT_DEVICE_ADDED, ABSOLUTE),
        (EI_EVENT_DEVICE_ADDED, KEYBOARD),
        (EI_EVENT_DEVICE_ADDED, RELATIVE),
        (EI_EVENT_DEVICE_RESUMED, ABSOLUTE),
        (EI_EVENT_DEVICE_RESUMED, KEYBOARD),
        (EI_EVENT_DEVICE_RESUMED, RELATIVE),
    ],
]

# A missing replacement fails the first call within the client's bounded wait;
# the bound leaves room for a slow CI machine without accepting a stall.
FIRST_FAILURE_SECONDS = 3.0
# Later calls must not wait again: the wait is anchored at the removal.
REPEAT_FAILURE_SECONDS = 0.2
# A role that was not removed must not wait for one that was.
UNAFFECTED_ROLE_SECONDS = 0.5

_TOUCH_BASE = 0x9000


class _ScriptedLibei:
    """Enough of libei for ``EISClient``'s handshake and input calls.

    Each ``ei_dispatch`` call queues the next batch of scripted events and
    returns a negative value, as the undefined return of a void function can.
    A batch entry is an event type for ``DEVICE`` or an ``(event type,
    device)`` pair, where device 0 means the event carries no device. With
    ``write_fd`` the descriptor holds one byte per pending batch, so it is
    readable exactly while batches remain; without it the caller's descriptor
    decides readiness. Every device and touch call is recorded against the
    device it reached, and a NULL handle fails the test. Batches scripted with
    ``arrive_during_next_send`` reach the socket at the next device call, as
    KWin's messages can while a frame is in flight, so the send's own
    ``ei_dispatch`` moves them into the event queue and leaves the descriptor
    idle.
    """

    def __init__(
        self,
        batches: list[list[_Event]],
        fd: int,
        *,
        write_fd: int | None = None,
        capabilities: dict[int, int] | None = None,
    ) -> None:
        self._batches: list[list[_Event]] = []
        self._fd = fd
        self._write_fd = write_fd
        self._capabilities = capabilities or {}
        self._queue: list[int] = []
        self._next_event = 1
        self._events: dict[int, tuple[int, int]] = {}
        self.dispatch_calls = 0
        self.emulating: list[int] = []
        self.bound_capabilities: list[int] = []
        self.device_calls: list[tuple[str, int]] = []
        self.refs: dict[int, int] = {}
        self.touches: dict[int, int] = {}
        self.touch_unrefs: dict[int, int] = {}
        self.released_events = 0
        self._in_flight: list[list[_Event]] = []
        self.push(*batches)

        def bind(_seat: int, *capabilities: object) -> None:
            self.bound_capabilities = [
                v for c in capabilities if (v := getattr(c, "value", None)) is not None
            ]

        self.ei_seat_bind_capabilities = bind

    def push(self, *batches: list[_Event]) -> None:
        """Script more batches, one dispatch round each."""
        for batch in batches:
            self._batches.append(batch)
            if self._write_fd is not None:
                os.write(self._write_fd, b"x")

    def arrive_during_next_send(self, *batches: list[_Event]) -> None:
        """Script batches that reach the socket at the next device call."""
        self._in_flight.extend(batches)

    def ei_get_fd(self, ei: int) -> int:
        assert ei == EI_CONTEXT
        return self._fd

    def ei_dispatch(self, ei: int) -> int:
        assert ei == EI_CONTEXT
        self.dispatch_calls += 1
        if self._batches:
            if self._write_fd is not None:
                os.read(self._fd, 1)
            for entry in self._batches.pop(0):
                event_type, device = entry if isinstance(entry, tuple) else (entry, DEVICE)
                event = self._next_event
                self._next_event += 1
                self._events[event] = (event_type, device)
                self._queue.append(event)
        return GARBAGE_DISPATCH_VALUE

    def ei_get_event(self, _ei: int) -> int | None:
        return self._queue.pop(0) if self._queue else None

    def ei_event_get_type(self, event: int) -> int:
        return self._events[event][0]

    def ei_event_unref(self, _event: int) -> None:
        self.released_events += 1

    def ei_event_get_seat(self, _event: int) -> int:
        return SEAT

    def ei_seat_has_capability(self, _seat: int, _capability: int) -> int:
        return 1

    def ei_event_get_device(self, event: int) -> int | None:
        return self._events[event][1] or None

    def ei_device_has_capability(self, device: int, capability: int) -> int:
        self._require(device, "ei_device_has_capability")
        return int(bool(self._capabilities.get(device, ALL_CAPABILITIES) & capability))

    def ei_device_ref(self, device: int) -> int:
        self._require(device, "ei_device_ref")
        self.refs[device] = self.refs.get(device, 0) + 1
        return device

    def ei_device_unref(self, device: int) -> None:
        self._require(device, "ei_device_unref")
        self.refs[device] = self.refs.get(device, 0) - 1
        if self.refs[device] < 0:
            pytest.fail(f"ei_device_unref({device:#x}) released a reference it never took")

    def ei_device_start_emulating(self, device: int, _sequence: int) -> None:
        self._require(device, "ei_device_start_emulating")
        self.emulating.append(device)

    def ei_device_stop_emulating(self, device: int) -> None:
        self._record("ei_device_stop_emulating", device)

    def ei_device_pointer_motion_absolute(self, device: int, _x: float, _y: float) -> None:
        self._record("ei_device_pointer_motion_absolute", device)

    def ei_device_button_button(self, device: int, _button: int, _state: int) -> None:
        self._record("ei_device_button_button", device)

    def ei_device_scroll_delta(self, device: int, _dx: float, _dy: float) -> None:
        self._record("ei_device_scroll_delta", device)

    def ei_device_scroll_discrete(self, device: int, _dx: int, _dy: int) -> None:
        self._record("ei_device_scroll_discrete", device)

    def ei_device_scroll_stop(self, device: int, _x: int, _y: int) -> None:
        self._record("ei_device_scroll_stop", device)

    def ei_device_keyboard_key(self, device: int, _keycode: int, _state: int) -> None:
        self._record("ei_device_keyboard_key", device)

    def ei_device_frame(self, device: int, _time_us: int) -> None:
        self._record("ei_device_frame", device)

    def ei_device_touch_new(self, device: int) -> int:
        self._record("ei_device_touch_new", device)
        touch = _TOUCH_BASE + len(self.touches) + 1
        self.touches[touch] = device
        return touch

    def ei_touch_down(self, touch: int, _x: float, _y: float) -> None:
        self._record("ei_touch_down", self._touch_device(touch, "ei_touch_down"))

    def ei_touch_motion(self, touch: int, _x: float, _y: float) -> None:
        self._record("ei_touch_motion", self._touch_device(touch, "ei_touch_motion"))

    def ei_touch_up(self, touch: int) -> None:
        self._record("ei_touch_up", self._touch_device(touch, "ei_touch_up"))

    def ei_touch_unref(self, touch: int) -> None:
        self._touch_device(touch, "ei_touch_unref")
        self.touch_unrefs[touch] = self.touch_unrefs.get(touch, 0) + 1

    def ei_unref(self, ei: int) -> None:
        assert ei == EI_CONTEXT

    def devices_reached(self) -> set[int]:
        return {device for _name, device in self.device_calls}

    def _require(self, device: int, name: str) -> None:
        if not device:
            pytest.fail(f"{name}(NULL) would crash libei")

    def _record(self, name: str, device: int) -> None:
        self._require(device, name)
        self.device_calls.append((name, device))
        if self._in_flight:
            self.push(*self._in_flight)
            self._in_flight.clear()

    def _touch_device(self, touch: int, name: str) -> int:
        if touch not in self.touches:
            pytest.fail(f"{name}({touch:#x}) received a touch libei never created")
        return self.touches[touch]


@pytest.fixture
def readable_fd() -> Iterator[int]:
    """A descriptor ``select`` always reports readable, like a busy EIS socket."""
    read_end, write_end = os.pipe()
    os.write(write_end, b"x")
    try:
        yield read_end
    finally:
        os.close(read_end)
        os.close(write_end)


@pytest.fixture
def eis_pipe() -> Iterator[tuple[int, int]]:
    """A pipe whose read end stands in for the EIS socket of ``_ScriptedLibei``."""
    read_end, write_end = os.pipe()
    try:
        yield read_end, write_end
    finally:
        os.close(read_end)
        os.close(write_end)


def _construct(monkeypatch: pytest.MonkeyPatch, libei: _ScriptedLibei) -> kwin_input.EISClient:
    """Build an EISClient through ``__init__`` whose handshake talks to ``libei``.

    Only the D-Bus connection and connectToEIS/ei_setup_backend_fd are
    replaced; the handshake runs through the client's own event handling.
    """
    monkeypatch.setattr(kwin_input, "_libei", libei)
    monkeypatch.setattr(kwin_input, "DBusGMainLoop", lambda **_kwargs: None)
    monkeypatch.setattr(kwin_input.dbus.bus, "BusConnection", lambda _address: None)

    def setup(client: kwin_input.EISClient) -> None:
        client._ei = EI_CONTEXT
        client._negotiate_devices(timeout=5.0)

    monkeypatch.setattr(kwin_input.EISClient, "_setup", setup)
    return kwin_input.EISClient("unix:path=/nonexistent")


def _connected_client(
    monkeypatch: pytest.MonkeyPatch,
    eis_pipe: tuple[int, int],
    handshake: list[list[_Event]] = HANDSHAKE,
) -> tuple[_ScriptedLibei, kwin_input.EISClient]:
    """A client that completed the scripted handshake with KWin's three devices.

    Device calls made during the handshake are forgotten so tests observe only
    what follows.
    """
    read_end, write_end = eis_pipe
    libei = _ScriptedLibei(handshake, read_end, write_fd=write_end, capabilities=CAPABILITIES)
    client = _construct(monkeypatch, libei)
    libei.device_calls.clear()
    return libei, client


# The EISClient calls that resolve a device, with the role whose device they use.
_SENDERS: dict[str, tuple[Callable[[kwin_input.EISClient], object], str]] = {
    "pointer_move_absolute": (lambda c: c.pointer_move_absolute(10.0, 20.0), "pointer"),
    "pointer_button": (lambda c: c.pointer_button(272, 1), "pointer"),
    "pointer_scroll": (lambda c: c.pointer_scroll(0.0, 15.0), "pointer"),
    "pointer_scroll_discrete": (lambda c: c.pointer_scroll_discrete(0, 120), "pointer"),
    "pointer_scroll_stop": (lambda c: c.pointer_scroll_stop(), "pointer"),
    "keyboard_key": (lambda c: c.keyboard_key(30, 1), "keyboard"),
    "touch_down": (lambda c: c.touch_down(10.0, 20.0), "touch"),
}
_REPLACED = {
    "pointer": (ABSOLUTE, ABSOLUTE_REPLACEMENT),
    "keyboard": (KEYBOARD, KEYBOARD_REPLACEMENT),
    "touch": (ABSOLUTE, ABSOLUTE_REPLACEMENT),
}


def test_negative_dispatch_value_does_not_abort_the_handshake(
    monkeypatch: pytest.MonkeyPatch, readable_fd: int
) -> None:
    libei = _ScriptedLibei(
        [
            [kwin_input._EI_EVENT_CONNECT],
            [kwin_input._EI_EVENT_SEAT_ADDED],
            [kwin_input._EI_EVENT_DEVICE_ADDED],
            [kwin_input._EI_EVENT_DEVICE_RESUMED],
        ],
        readable_fd,
    )
    client = _construct(monkeypatch, libei)

    assert libei.dispatch_calls == 4
    assert libei.bound_capabilities, "the seat was never bound"
    assert client._pointer == DEVICE
    assert client._keyboard == DEVICE
    assert libei.emulating == [DEVICE]


def test_disconnect_during_handshake_is_still_reported(
    monkeypatch: pytest.MonkeyPatch, readable_fd: int
) -> None:
    libei = _ScriptedLibei(
        [[kwin_input._EI_EVENT_CONNECT], [kwin_input._EI_EVENT_DISCONNECT]],
        readable_fd,
    )

    with pytest.raises(RuntimeError, match="EIS server disconnected during handshake"):
        _construct(monkeypatch, libei)

    assert libei.emulating == []


@pytest.mark.parametrize("sender", _SENDERS)
def test_send_after_split_replacement_uses_new_device(
    monkeypatch: pytest.MonkeyPatch, eis_pipe: tuple[int, int], sender: str
) -> None:
    send, role = _SENDERS[sender]
    old, new = _REPLACED[role]
    libei, client = _connected_client(monkeypatch, eis_pipe)
    # KWin's removal and the replacement's arrival land in separate dispatch rounds.
    libei.push(
        [(EI_EVENT_DEVICE_REMOVED, old)],
        [(EI_EVENT_DEVICE_ADDED, new), (EI_EVENT_DEVICE_RESUMED, new)],
    )

    send(client)

    assert libei.device_calls, "nothing reached libei"
    assert libei.devices_reached() == {new}, libei.device_calls
    assert libei.emulating.count(new) == 1, libei.emulating


@pytest.mark.parametrize("sender", _SENDERS)
def test_missing_replacement_raises_without_null_call(
    monkeypatch: pytest.MonkeyPatch, eis_pipe: tuple[int, int], sender: str
) -> None:
    send, role = _SENDERS[sender]
    old, _new = _REPLACED[role]
    libei, client = _connected_client(monkeypatch, eis_pipe)
    libei.push([(EI_EVENT_DEVICE_REMOVED, old)])

    started = time.monotonic()
    with pytest.raises(RuntimeError):
        send(client)
    first = time.monotonic() - started

    started = time.monotonic()
    with pytest.raises(RuntimeError):
        send(client)
    second = time.monotonic() - started

    assert libei.device_calls == []
    assert first < FIRST_FAILURE_SECONDS, first
    assert second < REPEAT_FAILURE_SECONDS, second


def test_device_roles_are_tracked_independently(
    monkeypatch: pytest.MonkeyPatch, eis_pipe: tuple[int, int]
) -> None:
    libei, client = _connected_client(monkeypatch, eis_pipe)
    # An output change replaces the pointer while the keyboard stays removed.
    libei.push(
        [(EI_EVENT_DEVICE_REMOVED, KEYBOARD)],
        [
            (EI_EVENT_DEVICE_REMOVED, ABSOLUTE),
            (EI_EVENT_DEVICE_ADDED, ABSOLUTE_REPLACEMENT),
            (EI_EVENT_DEVICE_RESUMED, ABSOLUTE_REPLACEMENT),
        ],
    )

    with pytest.raises(RuntimeError):
        client.keyboard_key(30, 1)
    client.pointer_move_absolute(10.0, 20.0)

    assert libei.devices_reached() == {ABSOLUTE_REPLACEMENT}, libei.device_calls


def test_pending_keyboard_does_not_delay_the_pointer(
    monkeypatch: pytest.MonkeyPatch, eis_pipe: tuple[int, int]
) -> None:
    libei, client = _connected_client(monkeypatch, eis_pipe)
    libei.push([(EI_EVENT_DEVICE_REMOVED, KEYBOARD)])

    started = time.monotonic()
    client.pointer_move_absolute(10.0, 20.0)
    elapsed = time.monotonic() - started

    assert elapsed < UNAFFECTED_ROLE_SECONDS, elapsed
    assert libei.devices_reached() == {ABSOLUTE}, libei.device_calls


def test_device_refs_balanced_across_replacement_and_close(
    monkeypatch: pytest.MonkeyPatch, eis_pipe: tuple[int, int]
) -> None:
    libei, client = _connected_client(monkeypatch, eis_pipe)
    libei.push(
        [(EI_EVENT_DEVICE_REMOVED, ABSOLUTE)],
        [
            (EI_EVENT_DEVICE_ADDED, ABSOLUTE_REPLACEMENT),
            (EI_EVENT_DEVICE_RESUMED, ABSOLUTE_REPLACEMENT),
        ],
        [
            (EI_EVENT_DEVICE_REMOVED, KEYBOARD),
            (EI_EVENT_DEVICE_ADDED, KEYBOARD_REPLACEMENT),
            (EI_EVENT_DEVICE_RESUMED, KEYBOARD_REPLACEMENT),
        ],
    )

    client.pointer_move_absolute(10.0, 20.0)
    client.keyboard_key(30, 1)

    # The absolute device fills both the pointer and the touch slot.
    assert libei.refs[ABSOLUTE] == 0, libei.refs
    assert libei.refs[KEYBOARD] == 0, libei.refs

    client.close()

    assert all(count == 0 for count in libei.refs.values()), libei.refs
    assert RELATIVE not in libei.emulating, libei.emulating


def test_touch_on_removed_device_is_abandoned(
    monkeypatch: pytest.MonkeyPatch, eis_pipe: tuple[int, int]
) -> None:
    libei, client = _connected_client(monkeypatch, eis_pipe)
    touch_id = client.touch_down(10.0, 20.0)
    (old_touch,) = libei.touches
    libei.push(
        [
            (EI_EVENT_DEVICE_REMOVED, ABSOLUTE),
            (EI_EVENT_DEVICE_ADDED, ABSOLUTE_REPLACEMENT),
            (EI_EVENT_DEVICE_RESUMED, ABSOLUTE_REPLACEMENT),
        ]
    )

    with pytest.raises(RuntimeError):
        client.touch_up(touch_id)

    assert ("ei_touch_up", ABSOLUTE) not in libei.device_calls, libei.device_calls
    assert libei.touch_unrefs.get(old_touch) == 1, libei.touch_unrefs

    libei.device_calls.clear()
    client.touch_up(client.touch_down(30.0, 40.0))

    assert ("ei_touch_down", ABSOLUTE_REPLACEMENT) in libei.device_calls, libei.device_calls
    assert ("ei_touch_up", ABSOLUTE_REPLACEMENT) in libei.device_calls, libei.device_calls
    assert libei.devices_reached() == {ABSOLUTE_REPLACEMENT}, libei.device_calls


def test_disconnect_fails_input_fast_and_close_is_quiet(
    monkeypatch: pytest.MonkeyPatch, eis_pipe: tuple[int, int]
) -> None:
    libei, client = _connected_client(monkeypatch, eis_pipe)
    # libei reports every device removed before the disconnect itself.
    libei.push(
        [
            (EI_EVENT_DEVICE_REMOVED, ABSOLUTE),
            (EI_EVENT_DEVICE_REMOVED, KEYBOARD),
            (EI_EVENT_DEVICE_REMOVED, RELATIVE),
            (EI_EVENT_DISCONNECT, 0),
        ]
    )

    for name, (send, _role) in _SENDERS.items():
        started = time.monotonic()
        with pytest.raises(RuntimeError):
            send(client)
        elapsed = time.monotonic() - started
        assert elapsed < REPEAT_FAILURE_SECONDS, (name, elapsed)

    client.close()

    assert libei.device_calls == []


def test_paused_device_resumes_before_sending(
    monkeypatch: pytest.MonkeyPatch, eis_pipe: tuple[int, int]
) -> None:
    libei, client = _connected_client(monkeypatch, eis_pipe)
    libei.push([(EI_EVENT_DEVICE_PAUSED, KEYBOARD)], [(EI_EVENT_DEVICE_RESUMED, KEYBOARD)])

    client.keyboard_key(30, 1)

    # A resumed device only accepts input once emulation restarts.
    assert libei.emulating.count(KEYBOARD) == 2, libei.emulating
    assert libei.device_calls[0] == ("ei_device_keyboard_key", KEYBOARD), libei.device_calls


def test_unrelated_events_are_released_and_ignored(
    monkeypatch: pytest.MonkeyPatch, eis_pipe: tuple[int, int]
) -> None:
    libei, client = _connected_client(monkeypatch, eis_pipe)
    unrelated: list[_Event] = [
        (EI_EVENT_KEYBOARD_MODIFIERS, KEYBOARD),
        (EI_EVENT_SYNC, 0),
        (UNKNOWN_EVENT, 0),
        (EI_EVENT_DEVICE_REMOVED, FOREIGN),
    ]
    libei.push(unrelated)
    released_before = libei.released_events

    client.keyboard_key(30, 1)

    assert libei.released_events - released_before == len(unrelated)
    assert libei.device_calls[0] == ("ei_device_keyboard_key", KEYBOARD), libei.device_calls
    assert libei.devices_reached() == {KEYBOARD}, libei.device_calls


def test_handshake_follows_replaced_device(
    monkeypatch: pytest.MonkeyPatch, eis_pipe: tuple[int, int]
) -> None:
    # KWin reconfigures the keyboard while the client is still negotiating.
    handshake: list[list[_Event]] = [
        [(EI_EVENT_CONNECT, 0)],
        [(EI_EVENT_SEAT_ADDED, 0)],
        [
            (EI_EVENT_DEVICE_ADDED, ABSOLUTE),
            (EI_EVENT_DEVICE_ADDED, KEYBOARD),
            (EI_EVENT_DEVICE_RESUMED, ABSOLUTE),
            (EI_EVENT_DEVICE_RESUMED, KEYBOARD),
            (EI_EVENT_DEVICE_REMOVED, KEYBOARD),
        ],
        [
            (EI_EVENT_DEVICE_ADDED, KEYBOARD_REPLACEMENT),
            (EI_EVENT_DEVICE_RESUMED, KEYBOARD_REPLACEMENT),
        ],
    ]
    libei, client = _connected_client(monkeypatch, eis_pipe, handshake)

    client.keyboard_key(30, 1)

    assert libei.device_calls[0] == ("ei_device_keyboard_key", KEYBOARD_REPLACEMENT), (
        libei.device_calls
    )
    assert libei.devices_reached() == {KEYBOARD_REPLACEMENT}, libei.device_calls
    assert KEYBOARD_REPLACEMENT in libei.emulating, libei.emulating


def test_replacement_queued_by_a_send_reaches_the_next_send(
    monkeypatch: pytest.MonkeyPatch, eis_pipe: tuple[int, int]
) -> None:
    libei, client = _connected_client(monkeypatch, eis_pipe)
    libei.arrive_during_next_send(
        [
            (EI_EVENT_DEVICE_REMOVED, ABSOLUTE),
            (EI_EVENT_DEVICE_ADDED, ABSOLUTE_REPLACEMENT),
            (EI_EVENT_DEVICE_RESUMED, ABSOLUTE_REPLACEMENT),
        ]
    )
    client.pointer_move_absolute(10.0, 20.0)
    libei.device_calls.clear()

    client.pointer_move_absolute(30.0, 40.0)

    assert libei.devices_reached() == {ABSOLUTE_REPLACEMENT}, libei.device_calls


def test_disconnect_queued_by_a_send_fails_the_next_send(
    monkeypatch: pytest.MonkeyPatch, eis_pipe: tuple[int, int]
) -> None:
    libei, client = _connected_client(monkeypatch, eis_pipe)
    libei.arrive_during_next_send(
        [
            (EI_EVENT_DEVICE_REMOVED, ABSOLUTE),
            (EI_EVENT_DEVICE_REMOVED, KEYBOARD),
            (EI_EVENT_DEVICE_REMOVED, RELATIVE),
            (EI_EVENT_DISCONNECT, 0),
        ]
    )
    client.pointer_move_absolute(10.0, 20.0)
    libei.device_calls.clear()

    started = time.monotonic()
    with pytest.raises(RuntimeError):
        client.pointer_move_absolute(30.0, 40.0)
    elapsed = time.monotonic() - started

    assert elapsed < REPEAT_FAILURE_SECONDS, elapsed
    assert libei.device_calls == []


def test_keyboard_removal_queued_by_a_send_fails_the_next_key(
    monkeypatch: pytest.MonkeyPatch, eis_pipe: tuple[int, int]
) -> None:
    libei, client = _connected_client(monkeypatch, eis_pipe)
    libei.arrive_during_next_send([(EI_EVENT_DEVICE_REMOVED, KEYBOARD)])
    client.pointer_move_absolute(10.0, 20.0)
    libei.device_calls.clear()

    started = time.monotonic()
    with pytest.raises(RuntimeError):
        client.keyboard_key(30, 1)
    elapsed = time.monotonic() - started

    assert elapsed < FIRST_FAILURE_SECONDS, elapsed
    assert libei.device_calls == []
