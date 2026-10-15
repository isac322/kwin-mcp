"""Every input option against the interaction probe, plus the 0.11 click timelines.

``test_interaction_options_change_visible_probe_state_and_preserve_evidence`` drives the
installed server over MCP stdio on a nested visual KWin (screenshots work there) and
re-expresses every 0.11 option in the 1.0 API: ``count``, ``modifiers`` (KeyChord),
``hold_ms``, ``axis``/``unit``/``steps``, ``pointer_hold`` press/release, ``Waypoint``
``{x, y, dwell_ms}``, element ``Target``s, ``keyboard_*`` and ``touch_*`` options and
``capture``. Evidence (frames, observations, the probe log) is kept in the visual
artifact directory.
"""

from __future__ import annotations

import contextlib
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from mcp_harness import running_mcp_server
from PIL import Image
from probe_session import (
    PROBE_APP,
    PROBE_COMMAND,
    Probe,
    assert_matches_golden,
    bounds_field,
    field,
    float_field,
    int_field,
    load_golden,
    point_field,
    probe_env,
    start_probe_session,
)
from visual_harness import nested_visual_kwin

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp_harness import McpTestClient

    from kwin_mcp.core import AutomationEngine

STATE_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.1
CAPTURE = {"delays_ms": [0]}
# The 0.11 click/move goldens at probe-local (300, 200), with their 1.0 arguments.
CLICK_GOLDENS: dict[str, dict[str, Any]] = {
    "click_left_single": {},
    "click_left_double": {"count": 2},
    "click_left_triple": {"count": 3},
    "click_right_single": {"button": "right"},
    "click_middle_single": {"button": "middle"},
    "click_alt": {"modifiers": "alt"},
    "click_double_ctrl_shift": {"count": 2, "modifiers": "ctrl+shift"},
    "click_hold_300": {"hold_ms": 300},
}


@pytest.fixture
def anyio_backend() -> str:
    """The installed MCP client runs on asyncio."""
    return "asyncio"


@pytest.mark.qa("QA-INP-003")
def test_click_timing_matches_0_11_golden(engine: AutomationEngine, tmp_path: Path) -> None:
    probe: Probe = start_probe_session(engine, tmp_path)
    for scenario, options in CLICK_GOLDENS.items():
        golden = load_golden(scenario)
        mark = probe.park()
        result = engine.pointer_click(probe.to_global(*golden["call"]["local"]), **options)
        presses = 1 + (options.get("count", 1) - 1)
        probe.wait_events(
            lambda evs, n=presses: sum(e["kind"] == "button_release" for e in evs) >= n,
            since=mark,
        )
        events = probe.settle(since=mark)
        assert_matches_golden(events, golden)
        assert result.held.buttons == [] and result.held.keys == []

    golden = load_golden("move")
    mark = probe.park()
    engine.pointer_move(probe.to_global(*golden["call"]["local"]))
    probe.wait_events(lambda evs: len(evs) >= len(golden["sequence"]), since=mark)
    assert_matches_golden(probe.settle(since=mark), golden)


# ── MCP stdio option matrix ───────────────────────────────────────────────


def _center(rect: dict[str, int]) -> dict[str, int]:
    return {"x": rect["x"] + rect["width"] // 2, "y": rect["y"] + rect["height"] // 2}


async def _element(client: McpTestClient, name: str) -> dict[str, Any]:
    found, _ = await client.call_structured(
        "element_query", {"locator": {"app": PROBE_APP, "query": name}}
    )
    matches = [e for e in found["elements"] if e["name"] == name and e["rect"] is not None]
    assert len(matches) == 1, found
    return matches[0]


async def _status(client: McpTestClient, prefix: str) -> str:
    found, _ = await client.call_structured(
        "element_query", {"locator": {"app": PROBE_APP, "query": prefix}}
    )
    names = [e["name"] for e in found["elements"] if e["name"].startswith(prefix)]
    assert len(names) == 1, found
    return names[0]


async def _wait_for_status(
    client: McpTestClient, prefix: str, predicate: Callable[[str], bool], message: str
) -> str:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    current = ""
    while time.monotonic() < deadline:
        current = await _status(client, prefix)
        if predicate(current):
            return current
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"{message}: {current}")


async def _keyboard_text(client: McpTestClient) -> str:
    return str((await _element(client, "Keyboard Target"))["text"])


async def _wait_for_keyboard_text(client: McpTestClient, expected: str) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    actual = ""
    while time.monotonic() < deadline:
        actual = await _keyboard_text(client)
        if actual == expected:
            return
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"Keyboard Target text never became {expected!r}: {actual!r}")


def _keep_frames(result: dict[str, Any], artifact_dir: Path, name: str) -> None:
    """The captured frame exists as a PNG of the reported size; keep it as evidence."""
    assert result["frames"], result
    frame = result["frames"][-1]
    source = Path(frame["path"])
    destination = artifact_dir / name
    shutil.copy2(source, destination)
    with Image.open(destination) as image:
        image.load()
        assert image.format == "PNG", destination
        assert image.size == (frame["width"], frame["height"]), (destination, image.size)


@pytest.mark.anyio
@pytest.mark.visual
async def test_interaction_options_change_visible_probe_state_and_preserve_evidence() -> None:
    """Exercise every pointer, keyboard, scroll and touch option over MCP stdio."""
    with nested_visual_kwin() as visual:
        evidence: list[str] = []
        log_path = visual.artifact_dir / "interaction-probe-events.jsonl"
        async with running_mcp_server(
            env={"DISPLAY": visual.x_display, "KWIN_MCP_X11_SCREENSHOT": "1"}
        ) as client:
            connected = False
            try:
                info, _ = await client.call_structured(
                    "session_connect",
                    {
                        "dbus_address": visual.dbus_address,
                        "wayland_display": visual.wayland_display,
                    },
                )
                connected = True
                assert info["input_backend"] == "eis", info
                launched, text = await client.call_structured(
                    "app_launch", {"command": PROBE_COMMAND, "env": probe_env(log_path)}
                )
                assert text.startswith(f"App launched: {PROBE_COMMAND}"), text
                ready, _ = await client.call_structured(
                    "element_query",
                    {
                        "locator": {"app": PROBE_APP, "query": "Click Target"},
                        "wait": {"timeout_ms": 15_000},
                    },
                )
                assert ready["wait"]["satisfied"], ready
                tree, tree_text = await client.call_structured(
                    "element_query", {"locator": {"app": PROBE_APP}}
                )
                names = {e["name"] for e in tree["elements"]}
                for name in (
                    "Interaction Probe",
                    "Click Target",
                    "Drag Target",
                    "Keyboard Target",
                    "Zoom Target",
                    "Horizontal Scroll Region",
                ):
                    assert name in names, tree_text[:3000]
                for prefix in (
                    "click_status:",
                    "drag_status:",
                    "zoom_status:",
                    "horizontal_scroll:",
                ):
                    assert any(n.startswith(prefix) for n in names), tree_text[:3000]
                (visual.artifact_dir / "interaction-initial-tree.txt").write_text(
                    tree_text, encoding="utf-8"
                )

                # Clicks: button, count, modifiers (KeyChord), hold_ms, capture.
                click = _center((await _element(client, "Click Target"))["rect"])
                cases: list[tuple[dict[str, Any], str, int, str]] = [
                    ({"button": "middle"}, "middle", 1, "none"),
                    ({"button": "right"}, "right", 1, "none"),
                    ({"count": 2}, "left", 2, "none"),
                    ({"count": 3}, "left", 3, "none"),
                    ({"modifiers": "ctrl+shift"}, "left", 1, "ctrl+shift"),
                ]
                for arguments, button, count, modifiers in cases:
                    await anyio.sleep(0.6)
                    await client.call_structured("pointer_click", {"target": click, **arguments})
                    status = await _wait_for_status(
                        client,
                        "click_status:",
                        lambda s, b=button, c=count, m=modifiers: (
                            field(s, "button") == b
                            and int_field(s, "count") == c
                            and field(s, "modifiers") == m
                        ),
                        f"{button} count={count} modifiers={modifiers} click was not observed",
                    )
                    evidence.append(status)
                await anyio.sleep(0.6)
                held_click, held_text = await client.call_structured(
                    "pointer_click", {"target": click, "hold_ms": 500, "capture": CAPTURE}
                )
                assert held_text.startswith(
                    f"Clicked left at ({click['x']}, {click['y']}) held 500ms"
                )
                status = await _wait_for_status(
                    client,
                    "click_status:",
                    lambda s: (
                        field(s, "button") == "left"
                        and int_field(s, "count") == 1
                        and int_field(s, "hold_ms") >= 400
                    ),
                    "held mouse click duration was not observed",
                )
                evidence.append(status)
                _keep_frames(held_click, visual.artifact_dir, "interaction-clicks.png")

                # Scrolling: axis, unit, steps, signs.
                scroll = _center((await _element(client, "Horizontal Scroll Region"))["rect"])
                status = await _status(client, "horizontal_scroll:")
                events, total = int_field(status, "events"), float_field(status, "total")
                position = int_field(status, "position")
                await client.call_structured(
                    "pointer_scroll",
                    {"target": scroll, "delta": 6, "axis": "horizontal", "steps": 3},
                )
                status = await _wait_for_status(
                    client,
                    "horizontal_scroll:",
                    lambda s: (
                        field(s, "mode") == "smooth"
                        and int_field(s, "events") >= events + 3
                        and float_field(s, "last") > 0
                        and float_field(s, "total") > total
                        and int_field(s, "position") > position
                    ),
                    "positive stepped smooth horizontal scroll was not observed",
                )
                evidence.append(status)
                events, total = int_field(status, "events"), float_field(status, "total")
                position = int_field(status, "position")
                await client.call_structured(
                    "pointer_scroll",
                    {"target": scroll, "delta": -3, "axis": "horizontal", "steps": 3},
                )
                status = await _wait_for_status(
                    client,
                    "horizontal_scroll:",
                    lambda s: (
                        field(s, "mode") == "smooth"
                        and int_field(s, "events") >= events + 3
                        and float_field(s, "last") < 0
                        and float_field(s, "total") < total
                        and int_field(s, "position") < position
                    ),
                    "negative stepped smooth horizontal scroll was not observed",
                )
                evidence.append(status)
                for delta, sign in ((2, 1), (-2, -1)):
                    events, total = int_field(status, "events"), float_field(status, "total")
                    position = int_field(status, "position")
                    _, text = await client.call_structured(
                        "pointer_scroll",
                        {"target": scroll, "delta": delta, "axis": "horizontal", "unit": "detent"},
                    )
                    assert text == (
                        f"Scrolled horizontal (discrete) by {delta} "
                        f"at ({scroll['x']}, {scroll['y']})"
                    )
                    evidence.append(text)
                    status = await _wait_for_status(
                        client,
                        "horizontal_scroll:",
                        lambda s, sg=sign, e=events, t=total, p=position: (
                            field(s, "mode") in {"smooth", "discrete"}
                            and int_field(s, "events") > e
                            and float_field(s, "last") * sg > 0
                            and (float_field(s, "total") - t) * sg > 0
                            and (int_field(s, "position") - p) * sg > 0
                        ),
                        f"discrete horizontal scroll by {delta} was not observed",
                    )
                    evidence.append(status)
                shot, _ = await client.call_structured(
                    "pointer_scroll",
                    {"target": scroll, "delta": 0, "axis": "horizontal", "capture": CAPTURE},
                )
                _keep_frames(shot, visual.artifact_dir, "interaction-horizontal-scroll.png")

                # pointer_hold press/release (0.11 down/up) with the right button.
                drag_rect = (await _element(client, "Drag Target"))["rect"]
                dx, dy, dw, dh = (drag_rect[k] for k in ("x", "y", "width", "height"))
                pair = {"x": dx + dw // 2, "y": dy + dh // 2}
                _, down = await client.call_structured(
                    "pointer_hold", {"phase": "press", "target": pair, "button": "right"}
                )
                assert (
                    down == f"Button right pressed at ({pair['x']}, {pair['y']})\nStill held: right"
                )
                _, up = await client.call_structured(
                    "pointer_hold", {"phase": "release", "target": pair, "button": "right"}
                )
                assert up == f"Button right released at ({pair['x']}, {pair['y']})"
                pair_status = await _wait_for_status(
                    client,
                    "drag_status:",
                    lambda s: (
                        field(s, "source") == "mouse"
                        and field(s, "press") == "right"
                        and field(s, "release") == "right"
                        and field(s, "modifiers") == "none"
                        and int_field(s, "fingers") == 0
                    ),
                    "right-button press/release pair was not observed",
                )
                # A stationary press/release must not synthesize a drag path (#66).
                assert int_field(pair_status, "motions") == 0, pair_status
                assert point_field(pair_status, "start") == point_field(pair_status, "end")
                min_x, min_y, max_x, max_y = bounds_field(pair_status)
                assert (min_x, min_y) == (max_x, max_y), pair_status
                evidence.append(pair_status)

                # pointer_drag: middle button, ctrl+shift, two Waypoint{x, y, dwell_ms}.
                start = {"x": dx + 28, "y": dy + dh // 2}
                waypoints = [
                    {"x": dx + dw - 28, "y": dy + 14, "dwell_ms": 40},
                    {"x": dx + dw - 28, "y": dy + dh - 14, "dwell_ms": 40},
                ]
                dragged, _ = await client.call_structured(
                    "pointer_drag",
                    {
                        "from": start,
                        "to": start,
                        "button": "middle",
                        "modifiers": "ctrl+shift",
                        "waypoints": waypoints,
                        "capture": CAPTURE,
                    },
                )
                assert dragged["waypoints"] == 2, dragged
                expected_local = (28, dh // 2)
                drag_status = await _wait_for_status(
                    client,
                    "drag_status:",
                    lambda s: (
                        field(s, "source") == "mouse"
                        and field(s, "press") == "middle"
                        and field(s, "release") == "middle"
                        and field(s, "modifiers") == "ctrl+shift"
                        and int_field(s, "motions") >= 40
                        and int_field(s, "fingers") == 0
                        and all(
                            abs(a - e) <= 3
                            for a, e in zip(point_field(s, "start"), expected_local, strict=True)
                        )
                        and all(
                            abs(a - e) <= 3
                            for a, e in zip(point_field(s, "end"), expected_local, strict=True)
                        )
                        and all(
                            abs(a - e) <= 3
                            for a, e in zip(
                                bounds_field(s), (28, 14, dw - 28, dh - 14), strict=True
                            )
                        )
                    ),
                    "middle-button modified waypoint drag was not observed",
                )
                evidence.append(drag_status)
                _keep_frames(dragged, visual.artifact_dir, "interaction-drag.png")

                # Keyboard: KeyChord presses, an unknown key, keys and unicode typing.
                keyboard = _center((await _element(client, "Keyboard Target"))["rect"])
                await client.call_structured("pointer_click", {"target": keyboard})
                await client.call_structured("keyboard_press", {"keys": "ctrl+a"})
                await client.call_structured("keyboard_press", {"keys": "Delete"})
                await _wait_for_keyboard_text(client, "")
                invalid_key = "DefinitelyNotARealKey"
                lines = await client.call_tool_error(
                    "keyboard_press", {"keys": invalid_key}, lines=None
                )
                assert f"Unknown key: {invalid_key}" in "\n".join(lines), lines
                assert await client.session.send_ping() is not None
                assert await _keyboard_text(client) == ""
                evidence.append(f"invalid_key: {invalid_key} rejected; visible text unchanged")
                ascii_text = "ASCII 42!"
                unicode_text = " 한글 界"
                await client.call_structured("keyboard_type", {"text": ascii_text})
                await _wait_for_keyboard_text(client, ascii_text)
                typed, _ = await client.call_structured(
                    "keyboard_type",
                    {"text": unicode_text, "method": "unicode", "capture": CAPTURE},
                )
                assert typed["route"] in {"wtype", "paste:ctrl+v"}, typed
                expected_text = ascii_text + unicode_text
                await _wait_for_keyboard_text(client, expected_text)
                evidence.append(f"keyboard_text: {expected_text} via {typed['route']}")
                _keep_frames(typed, visual.artifact_dir, "interaction-keyboard.png")

                # Touch: tap hold_ms (long press), pinch in/out, multi-finger swipes.
                zoom_rect = (await _element(client, "Zoom Target"))["rect"]
                zoom = _center(zoom_rect)
                tapped, _ = await client.call_structured(
                    "touch_tap", {"target": zoom, "hold_ms": 800, "capture": CAPTURE}
                )
                long_press = await _wait_for_status(
                    client,
                    "zoom_status:",
                    lambda s: field(s, "long_press") == "recognized",
                    "touch hold was not recognized by the probe",
                )
                expected_zoom_local = (zoom_rect["width"] // 2, zoom_rect["height"] // 2)
                assert all(
                    abs(a - e) <= 4
                    for a, e in zip(point_field(long_press, "at"), expected_zoom_local, strict=True)
                ), long_press
                evidence.append(long_press)
                _keep_frames(tapped, visual.artifact_dir, "interaction-touch-hold.png")
                pinch_distance = min(160, zoom_rect["width"] - 40)
                assert pinch_distance >= 100, zoom_rect
                await client.call_structured(
                    "touch_pinch",
                    {
                        "center": zoom,
                        "start_distance": pinch_distance,
                        "end_distance": 60,
                        "duration_ms": 500,
                    },
                )
                pinch_in = await _wait_for_status(
                    client,
                    "zoom_status:",
                    lambda s: field(s, "phase") == "completed" and float_field(s, "scale") < 0.8,
                    "pinch-in did not produce a completed zoom-out scale",
                )
                evidence.append(pinch_in)
                pinched, _ = await client.call_structured(
                    "touch_pinch",
                    {
                        "center": zoom,
                        "start_distance": 60,
                        "end_distance": pinch_distance,
                        "duration_ms": 500,
                        "capture": CAPTURE,
                    },
                )
                pinch_out = await _wait_for_status(
                    client,
                    "zoom_status:",
                    lambda s: field(s, "phase") == "completed" and float_field(s, "scale") > 1.2,
                    "pinch-out did not produce a completed zoom-in scale",
                )
                evidence.append(pinch_out)
                _keep_frames(pinched, visual.artifact_dir, "interaction-pinch-out.png")

                touch_from = {"x": dx + dw // 3, "y": dy + dh // 2}
                touch_to = {"x": dx + dw * 2 // 3, "y": dy + dh // 2}
                await client.call_structured(
                    "touch_swipe",
                    {"from": touch_from, "to": touch_to, "fingers": 2, "duration_ms": 400},
                )
                two_finger = await _wait_for_status(
                    client,
                    "drag_status:",
                    lambda s: (
                        field(s, "source") == "touch"
                        and field(s, "press") == "touch"
                        and field(s, "release") in {"cancel", "touch"}
                        and field(s, "modifiers") == "none"
                        and int_field(s, "fingers") == 2
                        and int_field(s, "motions") >= 20
                        and bounds_field(s)[2] - bounds_field(s)[0] >= dw // 4
                        and bounds_field(s)[3] - bounds_field(s)[1] >= 16
                    ),
                    "2-finger swipe did not reach the probe with both touch sequences",
                )
                evidence.append(f"2-finger: {two_finger}")
                for fingers in (3, 4, 5):
                    marker = await _status(client, "drag_status:")
                    swiped, text = await client.call_structured(
                        "touch_swipe",
                        {
                            "from": touch_from,
                            "to": touch_to,
                            "fingers": fingers,
                            "duration_ms": 400,
                            "capture": CAPTURE,
                        },
                    )
                    assert text.startswith(
                        f"{fingers}-finger swipe from ({touch_from['x']}, {touch_from['y']}) "
                        f"to ({touch_to['x']}, {touch_to['y']}) in 400ms"
                    ), text
                    await anyio.sleep(0.5)
                    after = await _status(client, "drag_status:")
                    observed = int_field(after, "fingers")
                    if after == marker:
                        outcome = "suppressed"
                    else:
                        assert field(after, "source") == "touch", after
                        assert field(after, "press") == "touch", after
                        assert field(after, "release") in {"cancel", "touch"}, after
                        if field(after, "release") == "cancel":
                            assert 1 <= observed <= fingers, after
                            outcome = "canceled"
                        else:
                            assert observed == fingers, after
                            bounds = bounds_field(after)
                            assert int_field(after, "motions") >= 20, after
                            assert bounds[2] - bounds[0] >= dw // 4, after
                            assert bounds[3] - bounds[1] >= 16, after
                            outcome = "delivered"
                    evidence.append(
                        f"{fingers}-finger {outcome} (requested={fingers}, observed={observed}): "
                        f"{after}"
                    )
                    _keep_frames(swiped, visual.artifact_dir, f"interaction-{fingers}-finger.png")

                _, final_tree = await client.call_structured(
                    "element_query", {"locator": {"app": PROBE_APP}}
                )
                assert await _keyboard_text(client) == expected_text, final_tree[:4000]
                (visual.artifact_dir / "interaction-final-tree.txt").write_text(
                    final_tree, encoding="utf-8"
                )
                assert launched["pid"] > 0
            finally:
                (visual.artifact_dir / "interaction-observations.txt").write_text(
                    "\n".join(evidence) + ("\n" if evidence else ""), encoding="utf-8"
                )
                (visual.artifact_dir / "mcp-server.stderr.log").write_text(
                    client.stderr_text(), encoding="utf-8"
                )
                if connected:
                    with contextlib.suppress(BaseException):
                        await client.call_result("session_stop")
