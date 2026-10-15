"""pointer_hold end to end: press/move/release across calls, held-state bookkeeping.

The probe's raw event log (``probe_session``) is the oracle; the 0.11 manual drag
(``mouse_button_down -> mouse_move -> mouse_button_up``) is the golden
``tests/e2e/golden/down_move_up.json`` (amendment A14).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from typing import TYPE_CHECKING

import pytest
from _suite_policy import require_or_skip
from probe_session import (
    BUTTON_KINDS,
    HOLD_SETTLE_S,
    POINTER_KINDS,
    STATE_TIMEOUT_S,
    Probe,
    assert_matches_golden,
    center_of,
    counts,
    field,
    kcalc_button,
    load_golden,
    of_kind,
    point_field,
    start_probe_session,
    wait_for_kcalc_binary,
)

from kwin_mcp.input import HeldControlError
from kwin_mcp.models import HeldInput

if TYPE_CHECKING:
    from pathlib import Path

    from kwin_mcp.core import AutomationEngine

STUCK_LIMIT = (
    "known KWin limit: release from a second EIS device does not clear another device's press"
)

# A second EIS client: presses left at (x, y), reports, then waits for stdin EOF and
# exits without ever releasing the button.
_EXTERNAL_PRESS = textwrap.dedent(
    """
    import os, sys, time
    from kwin_mcp.input import EISClient
    try:
        client = EISClient(sys.argv[1])
    except RuntimeError as exc:
        print(f"refused: {exc}", flush=True)
        sys.exit(0)
    x, y = float(sys.argv[2]), float(sys.argv[3])
    client.pointer_move_absolute(x - 1, y)
    client.pointer_move_absolute(x, y)
    time.sleep(0.05)
    client.pointer_button(0x110, 1)
    print("pressed", flush=True)
    sys.stdin.read()
    os._exit(0)
    """
)


@pytest.fixture
def probe(engine: AutomationEngine, tmp_path: Path) -> Probe:
    return start_probe_session(engine, tmp_path)


def _pointer_events(probe: Probe, mark: int) -> list[dict[str, object]]:
    return [e for e in probe.settle(since=mark) if e["kind"] in POINTER_KINDS]


def test_pointer_hold_press_and_release_activate_seven(kcalc_session: AutomationEngine) -> None:
    # 0.11 port (mouse_button_down/up -> pointer_hold press/release).
    seven = center_of(kcalc_button(kcalc_session, "Seven"))
    pressed = kcalc_session.pointer_hold("press", seven)
    assert pressed.changed and pressed.held == HeldInput(buttons=["left"])
    deadline = time.monotonic() + STATE_TIMEOUT_S
    while "pressed" not in kcalc_button(kcalc_session, "Seven").states:
        assert time.monotonic() < deadline, kcalc_button(kcalc_session, "Seven")
        time.sleep(0.1)
    released = kcalc_session.pointer_hold("release", seven)
    assert released.changed and released.held == HeldInput()
    wait_for_kcalc_binary(kcalc_session, "111")


@pytest.mark.qa("QA-INP-027")
def test_press_move_release_is_manual_drag(engine: AutomationEngine, probe: Probe) -> None:
    golden = load_golden("down_move_up")
    a, b, c = (probe.to_global(*golden["call"]["local"][name]) for name in ("A", "B", "C"))
    mark = probe.park()
    pressed = engine.pointer_hold("press", a)
    moved = engine.pointer_hold("move", b)
    released = engine.pointer_hold("release", c)
    # changed = whether the held set changed (S): a move never changes it.
    assert (pressed.changed, moved.changed, released.changed) == (True, False, True)
    assert pressed.held == moved.held == HeldInput(buttons=["left"])
    assert released.held == HeldInput()
    assert [r.render() for r in (pressed, moved, released)] == [
        f"Button left pressed at ({a.x}, {a.y})\nStill held: left",
        f"Button left moved to ({b.x}, {b.y}) while held\nStill held: left",
        f"Button left released at ({c.x}, {c.y})",
    ]
    probe.wait_events(lambda evs: bool(of_kind(evs, "button_release")), since=mark)
    assert_matches_golden(_pointer_events(probe, mark), golden)


@pytest.mark.qa("QA-INP-028")
def test_press_on_held_button_is_noop_changed_false_but_moves(
    engine: AutomationEngine, probe: Probe
) -> None:
    a = probe.center("Drag Target")
    b = a.model_copy(update={"x": a.x + 60})
    mark = probe.park()
    first = engine.pointer_hold("press", a)
    time.sleep(HOLD_SETTLE_S)
    second = engine.pointer_hold("press", b)
    assert first.changed and not second.changed
    assert second.held == HeldInput(buttons=["left"])
    assert second.render() == (
        f"Button left already held; moved to ({b.x}, {b.y}) without a press\nStill held: left"
    )
    probe.wait_events(
        lambda evs: any(probe.to_global(e["x"], e["y"]) == b for e in of_kind(evs, "motion")),
        since=mark,
    )
    events = _pointer_events(probe, mark)
    assert len(of_kind(events, "button_press")) == 1, events
    assert not of_kind(events, "button_release"), events
    status = probe.wait_status("hold_status:", lambda s: field(s, "held") == "left")
    assert counts(status, "left") == (1, 0), status

    engine.pointer_hold("release", b)
    # The drag target records widget-local points: the drag went 60 px right of the press.
    drag = probe.wait_status("drag_status:", lambda s: field(s, "release") == "left")
    assert field(drag, "source") == "mouse" and field(drag, "press") == "left", drag
    (sx, sy), (ex, ey) = point_field(drag, "start"), point_field(drag, "end")
    assert (ex - sx, ey - sy) == (60, 0), drag


@pytest.mark.qa("QA-INP-030")
def test_release_frees_externally_stuck_button(engine: AutomationEngine, probe: Probe) -> None:
    target = probe.to_global(300, 200)
    mark = probe.park()
    external = subprocess.Popen(
        [sys.executable, "-c", _EXTERNAL_PRESS, probe.dbus_address, str(target.x), str(target.y)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert external.stdout is not None
        line = external.stdout.readline().strip()
        if line.startswith("refused"):
            require_or_skip("eis-second-client", f"{STUCK_LIMIT} ({line})", os.environ)
        assert line == "pressed", line
        probe.wait_events(lambda evs: bool(of_kind(evs, "button_press")), since=mark)
        stuck = probe.wait_status("hold_status:", lambda s: field(s, "held") == "left")
        assert counts(stuck, "left") == (1, 0), stuck

        result = engine.pointer_hold("release", target)
        # kwin-mcp never held it, so its held set did not change, but the release is sent.
        assert not result.changed and result.held == HeldInput()
        assert result.render() == (
            f"Button left released at ({target.x}, {target.y}) (it was not held by kwin-mcp)"
        )
        deadline = time.monotonic() + STATE_TIMEOUT_S
        while not of_kind(probe.events_since(mark), "button_release"):
            if time.monotonic() >= deadline:
                require_or_skip("eis-cross-device-release", STUCK_LIMIT, os.environ)
            time.sleep(0.1)
        released = probe.wait_status("hold_status:", lambda s: field(s, "held") == "none")
        assert counts(released, "left") == (1, 1), released
    finally:
        if external.stdin is not None:
            external.stdin.close()
        external.wait(timeout=10)


@pytest.mark.qa("QA-INP-031")
def test_move_on_unheld_button_is_teaching_error(engine: AutomationEngine, probe: Probe) -> None:
    target = probe.center("Click Target")
    mark = probe.park()
    cursor = probe.oracle().cursor_pos()
    with pytest.raises(HeldControlError) as refused:
        engine.pointer_hold("move", target)
    assert str(refused.value) == (
        "pointer_hold(phase='move') refused: left is not held.\n"
        "Press it first with pointer_hold(phase='press', button='left', target={x, y})."
    )
    time.sleep(0.5)
    assert probe.events_since(mark) == []
    assert probe.oracle().cursor_pos() == cursor
    raw = probe.status("raw_events:")
    assert (field(raw, "bp"), field(raw, "br")) == ("0", "0"), raw


@pytest.mark.qa("QA-INP-032")
def test_move_phase_no_nudge(engine: AutomationEngine, probe: Probe) -> None:
    mark = probe.park()
    engine.pointer_hold("press", probe.to_global(500, 300))
    press_mark = len(probe.wait_events(lambda evs: bool(of_kind(evs, "button_press")), since=mark))
    engine.pointer_hold("move", probe.to_global(700, 300))
    engine.pointer_hold("release", probe.to_global(700, 300))
    probe.wait_events(lambda evs: bool(of_kind(evs, "button_release")), since=mark)
    after_press = _pointer_events(probe, mark + press_mark)
    motions = of_kind(after_press, "motion")
    xs = [e["x"] for e in motions]
    # One straight segment: monotonic, never an out-and-back via x=699.
    assert xs == sorted(xs) and xs[-1] == 700, motions
    assert 699 not in xs, motions
    assert all(e["held"] == ["left"] for e in motions), motions
    assert [e["kind"] for e in after_press if e["kind"] in BUTTON_KINDS] == ["button_release"]
