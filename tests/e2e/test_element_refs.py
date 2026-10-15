"""End-to-end contracts for element refs (``e<generation>.<n>``) and ElementPoint targets.

A ref names one live AT-SPI object for the life of one AT-SPI worker. It is
refused, never re-bound to another object, when its worker was replaced (a
killed or hung worker, a new session), when its element was destroyed, when
the object's role changed, or when the LRU table evicted it. ``{ref, dx, dy}``
targets re-read the element's live rect at action time.

Oracles: the window_zoo press log (stdout, read by ``ZooProcess``), the
interaction probe's raw event log, KCalc's display read over AT-SPI by
``atspi_oracle`` and the cursor position from ``kwin_oracle``.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import threading
import time
from typing import TYPE_CHECKING

import pytest
from atspi_oracle import AtspiOracle
from kwin_oracle import KWinOracle, Rect
from probe_session import PROBE_APP, Probe, of_kind, start_probe_session
from window_zoo import APP_ID, LABEL_COUNT, MAIN_CAPTION, ZooProcess

from kwin_mcp import core
from kwin_mcp.models import ElementPoint, Locator, Point, RefAddr, Wait, WindowSelector

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import AppLaunchResult, ElementList, SessionInfo, UIElement

_REF = re.compile(r"e(\d+)\.(\d+)")
# GTK3 buttons expose a single AT-SPI action named "click"; AT-SPI names their role "button".
GTK_CLICK = "click"
BUTTON = "button"
# How long a refused action is given to show up in the press log anyway.
NO_PRESS_GRACE_S = 1.0
WINDOW_SHIFT_PX = 200


def _parts(ref: str) -> tuple[int, int]:
    match = _REF.fullmatch(ref)
    assert match is not None, ref
    return int(match.group(1)), int(match.group(2))


def _dbus(engine: AutomationEngine) -> str:
    info = engine.session_info().session
    assert info is not None
    return info.dbus_address


def _stale_restarted(ref: str) -> str:
    return f"Stale ref {ref} (AT-SPI worker restarted); re-run element_query."


def _stale_gone(ref: str) -> str:
    return f"Stale ref {ref} (element no longer exists); re-run element_query."


# ── KCalc ─────────────────────────────────────────────────────────────────


def _kcalc_button(engine: AutomationEngine, name: str) -> UIElement:
    found = engine.element_query(Locator(app="kcalc", query=name, role="button"))
    exact = [element for element in found.elements if element.name == name]
    assert len(exact) == 1, found.render()[:2000]
    return exact[0]


def _kcalc_display(oracle: AtspiOracle) -> list[str | None]:
    """KCalc's labels (its displays) as the AT-SPI oracle reads them."""
    return [node.name for node in oracle.find(app="kcalc", role="label")]


def _wait_display_changed(oracle: AtspiOracle, before: list[str | None]) -> list[str | None]:
    deadline = time.monotonic() + 5.0
    current = _kcalc_display(oracle)
    while current == before:
        assert time.monotonic() < deadline, current
        time.sleep(0.2)
        current = _kcalc_display(oracle)
    return current


def _kill_worker(engine: AutomationEngine) -> int:
    proc = engine._atspi_proc
    assert proc is not None and proc.poll() is None, "no running AT-SPI worker"
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=5)
    return proc.pid


@pytest.mark.qa("QA-ELEM-033")
def test_stale_ref_after_worker_restart_is_tool_error_with_rerun_remedy(
    kcalc_session: AutomationEngine,
) -> None:
    engine = kcalc_session
    oracle = AtspiOracle(_dbus(engine))
    # Start each worker with an empty ref table, so both refs get n == 1: the
    # second ref collides with the first in everything but the generation.
    _kill_worker(engine)
    r1 = _kcalc_button(engine, "Seven").ref
    _kill_worker(engine)
    r2 = _kcalc_button(engine, "Eight").ref
    (gen1, n1), (gen2, n2) = _parts(r1), _parts(r2)
    assert n1 == n2 == 1, (r1, r2)
    assert gen2 > gen1, (r1, r2)

    before = _kcalc_display(oracle)
    assert before, "KCalc display not readable over AT-SPI"
    with pytest.raises(RuntimeError) as acted:
        engine.element_act(RefAddr(ref=r1), "Press")
    assert str(acted.value) == _stale_restarted(r1)
    with pytest.raises(RuntimeError) as clicked:
        engine.pointer_click(ElementPoint(ref=r1))
    assert str(clicked.value) == _stale_restarted(r1)
    time.sleep(NO_PRESS_GRACE_S)
    assert _kcalc_display(oracle) == before


@pytest.mark.qa("QA-ELEM-034")
def test_refs_have_generation_prefix_and_resolve(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
) -> None:
    start_session()
    launch("kcalc")
    listed = wait_for_app("kcalc")
    generations = {_parts(element.ref)[0] for element in listed.elements}
    assert generations == {engine._atspi_generation}, generations
    numbers = [_parts(element.ref)[1] for element in listed.elements]
    assert len(set(numbers)) == len(numbers), numbers

    seven = _kcalc_button(engine, "Seven")
    r1 = seven.ref
    moved = engine.pointer_move(ElementPoint(ref=r1))
    assert moved.element is not None and moved.element.ref == r1, moved.render()
    assert seven.rect is not None
    assert moved.at == Point(
        x=seven.rect.x + seven.rect.width // 2, y=seven.rect.y + seven.rect.height // 2
    )
    worker = engine._atspi_proc
    assert worker is not None

    assert engine.session_stop().stopped
    assert engine._atspi_proc is None
    assert worker.poll() is not None, "AT-SPI worker survived session_stop"

    start_session()
    launch("kcalc")
    wait_for_app("kcalc")
    oracle = AtspiOracle(_dbus(engine))
    r2 = _kcalc_button(engine, "Seven").ref
    assert _parts(r2)[0] > _parts(r1)[0], (r1, r2)

    before = _kcalc_display(oracle)
    with pytest.raises(RuntimeError) as stale:
        engine.element_act(RefAddr(ref=r1), "Press")
    assert str(stale.value) == _stale_restarted(r1)
    time.sleep(NO_PRESS_GRACE_S)
    assert _kcalc_display(oracle) == before

    pressed = engine.element_act(RefAddr(ref=r2), "Press")
    assert pressed.dispatch == "confirmed", pressed.render()
    assert pressed.element is not None and pressed.element.ref == r2
    _wait_display_changed(oracle, before)


@pytest.mark.qa("QA-ELEM-035")
@pytest.mark.timeout(300)
def test_retry_replacement_bumps_generation(kcalc_session: AutomationEngine) -> None:
    engine = kcalc_session
    pre_hang = _kcalc_button(engine, "Seven").ref
    proc = engine._atspi_proc
    assert proc is not None
    hung = proc.pid
    resumed: list[float] = []
    done = threading.Event()

    def resume_after_retry() -> None:
        # SIGCONT the hung worker once the retry has replaced it.
        while not done.wait(0.05):
            current = engine._atspi_proc
            if current is not None and current.pid != hung:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(hung, signal.SIGCONT)
                resumed.append(time.monotonic())
                return

    os.kill(hung, signal.SIGSTOP)
    watcher = threading.Thread(target=resume_after_retry, daemon=True)
    started = time.monotonic()
    watcher.start()
    try:
        found = engine.element_query(Locator(app="kcalc"), max_depth=13)
    finally:
        done.set()
        watcher.join(timeout=5)
        with contextlib.suppress(ProcessLookupError):
            os.kill(hung, signal.SIGCONT)
    elapsed = time.monotonic() - started

    assert resumed, "the retry never replaced the hung worker"
    assert elapsed >= core._ATSPI_TIMEOUT_S + 0.5, elapsed
    assert found.elements, found.render()[:500]
    generations = {_parts(element.ref)[0] for element in found.elements}
    assert generations == {_parts(pre_hang)[0] + 1}, generations
    assert engine._atspi_generation == _parts(pre_hang)[0] + 1
    with pytest.raises(RuntimeError) as stale:
        engine.element_act(RefAddr(ref=pre_hang), "Press")
    assert str(stale.value) == _stale_restarted(pre_hang)


# ── window_zoo ────────────────────────────────────────────────────────────


@pytest.fixture
def zoo(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> Iterator[ZooProcess]:
    """window_zoo in a virtual session, its main window queryable over AT-SPI."""
    info = start_session()
    with ZooProcess(wayland_display=info.wayland_socket, dbus_address=info.dbus_address) as app:
        app.expect(f"window: {MAIN_CAPTION}")
        found = engine.element_query(
            Locator(app=APP_ID, query="Play", role=BUTTON), wait=Wait(timeout_ms=15000)
        )
        assert found.wait is not None and found.wait.satisfied, found.render()[:2000]
        yield app


def _presses(zoo: ZooProcess) -> list[str]:
    return [line for line in zoo.lines if line.startswith("pressed: ")]


def _zoo_element(engine: AutomationEngine, name: str, role: str) -> UIElement:
    found = engine.element_query(Locator(app=APP_ID, query=name, role=role))
    exact = [element for element in found.elements if element.name == name]
    assert len(exact) == 1, found.render()[:2000]
    return exact[0]


def _assert_no_new_press(zoo: ZooProcess, before: list[str]) -> None:
    time.sleep(NO_PRESS_GRACE_S)
    assert _presses(zoo) == before, _presses(zoo)


@pytest.mark.qa("QA-ELEM-032")
def test_ref_whose_role_changed_is_rejected(engine: AutomationEngine, zoo: ZooProcess) -> None:
    play = _zoo_element(engine, "Play", BUTTON)
    assert GTK_CLICK in play.actions, play
    ref = play.ref

    # A name change is tolerated: the same ref acts twice.
    first = engine.element_act(RefAddr(ref=ref), GTK_CLICK)
    assert first.dispatch == "confirmed", first.render()
    zoo.expect("pressed: Play")
    second = engine.element_act(RefAddr(ref=ref), GTK_CLICK)
    assert second.dispatch == "confirmed", second.render()
    assert second.element is not None and second.element.name == "Pause", second.render()
    assert second.element.ref == ref
    zoo.expect("pressed: Pause")

    # The second press renamed it back to "Play"; that slot now gets a toggle
    # button with the same name.
    zoo.command("swap-role", "role: toggle button")
    swapped = engine.element_query(
        Locator(app=APP_ID, query="Play", role="toggle button"), wait=Wait(timeout_ms=5000)
    )
    assert swapped.wait is not None and swapped.wait.satisfied, swapped.render()
    assert [element.name for element in swapped.elements] == ["Play"], swapped.render()
    assert swapped.elements[0].ref != ref
    before = _presses(zoo)
    with pytest.raises(RuntimeError) as stale:
        engine.element_act(RefAddr(ref=ref), GTK_CLICK)
    assert str(stale.value) == _stale_gone(ref)
    with pytest.raises(RuntimeError) as clicked:
        engine.pointer_click(ElementPoint(ref=ref))
    assert str(clicked.value) == _stale_gone(ref)
    _assert_no_new_press(zoo, before)


@pytest.mark.qa("QA-ELEM-032")
def test_ref_whose_role_changed_in_place_is_rejected(
    engine: AutomationEngine, zoo: ZooProcess
) -> None:
    """The same AT-SPI object answers with a new role: the ref is stale, nothing acts.

    Replacing a widget (the test above) destroys the old accessible, so its ref is
    caught as gone before the role is compared; only an in-place role change reaches
    the role check itself.
    """
    play = _zoo_element(engine, "Play", BUTTON)
    ref = play.ref
    zoo.command("retag-role", "role: toggle button (retagged)")
    retagged = engine.element_query(
        Locator(app=APP_ID, query="Play", role="toggle button"), wait=Wait(timeout_ms=5000)
    )
    assert retagged.wait is not None and retagged.wait.satisfied, retagged.render()
    before = _presses(zoo)
    expected = (
        f"Stale ref {ref} (role changed from '{BUTTON}' to 'toggle button'); re-run element_query."
    )
    with pytest.raises(RuntimeError) as stale:
        engine.element_act(RefAddr(ref=ref), GTK_CLICK)
    assert str(stale.value) == expected
    with pytest.raises(RuntimeError) as clicked:
        engine.pointer_click(ElementPoint(ref=ref))
    assert str(clicked.value) == expected
    _assert_no_new_press(zoo, before)


@pytest.mark.qa("QA-ELEM-036")
def test_destroyed_element_ref_errors_before_input(
    engine: AutomationEngine, zoo: ZooProcess
) -> None:
    close_me = _zoo_element(engine, "Close Me", BUTTON)
    ref = close_me.ref
    pressed = engine.element_act(RefAddr(ref=ref), GTK_CLICK)
    assert pressed.dispatch == "confirmed", pressed.render()
    zoo.expect("pressed: Close Me")
    gone = engine.element_query(
        Locator(app=APP_ID, query="Close Me", role=BUTTON),
        wait=Wait(until="absent", timeout_ms=5000),
    )
    assert gone.wait is not None and gone.wait.satisfied, gone.render()

    oracle = KWinOracle(_dbus(engine))
    cursor = oracle.cursor_pos()
    before = _presses(zoo)
    with pytest.raises(RuntimeError) as acted:
        engine.element_act(RefAddr(ref=ref), GTK_CLICK)
    assert str(acted.value) == _stale_gone(ref)
    with pytest.raises(RuntimeError) as clicked:
        engine.pointer_click(ElementPoint(ref=ref))
    assert str(clicked.value) == _stale_gone(ref)
    _assert_no_new_press(zoo, before)
    assert oracle.cursor_pos() == cursor


@pytest.mark.qa("QA-ELEM-037")
def test_ref_after_window_reopen_is_stale(engine: AutomationEngine, zoo: ZooProcess) -> None:
    ok = _zoo_element(engine, "OK", BUTTON)
    ref = ok.ref
    (main,) = engine.window_list(WindowSelector(app_id=APP_ID, caption=MAIN_CAPTION)).windows

    start = len(zoo.lines)
    closed = engine.window_close(WindowSelector(id=main.id))
    assert closed.requested, closed.render()
    zoo.expect(f"closed: {MAIN_CAPTION}", start=start)
    zoo.command("reopen", f"window: {MAIN_CAPTION}")
    reopened = engine.element_query(
        Locator(app=APP_ID, query="OK", role=BUTTON), wait=Wait(timeout_ms=15000)
    )
    assert reopened.wait is not None and reopened.wait.satisfied, reopened.render()
    assert all(element.ref != ref for element in reopened.elements)

    before = _presses(zoo)
    with pytest.raises(RuntimeError) as stale:
        engine.element_act(RefAddr(ref=ref), GTK_CLICK)
    assert str(stale.value) == _stale_gone(ref)
    _assert_no_new_press(zoo, before)


@pytest.mark.qa("QA-ELEM-038")
@pytest.mark.slow
@pytest.mark.timeout(600)
def test_lru_eviction_at_4097(engine: AutomationEngine, zoo: ZooProcess) -> None:
    zoo.command("labels", "window: Zoo Labels")
    # The worker's deadline is the wait's timeout + 30 s: room for a 5000-label walk.
    found = engine.element_query(
        Locator(app=APP_ID, query="Zoo Label ", role="label"), wait=Wait(timeout_ms=120_000)
    )
    assert found.wait is not None and found.wait.satisfied, found.render()[:500]
    # GTK lists a grid's children last-attached first; refs are minted in tree order.
    names = sorted(element.name for element in found.elements)
    assert names == [f"Zoo Label {index:04d}" for index in range(LABEL_COUNT)], names[:5]
    first, last = found.elements[0], found.elements[-1]
    (gen_first, n_first), (gen_last, n_last) = _parts(first.ref), _parts(last.ref)
    assert gen_first == gen_last
    assert n_last - n_first == LABEL_COUNT - 1

    with pytest.raises(RuntimeError) as evicted:
        engine.element_act(RefAddr(ref=first.ref), GTK_CLICK)
    assert str(evicted.value) == (
        f"Stale ref {first.ref} (evicted from the ref table or never issued); re-run element_query."
    )
    # The last ref still resolves: the refusal names its live element.
    with pytest.raises(RuntimeError) as resolved:
        engine.element_act(RefAddr(ref=last.ref), GTK_CLICK)
    assert str(resolved.value) == (
        f'Element {last.ref} [label] "{last.name}" has no AT-SPI action '
        f"'{GTK_CLICK}'; it exposes: (none).\nUse one of those action names."
    )
    oracle = AtspiOracle(_dbus(engine), timeout_s=300.0)
    assert len(oracle.find(app=APP_ID, name=last.name, role="label")) == 1


# ── ElementPoint targets on the interaction probe ─────────────────────────


@pytest.fixture
def probe(engine: AutomationEngine, tmp_path: Path) -> Probe:
    return start_probe_session(engine, tmp_path)


def _center(element: UIElement) -> Point:
    rect = element.rect
    assert rect is not None and rect.width > 0 and rect.height > 0, element
    return Point(x=rect.x + rect.width // 2, y=rect.y + rect.height // 2)


def _press_at(probe: Probe, mark: int) -> tuple[int, int]:
    """Probe-local position of the single button press logged after ``mark``."""
    probe.wait_events(lambda events: bool(of_kind(events, "button_release")), since=mark)
    presses = of_kind(probe.settle(since=mark), "button_press")
    assert len(presses) == 1, presses
    return int(presses[0]["x"]), int(presses[0]["y"])


def _inside(local: tuple[int, int], probe: Probe, element: UIElement) -> bool:
    rect = element.rect
    assert rect is not None
    x0, y0 = probe.to_local(rect.x, rect.y)
    return x0 <= local[0] < x0 + rect.width and y0 <= local[1] < y0 + rect.height


@pytest.mark.qa("QA-INP-007")
def test_element_point_offset_dx_dy_hits_neighbour(engine: AutomationEngine, probe: Probe) -> None:
    ref = probe.element("Click Target").ref

    # Move the window with the oracle, not kwin-mcp: the ref must follow it.
    oracle = probe.oracle()
    window = oracle.window(probe.window_id)
    assert window is not None and window.frame is not None, window
    old_origin = probe.origin
    frame = window.frame
    assert oracle.set_frame(
        probe.window_id, Rect(frame.x + WINDOW_SHIFT_PX, frame.y, frame.width, frame.height)
    )
    oracle.wait_for_windows(
        lambda ws: any(
            w.id == probe.window_id
            and w.frame is not None
            and round(w.frame.x) == round(frame.x) + WINDOW_SHIFT_PX
            for w in ws
        )
    )
    assert probe.refresh_origin() == (old_origin[0] + WINDOW_SHIFT_PX, old_origin[1])

    target = probe.element("Click Target")
    assert target.ref != ref
    centre = _center(target)

    mark = probe.park()
    offset = engine.pointer_click(ElementPoint(ref=ref, dx=5, dy=-3))
    assert offset.at == Point(x=centre.x + 5, y=centre.y - 3), offset.render()
    assert offset.element is not None and offset.element.ref == ref, offset.render()
    assert offset.element.rect == target.rect, (offset.element, target)
    assert _press_at(probe, mark) == probe.to_local(centre.x + 5, centre.y - 3)

    mark = probe.park()
    plain = engine.pointer_click(ElementPoint(ref=ref, dx=0, dy=0))
    assert plain.at == centre, plain.render()
    assert _press_at(probe, mark) == probe.to_local(centre.x, centre.y)

    # An offset as wide as the gap to the next button lands on that neighbour.
    neighbour = probe.element("Drag Target")
    neighbour_centre = _center(neighbour)
    mark = probe.park()
    across = engine.pointer_click(
        ElementPoint(ref=ref, dx=neighbour_centre.x - centre.x, dy=neighbour_centre.y - centre.y)
    )
    assert across.at == neighbour_centre, across.render()
    assert across.element is not None and across.element.ref == ref
    local = _press_at(probe, mark)
    assert _inside(local, probe, neighbour), (local, neighbour)
    assert not _inside(local, probe, target), (local, target)


@pytest.mark.qa("QA-INP-008")
def test_ref_with_null_rect_as_target_is_error_quoting_unavailable(
    engine: AutomationEngine, probe: Probe
) -> None:
    found = engine.element_query(Locator(app=PROBE_APP))
    unplaced = [element for element in found.elements if element.rect is None]
    assert unplaced, found.render()[:2000]
    element = unplaced[-1]
    assert element.unavailable, element
    assert f"@ unavailable ({element.unavailable})" in element.render()

    oracle = probe.oracle()
    mark = probe.park()
    cursor = oracle.cursor_pos()
    with pytest.raises(RuntimeError) as failure:
        engine.pointer_click(ElementPoint(ref=element.ref))
    assert str(failure.value) == (
        f"Element {element.ref} has no screen position (unavailable: {element.unavailable}).\n"
        "Use element_act(element={ref}, action) or pick another element."
    )
    assert probe.settle(since=mark) == []
    assert oracle.cursor_pos() == cursor
