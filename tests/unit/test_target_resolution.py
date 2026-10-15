"""Target resolution: ``Point`` passes through, ``ElementPoint`` re-reads its live rect.

The engine resolves every ``ElementPoint`` of one call in a single worker
``resolve`` request, aims at the rect centre plus ``(dx, dy)``, rejects
malformed and other-generation refs itself, and refuses an element without a
screen rect before any input event is sent.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from kwin_mcp.models import ElementPoint, Point

if TYPE_CHECKING:
    from ._fakes import FakeEngine

GENERATION = 3


def _element(
    ref: str, *, x: int = 100, y: int = 200, width: int = 40, height: int = 20
) -> dict[str, object]:
    """One element as the AT-SPI worker reports it, mapped to a screen rect."""
    return {
        "ref": ref,
        "role": "push button",
        "name": "OK",
        "description": "",
        "states": ["enabled", "showing", "visible"],
        "depth": 3,
        "app": "kcalc",
        "window_id": "{w1}",
        "children_count": 0,
        "mapped": True,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "actions": ["Press"],
        "text": "",
        "value": None,
        "value_max": None,
    }


def _unmapped(ref: str, reason: str) -> dict[str, object]:
    element = _element(ref)
    element.update(mapped=False, unavailable=reason)
    return element


@pytest.fixture
def engine(fake_engine: FakeEngine) -> FakeEngine:
    # Refs carry the worker generation; pretend the current worker is GENERATION.
    fake_engine.engine._atspi_generation = GENERATION
    return fake_engine


def _resolve_reply(*elements: dict[str, object]) -> dict[str, object]:
    return {
        "ok": True,
        "result": [{"ref": el["ref"], "ok": True, "element": el} for el in elements],
    }


@pytest.mark.qa("QA-INP-007")
def test_point_passes_through_without_a_worker_request(engine: FakeEngine) -> None:
    resolved = engine.engine._resolve_targets(Point(x=7, y=9))

    assert resolved == [(Point(x=7, y=9), None)]
    assert engine.atspi.calls == []


@pytest.mark.qa("QA-INP-007")
def test_element_point_aims_at_live_rect_centre_plus_offset(engine: FakeEngine) -> None:
    ref = f"e{GENERATION}.17"
    engine.atspi.respond(
        "resolve", _resolve_reply(_element(ref, x=100, y=200, width=41, height=21))
    )

    ((point, element),) = engine.engine._resolve_targets(ElementPoint(ref=ref, dx=5, dy=-3))

    # Centre is x + width // 2, y + height // 2, then the offset.
    assert point == Point(x=100 + 20 + 5, y=200 + 10 - 3)
    assert element is not None
    assert element.ref == ref
    assert [call.op for call in engine.atspi.calls] == ["resolve"]
    assert engine.atspi.calls[0].kwargs == {"refs": [ref]}


@pytest.mark.qa("QA-INP-007")
def test_all_element_points_of_one_call_resolve_in_one_request(engine: FakeEngine) -> None:
    start, end = f"e{GENERATION}.1", f"e{GENERATION}.2"
    engine.atspi.respond(
        "resolve",
        _resolve_reply(_element(start, x=0, y=0), _element(end, x=500, y=300)),
    )

    resolved = engine.engine._resolve_targets(
        ElementPoint(ref=start), Point(x=1, y=1), ElementPoint(ref=end), ElementPoint(ref=start)
    )

    assert [point for point, _ in resolved] == [
        Point(x=20, y=10),
        Point(x=1, y=1),
        Point(x=520, y=310),
        Point(x=20, y=10),
    ]
    assert [element.ref if element else None for _, element in resolved] == [
        start,
        None,
        end,
        start,
    ]
    # One request, each ref once.
    assert len(engine.atspi.calls) == 1
    assert engine.atspi.calls[0].kwargs == {"refs": [start, end]}


@pytest.mark.qa("QA-INP-008")
def test_element_without_rect_is_an_error_quoting_unavailable(engine: FakeEngine) -> None:
    ref = f"e{GENERATION}.4"
    engine.atspi.respond("resolve", _resolve_reply(_unmapped(ref, "offscreen")))

    with pytest.raises(RuntimeError) as excinfo:
        engine.engine._resolve_targets(ElementPoint(ref=ref))

    assert str(excinfo.value) == (
        f"Element {ref} has no screen position (unavailable: offscreen).\n"
        "Use element_act(element={ref}, action) or pick another element."
    )


@pytest.mark.qa("QA-INP-008")
def test_element_without_rect_sends_no_input(engine: FakeEngine) -> None:
    ref = f"e{GENERATION}.4"
    engine.atspi.respond("resolve", _resolve_reply(_unmapped(ref, "no KWin window")))

    with pytest.raises(RuntimeError, match=re.escape("(unavailable: no KWin window)")):
        engine.engine.pointer_move(target=ElementPoint(ref=ref))

    assert engine.eis.events == []


@pytest.mark.qa("QA-INP-008")
def test_worker_ref_error_is_raised_verbatim(engine: FakeEngine) -> None:
    ref = f"e{GENERATION}.9"
    error = f"Stale ref {ref} (element no longer exists); re-run element_query."
    engine.atspi.respond(
        "resolve", {"ok": True, "result": [{"ref": ref, "ok": False, "error": error}]}
    )

    with pytest.raises(RuntimeError) as excinfo:
        engine.engine._resolve_targets(ElementPoint(ref=ref))

    assert str(excinfo.value) == error


@pytest.mark.qa("QA-INP-008")
@pytest.mark.parametrize("ref", ["", "e3", "3.1", "e3.x", "E3.1", "e3.1.2", "x e3.1"])
def test_malformed_ref_is_rejected_before_the_worker(engine: FakeEngine, ref: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        engine.engine._resolve_targets(ElementPoint(ref=ref))

    assert str(excinfo.value) == (
        f"Malformed ref {ref!r}: refs look like e<generation>.<n>, as element_query "
        "returns them.\nRe-run element_query and copy a ref."
    )
    assert engine.atspi.calls == []


@pytest.mark.qa("QA-INP-008")
def test_ref_of_another_generation_is_stale_before_the_worker(engine: FakeEngine) -> None:
    ref = f"e{GENERATION - 1}.5"

    with pytest.raises(RuntimeError) as excinfo:
        engine.engine._resolve_targets(Point(x=0, y=0), ElementPoint(ref=ref))

    assert str(excinfo.value) == (
        f"Stale ref {ref} (AT-SPI worker restarted); re-run element_query."
    )
    assert engine.atspi.calls == []
