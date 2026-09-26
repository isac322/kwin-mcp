"""EIS handshake handling of ``ei_dispatch``, driven through a scripted libei.

libei declares ``void ei_dispatch(struct ei *ei)``. kwin-mcp used to bind it
with a ``c_int`` return type and abort the handshake when that "value" was
negative, but a void function leaves whatever the return register held: on
Fedora 44 and Tumbleweed aarch64 builds it was negative on every call, so the
loop exited before DEVICE_ADDED and ``session_start`` reported "No input backend
available". These tests replay the handshake KWin sends through a stand-in whose
dispatch leaves such a garbage value, so the behavior does not depend on which
libei build or architecture the suite runs on. Real KWin coverage is the rest of
the suite, which asserts "Input backend: KWin EIS" on every image in CI.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from kwin_mcp import input as kwin_input

if TYPE_CHECKING:
    from collections.abc import Iterator

# The value one Tumbleweed aarch64 build left in the return register.
GARBAGE_DISPATCH_VALUE = -1487655808

EI_CONTEXT = 0x1000
SEAT = 0x2000
DEVICE = 0x3000


class _ScriptedLibei:
    """Enough of libei for ``EISClient._negotiate_devices``.

    Each ``ei_dispatch`` call queues the next batch of scripted events and
    returns a negative value, as the undefined return of a void function can.
    """

    def __init__(self, batches: list[list[int]], fd: int) -> None:
        self._batches = batches
        self._fd = fd
        self._queue: list[int] = []
        self._next_event = 1
        self._event_types: dict[int, int] = {}
        self.dispatch_calls = 0
        self.emulating: list[int] = []
        self.bound_capabilities: list[int] = []

        def bind(_seat: int, *capabilities: object) -> None:
            self.bound_capabilities = [
                v for c in capabilities if (v := getattr(c, "value", None)) is not None
            ]

        self.ei_seat_bind_capabilities = bind

    def ei_get_fd(self, ei: int) -> int:
        assert ei == EI_CONTEXT
        return self._fd

    def ei_dispatch(self, ei: int) -> int:
        assert ei == EI_CONTEXT
        self.dispatch_calls += 1
        if self._batches:
            for event_type in self._batches.pop(0):
                event = self._next_event
                self._next_event += 1
                self._event_types[event] = event_type
                self._queue.append(event)
        return GARBAGE_DISPATCH_VALUE

    def ei_get_event(self, _ei: int) -> int | None:
        return self._queue.pop(0) if self._queue else None

    def ei_event_get_type(self, event: int) -> int:
        return self._event_types[event]

    def ei_event_unref(self, _event: int) -> None:
        return None

    def ei_event_get_seat(self, _event: int) -> int:
        return SEAT

    def ei_seat_has_capability(self, _seat: int, _capability: int) -> int:
        return 1

    def ei_event_get_device(self, _event: int) -> int:
        return DEVICE

    def ei_device_has_capability(self, _device: int, _capability: int) -> int:
        return 1

    def ei_device_ref(self, device: int) -> int:
        return device

    def ei_device_start_emulating(self, device: int, _sequence: int) -> None:
        self.emulating.append(device)


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


def _client() -> kwin_input.EISClient:
    """An EISClient past connectToEIS/ei_setup_backend_fd, before negotiation."""
    client = kwin_input.EISClient.__new__(kwin_input.EISClient)
    client._ei = EI_CONTEXT
    client._pointer = 0
    client._keyboard = 0
    client._touch_device = 0
    return client


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
    monkeypatch.setattr(kwin_input, "_libei", libei)
    client = _client()

    client._negotiate_devices(timeout=5.0)

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
    monkeypatch.setattr(kwin_input, "_libei", libei)

    with pytest.raises(RuntimeError, match="EIS server disconnected during handshake"):
        _client()._negotiate_devices(timeout=5.0)

    assert libei.emulating == []
