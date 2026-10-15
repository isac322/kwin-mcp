"""pointer_scroll end to end: smooth 15 px units (G6), detents, axes, steps, delta 0.

GTK reports a smooth wl_pointer axis value as px / 10, so one 15 px unit arrives as
``dy=1.5``; the probe's Horizontal Scroll Region turns each raw delta into
``SCROLL_STEP`` (48) px of adjustment.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from probe_session import (
    Probe,
    assert_interval,
    assert_matches_golden,
    center_of,
    float_field,
    int_field,
    kinds,
    load_golden,
    of_kind,
    start_probe_session,
)

from kwin_mcp.models import Locator

if TYPE_CHECKING:
    from collections.abc import Callable

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import ElementList, UIElement

STEP_INTERVAL_MS = 10.0
LONG_DOCUMENT = Path("/tmp/kwin-mcp-scroll.txt")


@pytest.fixture
def probe(engine: AutomationEngine, tmp_path: Path) -> Probe:
    return start_probe_session(engine, tmp_path)


def _scroll_events(probe: Probe, mark: int) -> list[dict[str, object]]:
    """Every event after ``mark`` once the scroll-stop event arrived."""
    probe.wait_events(
        lambda evs: any(e["kind"] == "scroll" and e.get("is_stop") for e in evs), since=mark
    )
    return probe.settle(since=mark)


def _smooth(events: list[dict[str, object]]) -> list[dict[str, object]]:
    return [e for e in of_kind(events, "scroll") if e["direction"] == "smooth" and not e["is_stop"]]


def _number(value: object) -> float:
    """A numeric probe event field (the log is JSON, so ints and floats)."""
    assert isinstance(value, int | float), value
    return float(value)


def _scrollbar(engine: AutomationEngine) -> UIElement:
    found = engine.element_query(Locator(app="kwrite", role="scroll bar"))
    usable = [
        e
        for e in found.elements
        if e.value is not None
        and e.value_max
        and e.rect is not None
        and e.rect.height > e.rect.width
    ]
    assert usable, found.render()[:1500]
    return usable[0]


def test_pointer_scroll_moves_the_scrollbar(
    kcalc_session: AutomationEngine,
    launch: Callable[..., object],
    wait_for_app: Callable[[str], ElementList],
) -> None:
    LONG_DOCUMENT.write_text(
        "\n".join(f"line {i:03d} ---------------------------" for i in range(200)), encoding="utf-8"
    )
    launch(f"kwrite {LONG_DOCUMENT}")
    wait_for_app("kwrite")
    # Placement and the first paint settle shortly after the app reaches the bus.
    time.sleep(2)
    found = kcalc_session.element_query(
        Locator(app="kwrite", query=LONG_DOCUMENT.name, role="text")
    )
    (area,) = [e for e in found.elements if e.rect is not None]
    target = center_of(area)
    assert _scrollbar(kcalc_session).value == 0.0

    kcalc_session.pointer_scroll(target, delta=10)
    time.sleep(1)
    smooth = _scrollbar(kcalc_session).value
    assert smooth is not None and smooth > 0.0, "smooth scrolling did not move the view"

    # Detents travel as 120ths of a wheel click through libei.
    kcalc_session.pointer_scroll(target, delta=5, unit="detent")
    time.sleep(1)
    detent = _scrollbar(kcalc_session).value
    assert detent is not None and detent > smooth, "detent scrolling did nothing"


@pytest.mark.qa("QA-INP-016")
def test_smooth_step_default_15px_per_unit(engine: AutomationEngine, probe: Probe) -> None:
    # The 0.11 smooth timelines at probe-local (300, 200), one call and three steps.
    for scenario, steps in (("scroll_smooth_3", 1), ("scroll_smooth_3_steps_3", 3)):
        golden = load_golden(scenario)
        mark = probe.park()
        engine.pointer_scroll(probe.to_global(*golden["call"]["local"]), delta=3, steps=steps)
        assert_matches_golden(_scroll_events(probe, mark), golden)

    # Horizontal, in 3 steps, over the probe's scroll region: 3 x 15 px = 45 px.
    region = probe.center("Horizontal Scroll Region")
    before = probe.status("horizontal_scroll:")
    mark = probe.park()
    result = engine.pointer_scroll(region, delta=3, axis="horizontal", steps=3)
    assert (result.unit, result.steps) == ("smooth_step", 3)
    assert result.render() == (
        f"Scrolled horizontal (smooth) by 3 at ({region.x}, {region.y}) in 3 steps"
    )
    events = _scroll_events(probe, mark)
    smooth = _smooth(events)
    assert [(e["dx"], e["dy"]) for e in smooth] == [(1.5, 0.0)] * 3, events
    stop = of_kind(events, "scroll")[-1]
    assert stop["is_stop"], events
    for earlier, later in zip(smooth, [*smooth[1:], stop], strict=True):
        # The 10 ms step sleep is the lower bound that detects merged steps. The
        # upper basis is wider than the shared 3x rule's default: under host CPU
        # starvation the sender's sleep overshoots (measured 37-54 ms on BOTH the
        # probe clock and KWin's event_time, so it is sender-side, not dispatch
        # jitter); 90 ms still catches a reordered or missing 10 ms wait.
        assert_interval(earlier, later, at_least_ms=STEP_INTERVAL_MS, golden_ms=30.0)
    status = probe.wait_status(
        "horizontal_scroll:",
        lambda s: int_field(s, "events") >= int_field(before, "events") + 3,
    )
    assert status.split()[1] == "mode=smooth", status
    assert float_field(status, "total") - float_field(before, "total") == pytest.approx(4.5)


@pytest.mark.qa("QA-INP-017")
def test_detent_unit_whole_clicks(engine: AutomationEngine, probe: Probe) -> None:
    golden = load_golden("scroll_discrete_2")
    mark = probe.park()
    engine.pointer_scroll(probe.to_global(*golden["call"]["local"]), delta=2, unit="detent")
    assert_matches_golden(_scroll_events(probe, mark), golden)

    target = probe.to_global(300, 200)
    for steps in (5, 3):
        mark = probe.park()
        result = engine.pointer_scroll(target, delta=2, unit="detent", steps=steps)
        assert result.render() == (
            f"Scrolled vertical (discrete) by 2 at ({target.x}, {target.y}) in {steps} steps"
        )
        events = _scroll_events(probe, mark)
        # Two whole clicks (2 x 120 v120) in two increments; the empty steps send nothing.
        assert [
            e["direction"] for e in of_kind(events, "scroll") if e["direction"] != "smooth"
        ] == [
            "down",
            "down",
        ], events
        assert sum(_number(e["dy"]) for e in _smooth(events)) == pytest.approx(3.0), events
        assert of_kind(events, "scroll")[-1]["is_stop"], events


@pytest.mark.qa("QA-INP-018")
def test_axis_horizontal(engine: AutomationEngine, probe: Probe) -> None:
    golden = load_golden("scroll_horizontal_3")
    mark = probe.park()
    engine.pointer_scroll(probe.to_global(*golden["call"]["local"]), delta=3, axis="horizontal")
    assert_matches_golden(_scroll_events(probe, mark), golden)

    region = probe.center("Horizontal Scroll Region")
    engine.pointer_scroll(region, delta=3, axis="horizontal")
    scrolled = probe.wait_status("horizontal_scroll:", lambda s: int_field(s, "position") > 0)
    # Negative = left: -2 units = -30 px, which GTK reports as dx -3.0.
    mark = probe.park()
    engine.pointer_scroll(region, delta=-2, axis="horizontal")
    events = _scroll_events(probe, mark)
    assert [(e["dx"], e["dy"]) for e in _smooth(events)] == [(-3.0, 0.0)], events
    status = probe.wait_status(
        "horizontal_scroll:", lambda s: int_field(s, "position") < int_field(scrolled, "position")
    )
    assert float_field(status, "last") == pytest.approx(-3.0), status

    # Vertical negative: up 30 px.
    mark = probe.park()
    engine.pointer_scroll(probe.to_global(300, 200), delta=-2)
    events = _scroll_events(probe, mark)
    assert [(e["dx"], e["dy"]) for e in _smooth(events)] == [(0.0, -3.0)], events


@pytest.mark.qa("QA-INP-019")
def test_steps_zero_clamped_to_one(engine: AutomationEngine, probe: Probe) -> None:
    target = probe.to_global(300, 200)
    for steps in (0, -4):
        mark = probe.park()
        result = engine.pointer_scroll(target, delta=1, steps=steps)
        assert result.steps == 1
        assert result.render() == f"Scrolled vertical (smooth) by 1 at ({target.x}, {target.y})"
        events = _scroll_events(probe, mark)
        assert [(e["dy"], e["is_stop"]) for e in of_kind(events, "scroll")] == [
            (1.5, False),
            (0.0, True),
        ], events

    # delta=0 succeeds and sends no scroll event (SPEC-GAP-11).
    mark = probe.park()
    result = engine.pointer_scroll(target, delta=0)
    assert result.delta == 0 and result.warnings == []
    assert result.render() == f"Scrolled vertical (smooth) by 0 at ({target.x}, {target.y})"
    time.sleep(0.5)
    assert of_kind(probe.events_since(mark), "scroll") == []


@pytest.mark.qa("QA-INP-020")
def test_scroll_moves_to_target_first(engine: AutomationEngine, probe: Probe) -> None:
    target = probe.to_global(300, 200)
    mark = probe.park()
    result = engine.pointer_scroll(target, delta=1)
    assert result.at == target and result.element is None
    assert result.render() == f"Scrolled vertical (smooth) by 1 at ({target.x}, {target.y})"
    events = _scroll_events(probe, mark)
    assert kinds(events) == ["motion", "motion", "scroll", "scroll"], events
    assert (events[1]["x"], events[1]["y"]) == (300, 200)
    assert all((e["x"], e["y"]) == (300, 200) for e in of_kind(events, "scroll")), events
