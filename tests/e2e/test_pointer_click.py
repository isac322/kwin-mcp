"""pointer_click end to end: buttons, multi-click counts, hold_ms, the motion nudge, modifiers.

The oracle is the interaction probe's raw event log (``probe_session``) plus KCalc's
display for the ported 0.11 "hits seven, then zero" case.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

import pytest
from probe_session import (
    BUTTON_KINDS,
    GESTURE_GAP_S,
    POINTER_KINDS,
    Probe,
    assert_interval,
    center_of,
    field,
    int_field,
    kcalc_button,
    kinds,
    of_kind,
    start_probe_session,
    wait_for_kcalc_binary,
)

from kwin_mcp.models import HeldInput, Point

if TYPE_CHECKING:
    from pathlib import Path

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import MouseButtonName

# Press length kwin-mcp uses for every click but a held last one.
CLICK_PRESS_MS = 25.0
MULTI_CLICK_GAP_MS = 60.0


@pytest.fixture
def probe(engine: AutomationEngine, tmp_path: Path) -> Probe:
    return start_probe_session(engine, tmp_path)


def _click_events(probe: Probe, mark: int, presses: int) -> list[dict[str, object]]:
    """Pointer events after ``mark`` once ``presses`` button releases arrived."""
    probe.wait_events(lambda evs: len(of_kind(evs, "button_release")) >= presses, since=mark)
    return [e for e in probe.settle(since=mark) if e["kind"] in POINTER_KINDS]


@pytest.mark.qa("QA-INP-001")
def test_click_point_hits_kcalc_seven(
    engine: AutomationEngine, kcalc_session: AutomationEngine, tmp_path: Path
) -> None:
    # 0.11 port: a click on Seven, then on Zero, reaches KCalc (binary 111, then 1000110).
    seven = center_of(kcalc_button(engine, "Seven"))
    zero = center_of(kcalc_button(engine, "Zero"))
    result = engine.pointer_click(seven)
    assert result.tool == "pointer_click"
    assert result.at == seven and result.element is None
    assert result.held == HeldInput() and result.warnings == []
    assert result.render() == f"Clicked left at ({seven.x}, {seven.y})"
    wait_for_kcalc_binary(engine, "111")
    engine.pointer_click(zero)
    wait_for_kcalc_binary(engine, "1000110")
    assert engine.session_stop().stopped

    # Each button presses and releases at the mapped point (GDK buttons 1/3/2).
    probe = start_probe_session(engine, tmp_path)
    target = probe.center("Click Target")
    local = probe.to_local(target.x, target.y)
    for button in ("left", "right", "middle"):
        mark = probe.park()
        result = engine.pointer_click(target, button=button)
        assert result.model_dump() == {
            "tool": "pointer_click",
            "at": {"x": target.x, "y": target.y},
            "element": None,
            "frames": [],
            "held": {"buttons": [], "keys": []},
            "warnings": [],
            "button": button,
            "count": 1,
            "modifiers": None,
            "hold_ms": 0,
        }
        buttons = of_kind(_click_events(probe, mark, 1), *BUTTON_KINDS)
        assert [(e["kind"], e["button"], e["x"], e["y"]) for e in buttons] == [
            ("button_press", button, *local),
            ("button_release", button, *local),
        ]

    # 'back' is not a button: refused before any event. The cast states the
    # deliberately invalid value the engine's own validation must reject.
    mark = probe.park()
    with pytest.raises(ValueError, match="back"):
        engine.pointer_click(target, button=cast("MouseButtonName", "back"))
    time.sleep(0.5)
    assert probe.events_since(mark) == []


@pytest.mark.qa("QA-INP-002")
def test_count_two_three_reach_probe(engine: AutomationEngine, probe: Probe) -> None:
    target = probe.center("Click Target")
    expected = {
        2: ["button_press", "button_release", "button_press", "gtk_double_press", "button_release"],
        3: [
            "button_press",
            "button_release",
            "button_press",
            "gtk_double_press",
            "button_release",
            "button_press",
            "gtk_triple_press",
            "button_release",
        ],
    }
    for count, sequence in expected.items():
        mark = probe.park()
        result = engine.pointer_click(target, count=count)
        assert result.count == count
        suffix = " (double)" if count == 2 else " (triple)"
        assert result.render() == f"Clicked left at ({target.x}, {target.y}){suffix}"
        events = _click_events(probe, mark, count)
        assert [k for k in kinds(events) if k != "motion"] == sequence
        status = probe.wait_status("click_status:", lambda s, c=count: int_field(s, "count") == c)
        assert field(status, "button") == "left", status

    mark = probe.park()
    with pytest.raises(ValueError, match=r"count must be 1, 2 or 3, got 4\."):
        engine.pointer_click(target, count=4)
    time.sleep(0.5)
    assert probe.events_since(mark) == []


@pytest.mark.qa("QA-INP-004")
def test_hold_ms_zero_clamped_to_25ms_not_rejected(engine: AutomationEngine, probe: Probe) -> None:
    target = probe.center("Click Target")
    for hold_ms in (0, -5):
        mark = probe.park()
        result = engine.pointer_click(target, hold_ms=hold_ms)
        assert result.hold_ms == hold_ms
        press, release = of_kind(_click_events(probe, mark, 1), *BUTTON_KINDS)
        assert (press["kind"], release["kind"]) == ("button_press", "button_release")
        assert_interval(press, release, at_least_ms=CLICK_PRESS_MS)


@pytest.mark.qa("QA-INP-004")
def test_hold_ms_measured_by_probe(engine: AutomationEngine, probe: Probe) -> None:
    target = probe.center("Click Target")
    mark = probe.park()
    result = engine.pointer_click(target, count=2, hold_ms=400)
    assert result.render() == f"Clicked left at ({target.x}, {target.y}) (double) held 400ms"
    presses = of_kind(_click_events(probe, mark, 2), *BUTTON_KINDS)
    assert kinds(presses) == ["button_press", "button_release"] * 2
    # hold_ms applies to the last click only; earlier presses keep the 25 ms default.
    assert_interval(presses[0], presses[1], at_least_ms=CLICK_PRESS_MS)
    assert_interval(presses[1], presses[2], at_least_ms=MULTI_CLICK_GAP_MS)
    assert_interval(presses[2], presses[3], at_least_ms=400)
    status = probe.wait_status("click_status:", lambda s: int_field(s, "count") == 2)
    assert int_field(status, "hold_ms") >= 360, status


@pytest.mark.qa("QA-INP-006")
def test_motion_nudge_before_press(engine: AutomationEngine, probe: Probe) -> None:
    point = probe.to_global(200, 200)
    probe.park()
    engine.pointer_move(point)
    time.sleep(GESTURE_GAP_S)
    mark = probe.mark()
    engine.pointer_click(Point(x=point.x, y=point.y))
    events = _click_events(probe, mark, 1)
    # The pointer is already there, yet the click re-sends motion (x-1, then back)
    # so the surface gets a fresh position before the press.
    assert [(e["kind"], e["x"], e["y"]) for e in events] == [
        ("motion", 199, 200),
        ("motion", 200, 200),
        ("button_press", 200, 200),
        ("button_release", 200, 200),
    ]


@pytest.mark.qa("QA-INP-009")
def test_unknown_modifier_error_before_input(engine: AutomationEngine, probe: Probe) -> None:
    target = probe.center("Click Target")
    mark = probe.park()
    with pytest.raises(ValueError, match=r"^Unknown modifier: 'hyperx'$"):
        engine.pointer_click(target, modifiers="ctrl+hyperx")
    time.sleep(0.5)
    assert probe.events_since(mark) == []

    # Aliases are case-insensitive: control == ctrl.
    mark = probe.park()
    engine.pointer_click(target, modifiers="control+shift")
    probe.wait_events(lambda evs: len(of_kind(evs, "key_release")) >= 2, since=mark)
    events = probe.settle(since=mark)
    keys = [(e["kind"], e["key"]) for e in of_kind(events, "key_press", "key_release")]
    assert keys == [
        ("key_press", "Control_L"),
        ("key_press", "Shift_L"),
        ("key_release", "Shift_L"),
        ("key_release", "Control_L"),
    ]
    (press,) = of_kind(events, "button_press")
    assert press["mods"] == ["ctrl", "shift"]

    # 'META' and 'super' both name the Super key.
    for modifiers in ("META", "super"):
        mark = probe.park()
        result = engine.pointer_click(target, modifiers=modifiers)
        assert result.modifiers == modifiers
        probe.wait_events(lambda evs: len(of_kind(evs, "key_release")) >= 1, since=mark)
        events = probe.settle(since=mark)
        keys = [(e["kind"], e["key"]) for e in of_kind(events, "key_press", "key_release")]
        assert keys == [("key_press", "Super_L"), ("key_release", "Super_L")], events
