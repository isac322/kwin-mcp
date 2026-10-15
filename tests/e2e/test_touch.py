"""End-to-end contracts for ``touch_tap``, ``touch_swipe`` and ``touch_pinch``.

Oracles: the interaction probe's raw event log (every touch begin/update/end/cancel
with its finger slot, probe-local coordinates and CLOCK_MONOTONIC stamp), its
``drag_status:``/``zoom_status:`` labels, KCalc's binary display, KWrite's scrollbar
value, and KWin's own ``workspace.cursorPos`` (touch never moves the pointer).

Timing follows amendment A15: exact event sequences, lower bound = stated value
minus 10 %, upper bound <= 3x.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from _asserts import element_count, has_kcalc_binary
from kwin_oracle import KWinOracle
from mcp_harness import ProgressLog, running_mcp_server
from probe_session import (
    EVENT_LOG_ENV,
    PROBE_APP,
    PROBE_COMMAND,
    PROBE_ENV,
    assert_interval,
    bounds_field,
    field,
    float_field,
    golden_core,
    int_field,
    launch_probe,
    load_golden,
    of_kind,
    start_probe_session,
)

from kwin_mcp.core import AutomationEngine
from kwin_mcp.models import (
    ElementList,
    Locator,
    Point,
    TouchPinchResult,
    TouchSwipeResult,
    TouchTapResult,
    Wait,
    WindowSelector,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mcp_harness import McpTestClient
    from probe_session import Event, Probe

    from kwin_mcp.models import UIElement

SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800
STATE_TIMEOUT_S = 5.0
POLL_S = 0.1
APP_WAIT_MS = 15000
# Longer than GTK's double-click time, so separate gestures never merge.
GESTURE_GAP_S = 1.2
# The vertical gap between parallel swipe contacts (S §2.24).
FINGER_SPACING = 20
TOUCH_KINDS = ("touch_begin", "touch_update", "touch_end", "touch_cancel")
DOCUMENT_LINES = "\n".join(f"line {i:03d} ---------------------------" for i in range(200))
KWRITE_APP_ID = "org.kde.kwrite"
PROBE_CAPTION = "Interaction Probe"
PARKED = Point(x=10, y=10)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── shared helpers ────────────────────────────────────────────────────────


def _exact(found: ElementList, name: str, role: str | None = None) -> UIElement:
    matches = [e for e in found.elements if e.name == name and (role is None or e.role == role)]
    assert len(matches) == 1, found.render()[:2000]
    return matches[0]


def _center(element: UIElement) -> Point:
    rect = element.rect
    assert rect is not None and rect.width > 0 and rect.height > 0, element
    return Point(x=rect.x + rect.width // 2, y=rect.y + rect.height // 2)


def _element_center(engine: AutomationEngine, app: str, role: str, name: str) -> Point:
    found = engine.element_query(Locator(app=app, query=name, role=role))
    element_count(found)
    return _center(_exact(found, name, role))


def _wait_until[T](read: Callable[[], T], done: Callable[[T], bool], what: str) -> T:
    deadline = time.monotonic() + STATE_TIMEOUT_S
    value = read()
    while not done(value):
        if time.monotonic() >= deadline:
            msg = f"{what}: {value!r}"[:2000]
            raise AssertionError(msg)
        time.sleep(POLL_S)
        value = read()
    return value


def _wait_for_binary(engine: AutomationEngine, expected: str) -> None:
    _wait_until(
        lambda: engine.element_query(Locator(app="kcalc")),
        lambda found: has_kcalc_binary(found, expected),
        f"KCalc never showed binary {expected}",
    )


def _oracle(engine: AutomationEngine) -> KWinOracle:
    info = engine.session_info()
    assert info.session is not None, info.render()
    return KWinOracle(info.session.dbus_address)


def _open_kwrite(
    engine: AutomationEngine, tmp_path: Path, wait_for_app: Callable[[str], ElementList]
) -> str:
    document = tmp_path / "kwin-mcp-touch-scroll.txt"
    document.write_text(DOCUMENT_LINES)
    engine.app_launch(f"kwrite {document}")
    wait_for_app("kwrite")
    found = engine.element_query(
        Locator(app="kwrite", role="text", query=document.name),
        wait=Wait(timeout_ms=APP_WAIT_MS),
    )
    assert found.wait is not None and found.wait.satisfied, found.render()[:2000]
    # Touch lands by position, so KWrite must be on top of anything else.
    engine.window_focus(WindowSelector(app_id=KWRITE_APP_ID))
    # Placement and the first paint settle shortly after the app reaches the bus.
    time.sleep(1.0)
    return document.name


def _swipe_span(engine: AutomationEngine, name: str) -> tuple[Point, Point]:
    """Bottom-to-top points kept inside the editor (a swipe that starts off it scrolls nothing)."""
    found = engine.element_query(Locator(app="kwrite", role="text", query=name))
    rect = _exact(found, name, "text").rect
    assert rect is not None, found.render()[:2000]
    x = rect.x + rect.width // 2
    return Point(x=x, y=rect.y + rect.height * 3 // 4), Point(x=x, y=rect.y + rect.height // 4)


def _vertical_scroll(elements: list[UIElement], context: str) -> float:
    """Vertical scrollbar position, which AT-SPI exposes through Value."""
    for element in elements:
        rect = element.rect
        if (
            element.role == "scroll bar"
            and element.value is not None
            and element.value_max
            and rect is not None
            and rect.height > rect.width
        ):
            return element.value
    msg = f"no vertical scrollbar reported a usable value:\n{context[:2000]}"
    raise AssertionError(msg)


def _scroll_position(engine: AutomationEngine) -> float:
    found = engine.element_query(Locator(app="kwrite", role="scroll bar"))
    return _vertical_scroll(found.elements, found.render())


def _touch(events: list[Event]) -> list[Event]:
    return of_kind(events, *TOUCH_KINDS)


def _gesture(probe: Probe, mark: int) -> list[Event]:
    """All events since ``mark`` once every contact that began has ended or been cancelled."""

    def finished(events: list[Event]) -> bool:
        begins = len(of_kind(events, "touch_begin"))
        return begins > 0 and begins == len(of_kind(events, "touch_end", "touch_cancel"))

    probe.wait_events(finished, since=mark)
    return probe.settle(since=mark)


def _quiet_mark(probe: Probe) -> int:
    """A log mark after the previous gesture fully settled and GTK's multi-tap window passed."""
    time.sleep(GESTURE_GAP_S)
    probe.settle()
    return probe.mark()


def _local(probe: Probe, point: Point) -> tuple[int, int]:
    return probe.to_local(point.x, point.y)


def _xy(event: Event) -> tuple[int, int]:
    return int(event["x"]), int(event["y"])


def _expected_offsets(fingers: int) -> list[int]:
    offsets = [(i - (fingers - 1) / 2) * FINGER_SPACING for i in range(fingers)]
    assert all(offset == int(offset) for offset in offsets), offsets
    return [int(offset) for offset in offsets]


def _assert_touch_only(events: list[Event]) -> None:
    others = [e for e in events if e["kind"] not in TOUCH_KINDS]
    assert others == [], others


def _assert_golden(events: list[Event], golden: dict[str, Any], *, stated_ms: int) -> None:
    """The 0.11 golden's exact event sequence, timed by the stated duration (A15).

    The golden's per-gap minimums are not used as lower bounds: they record 0.11
    capture overhead (e.g. a 13 ms step), while the stated step is 10 ms.
    """
    assert golden_core(events, golden) == golden["sequence"], (golden["scenario"], events)
    assert_interval(events[0], events[-1], at_least_ms=stated_ms)


def _per_slot(touches: list[Event]) -> dict[int, list[Event]]:
    slots: dict[int, list[Event]] = {}
    for event in touches:
        slots.setdefault(int(event["touch"]), []).append(event)
    return slots


def _assert_contact_starts(
    touches: list[Event], start: tuple[int, int], fingers: int, *, all_delivered: bool
) -> None:
    """The i-th contact begins at ``start`` + (0, (i - (n-1)/2) * 20)."""
    begins = of_kind(touches, "touch_begin")
    if all_delivered:
        assert len(begins) == fingers, touches
    else:
        assert 1 <= len(begins) <= fingers, touches
    offsets = _expected_offsets(fingers)
    for index, begin in enumerate(begins):
        assert _xy(begin) == (start[0], start[1] + offsets[index]), (index, offsets, touches)


def _drag_rect(probe: Probe) -> tuple[Point, Point]:
    """Lower-left to upper-right quarter points of the probe's Drag Target."""
    rect = probe.element("Drag Target").rect
    assert rect is not None, "Drag Target has no rect"
    start = Point(x=rect.x + rect.width // 4, y=rect.y + rect.height * 3 // 4)
    end = Point(x=rect.x + rect.width * 3 // 4, y=rect.y + rect.height // 4)
    return start, end


# ── fingers=0 (KWin-free) ─────────────────────────────────────────────────


def test_touch_swipe_rejects_zero_fingers_without_a_session() -> None:
    """The engine checks ``fingers`` before it needs an input backend (KWin-free)."""
    with pytest.raises(ValueError, match="touch_swipe fingers must be at least 1, got 0"):
        AutomationEngine().touch_swipe(Point(x=10, y=10), Point(x=100, y=100), fingers=0)


@pytest.mark.anyio
async def test_touch_swipe_fingers_below_one_is_a_schema_error() -> None:
    """MCP rejects ``fingers < 1`` with the schema's own error, before any session gate."""
    async with running_mcp_server() as client:
        for fingers in (0, -1):
            lines = await client.call_tool_error(
                "touch_swipe",
                {"from": {"x": 10, "y": 10}, "to": {"x": 100, "y": 10}, "fingers": fingers},
                lines=None,
            )
            text = "\n".join(lines)
            assert "fingers" in text, text
            assert "greater than or equal to 1" in text, text
            assert "No active session" not in text, text


# ── touch_tap ─────────────────────────────────────────────────────────────


@pytest.mark.qa("QA-INP-067")
def test_tap_hits_seven_without_moving_pointer(
    kcalc_session: AutomationEngine, tmp_path: Path
) -> None:
    engine = kcalc_session
    oracle = _oracle(engine)
    parked = engine.pointer_move(PARKED)
    assert parked.held.is_empty(), parked
    assert oracle.cursor_pos() == (10.0, 10.0)

    seven = _element_center(engine, "kcalc", "button", "Seven")
    # One tap per hold variant: 7, 77, 777 in KCalc's binary display.
    for hold_ms, binary in ((None, "111"), (300, "1001101"), (0, "1100001001")):
        result = (
            engine.touch_tap(seven) if hold_ms is None else engine.touch_tap(seven, hold_ms=hold_ms)
        )
        assert isinstance(result, TouchTapResult)
        assert result.at == seven, result
        assert result.hold_ms == (hold_ms or 0), result
        assert result.warnings == [], result
        held = f" held {hold_ms}ms" if hold_ms else ""
        assert result.render() == f"Touch tap at ({seven.x}, {seven.y}){held}", result.render()
        _wait_for_binary(engine, binary)
        assert oracle.cursor_pos() == (10.0, 10.0), "touch_tap moved the pointer"

    # The probe's raw log times the contact against the 0.11 goldens (A15) and
    # proves no pointer event was sent.
    probe = launch_probe(engine, tmp_path)
    engine.pointer_move(PARKED)
    # hold_ms omitted and 0 both keep the contact down 10 ms (S §8 row 72, G8).
    for hold_ms, golden_name in (
        (None, "touch_tap"),
        (300, "touch_tap_hold_300"),
        (0, "touch_tap"),
    ):
        golden = load_golden(golden_name)
        target = probe.to_global(*golden["call"]["local"])
        mark = _quiet_mark(probe)
        if hold_ms is None:
            engine.touch_tap(target)
        else:
            engine.touch_tap(target, hold_ms=hold_ms)
        events = _gesture(probe, mark)
        _assert_touch_only(events)
        _assert_golden(events, golden, stated_ms=hold_ms or 10)
        assert oracle.cursor_pos() == (10.0, 10.0), "touch_tap moved the pointer"


# ── touch_swipe ───────────────────────────────────────────────────────────


@pytest.mark.qa("QA-INP-068")
def test_swipe_scrolls_kwrite(
    engine: AutomationEngine,
    start_session: Callable[..., Any],
    wait_for_app: Callable[[str], ElementList],
    tmp_path: Path,
) -> None:
    start_session()
    name = _open_kwrite(engine, tmp_path, wait_for_app)
    start, end = _swipe_span(engine, name)
    assert _scroll_position(engine) == 0.0
    result = engine.touch_swipe(start, end, duration_ms=300)
    assert isinstance(result, TouchSwipeResult)
    assert (result.start, result.at, result.fingers) == (start, end, 1), result
    assert result.render() == (
        f"Touch swipe from ({start.x}, {start.y}) to ({end.x}, {end.y}) in 300ms"
    ), result.render()
    _wait_until(
        lambda: _scroll_position(engine), lambda value: value > 0.0, "touch swipe did not scroll"
    )

    # One contact, 30 steps over the default 300 ms (golden ``touch_swipe``); 0 and
    # negative durations run the 10-step minimum about 1 ms apart and are accepted
    # (no schema bound, S §8 row 74).
    probe = launch_probe(engine, tmp_path)
    golden = load_golden("touch_swipe")
    begin = probe.to_global(*golden["call"]["from_local"])
    finish = probe.to_global(*golden["call"]["to_local"])
    for duration_ms in (None, 0, -50):
        mark = _quiet_mark(probe)
        if duration_ms is None:
            swiped = engine.touch_swipe(begin, finish)
        else:
            swiped = engine.touch_swipe(begin, finish, duration_ms=duration_ms)
        assert swiped.fingers == 1, swiped
        assert swiped.duration_ms == (300 if duration_ms is None else duration_ms), swiped
        assert swiped.warnings == [], swiped
        events = _gesture(probe, mark)
        _assert_touch_only(events)
        if duration_ms is None:
            _assert_golden(events, golden, stated_ms=300)
            continue
        assert [e["kind"] for e in events] == [
            "touch_begin",
            *["touch_update"] * 10,
            "touch_end",
        ], events
        assert {e["touch"] for e in events} == {0}, events
        assert _xy(events[0]) == tuple(golden["call"]["from_local"]), events[0]
        assert _xy(events[-2]) == tuple(golden["call"]["to_local"]), events[-2]
        ys = [int(e["y"]) for e in events[1:-1]]
        assert ys == sorted(ys) and len(set(ys)) == 10, ys
        # Ten steps of max(1 ms, duration/steps) = 1 ms each.
        assert_interval(events[1], events[-2], at_least_ms=9)


def test_touch_swipe_multi_finger_delivers_both_fingers_to_the_app(
    engine: AutomationEngine, tmp_path: Path
) -> None:
    """Both touch sequences of a two-finger swipe reach the application.

    KWrite cannot show this: its QScroller only flicks on exactly one touchscreen
    point, and once Qt groups the second finger with the first (QTBUG-125197) a
    two-finger swipe no longer scrolls it. The GTK probe records every sequence.
    """
    probe = start_probe_session(engine, tmp_path)
    start, end = _drag_rect(probe)
    mark = _quiet_mark(probe)
    result = engine.touch_swipe(start, end, fingers=2, duration_ms=400)
    assert (result.start, result.at, result.fingers) == (start, end, 2), result
    assert result.render() == (
        f"2-finger swipe from ({start.x}, {start.y}) to ({end.x}, {end.y}) in 400ms"
    ), result.render()
    touches = _touch(_gesture(probe, mark))
    _assert_contact_starts(touches, _local(probe, start), 2, all_delivered=True)
    assert set(_per_slot(touches)) == {0, 1}, touches
    assert of_kind(touches, "touch_cancel") == [], touches

    status = probe.wait_status("drag_status:", lambda s: field(s, "release") != "none")
    assert field(status, "source") == "touch", status
    assert field(status, "release") == "touch", status
    assert int_field(status, "fingers") == 2, status
    assert int_field(status, "motions") >= 20, status
    # bounds are (min_x, min_y, max_x, max_y) over both fingers in widget pixels.
    # The spans prove the requested displacement arrived in both axes; the
    # vertical span is the swipe's dy plus the fingers' 20 px spacing.
    x0, y0, x1, y1 = bounds_field(status)
    assert x1 - x0 >= (end.x - start.x) * 3 // 4, status
    assert y1 - y0 >= (start.y - end.y + FINGER_SPACING) * 3 // 4, status

    # The 0.11 golden: two contacts 20 px apart, 30 update pairs over 300 ms.
    golden = load_golden("touch_multi_swipe_2")
    mark = _quiet_mark(probe)
    engine.touch_swipe(
        probe.to_global(*golden["call"]["from_local"]),
        probe.to_global(*golden["call"]["to_local"]),
        fingers=2,
        duration_ms=golden["call"]["duration_ms"],
    )
    events = _gesture(probe, mark)
    _assert_touch_only(events)
    _assert_golden(events, golden, stated_ms=300)


@pytest.mark.qa("QA-INP-069")
def test_fingers_unbounded_above(engine: AutomationEngine, tmp_path: Path) -> None:
    """``fingers`` has no upper bound; contacts sit at y offsets (i - (n-1)/2) * 20.

    KWin may claim >= 3 fingers as a compositor gesture (see
    ``test_three_to_five_finger_swipe_consumed_by_kwin``), so the assertion is on
    the kwin-mcp result plus the contacts the probe did receive, never on delivery.
    """
    probe = start_probe_session(engine, tmp_path)
    start = probe.to_global(460, 350)
    end = probe.to_global(660, 350)
    for fingers in (3, 12):
        mark = _quiet_mark(probe)
        result = engine.touch_swipe(start, end, fingers=fingers)
        assert isinstance(result, TouchSwipeResult)
        assert (result.fingers, result.duration_ms, result.warnings) == (fingers, 300, []), result
        assert result.render() == (
            f"{fingers}-finger swipe from ({start.x}, {start.y}) to ({end.x}, {end.y}) in 300ms"
        ), result.render()
        touches = _touch(_gesture(probe, mark))
        _assert_contact_starts(touches, _local(probe, start), fingers, all_delivered=False)
        # Every delivered contact ended or was cancelled: nothing stays down.
        for slot, sequence in _per_slot(touches).items():
            assert sequence[-1]["kind"] in {"touch_end", "touch_cancel"}, (slot, sequence)

    with pytest.raises(ValueError, match="touch_swipe fingers must be at least 1, got 0"):
        engine.touch_swipe(start, end, fingers=0)


@pytest.mark.qa("QA-INP-069")
def test_three_to_five_finger_swipe_consumed_by_kwin(
    engine: AutomationEngine, tmp_path: Path
) -> None:
    """KWin claims >= 3 fingers as a compositor gesture and cancels the app's contacts.

    This is the documented KWin limit (S §2.24: "KWin consumes >=3-finger swipes as
    global gestures"); kwin-mcp still reports the swipe as sent.
    """
    probe = start_probe_session(engine, tmp_path)
    start, end = _drag_rect(probe)
    for fingers in (3, 4, 5):
        mark = _quiet_mark(probe)
        result = engine.touch_swipe(start, end, fingers=fingers, duration_ms=400)
        assert (result.fingers, result.warnings) == (fingers, []), result
        touches = _touch(_gesture(probe, mark))
        assert len(of_kind(touches, "touch_begin")) < fingers, touches
        assert of_kind(touches, "touch_cancel"), touches
        status = probe.wait_status("drag_status:", lambda s: field(s, "release") == "cancel")
        assert int_field(status, "fingers") < fingers, status


# ── touch_pinch ───────────────────────────────────────────────────────────


@pytest.mark.qa("QA-INP-071")
def test_pinch_zoom_scale_direction(engine: AutomationEngine, tmp_path: Path) -> None:
    probe = start_probe_session(engine, tmp_path)
    center = probe.center("Zoom Target")
    cx, cy = _local(probe, center)
    for start_distance, end_distance, direction, reached in (
        (200, 50, "in", lambda scale: scale < 0.8),
        (50, 200, "out", lambda scale: scale > 1.2),
    ):
        mark = _quiet_mark(probe)
        result = engine.touch_pinch(center, start_distance, end_distance)
        assert isinstance(result, TouchPinchResult)
        assert (result.at, result.duration_ms, result.warnings) == (center, 500, []), result
        assert result.render() == (
            f"Pinch {direction} at ({center.x}, {center.y}): "
            f"{start_distance}→{end_distance}px in 500ms"
        ), result.render()
        events = _gesture(probe, mark)
        _assert_touch_only(events)
        slots = _per_slot(events)
        assert set(slots) == {0, 1}, events
        half_start, half_end = start_distance // 2, end_distance // 2
        for slot, sign in ((0, -1), (1, 1)):
            sequence = slots[slot]
            assert sequence[0]["kind"] == "touch_begin", sequence
            assert sequence[-1]["kind"] == "touch_end", sequence
            assert _xy(sequence[0]) == (cx + sign * half_start, cy), sequence[0]
            assert _xy(sequence[-2]) == (cx + sign * half_end, cy), sequence[-2]
            assert {int(e["y"]) for e in sequence} == {cy}, sequence
            assert len(of_kind(sequence, "touch_update")) == 50, sequence
        assert_interval(events[0], events[-1], at_least_ms=500)
        probe.wait_status(
            "zoom_status:",
            lambda s, reached=reached: (
                field(s, "phase") == "completed" and reached(float_field(s, "scale"))
            ),
        )

    # The 0.11 golden: pinch out 100 -> 300 px, two contacts, 50 update pairs.
    golden = load_golden("touch_pinch")
    mark = _quiet_mark(probe)
    engine.touch_pinch(
        probe.to_global(*golden["call"]["center_local"]),
        golden["call"]["start_distance"],
        golden["call"]["end_distance"],
        duration_ms=golden["call"]["duration_ms"],
    )
    events = _gesture(probe, mark)
    _assert_touch_only(events)
    _assert_golden(events, golden, stated_ms=500)


# ── held buttons (Q10 (a)) ────────────────────────────────────────────────


def test_touch_gestures_unaffected_by_held_buttons(
    engine: AutomationEngine, tmp_path: Path
) -> None:
    """Touch uses a separate EIS device: a held mouse button neither blocks nor moves it.

    S §3.4 rule 4 (Q10 (a)): only a gesture that would itself press or release a
    held control is refused; touch gestures are unaffected by held buttons. Every
    ActionResult still reports the held set.
    """
    probe = start_probe_session(engine, tmp_path)
    oracle = probe.oracle()
    click = probe.center("Click Target")
    pressed = engine.pointer_hold("press", click)
    assert pressed.held.buttons == ["left"], pressed
    probe.wait_events(lambda events: bool(of_kind(events, "button_press")))
    mark = _quiet_mark(probe)
    cursor = oracle.cursor_pos()
    assert cursor == (float(click.x), float(click.y)), cursor

    start, end = _drag_rect(probe)
    zoom = probe.center("Zoom Target")
    results = [
        engine.touch_tap(start),
        engine.touch_swipe(start, end),
        engine.touch_swipe(start, end, fingers=2),
        engine.touch_pinch(zoom, 120, 60),
    ]
    for result in results:
        assert result.held.buttons == ["left"], result
        assert result.warnings == [], result
        assert result.render().splitlines()[-1] == "Still held: left", result.render()

    events = _gesture(probe, mark)
    _assert_touch_only(events)
    # tap 1 + swipe 1 + 2-finger swipe 2 + pinch 2 contacts, each ended normally.
    assert len(of_kind(events, "touch_begin")) == 6, events
    assert len(of_kind(events, "touch_end")) == 6, events
    assert of_kind(events, "touch_cancel") == [], events
    assert oracle.cursor_pos() == cursor, "a touch gesture moved the held pointer"

    released = engine.pointer_hold("release", click)
    assert released.held.is_empty(), released
    probe.wait_events(lambda events: bool(of_kind(events, "button_release")), since=mark)


# ── MCP stdio ─────────────────────────────────────────────────────────────


async def _mcp_wait(client: McpTestClient, locator: dict[str, Any]) -> ElementList:
    found = await client.call_model(
        "element_query", {"locator": locator, "wait": {"timeout_ms": APP_WAIT_MS}}
    )
    assert isinstance(found, ElementList)
    assert found.wait is not None and found.wait.satisfied, found.render()[:2000]
    return found


async def _mcp_query(client: McpTestClient, locator: dict[str, Any]) -> ElementList:
    found = await client.call_model("element_query", {"locator": locator})
    assert isinstance(found, ElementList)
    return found


async def _mcp_poll[T](read: Callable[[], Awaitable[T]], done: Callable[[T], bool], what: str) -> T:
    deadline = time.monotonic() + STATE_TIMEOUT_S
    value = await read()
    while not done(value):
        if time.monotonic() >= deadline:
            msg = f"{what}: {value!r}"[:2000]
            raise AssertionError(msg)
        await anyio.sleep(POLL_S)
        value = await read()
    return value


async def _mcp_status(client: McpTestClient, prefix: str) -> str:
    found = await _mcp_query(client, {"app": PROBE_APP, "query": prefix})
    names = [e.name for e in found.elements if e.name.startswith(prefix)]
    assert len(names) == 1, found.render()[:2000]
    return names[0]


def _wire(point: Point) -> dict[str, int]:
    return {"x": point.x, "y": point.y}


@pytest.mark.anyio
@pytest.mark.timeout(300)
async def test_mcp_touch_tools_change_gui_state_and_report_kwin_limits(tmp_path: Path) -> None:
    document = tmp_path / "kwin-mcp-mcp-touch.txt"
    document.write_text(DOCUMENT_LINES)

    async with running_mcp_server() as client:
        try:
            info, _ = await client.call_structured(
                "session_start", {"screen_width": SCREEN_WIDTH, "screen_height": SCREEN_HEIGHT}
            )
            assert info["input_backend"] == "eis", info
            await client.call_structured("app_launch", {"command": "kcalc"})
            found = await _mcp_wait(client, {"app": "kcalc", "query": "Seven", "role": "button"})
            seven = _center(_exact(found, "Seven", "button"))

            # A held tap reports "Holding touch" progress in <= 0.5 s slices.
            progress = ProgressLog()
            tapped, text = await client.call_structured(
                "touch_tap",
                {"target": _wire(seven), "hold_ms": 650},
                progress_callback=progress,
            )
            await progress.settle()
            assert text == f"Touch tap at ({seven.x}, {seven.y}) held 650ms", text
            assert tapped["hold_ms"] == 650, tapped
            messages = [message for _, _, message in progress.events]
            assert messages == ["Holding touch"] * 2, progress.events
            assert progress.values == sorted(progress.values), progress.events
            assert progress.events[-1][:2] == (0.65, 0.65), progress.events
            await _mcp_poll(
                lambda: _mcp_query(client, {"app": "kcalc"}),
                lambda found: has_kcalc_binary(found, "111"),
                "KCalc never showed binary 111",
            )

            await client.call_structured("app_launch", {"command": f"kwrite {document}"})
            text_locator = {"app": "kwrite", "role": "text", "query": document.name}
            await _mcp_wait(client, text_locator)
            await client.call_structured("window_focus", {"window": {"app_id": KWRITE_APP_ID}})
            await anyio.sleep(1.0)
            rect = _exact(await _mcp_query(client, text_locator), document.name, "text").rect
            assert rect is not None
            column = rect.x + rect.width // 2
            start = Point(x=column, y=rect.y + rect.height * 3 // 4)
            end = Point(x=column, y=rect.y + rect.height // 4)

            async def scroll() -> float:
                found = await _mcp_query(client, {"app": "kwrite", "role": "scroll bar"})
                return _vertical_scroll(found.elements, found.render())

            assert await scroll() == 0.0
            _, text = await client.call_structured(
                "touch_swipe", {"from": _wire(start), "to": _wire(end), "duration_ms": 350}
            )
            assert text == (
                f"Touch swipe from ({start.x}, {start.y}) to ({end.x}, {end.y}) in 350ms"
            ), text
            await _mcp_poll(scroll, lambda value: value > 0.0, "single-finger swipe did not scroll")

            # KWrite cannot report multi-touch (no pinch; its QScroller only flicks on
            # exactly one point, QTBUG-125197). The GTK probe records every sequence
            # and runs a GestureZoom, so it observes both.
            await client.call_structured(
                "app_launch",
                {
                    "command": PROBE_COMMAND,
                    "env": {**PROBE_ENV, EVENT_LOG_ENV: str(tmp_path / "probe-events.jsonl")},
                },
            )
            await _mcp_wait(client, {"app": PROBE_APP, "query": "Zoom Target"})
            await client.call_structured("window_focus", {"window": {"caption": PROBE_CAPTION}})
            await anyio.sleep(0.5)

            zoom = _exact(
                await _mcp_query(client, {"app": PROBE_APP, "query": "Zoom Target"}),
                "Zoom Target",
            )
            assert zoom.rect is not None
            zoom_center = _center(zoom)
            distance = min(160, zoom.rect.width - 40)
            for start_distance, end_distance, direction, reached in (
                (distance, 60, "in", lambda scale: scale < 0.8),
                (60, distance, "out", lambda scale: scale > 1.2),
            ):
                _, text = await client.call_structured(
                    "touch_pinch",
                    {
                        "center": _wire(zoom_center),
                        "start_distance": start_distance,
                        "end_distance": end_distance,
                        "duration_ms": 500,
                    },
                )
                assert text == (
                    f"Pinch {direction} at ({zoom_center.x}, {zoom_center.y}): "
                    f"{start_distance}→{end_distance}px in 500ms"
                ), text
                await _mcp_poll(
                    lambda: _mcp_status(client, "zoom_status:"),
                    lambda status, reached=reached: (
                        field(status, "phase") == "completed"
                        and reached(float_field(status, "scale"))
                    ),
                    f"pinch {direction} did not complete a zoom on the probe",
                )

            drag = _exact(
                await _mcp_query(client, {"app": PROBE_APP, "query": "Drag Target"}),
                "Drag Target",
            ).rect
            assert drag is not None
            swipe_from = Point(x=drag.x + drag.width // 4, y=drag.y + drag.height * 3 // 4)
            swipe_to = Point(x=drag.x + drag.width * 3 // 4, y=drag.y + drag.height // 4)
            for fingers in (2, 3, 4, 5):
                await anyio.sleep(GESTURE_GAP_S)
                marker = await _mcp_status(client, "drag_status:")
                _, text = await client.call_structured(
                    "touch_swipe",
                    {
                        "from": _wire(swipe_from),
                        "to": _wire(swipe_to),
                        "fingers": fingers,
                        "duration_ms": 400,
                    },
                )
                assert text == (
                    f"{fingers}-finger swipe from ({swipe_from.x}, {swipe_from.y}) "
                    f"to ({swipe_to.x}, {swipe_to.y}) in 400ms"
                ), text
                status = await _mcp_poll(
                    lambda: _mcp_status(client, "drag_status:"),
                    lambda status, marker=marker: (
                        status != marker and field(status, "release") != "none"
                    ),
                    f"{fingers}-finger swipe never reached the probe",
                )
                observed = int_field(status, "fingers")
                if fingers == 2:
                    # KWin passes two-finger streams to the application untouched.
                    assert field(status, "release") == "touch", status
                    assert observed == 2, status
                    assert int_field(status, "motions") >= 20, status
                    x0, y0, x1, y1 = bounds_field(status)
                    assert x1 - x0 >= (swipe_to.x - swipe_from.x) * 3 // 4, status
                    assert y1 - y0 >= (swipe_from.y - swipe_to.y + FINGER_SPACING) * 3 // 4, status
                else:
                    # KWin claims three or more fingers as a compositor gesture and
                    # cancels the fingers the application had already received.
                    assert field(status, "release") == "cancel", status
                    assert observed < fingers, status
        finally:
            await client.call_result("session_stop")
