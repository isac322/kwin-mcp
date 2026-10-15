"""pointer_drag end to end: the 0.11 timeline (G7), waypoints, dwell, refs, finally-release.

Oracles: the probe's raw event log and ``drag_status`` (``probe_session``), the 0.11
goldens ``drag_left``/``drag_right``/``drag_waypoint_dwell``, ``atspi_oracle`` rects,
and the KWrite selection read back through the session clipboard.
"""

from __future__ import annotations

import itertools
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from atspi_oracle import AtspiOracle
from probe_session import (
    BUTTON_KINDS,
    POINTER_KINDS,
    PROBE_APP,
    Probe,
    assert_interval,
    assert_matches_golden,
    center_of,
    field,
    int_field,
    interval_ms,
    launch_probe,
    load_golden,
    of_kind,
    start_probe_session,
)
from visual_harness import nested_visual_kwin

from kwin_mcp.models import ElementPoint, HeldInput, Locator, Point, Waypoint

if TYPE_CHECKING:
    from collections.abc import Callable

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import ElementList

DOCUMENT = Path("/tmp/kwin-mcp-drag.txt")
FIRST_LINE = "line 000 ---------------------------"


@pytest.fixture
def probe(engine: AutomationEngine, tmp_path: Path) -> Probe:
    return start_probe_session(engine, tmp_path)


def _drag_events(probe: Probe, mark: int) -> list[dict[str, object]]:
    probe.wait_events(lambda evs: bool(of_kind(evs, "button_release")), since=mark)
    return [e for e in probe.settle(since=mark) if e["kind"] in POINTER_KINDS]


@pytest.mark.qa("QA-INP-021")
def test_drag_selects_text_in_kwrite(
    engine: AutomationEngine,
    tmp_path: Path,
    start_session: Callable[..., object],
    launch: Callable[..., object],
    wait_for_app: Callable[[str], ElementList],
) -> None:
    # The G7 timeline equals the 0.11 goldens, for the default and the right button.
    probe = start_probe_session(engine, tmp_path)
    for scenario, button in (("drag_left", "left"), ("drag_right", "right")):
        golden = load_golden(scenario)
        start = probe.to_global(*golden["call"]["from_local"])
        end = probe.to_global(*golden["call"]["to_local"])
        mark = probe.park()
        result = engine.pointer_drag(start, end, button=button)
        suffix = "" if button == "left" else " with the right button"
        assert result.render() == (
            f"Dragged from ({start.x}, {start.y}) to ({end.x}, {end.y}){suffix}"
        )
        assert (result.start, result.at, result.waypoints) == (start, end, 0)
        assert_matches_golden(_drag_events(probe, mark), golden)

    # The probe's Drag Target sees a right-button mouse drag.
    target = probe.center("Drag Target")
    probe.park()
    engine.pointer_drag(target, Point(x=target.x + 120, y=target.y), button="right")
    drag = probe.wait_status("drag_status:", lambda s: field(s, "release") == "right")
    assert (field(drag, "source"), field(drag, "press")) == ("mouse", "right"), drag
    assert int_field(drag, "motions") >= 10, drag
    assert engine.session_stop().stopped

    # 0.11 port: a left drag across the first line selects text in KWrite.
    start_session(enable_clipboard=True)
    DOCUMENT.write_text(
        "\n".join(f"line {i:03d} ---------------------------" for i in range(50)),
        encoding="utf-8",
    )
    launch(f"kwrite {DOCUMENT}")
    wait_for_app("kwrite")
    time.sleep(2)
    found = engine.element_query(Locator(app="kwrite", query=DOCUMENT.name, role="text"))
    (area,) = [e for e in found.elements if e.rect is not None]
    assert area.rect is not None
    line_y = area.rect.y + 12
    engine.pointer_drag(Point(x=area.rect.x + 60, y=line_y), Point(x=area.rect.x + 170, y=line_y))
    time.sleep(0.8)
    engine.keyboard_press("ctrl+c")
    time.sleep(0.8)
    selection = engine.clipboard_read().text
    # A near-no-op drag would still copy a character or two, so require a span.
    assert len(selection) >= 8, repr(selection)
    assert selection in FIRST_LINE, repr(selection)


@pytest.mark.qa("QA-INP-022")
def test_waypoints_dwell_reached_in_order(engine: AutomationEngine, probe: Probe) -> None:
    golden = load_golden("drag_waypoint_dwell")
    (wx, wy, wdwell) = golden["call"]["waypoints_local"][0]
    waypoint = probe.to_global(wx, wy)
    mark = probe.park()
    engine.pointer_drag(
        probe.to_global(*golden["call"]["from_local"]),
        probe.to_global(*golden["call"]["to_local"]),
        waypoints=[Waypoint(x=waypoint.x, y=waypoint.y, dwell_ms=wdwell)],
    )
    assert_matches_golden(_drag_events(probe, mark), golden)

    path = [(250, 200, 300), (300, 250, 0), (320, 260, -10)]
    mark = probe.park()
    result = engine.pointer_drag(
        probe.to_global(100, 300),
        probe.to_global(400, 300),
        waypoints=[
            Waypoint(x=probe.to_global(x, y).x, y=probe.to_global(x, y).y, dwell_ms=d)
            for x, y, d in path
        ],
    )
    assert result.waypoints == 3 and result.warnings == []
    assert result.render().endswith(" via 3 waypoints"), result.render()
    events = _drag_events(probe, mark)
    motions = of_kind(events, "motion")
    points = [(e["x"], e["y"]) for e in motions]
    # Every waypoint is reached, in order, and the drag ends at `to`.
    indexes = [points.index((x, y)) for x, y, _ in path]
    assert indexes == sorted(indexes), points
    assert points[-1] == (400, 300), points
    # Only the first waypoint pauses (300 ms); dwell 0 and -10 do not.
    gaps = [interval_ms(a, b) for a, b in itertools.pairwise(motions)]
    pause = indexes[0]
    assert_interval(motions[pause], motions[pause + 1], at_least_ms=300)
    assert all(gap < 150 for i, gap in enumerate(gaps) if i != pause), gaps


@pytest.mark.qa("QA-INP-023")
def test_dwell_ms_pauses_at_to(engine: AutomationEngine, probe: Probe) -> None:
    start, end = probe.to_global(100, 300), probe.to_global(400, 300)
    mark = probe.park()
    result = engine.pointer_drag(start, end, dwell_ms=400)
    assert result.dwell_ms == 400
    assert result.render() == (
        f"Dragged from ({start.x}, {start.y}) to ({end.x}, {end.y}), "
        "dwelling 400ms before the release"
    )
    events = _drag_events(probe, mark)
    last_motion = of_kind(events, "motion")[-1]
    (release,) = of_kind(events, "button_release")
    assert (last_motion["x"], last_motion["y"]) == (400, 300)
    assert_interval(last_motion, release, at_least_ms=400)


@pytest.mark.qa("QA-INP-024")
@pytest.mark.visual
def test_button_and_modifiers_released_in_finally_on_error(
    engine: AutomationEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The drag fails after its press (the X11 screenshot-cursor mirror fails); the
    # button and modifier it pressed must still come up and nothing stays latched.
    # A mid-path EIS loss cannot show this: KWin keeps an emulated button down after
    # the client disconnects or its pointer device is replaced (QA-INP-030/046).
    with nested_visual_kwin() as visual:
        monkeypatch.setenv("DISPLAY", visual.x_display)
        monkeypatch.setenv("KWIN_MCP_X11_SCREENSHOT", "1")
        info = engine.session_connect(
            dbus_address=visual.dbus_address, wayland_display=visual.wayland_display
        )
        assert info.input_backend == "eis", info.render()
        probe = launch_probe(engine, tmp_path)
        mark = probe.park()

        # A fake xdotool whose first mirror (the move to `from`) works and every
        # later one (the final mirror at `to`) fails.
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        xdotool = fake_bin / "xdotool"
        xdotool.write_text(
            '#!/bin/sh\nmarker="$0.called"\n'
            'if [ -e "$marker" ]; then echo "synthetic final mirror failure" >&2; exit 73; fi\n'
            ': > "$marker"\n',
            encoding="utf-8",
        )
        xdotool.chmod(0o755)
        real_path = os.environ["PATH"]
        monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{real_path}")
        with pytest.raises(RuntimeError, match=r"xdotool failed to mirror the screenshot cursor"):
            engine.pointer_drag(
                probe.to_global(100, 300), probe.to_global(400, 300), modifiers="ctrl"
            )
        monkeypatch.setenv("PATH", real_path)

        probe.wait_events(
            lambda evs: bool(of_kind(evs, "button_release")) and bool(of_kind(evs, "key_release")),
            since=mark,
        )
        events = probe.settle(since=mark)
        order = [
            (e["kind"], e.get("key") or e.get("button"))
            for e in events
            if e["kind"] in BUTTON_KINDS or e["kind"] in {"key_press", "key_release"}
        ]
        # ctrl down before the press; release, then ctrl up, after it.
        assert order == [
            ("key_press", "Control_L"),
            ("button_press", "left"),
            ("button_release", "left"),
            ("key_release", "Control_L"),
        ], order
        probe.wait_status("raw_events:", lambda s: field(s, "held_mods") == "none")
        probe.wait_status("hold_status:", lambda s: field(s, "held") == "none")
        assert engine.session_info().held == HeldInput()

        # Nothing is latched: typed text stays lower-case, a click carries no modifier.
        engine.keyboard_type("lowercase-clean")
        deadline = time.monotonic() + 5.0
        while probe.entry_text() != "lowercase-clean":
            assert time.monotonic() < deadline, probe.entry_text()
            time.sleep(0.1)
        before = probe.status("click_status:")
        engine.pointer_click(probe.center("Click Target"), button="right")
        clicked = probe.wait_status("click_status:", lambda s: s != before)
        assert (field(clicked, "button"), field(clicked, "modifiers")) == ("right", "none")
        assert int_field(clicked, "count") == 1 and int_field(clicked, "hold_ms") < 500
        stopped = engine.session_stop()
        assert stopped.stopped and stopped.released == HeldInput()


@pytest.mark.qa("QA-INP-026")
def test_ref_endpoints_resolved_in_one_batch(engine: AutomationEngine, probe: Probe) -> None:
    source = probe.element("Click Target")
    destination = probe.element("Drag Target")
    oracle = AtspiOracle(probe.dbus_address)
    # AT-SPI extents on Wayland are window-relative; KWin's client origin makes them global.
    rects: dict[str, Point] = {}
    for name in ("Click Target", "Drag Target"):
        (node,) = [n for n in oracle.find(app=PROBE_APP, name=name) if n.window is not None]
        assert node.window is not None, node
        x, y, w, h = node.window
        rects[name] = probe.to_global(x + w // 2, y + h // 2)
    assert center_of(source) == rects["Click Target"]
    assert center_of(destination) == rects["Drag Target"]

    mark = probe.park()
    result = engine.pointer_drag(
        ElementPoint(ref=source.ref), ElementPoint(ref=destination.ref), dwell_ms=300
    )
    assert result.start == rects["Click Target"] and result.at == rects["Drag Target"]
    assert result.element is not None and result.element.name == "Drag Target"
    events = _drag_events(probe, mark)
    (press,) = of_kind(events, "button_press")
    (release,) = of_kind(events, "button_release")
    a, b = rects["Click Target"], rects["Drag Target"]
    assert (press["x"], press["y"]) == probe.to_local(a.x, a.y)
    assert (release["x"], release["y"]) == probe.to_local(b.x, b.y)
