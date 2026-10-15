"""pointer_move end to end: hover, element points, and the held-button refusal (Q5)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from probe_session import (
    BUTTON_KINDS,
    GESTURE_GAP_S,
    POLL_S,
    STATE_TIMEOUT_S,
    Probe,
    center_of,
    kcalc_button,
    kinds,
    of_kind,
    start_probe_session,
    wait_for_kcalc_binary,
)

from kwin_mcp.input import HeldControlError
from kwin_mcp.models import ElementPoint, HeldInput, Locator, Point, Wait

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import SessionInfo

GUI_PROBE_COMMAND = "python3 /app/tests/e2e/gui_probe.py"
GUI_PROBE_APP = "gui_probe.py"


@pytest.fixture
def probe(engine: AutomationEngine, tmp_path: Path) -> Probe:
    return start_probe_session(engine, tmp_path)


def _wait_for_name(engine: AutomationEngine, app: str, name: str) -> None:
    found = engine.element_query(
        Locator(app=app, query=name), wait=Wait(timeout_ms=int(STATE_TIMEOUT_S * 1000))
    )
    assert any(e.name == name for e in found.elements), found.render()[:1500]


def _cursor(probe: Probe) -> tuple[int, int]:
    x, y = probe.oracle().cursor_pos()
    return round(x), round(y)


@pytest.mark.qa("QA-INP-011")
def test_move_enters_hover_target(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., object],
    wait_for_app: Callable[[str], object],
    tmp_path: Path,
) -> None:
    # 0.11 port: a standalone virtual KWin (not the nested xdotool display) delivers
    # the hover to the GTK widget under the pointer.
    info = start_session(home="isolated")
    assert info.kind == "virtual" and info.input_backend == "eis", info.render()
    launch(GUI_PROBE_COMMAND)
    wait_for_app(GUI_PROBE_APP)
    _wait_for_name(engine, GUI_PROBE_APP, "hover_status: idle")
    found = engine.element_query(Locator(app=GUI_PROBE_APP, query="Hover Target"))
    (hover,) = [e for e in found.elements if e.name == "Hover Target"]
    target = center_of(hover)
    result = engine.pointer_move(target)
    assert result.tool == "pointer_move" and result.at == target
    assert result.held == HeldInput() and result.warnings == []
    assert result.render() == f"Mouse moved to ({target.x}, {target.y})"
    _wait_for_name(engine, GUI_PROBE_APP, "hover_status: entered")
    assert engine.session_stop().stopped

    # The raw event log shows motion only: a hover never presses anything.
    probe = start_probe_session(engine, tmp_path)
    mark = probe.park()
    goal = probe.center("Click Target")
    engine.pointer_move(goal)
    events = probe.wait_events(
        lambda evs: any(
            e["kind"] == "motion" and probe.to_global(e["x"], e["y"]) == goal for e in evs
        ),
        since=mark,
    )
    time.sleep(0.5)
    events = probe.events_since(mark)
    assert set(kinds(events)) == {"motion"}, events
    assert all(e["held"] == [] for e in events), events


def test_pointer_move_then_click_keeps_the_pointer_on_seven(
    kcalc_session: AutomationEngine,
) -> None:
    # KCalc exposes no hover state; the click after the move proves where it landed.
    seven = center_of(kcalc_button(kcalc_session, "Seven"))
    assert kcalc_session.pointer_move(seven).render() == f"Mouse moved to ({seven.x}, {seven.y})"
    kcalc_session.pointer_click(seven)
    wait_for_kcalc_binary(kcalc_session, "111")


@pytest.mark.qa("QA-INP-012")
def test_move_refused_while_button_held_with_literal_remedy(
    engine: AutomationEngine, probe: Probe
) -> None:
    a = probe.to_global(200, 200)
    b = probe.to_global(400, 300)
    probe.park()
    pressed = engine.pointer_hold("press", a)
    assert pressed.held == HeldInput(buttons=["left"])
    time.sleep(0.3)
    mark = probe.mark()
    cursor = _cursor(probe)
    assert cursor == (a.x, a.y)

    with pytest.raises(HeldControlError) as refused:
        engine.pointer_move(b)
    assert str(refused.value) == (
        "pointer_move refused: left is held by pointer_hold.\n"
        "To continue: use pointer_hold(phase='move', button, target) to move a held button."
    )
    time.sleep(0.5)
    assert probe.events_since(mark) == []
    assert _cursor(probe) == cursor
    info = engine.session_info()
    assert info.held == HeldInput(buttons=["left"]), info.render()

    released = engine.pointer_hold("release", a)
    assert released.held == HeldInput()


@pytest.mark.qa("QA-INP-013")
def test_move_allowed_while_only_keys_held(engine: AutomationEngine, probe: Probe) -> None:
    target = probe.center("Click Target")
    probe.park()
    engine.keyboard_hold("press", "shift")
    result = engine.pointer_move(target)
    assert result.held == HeldInput(keys=["shift"])
    assert result.render() == (f"Mouse moved to ({target.x}, {target.y})\nStill held: shift")
    deadline = time.monotonic() + STATE_TIMEOUT_S
    while _cursor(probe) != (target.x, target.y):
        assert time.monotonic() < deadline, _cursor(probe)
        time.sleep(POLL_S)
    assert engine.keyboard_hold("release", "shift").held == HeldInput()


@pytest.mark.qa("QA-INP-015")
def test_move_to_element_point(engine: AutomationEngine, probe: Probe) -> None:
    element = probe.element("Click Target")
    assert element.rect is not None
    centre = center_of(element)
    for dx, dy in ((0, 0), (-50, 10)):
        mark = probe.park()
        result = engine.pointer_move(ElementPoint(ref=element.ref, dx=dx, dy=dy))
        expected = Point(x=centre.x + dx, y=centre.y + dy)
        assert result.at == expected
        assert result.element is not None and result.element.name == "Click Target"
        local = probe.to_local(expected.x, expected.y)
        events = probe.wait_events(
            lambda evs, lo=local: any((e["x"], e["y"]) == lo for e in of_kind(evs, "motion")),
            since=mark,
        )
        assert not of_kind(events, *BUTTON_KINDS), events
        motions = of_kind(probe.settle(since=mark), "motion")
        assert (motions[-1]["x"], motions[-1]["y"]) == local, motions
        time.sleep(GESTURE_GAP_S / 4)
