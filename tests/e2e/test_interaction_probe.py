"""Deterministic installed-package interaction contracts against a GTK probe."""

from __future__ import annotations

import ast
import contextlib
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import pytest
from mcp_harness import running_mcp_server
from PIL import Image
from visual_harness import nested_visual_kwin

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp_harness import McpTestClient

SCREEN_SIZE = (1280, 800)
PROBE_COMMAND = "python3 /app/tests/e2e/interaction_probe.py"
PROBE_SELECTOR = "interaction_probe.py"
PROBE_TITLE = "Interaction Probe"
POLL_INTERVAL_SECONDS = 0.1
STATE_TIMEOUT_SECONDS = 5.0
_RECT = r"\((-?\d+), (-?\d+), (\d+)x(\d+)\)"

pytestmark = [pytest.mark.anyio, pytest.mark.visual]


@pytest.fixture
def anyio_backend() -> str:
    """The installed MCP client runs on asyncio."""
    return "asyncio"


def _result_text(result: object) -> str:
    content = getattr(result, "content", [])
    return "\n".join(str(getattr(block, "text", "")) for block in content)


def _single_rect(output: str, pattern: str) -> tuple[int, int, int, int]:
    matches = re.findall(pattern, output, re.MULTILINE)
    assert len(matches) == 1, output[:2000]
    x, y, width, height = matches[0]
    return int(x), int(y), int(width), int(height)


async def _global_element_rect(
    client: McpTestClient,
    name: str,
) -> tuple[int, int, int, int]:
    elements = await client.call_text(
        "find_ui_elements",
        {"query": name, "app_name": PROBE_SELECTOR},
    )
    local_x, local_y, width, height = _single_rect(
        elements,
        rf'^- \[[^]]+] "{re.escape(name)}" @ {_RECT}(?:\s|$)',
    )
    assert width > 0 and height > 0, elements[:2000]

    geometry = await client.call_text("window_geometry", {"app_name": PROBE_SELECTOR})
    client_x, client_y, _, _ = _single_rect(geometry, rf"client:\s+{_RECT}")
    return client_x + local_x, client_y + local_y, width, height


def _center(rect: tuple[int, int, int, int]) -> tuple[int, int]:
    x, y, width, height = rect
    return x + width // 2, y + height // 2


def _status_from_elements(output: str, prefix: str) -> str:
    matches = re.findall(
        rf'^- \[label] "({re.escape(prefix)}:[^"]*)"(?:\s|$)',
        output,
        re.MULTILINE,
    )
    assert len(matches) == 1, output[:2000]
    return matches[0]


async def _status(client: McpTestClient, prefix: str) -> str:
    output = await client.call_text(
        "find_ui_elements",
        {"query": f"{prefix}:", "app_name": PROBE_SELECTOR},
    )
    return _status_from_elements(output, prefix)


async def _wait_for_status(
    client: McpTestClient,
    prefix: str,
    predicate: Callable[[str], bool],
    message: str,
) -> str:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    current = ""
    while time.monotonic() < deadline:
        current = await _status(client, prefix)
        if predicate(current):
            return current
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"{message}: {current}")


def _field(status: str, name: str) -> str:
    match = re.search(rf"(?:^| )({re.escape(name)})=([^ ]+)", status)
    assert match is not None, status
    return match.group(2)


def _integer_field(status: str, name: str) -> int:
    return int(_field(status, name))


def _float_field(status: str, name: str) -> float:
    return float(_field(status, name))


def _point_field(status: str, name: str) -> tuple[int, int]:
    match = re.fullmatch(r"\((-?\d+),(-?\d+)\)", _field(status, name))
    assert match is not None, status
    return int(match.group(1)), int(match.group(2))


def _bounds_field(status: str, name: str = "bounds") -> tuple[int, int, int, int]:
    match = re.fullmatch(
        r"\((-?\d+),(-?\d+),(-?\d+),(-?\d+)\)",
        _field(status, name),
    )
    assert match is not None, status
    x, y, width, height = match.groups()
    return int(x), int(y), int(width), int(height)


async def _keyboard_text(client: McpTestClient) -> str:
    output = await client.call_text(
        "find_ui_elements",
        {"query": "Keyboard Target", "app_name": PROBE_SELECTOR},
    )
    target = re.compile(r'^- \[[^]]+] "Keyboard Target" @ ')
    lines = [line for line in output.splitlines() if target.match(line)]
    assert len(lines) == 1, output[:2000]
    match = re.search(r" text=(.*?)(?: \[actions:|$)", lines[0])
    return "" if match is None else ast.literal_eval(match.group(1))


async def _wait_for_keyboard_text(client: McpTestClient, expected: str) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    actual = ""
    while time.monotonic() < deadline:
        actual = await _keyboard_text(client)
        if actual == expected:
            return
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"Keyboard Target text never became {expected!r}: {actual!r}")


def _screenshot_source(output: str) -> Path:
    prefix = "Screenshot saved: "
    assert output.startswith(prefix), output
    source = Path(output.removeprefix(prefix).rsplit(" (", 1)[0])
    assert source.is_file(), output
    return source


async def _capture(client: McpTestClient, artifact_dir: Path, name: str) -> None:
    source = _screenshot_source(await client.call_text("screenshot"))
    destination = artifact_dir / name
    shutil.copy2(source, destination)
    with Image.open(destination) as image:
        image.load()
        assert image.format == "PNG", destination
        assert image.size == SCREEN_SIZE, (destination, image.size)


def _app_log_path(launch_output: str) -> Path:
    match = re.search(r"^App log: (.+)$", launch_output, re.MULTILINE)
    assert match is not None, launch_output
    return Path(match.group(1))


async def _connect(client: McpTestClient, dbus_address: str, wayland_display: str) -> None:
    output = await client.call_text(
        "session_connect",
        {
            "dbus_address": dbus_address,
            "wayland_display": wayland_display,
            "keep_screenshots": True,
        },
    )
    assert output.startswith("Connected to live KWin session."), output
    assert "Input backend: KWin EIS" in output, output


async def _mark_long_press(
    client: McpTestClient,
    zoom_center: tuple[int, int],
) -> str:
    await client.call_text(
        "touch_tap",
        {"x": zoom_center[0], "y": zoom_center[1], "hold_ms": 800},
    )
    return await _wait_for_status(
        client,
        "zoom_status",
        lambda status: _field(status, "long_press") == "recognized",
        "touch hold was not recognized by the probe",
    )


async def test_interaction_options_change_visible_probe_state_and_preserve_evidence() -> None:
    """Exercise material pointer, keyboard, scroll, and touch options over MCP stdio."""
    with nested_visual_kwin() as visual:
        evidence: list[str] = []
        app_log_path: Path | None = None
        async with running_mcp_server(
            env={
                "DISPLAY": visual.x_display,
                "KWIN_MCP_X11_SCREENSHOT": "1",
            }
        ) as client:
            connected = False
            try:
                await _connect(client, visual.dbus_address, visual.wayland_display)
                connected = True
                launch_output = await client.call_text(
                    "launch_app",
                    {
                        "command": PROBE_COMMAND,
                        "env": {
                            "GDK_BACKEND": "wayland",
                            "GTK_MODULES": "gail:atk-bridge",
                            "NO_AT_BRIDGE": "0",
                            "XDG_SESSION_TYPE": "wayland",
                        },
                    },
                )
                assert f"App launched: {PROBE_COMMAND}" in launch_output, launch_output
                app_log_path = _app_log_path(launch_output)
                ready = await client.call_text(
                    "wait_for_element",
                    {
                        "query": "Click Target",
                        "app_name": PROBE_SELECTOR,
                        "timeout_ms": 15_000,
                    },
                )
                assert '[button] "Click Target"' in ready, ready[:2000]

                initial_tree = await client.call_text(
                    "accessibility_tree", {"app_name": PROBE_SELECTOR}
                )
                for name in (
                    PROBE_TITLE,
                    "Click Target",
                    "Drag Target",
                    "Keyboard Target",
                    "Zoom Target",
                    "Horizontal Scroll Region",
                    "click_status:",
                    "drag_status:",
                    "zoom_status:",
                    "horizontal_scroll:",
                ):
                    assert f'"{name}' in initial_tree, initial_tree[:3000]
                (visual.artifact_dir / "interaction-initial-tree.txt").write_text(
                    initial_tree,
                    encoding="utf-8",
                )
                await _capture(client, visual.artifact_dir, "interaction-initial.png")

                click_rect = await _global_element_rect(client, "Click Target")
                click_x, click_y = _center(click_rect)
                click_cases = (
                    ({"button": "middle"}, "middle", 1, "none"),
                    ({"button": "right"}, "right", 1, "none"),
                    ({"double": True}, "left", 2, "none"),
                    ({"triple": True}, "left", 3, "none"),
                    ({"modifiers": ["ctrl", "shift"]}, "left", 1, "ctrl+shift"),
                )
                for arguments, button, count, modifiers in click_cases:
                    await anyio.sleep(0.6)
                    await client.call_text(
                        "mouse_click",
                        {"x": click_x, "y": click_y, **arguments},
                    )
                    click_status = await _wait_for_status(
                        client,
                        "click_status",
                        lambda status, button=button, count=count, modifiers=modifiers: (
                            _field(status, "button") == button
                            and _integer_field(status, "count") == count
                            and _field(status, "modifiers") == modifiers
                        ),
                        f"{button} count={count} modifiers={modifiers} click was not observed",
                    )
                    evidence.append(click_status)

                await anyio.sleep(0.6)
                await client.call_text(
                    "mouse_click",
                    {"x": click_x, "y": click_y, "button": "left", "hold_ms": 500},
                )
                held_click = await _wait_for_status(
                    client,
                    "click_status",
                    lambda status: (
                        _field(status, "button") == "left"
                        and _integer_field(status, "count") == 1
                        and _integer_field(status, "hold_ms") >= 400
                    ),
                    "held mouse click duration was not observed",
                )
                evidence.append(held_click)
                await _capture(client, visual.artifact_dir, "interaction-clicks.png")

                scroll_rect = await _global_element_rect(
                    client,
                    "Horizontal Scroll Region",
                )
                scroll_x, scroll_y = _center(scroll_rect)
                scroll_status = await _status(client, "horizontal_scroll")
                scroll_events = _integer_field(scroll_status, "events")
                scroll_total = _float_field(scroll_status, "total")
                scroll_position = _integer_field(scroll_status, "position")

                await client.call_text(
                    "mouse_scroll",
                    {
                        "x": scroll_x,
                        "y": scroll_y,
                        "delta": 6,
                        "horizontal": True,
                        "steps": 3,
                    },
                )
                smooth_positive = await _wait_for_status(
                    client,
                    "horizontal_scroll",
                    lambda status: (
                        _field(status, "mode") == "smooth"
                        and _integer_field(status, "events") >= scroll_events + 3
                        and _float_field(status, "last") > 0
                        and _float_field(status, "total") > scroll_total
                        and _integer_field(status, "position") > scroll_position
                    ),
                    "positive stepped smooth horizontal scroll was not observed",
                )
                evidence.append(smooth_positive)

                positive_events = _integer_field(smooth_positive, "events")
                positive_total = _float_field(smooth_positive, "total")
                positive_position = _integer_field(smooth_positive, "position")
                await client.call_text(
                    "mouse_scroll",
                    {
                        "x": scroll_x,
                        "y": scroll_y,
                        "delta": -3,
                        "horizontal": True,
                        "steps": 3,
                    },
                )
                smooth_negative = await _wait_for_status(
                    client,
                    "horizontal_scroll",
                    lambda status: (
                        _field(status, "mode") == "smooth"
                        and _integer_field(status, "events") >= positive_events + 3
                        and _float_field(status, "last") < 0
                        and _float_field(status, "total") < positive_total
                        and _integer_field(status, "position") < positive_position
                    ),
                    "negative stepped smooth horizontal scroll was not observed",
                )
                evidence.append(smooth_negative)

                smooth_events = _integer_field(smooth_negative, "events")
                smooth_total = _float_field(smooth_negative, "total")
                smooth_position = _integer_field(smooth_negative, "position")
                discrete_positive_result = await client.call_text(
                    "mouse_scroll",
                    {
                        "x": scroll_x,
                        "y": scroll_y,
                        "delta": 2,
                        "horizontal": True,
                        "discrete": True,
                    },
                )
                assert discrete_positive_result == (
                    f"Scrolled horizontal (discrete) by 2 at ({scroll_x}, {scroll_y})"
                )
                evidence.append(discrete_positive_result)
                discrete_positive = await _wait_for_status(
                    client,
                    "horizontal_scroll",
                    lambda status: (
                        _field(status, "mode") in {"smooth", "discrete"}
                        and _integer_field(status, "events") > smooth_events
                        and _float_field(status, "last") > 0
                        and _float_field(status, "total") > smooth_total
                        and _integer_field(status, "position") > smooth_position
                    ),
                    "positive discrete horizontal scroll was not observed",
                )
                evidence.append(discrete_positive)

                discrete_events = _integer_field(discrete_positive, "events")
                discrete_total = _float_field(discrete_positive, "total")
                discrete_position = _integer_field(discrete_positive, "position")
                discrete_negative_result = await client.call_text(
                    "mouse_scroll",
                    {
                        "x": scroll_x,
                        "y": scroll_y,
                        "delta": -2,
                        "horizontal": True,
                        "discrete": True,
                    },
                )
                assert discrete_negative_result == (
                    f"Scrolled horizontal (discrete) by -2 at ({scroll_x}, {scroll_y})"
                )
                evidence.append(discrete_negative_result)
                discrete_negative = await _wait_for_status(
                    client,
                    "horizontal_scroll",
                    lambda status: (
                        _field(status, "mode") in {"smooth", "discrete"}
                        and _integer_field(status, "events") > discrete_events
                        and _float_field(status, "last") < 0
                        and _float_field(status, "total") < discrete_total
                        and _integer_field(status, "position") < discrete_position
                    ),
                    "negative discrete horizontal scroll was not observed",
                )
                evidence.append(discrete_negative)
                await _capture(
                    client,
                    visual.artifact_dir,
                    "interaction-horizontal-scroll.png",
                )

                drag_x, drag_y, drag_width, drag_height = await _global_element_rect(
                    client, "Drag Target"
                )
                start = (drag_x + 28, drag_y + drag_height // 2)
                button_pair_point = (drag_x + drag_width // 2, drag_y + drag_height // 2)
                button_down = await client.call_text(
                    "mouse_button_down",
                    {
                        "x": button_pair_point[0],
                        "y": button_pair_point[1],
                        "button": "right",
                    },
                )
                assert button_down == (
                    f"Button right pressed at ({button_pair_point[0]}, {button_pair_point[1]})"
                )
                button_up = await client.call_text(
                    "mouse_button_up",
                    {
                        "x": button_pair_point[0],
                        "y": button_pair_point[1],
                        "button": "right",
                    },
                )
                assert button_up == (
                    f"Button right released at ({button_pair_point[0]}, {button_pair_point[1]})"
                )
                button_pair_status = await _wait_for_status(
                    client,
                    "drag_status",
                    lambda status: (
                        _field(status, "source") == "mouse"
                        and _field(status, "press") == "right"
                        and _field(status, "release") == "right"
                        and _field(status, "modifiers") == "none"
                        and _integer_field(status, "fingers") == 0
                    ),
                    "right-button down/up pair was not observed",
                )
                evidence.append(button_pair_status)

                waypoint_one = (drag_x + drag_width - 28, drag_y + 14)
                waypoint_two = (drag_x + drag_width - 28, drag_y + drag_height - 14)
                await client.call_text(
                    "mouse_drag",
                    {
                        "from_x": start[0],
                        "from_y": start[1],
                        "to_x": start[0],
                        "to_y": start[1],
                        "button": "middle",
                        "modifiers": ["ctrl", "shift"],
                        "waypoints": [
                            [waypoint_one[0], waypoint_one[1], 40],
                            [waypoint_two[0], waypoint_two[1], 40],
                        ],
                    },
                )
                expected_local = (28, drag_height // 2)
                expected_max_x = drag_width - 28
                expected_min_y = 14
                expected_max_y = drag_height - 14
                drag_status = await _wait_for_status(
                    client,
                    "drag_status",
                    lambda status: (
                        _field(status, "source") == "mouse"
                        and _field(status, "press") == "middle"
                        and _field(status, "release") == "middle"
                        and _field(status, "modifiers") == "ctrl+shift"
                        and _integer_field(status, "motions") >= 40
                        and _integer_field(status, "fingers") == 0
                        and all(
                            abs(actual - expected) <= 3
                            for actual, expected in zip(
                                _point_field(status, "start"), expected_local, strict=True
                            )
                        )
                        and all(
                            abs(actual - expected) <= 3
                            for actual, expected in zip(
                                _point_field(status, "end"), expected_local, strict=True
                            )
                        )
                        and abs(_bounds_field(status)[0] - expected_local[0]) <= 3
                        and abs(_bounds_field(status)[1] - expected_min_y) <= 3
                        and abs(_bounds_field(status)[2] - expected_max_x) <= 3
                        and abs(_bounds_field(status)[3] - expected_max_y) <= 3
                    ),
                    "middle-button modified waypoint drag was not observed",
                )
                evidence.append(drag_status)
                await _capture(client, visual.artifact_dir, "interaction-drag.png")

                keyboard_rect = await _global_element_rect(client, "Keyboard Target")
                keyboard_x, keyboard_y = _center(keyboard_rect)
                await client.call_text(
                    "mouse_click",
                    {"x": keyboard_x, "y": keyboard_y},
                )
                await client.call_text("keyboard_key", {"key": "ctrl+a"})
                await client.call_text("keyboard_key", {"key": "Delete"})
                await _wait_for_keyboard_text(client, "")

                invalid_key = "DefinitelyNotARealKey"
                invalid_result = await client.call_result("keyboard_key", {"key": invalid_key})
                invalid_error = _result_text(invalid_result)
                assert invalid_result.isError is True, invalid_error
                assert invalid_key in invalid_error, invalid_error
                assert await client.session.send_ping() is not None
                assert await _keyboard_text(client) == ""
                evidence.append(f"invalid_key: {invalid_key} rejected; visible text unchanged")

                ascii_text = "ASCII 42!"
                unicode_text = " 한글界"
                await client.call_text("keyboard_type", {"text": ascii_text})
                await _wait_for_keyboard_text(client, ascii_text)
                await client.call_text("keyboard_type_unicode", {"text": unicode_text})
                expected_text = ascii_text + unicode_text
                await _wait_for_keyboard_text(client, expected_text)
                evidence.append(f"keyboard_text: {expected_text}")
                await _capture(client, visual.artifact_dir, "interaction-keyboard.png")

                zoom_rect = await _global_element_rect(client, "Zoom Target")
                zoom_x, zoom_y = _center(zoom_rect)
                zoom_center = (zoom_x, zoom_y)
                long_press = await _mark_long_press(client, zoom_center)
                long_press_at = _point_field(long_press, "at")
                expected_zoom_local = (zoom_rect[2] // 2, zoom_rect[3] // 2)
                assert all(
                    abs(actual - expected) <= 4
                    for actual, expected in zip(
                        long_press_at,
                        expected_zoom_local,
                        strict=True,
                    )
                ), long_press
                evidence.append(long_press)
                await _capture(
                    client,
                    visual.artifact_dir,
                    "interaction-touch-hold.png",
                )

                pinch_distance = min(160, zoom_rect[2] - 40)
                assert pinch_distance >= 100, zoom_rect
                await client.call_text(
                    "touch_pinch",
                    {
                        "center_x": zoom_x,
                        "center_y": zoom_y,
                        "start_distance": pinch_distance,
                        "end_distance": 60,
                        "duration_ms": 500,
                    },
                )
                pinch_in = await _wait_for_status(
                    client,
                    "zoom_status",
                    lambda status: (
                        _field(status, "phase") == "completed"
                        and _float_field(status, "scale") < 0.8
                    ),
                    "pinch-in did not produce a completed zoom-out scale",
                )
                evidence.append(pinch_in)
                await _capture(client, visual.artifact_dir, "interaction-pinch-in.png")

                await client.call_text(
                    "touch_pinch",
                    {
                        "center_x": zoom_x,
                        "center_y": zoom_y,
                        "start_distance": 60,
                        "end_distance": pinch_distance,
                        "duration_ms": 500,
                    },
                )
                pinch_out = await _wait_for_status(
                    client,
                    "zoom_status",
                    lambda status: (
                        _field(status, "phase") == "completed"
                        and _float_field(status, "scale") > 1.2
                    ),
                    "pinch-out did not produce a completed zoom-in scale",
                )
                evidence.append(pinch_out)
                await _capture(client, visual.artifact_dir, "interaction-pinch-out.png")

                touch_from = (
                    drag_x + drag_width // 3,
                    drag_y + drag_height // 2,
                )
                touch_to = (
                    drag_x + drag_width * 2 // 3,
                    drag_y + drag_height // 2,
                )
                await client.call_text(
                    "touch_multi_swipe",
                    {
                        "from_x": touch_from[0],
                        "from_y": touch_from[1],
                        "to_x": touch_to[0],
                        "to_y": touch_to[1],
                        "fingers": 2,
                        "duration_ms": 400,
                    },
                )
                two_finger = await _wait_for_status(
                    client,
                    "drag_status",
                    lambda status: (
                        _field(status, "source") == "touch"
                        and _field(status, "press") == "touch"
                        and _field(status, "release") in {"cancel", "touch"}
                        and _field(status, "modifiers") == "none"
                        and _integer_field(status, "fingers") == 2
                        and _integer_field(status, "motions") >= 20
                        and _bounds_field(status)[2] - _bounds_field(status)[0] >= drag_width // 4
                        and _bounds_field(status)[3] - _bounds_field(status)[1] >= 16
                    ),
                    "2-finger swipe did not reach the probe with both touch sequences",
                )
                evidence.append(f"2-finger: {two_finger}")
                await _capture(
                    client,
                    visual.artifact_dir,
                    "interaction-2-finger.png",
                )

                for fingers in (3, 4, 5):
                    marker = await _status(client, "drag_status")
                    swipe_output = await client.call_text(
                        "touch_multi_swipe",
                        {
                            "from_x": touch_from[0],
                            "from_y": touch_from[1],
                            "to_x": touch_to[0],
                            "to_y": touch_to[1],
                            "fingers": fingers,
                            "duration_ms": 400,
                        },
                    )
                    assert swipe_output == (
                        f"{fingers}-finger swipe from ({touch_from[0]}, {touch_from[1]}) "
                        f"to ({touch_to[0]}, {touch_to[1]}) in 400ms"
                    )
                    await anyio.sleep(0.5)
                    after_swipe = await _status(client, "drag_status")
                    observed_fingers = _integer_field(after_swipe, "fingers")
                    if after_swipe == marker:
                        outcome = "suppressed"
                    else:
                        assert _field(after_swipe, "source") == "touch", after_swipe
                        assert _field(after_swipe, "press") == "touch", after_swipe
                        release = _field(after_swipe, "release")
                        assert release in {"cancel", "touch"}, after_swipe
                        if release == "cancel":
                            assert 1 <= observed_fingers <= fingers, after_swipe
                            outcome = "canceled"
                        else:
                            assert observed_fingers == fingers, after_swipe
                            bounds = _bounds_field(after_swipe)
                            assert _integer_field(after_swipe, "motions") >= 20, after_swipe
                            assert bounds[2] - bounds[0] >= drag_width // 4, after_swipe
                            assert bounds[3] - bounds[1] >= 16, after_swipe
                            outcome = "delivered"
                    evidence.append(
                        f"{fingers}-finger {outcome} "
                        f"(requested={fingers}, observed={observed_fingers}): {after_swipe}"
                    )
                    await _capture(
                        client,
                        visual.artifact_dir,
                        f"interaction-{fingers}-finger.png",
                    )

                final_tree = await client.call_text(
                    "accessibility_tree", {"app_name": PROBE_SELECTOR}
                )
                assert expected_text in final_tree, final_tree[:4000]
                (visual.artifact_dir / "interaction-final-tree.txt").write_text(
                    final_tree,
                    encoding="utf-8",
                )
            finally:
                try:
                    (visual.artifact_dir / "interaction-observations.txt").write_text(
                        "\n".join(evidence) + ("\n" if evidence else ""),
                        encoding="utf-8",
                    )
                    (visual.artifact_dir / "mcp-server.stderr.log").write_text(
                        client.stderr_text(),
                        encoding="utf-8",
                    )
                    if app_log_path is not None and app_log_path.is_file():
                        shutil.copy2(
                            app_log_path,
                            visual.artifact_dir / "interaction-probe.log",
                        )
                finally:
                    if connected:
                        with contextlib.suppress(BaseException):
                            await client.call_result("session_stop")
