"""Tests for the EI_EVENT_DEVICE_RESUMED wait before start_emulating.

EIS input injection used to start emulating as soon as devices were added,
before the compositor resumed them. libei rejects events sent to a
non-emulating device ("device is not emulating") and every injected event
afterwards was silently dropped. ``_negotiate_devices`` now tracks RESUMED
events and only exits the handshake loop once both pointer and keyboard have
resumed.

Devices that never resume are still started (graceful fallback): emulating a
paused device is no worse than the unconditional start this wait replaced,
and failing the handshake would drop input that used to work.
"""

from __future__ import annotations

import pytest

import kwin_mcp.input as input_module
from kwin_mcp.input import (
    _EI_CAP_KEYBOARD,
    _EI_CAP_POINTER_ABSOLUTE,
    _EI_EVENT_DEVICE_ADDED,
    _EI_EVENT_DEVICE_RESUMED,
    EISClient,
)

POINTER = 0x101
KEYBOARD = 0x102


class FakeLibei:
    """Minimal libei stub covering the handshake calls of _negotiate_devices."""

    def __init__(
        self,
        events: list[tuple[int, int]],
        device_caps: dict[int, set[int]],
    ) -> None:
        # events: (event_type, device_ptr) per event, popped in order
        self._events = list(events)
        self._event_meta: dict[int, tuple[int, int]] = {}
        self._next_id = 0
        self.device_caps = device_caps
        self.started: list[int] = []

    def ei_get_fd(self, ei: int) -> int:
        return 9

    def ei_dispatch(self, ei: int) -> int:
        return 0

    def ei_get_event(self, ei: int) -> int:
        if not self._events:
            return 0
        self._next_id += 1
        self._event_meta[self._next_id] = self._events.pop(0)
        return self._next_id

    def ei_event_get_type(self, event: int) -> int:
        return self._event_meta[event][0]

    def ei_event_get_device(self, event: int) -> int:
        return self._event_meta[event][1]

    def ei_event_unref(self, event: int) -> None:
        return None

    def ei_device_has_capability(self, device: int, cap: int) -> int:
        return 1 if cap in self.device_caps.get(device, set()) else 0

    def ei_device_ref(self, device: int) -> int:
        return device

    def ei_device_start_emulating(self, device: int, sequence: int) -> None:
        self.started.append(device)


def _client() -> EISClient:
    """An EISClient that skipped __init__ (no D-Bus, no libei load)."""
    client = EISClient.__new__(EISClient)
    client._ei = 1
    client._pointer = 0
    client._keyboard = 0
    client._touch_device = 0
    return client


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeLibei) -> None:
    # The module loads libei eagerly at import time; swapping the module
    # global with the stub keeps the test free of native dependencies.
    monkeypatch.setattr(input_module, "_libei", fake)
    # Never readable: _negotiate_devices still drains the event queue below.
    monkeypatch.setattr(input_module.select, "select", lambda *a, **k: ([], [], []))


def test_negotiate_waits_for_resumed_before_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    """start_emulating happens only after RESUMED arrived for both devices."""
    caps = {POINTER: {_EI_CAP_POINTER_ABSOLUTE}, KEYBOARD: {_EI_CAP_KEYBOARD}}
    events = [
        (_EI_EVENT_DEVICE_ADDED, POINTER),
        (_EI_EVENT_DEVICE_ADDED, KEYBOARD),
        (_EI_EVENT_DEVICE_RESUMED, KEYBOARD),
        (_EI_EVENT_DEVICE_RESUMED, POINTER),
    ]
    fake = FakeLibei(events, caps)
    _install(monkeypatch, fake)

    client = _client()
    client._negotiate_devices(timeout=1.0)

    assert sorted(fake.started) == sorted([POINTER, KEYBOARD])


def test_negotiate_timeout_falls_back_to_unconditional_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Devices added but never resumed → still started (graceful fallback):
    the handshake wait may not turn a session that worked before into a
    hard failure."""
    caps = {POINTER: {_EI_CAP_POINTER_ABSOLUTE}, KEYBOARD: {_EI_CAP_KEYBOARD}}
    events = [
        (_EI_EVENT_DEVICE_ADDED, POINTER),
        (_EI_EVENT_DEVICE_ADDED, KEYBOARD),
    ]
    fake = FakeLibei(events, caps)
    _install(monkeypatch, fake)

    client = _client()
    client._negotiate_devices(timeout=0.1)

    assert sorted(fake.started) == sorted([POINTER, KEYBOARD])


def test_negotiate_times_out_without_pointer_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """No pointer device at all remains a hard error (nothing to start)."""
    caps = {KEYBOARD: {_EI_CAP_KEYBOARD}}
    events = [
        (_EI_EVENT_DEVICE_ADDED, KEYBOARD),
        (_EI_EVENT_DEVICE_RESUMED, KEYBOARD),
    ]
    fake = FakeLibei(events, caps)
    _install(monkeypatch, fake)

    client = _client()
    with pytest.raises(RuntimeError, match="No pointer device"):
        client._negotiate_devices(timeout=0.1)
    assert fake.started == []
