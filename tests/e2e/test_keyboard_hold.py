"""End-to-end contracts for keyboard_hold: keys held across calls.

The oracle is the interaction probe's raw event log (PEL): every key and button
event GTK receives, with the modifiers held before it, plus its ``key_log:`` and
``click_status:`` labels. KWrite's document is read through the independent
AT-SPI oracle.
"""

from __future__ import annotations

import shlex
import time
from typing import TYPE_CHECKING

import pytest
from atspi_oracle import AtspiOracle
from probe_session import (
    BUTTON_KINDS,
    KEY_KINDS,
    Probe,
    assert_matches_golden,
    field,
    load_golden,
    of_kind,
    start_probe_session,
)

from kwin_mcp.models import HeldInput, KeyboardHoldResult, WindowSelector

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import AppLaunchResult, ElementList, KeyboardHoldPhase, SessionInfo

WAIT_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.1


@pytest.fixture
def probe(engine: AutomationEngine, tmp_path: Path) -> Probe:
    return start_probe_session(engine, tmp_path)


def _hold(
    probe: Probe, phase: KeyboardHoldPhase, keys: str
) -> tuple[KeyboardHoldResult, list[dict[str, object]]]:
    """Run one keyboard_hold phase and return the result with the key events it sent."""
    mark = probe.mark()
    result = probe.engine.keyboard_hold(phase, keys)
    events = of_kind(probe.settle(since=mark), *KEY_KINDS)
    return result, events


def _keys(events: list[dict[str, object]]) -> list[tuple[str, str]]:
    return [(str(e["kind"]), str(e["key"])) for e in events]


def _wait_until(predicate: Callable[[], bool], description: str) -> None:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out waiting for {description}")
        time.sleep(POLL_INTERVAL_SECONDS)


def _focused_text(atspi: AtspiOracle) -> str:
    """Text of KWrite's focused document, read by the test's own AT-SPI walk."""
    nodes = [
        n
        for n in atspi.find(app="kwrite", role="text")
        if "focused" in n.states and n.error is None
    ]
    assert len(nodes) == 1, [(n.role, n.name, sorted(n.states)) for n in nodes]
    return nodes[0].text or ""


def test_keyboard_hold_press_and_release_control_shift(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
    tmp_path: Path,
) -> None:
    info = start_session()
    assert info.input_backend == "eis", info
    launch(f"kwrite {shlex.quote(str(tmp_path / 'kwin-mcp-key-pair.txt'))}")
    wait_for_app("kwrite")
    engine.window_focus(WindowSelector(app_id="org.kde.kwrite"))
    atspi = AtspiOracle(info.dbus_address)

    down = engine.keyboard_hold("press", "shift")
    assert down.render() == "Key down: shift\nStill held: shift"
    engine.keyboard_press("a")
    up = engine.keyboard_hold("release", "shift")
    assert up.render() == "Key up: shift"
    engine.keyboard_press("a")

    _wait_until(lambda: _focused_text(atspi) == "Aa", "document text 'Aa'")


@pytest.mark.qa("QA-INP-040")
def test_press_shift_then_click_reports_shift_modifier(probe: Probe) -> None:
    target = probe.center("Click Target")
    mark = probe.park()

    pressed = probe.engine.keyboard_hold("press", "ctrl+shift")
    assert pressed.changed is True
    assert pressed.held == HeldInput(keys=["ctrl", "shift"])
    assert pressed.render() == "Key down: ctrl+shift\nStill held: ctrl, shift"

    clicked = probe.engine.pointer_click(target)
    assert clicked.held == HeldInput(keys=["ctrl", "shift"])
    assert clicked.render().splitlines()[-1] == "Still held: ctrl, shift"
    status = probe.wait_status("click_status:", lambda s: field(s, "count") == "1")
    assert field(status, "modifiers") == "ctrl+shift", status

    released = probe.engine.keyboard_hold("release", "ctrl+shift")
    assert released.changed is True
    assert released.held == HeldInput()
    assert released.render() == "Key up: ctrl+shift"

    events = probe.settle(since=mark)
    buttons = of_kind(events, *BUTTON_KINDS)
    assert [(e["kind"], e["button"], e["mods"]) for e in buttons] == [
        ("button_press", "left", ["ctrl", "shift"]),
        ("button_release", "left", ["ctrl", "shift"]),
    ]
    assert _keys(of_kind(events, *KEY_KINDS)) == [
        ("key_press", "Control_L"),
        ("key_press", "Shift_L"),
        ("key_release", "Shift_L"),
        ("key_release", "Control_L"),
    ]

    # Any key or full chord can be held (R8-15); held.keys lists every key.
    chord, events = _hold(probe, "press", "ctrl+shift+t")
    assert chord.changed is True
    assert chord.held == HeldInput(keys=["ctrl", "shift", "t"])
    assert chord.render() == "Key down: ctrl+shift+t\nStill held: ctrl, shift, t"
    assert _keys(events) == [
        ("key_press", "Control_L"),
        ("key_press", "Shift_L"),
        ("key_press", "T"),
    ]


@pytest.mark.qa("QA-INP-040")
def test_release_reverse_order(probe: Probe) -> None:
    """Press goes modifiers first; release goes in reverse, timed as 0.11's key_down/up."""
    mark = probe.mark()
    pressed = probe.engine.keyboard_hold("press", "ctrl+shift")
    released = probe.engine.keyboard_hold("release", "ctrl+shift")
    events = of_kind(probe.settle(since=mark), *KEY_KINDS)

    assert (pressed.render(), released.render()) == (
        "Key down: ctrl+shift\nStill held: ctrl, shift",
        "Key up: ctrl+shift",
    )
    assert_matches_golden(events, load_golden("key_down_up_ctrl_shift"))

    _, down = _hold(probe, "press", "ctrl+shift+t")
    _, up = _hold(probe, "release", "ctrl+shift+t")
    assert _keys(down) == [
        ("key_press", "Control_L"),
        ("key_press", "Shift_L"),
        ("key_press", "T"),
    ]
    assert _keys(up) == [
        ("key_release", "T"),
        ("key_release", "Shift_L"),
        ("key_release", "Control_L"),
    ]
    assert probe.engine.session_info().held == HeldInput()


@pytest.mark.qa("QA-INP-041")
def test_second_press_sends_no_event(probe: Probe) -> None:
    first, events = _hold(probe, "press", "shift")
    assert first.changed is True
    assert _keys(events) == [("key_press", "Shift_L")]

    second, events = _hold(probe, "press", "shift")
    assert second.changed is False
    assert events == []
    assert second.held == HeldInput(keys=["shift"])
    assert second.render() == ("Key down: shift (already held; no event sent)\nStill held: shift")

    # A partial overlap sends only the keys not held yet.
    overlap, events = _hold(probe, "press", "ctrl+shift")
    assert overlap.changed is True
    assert _keys(events) == [("key_press", "Control_L")]
    # Held keys are listed in press order.
    assert overlap.held == HeldInput(keys=["shift", "ctrl"])

    key_log = probe.status("key_log:")
    assert "Shift_L=1/0" in key_log and "Control_L=1/0" in key_log, key_log
    assert key_log.endswith(" repeats=0"), key_log


@pytest.mark.qa("QA-INP-042")
def test_release_superset_chord(probe: Probe) -> None:
    _hold(probe, "press", "ctrl")

    released, events = _hold(probe, "release", "ctrl+shift")

    assert released.changed is True
    assert released.held == HeldInput()
    assert released.render() == "Key up: ctrl+shift"
    # Both releases are sent to EIS (F110; the sent events are pinned on the fake
    # client in tests/unit/test_held_state_machine.py), but KWin drops the release
    # of a key that is not down, so the client sees only the held ctrl going up.
    assert _keys(events) == [("key_release", "Control_L")]
    assert events[0]["held_mods"] == "none"
    assert "Shift_L" not in probe.status("key_log:")
    assert probe.engine.session_info().held == HeldInput()


@pytest.mark.qa("QA-INP-043")
def test_invalid_key_in_hold_sends_nothing(probe: Probe) -> None:
    before = probe.status("key_log:")
    mark = probe.mark()

    with pytest.raises(ValueError) as excinfo:
        probe.engine.keyboard_hold("press", "ctrl+notakey")

    assert str(excinfo.value) == "Unknown key: ctrl+notakey"
    assert probe.settle(since=mark) == []
    assert probe.status("key_log:") == before
    assert probe.engine.session_info().held == HeldInput()
