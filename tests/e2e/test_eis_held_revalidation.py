"""Held input is revalidated against the EIS connection before every report (S §3.4 6/6a/6b).

When the EIS connection closes, kwin-mcp can no longer send a release: the disconnect
clears both sets with ``EIS disconnected: held input is no longer tracked; KWin may
keep it pressed`` (KWin 6.3.6 and 6.7.5 both keep an emulated button down, a
registered limit). When KWin replaces a device, the app gets no release for that
device's pressed controls, so kwin-mcp keeps them held and warns ``KWin replaced the
EIS <role> device: held <controls> stays pressed``; reporting never sends input. An
explicit release then reaches the app on every KWin version. The checks read what
the app saw from the probe.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import pytest
from _suite_policy import require_or_skip
from probe_session import POLL_S, STATE_TIMEOUT_S, Probe, field, of_kind, start_probe_session
from test_eis_device_replacement import (
    EIS_CLOSED,
    EIS_DISCONNECTED_WARNING,
    KeyboardLayoutTrigger,
    change_output,
    disconnect_eis,
)

from kwin_mcp.input import HeldControlError
from kwin_mcp.models import HeldInput

if TYPE_CHECKING:
    from pathlib import Path

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import SessionInfoResult

KEYBOARD_REPLACED = (
    "KWin replaced the EIS keyboard device: held shift stays pressed until released, "
    "but new keys no longer see it as a modifier"
)
POINTER_REPLACED = "KWin replaced the EIS pointer device: held left stays pressed"
LEAK_LIMIT = "KWin limit: emulated state survives client disconnect"


@pytest.fixture
def probe(engine: AutomationEngine, tmp_path: Path) -> Probe:
    return start_probe_session(engine, tmp_path)


def _hold_left_and_shift(engine: AutomationEngine, probe: Probe) -> None:
    probe.park()
    engine.pointer_hold("press", probe.center("Drag Target"))
    held = engine.keyboard_hold("press", "shift").held
    assert held == HeldInput(buttons=["left"], keys=["shift"])
    probe.wait_status("hold_status:", lambda s: field(s, "held") == "left")
    probe.wait_status("raw_events:", lambda s: field(s, "held_mods") == "shift")


@pytest.mark.qa("QA-INP-045")
def test_eis_disconnect_clears_held_and_warns(engine: AutomationEngine, probe: Probe) -> None:
    _hold_left_and_shift(engine, probe)
    target = probe.center("Drag Target")

    disconnect_eis(engine)
    # session_info revalidates first: never left/shift after the disconnect.
    deadline = time.monotonic() + STATE_TIMEOUT_S
    info = engine.session_info()
    while EIS_DISCONNECTED_WARNING not in info.warnings:
        assert info.held in (HeldInput(), HeldInput(buttons=["left"], keys=["shift"])), info
        assert time.monotonic() < deadline, info.render()
        time.sleep(POLL_S)
        info = engine.session_info()
    assert info.held == HeldInput(), info.render()
    assert info.render().endswith(f"Warning: {EIS_DISCONNECTED_WARNING}"), info.render()
    assert info.session is not None and info.session.input_backend is None

    # Input calls fail closed once the disconnect is known. Whether KWin releases
    # the dead connection's held input on its own is a registered KWin limit (see
    # the app-side check in the next test); let whatever it does settle so events
    # after ``mark`` are either KWin's cleanup or a leak from the refused calls.
    probe.settle()
    mark = probe.mark()
    with pytest.raises(RuntimeError, match=EIS_CLOSED):
        engine.keyboard_press("a")
    with pytest.raises(RuntimeError, match=EIS_CLOSED):
        engine.pointer_hold("release", target)
    assert engine.session_info().held == HeldInput()
    time.sleep(0.5)
    for event in probe.events_since(mark):
        # The refused calls sent nothing, so the only legitimate events are KWin's
        # own releases of the button and key it kept pressed (or never released).
        if event["kind"] == "button_release":
            assert event["button"] == "left", event
        elif event["kind"] == "key_release":
            assert event["key"] == "Shift_L", event
        else:
            pytest.fail(f"the refused calls leaked an event: {event}")

    stopped = engine.session_stop()
    assert stopped.released == HeldInput(), stopped.render()
    assert EIS_DISCONNECTED_WARNING in stopped.warnings, stopped.render()


@pytest.mark.qa("QA-INP-045")
def test_eis_disconnect_app_side_release(engine: AutomationEngine, probe: Probe) -> None:
    """What the app sees after the EIS client vanishes: the warning's honest claim.

    The engine side is covered by test_eis_disconnect_clears_held_and_warns. Whether
    KWin releases the input a disconnected client held is a registered KWin limit:
    6.3.6 and 6.7.5 keep an emulated button down. Where KWin does clean up, the
    probe's held button and modifier state go "none".
    """
    _hold_left_and_shift(engine, probe)
    disconnect_eis(engine)

    deadline = time.monotonic() + STATE_TIMEOUT_S
    while time.monotonic() < deadline:
        held = field(probe.status("hold_status:"), "held")
        mods = field(probe.status("raw_events:"), "held_mods")
        if held == "none" and mods == "none":
            return
        time.sleep(POLL_S)
    require_or_skip("eis-disconnect-release", LEAK_LIMIT, os.environ)


def _reports_until(engine: AutomationEngine, warning: str, probe: Probe) -> SessionInfoResult:
    """session_info until ``warning`` appears (once, with the first report after it)."""
    deadline = time.monotonic() + STATE_TIMEOUT_S
    while True:
        info = engine.session_info()
        if warning in info.warnings:
            return info
        assert time.monotonic() < deadline, (info.render(), probe.status("raw_events:"))
        time.sleep(POLL_S)


def _app_holds_left_and_shift(probe: Probe) -> None:
    """The app still sees left and shift pressed: reporting sent no release."""
    assert field(probe.status("hold_status:"), "held") == "left", probe.status("hold_status:")
    assert field(probe.status("raw_events:"), "held_mods") == "shift", probe.status("raw_events:")


def _wait_release(probe: Probe, mark: int, kind: str, field_name: str, value: str) -> None:
    """A ``kind`` event for ``value`` reached the app after ``mark``, and no new press of
    it: the replacement device's press before its release is absorbed by KWin."""
    events = probe.wait_events(
        lambda evs: any(e[field_name] == value for e in of_kind(evs, kind)), since=mark
    )
    press = kind.replace("release", "press")
    assert not [e for e in of_kind(events, press) if e[field_name] == value], events


@pytest.mark.qa("QA-INP-046")
def test_device_replacement_keeps_held_and_warns(
    engine: AutomationEngine, probe: Probe, tmp_path: Path
) -> None:
    # (a) keyboard replaced (layout reconfigure) while shift (and left) are held.
    _hold_left_and_shift(engine, probe)
    trigger = KeyboardLayoutTrigger(probe.dbus_address)
    try:
        trigger.reconfigure()
    finally:
        trigger.close()
    info = _reports_until(engine, KEYBOARD_REPLACED, probe)
    assert info.held == HeldInput(buttons=["left"], keys=["shift"]), info.render()
    assert engine.session_info().warnings == []
    _app_holds_left_and_shift(probe)
    # As the warning says: KWin reset its modifier state, so a new key is unshifted
    # although the app saw no shift release.
    mark = probe.mark()
    pressed = engine.keyboard_press("a")
    (key,) = of_kind(
        probe.wait_events(lambda evs: bool(of_kind(evs, "key_press")), since=mark), "key_press"
    )
    assert "shift" not in key["mods"], key
    assert pressed.held == HeldInput(buttons=["left"], keys=["shift"]), pressed.render()
    # The explicit release reaches the app through the replacement keyboard: a fresh
    # Shift_L release event (held_mods alone already reads none after the unshifted a).
    mark = probe.mark()
    assert engine.keyboard_hold("release", "shift").held == HeldInput(buttons=["left"])
    _wait_release(probe, mark, "key_release", "key", "Shift_L")
    mark = probe.mark()
    released = engine.pointer_hold("release", probe.center("Drag Target"))
    assert released.held == HeldInput()
    _wait_release(probe, mark, "button_release", "button", "left")

    # (b) the absolute pointer replaced (output change) while left (and shift) are held.
    _hold_left_and_shift(engine, probe)
    change_output(engine, tmp_path)
    info = _reports_until(engine, POINTER_REPLACED, probe)
    assert info.held == HeldInput(buttons=["left"], keys=["shift"]), info.render()
    _app_holds_left_and_shift(probe)
    # The drag goes on, and its release reaches the app through the new pointer.
    moved = engine.pointer_hold("move", probe.center("Click Target"))
    assert moved.held == HeldInput(buttons=["left"], keys=["shift"]), moved.render()
    mark = probe.mark()
    assert engine.pointer_hold("release", probe.center("Click Target")).held == HeldInput(
        keys=["shift"]
    )
    _wait_release(probe, mark, "button_release", "button", "left")
    with pytest.raises(HeldControlError, match="left is not held"):
        engine.pointer_hold("move", probe.center("Click Target"))
    mark = probe.mark()
    assert engine.keyboard_hold("release", "shift").held == HeldInput()
    _wait_release(probe, mark, "key_release", "key", "Shift_L")
