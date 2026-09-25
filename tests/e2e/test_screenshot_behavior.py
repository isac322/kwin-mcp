"""Installed-package screenshot behavior against real KWin backends."""

from __future__ import annotations

import ast
import os
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import pytest
from _asserts import coordinate_spaces, screenshot_path
from mcp_harness import running_mcp_server
from PIL import Image, ImageChops
from visual_harness import nested_visual_kwin

if TYPE_CHECKING:
    from mcp_harness import McpTestClient

SCREEN_SIZE = (1280, 800)
PROBE_COMMAND = "python3 /app/tests/e2e/interaction_probe.py"
PROBE_SELECTOR = "interaction_probe.py"
PROBE_TITLE = "Interaction Probe"
POLL_INTERVAL_SECONDS = 0.1
STATE_TIMEOUT_SECONDS = 5.0
GUI_PROBE_COMMAND = "python3 /app/tests/e2e/gui_probe.py"
GUI_PROBE_SELECTOR = "gui_probe.py"
# gui_probe.py paints its Animation Target button #1f5f9f. TARGET_WIDTH/HEIGHT
# are set_size_request minima: the homogeneous grid stretches the rendered
# button wider (observed ~432x84 logical), so only a minimum width is asserted.
ANIMATION_BLUE = (0x1F, 0x5F, 0x9F)
ANIMATION_TARGET_MIN = (320, 84)
ANIMATION_DONE = "animation_status: 12"
COLOR_TOLERANCE = 12
TARGET_SLACK_PX = 16
FRAME_SLACK_PX = 3
LOGICAL_SLACK_PX = 2
_RECT = r"\((-?\d+), (-?\d+), (\d+)x(\d+)\)"
_KEYBOARD_TARGET_LINE = re.compile(
    r'^\s*- \[[^]]+\] "Keyboard Target"(?:\s|$)',
    re.MULTILINE,
)
_FRAME_LINE = re.compile(
    r"^\s+(\d+)ms: (.+?) \((\d+(?:\.\d+)?) KB\)$",
    re.MULTILINE,
)


def _server_process_dir() -> Path:
    """Return the /proc directory for this test's MCP server child."""
    matches: list[Path] = []
    for process_dir in Path("/proc").iterdir():
        if not process_dir.name.isdigit():
            continue
        try:
            status = (process_dir / "status").read_text(encoding="utf-8")
            parent_line = next(line for line in status.splitlines() if line.startswith("PPid:"))
            parent_pid = int(parent_line.split(":", 1)[1])
            command = (process_dir / "cmdline").read_bytes().replace(b"\0", b" ")
        except (FileNotFoundError, PermissionError, ProcessLookupError, StopIteration):
            continue
        if parent_pid == os.getpid() and b"kwin-mcp" in command:
            matches.append(process_dir)

    assert len(matches) == 1, f"expected one kwin-mcp child, found {matches}"
    return matches[0]


def _process_resource_counts(process_dir: Path) -> tuple[int, int]:
    """Return the process's live thread and file-descriptor counts."""
    return (
        sum(1 for _ in (process_dir / "task").iterdir()),
        sum(1 for _ in (process_dir / "fd").iterdir()),
    )


@pytest.fixture
def anyio_backend() -> str:
    """The installed MCP client runs on asyncio."""
    return "asyncio"


def _result_text(result: object) -> str:
    content = getattr(result, "content", [])
    return "\n".join(str(getattr(block, "text", "")) for block in content)


def _single_rect(output: str, pattern: str) -> tuple[int, int, int, int]:
    matches = re.findall(pattern, output, re.MULTILINE)
    assert len(matches) == 1, output[:1500]
    x, y, width, height = matches[0]
    return int(x), int(y), int(width), int(height)


async def _global_element_rect(
    client: McpTestClient,
    name: str,
    app_name: str = PROBE_SELECTOR,
) -> tuple[int, int, int, int]:
    elements = await client.call_text(
        "find_ui_elements",
        {"query": name, "app_name": app_name},
    )
    # find_ui_elements already reports global screen coordinates.
    return _single_rect(
        elements,
        rf'^- \[[^]]+] "{re.escape(name)}" @ screen {_RECT}(?:\s|$)',
    )


def _center(rect: tuple[int, int, int, int]) -> tuple[int, int]:
    x, y, width, height = rect
    return x + width // 2, y + height // 2


def _screenshot_source(output: str) -> Path:
    source = screenshot_path(output)
    assert source.is_file() and source.stat().st_size > 0, output
    assert [(s.origin, s.size) for s in coordinate_spaces(output)] == [((0, 0), SCREEN_SIZE)], (
        output
    )
    return source


def _assert_png(path: Path, screen_size: tuple[int, int] = SCREEN_SIZE) -> None:
    with Image.open(path) as image:
        image.load()
        assert image.format == "PNG", path
        assert image.size == screen_size, (path, image.size)


def _preserve_png(source: Path, artifact_dir: Path, name: str) -> Path:
    destination = artifact_dir / name
    shutil.copy2(source, destination)
    _assert_png(destination)
    return destination


def _frame_sources(
    output: str, expected_delays: list[int], screen_size: tuple[int, int] = SCREEN_SIZE
) -> list[Path]:
    matches = _FRAME_LINE.findall(output)
    assert len(matches) == len(expected_delays), output
    assert [int(delay) for delay, _, _ in matches] == sorted(expected_delays), output
    spaces = coordinate_spaces(output)
    assert [(s.origin, s.size, s.coverage) for s in spaces] == [
        ((0, 0), screen_size, "full")
    ] * len(expected_delays), output
    sources = [Path(path_text) for _, path_text, _ in matches]
    assert len({source.parent for source in sources}) == 1, sources
    for source in sources:
        assert source.is_file() and source.stat().st_size > 0, source
        _assert_png(source, screen_size)
    return sources


def _preserve_frames(
    output: str,
    expected_delays: list[int],
    artifact_dir: Path,
    action_name: str,
) -> list[Path]:
    sources = _frame_sources(output, expected_delays)
    for index, source in enumerate(sources):
        _preserve_png(source, artifact_dir, f"{action_name}-{index:03d}.png")
    return sources


def _status_name(tree: str, prefix: str) -> str:
    matches = re.findall(rf'\[label] "({re.escape(prefix)}[^"]*)"', tree)
    assert len(matches) == 1, tree[:2000]
    return matches[0]


async def _wait_for_status_change(
    client: McpTestClient,
    prefix: str,
    previous: str,
    app_name: str = PROBE_SELECTOR,
) -> str:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    tree = ""
    while time.monotonic() < deadline:
        tree = await client.call_text("accessibility_tree", {"app_name": app_name})
        current = _status_name(tree, prefix)
        if current != previous:
            return current
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"{prefix} did not change from {previous!r}:\n{tree[:2000]}")


def _entry_text(tree: str) -> str:
    for line in tree.splitlines():
        if _KEYBOARD_TARGET_LINE.match(line) is None:
            continue
        match = re.search(r"\btext=(.+?)(?: \[actions:|$)", line)
        return ast.literal_eval(match.group(1)) if match is not None else ""
    raise AssertionError(f"Keyboard Target was absent from the accessibility tree:\n{tree[:2000]}")


async def _wait_for_entry_text(client: McpTestClient, expected: str) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    actual = ""
    tree = ""
    while time.monotonic() < deadline:
        tree = await client.call_text("accessibility_tree", {"app_name": PROBE_SELECTOR})
        actual = _entry_text(tree)
        if actual == expected:
            return
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    assert actual == expected, tree[:2000]


async def _connect(
    client: McpTestClient,
    dbus_address: str,
    wayland_display: str,
    *,
    keep_screenshots: bool,
) -> None:
    output = await client.call_text(
        "session_connect",
        {
            "dbus_address": dbus_address,
            "wayland_display": wayland_display,
            "keep_screenshots": keep_screenshots,
        },
    )
    assert output.startswith("Connected to live KWin session."), output
    assert "Input backend: KWin EIS" in output, output


async def _launch_probe(client: McpTestClient) -> Path:
    output = await client.call_text(
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
    assert f"App launched: {PROBE_COMMAND}" in output, output
    log_match = re.search(r"^App log: (.+)$", output, re.MULTILINE)
    assert log_match is not None, output
    waited = await client.call_text(
        "wait_for_element",
        {"query": "Keyboard Target", "app_name": PROBE_SELECTOR, "timeout_ms": 15_000},
    )
    assert _KEYBOARD_TARGET_LINE.search(waited) is not None, waited[:1500]
    tree = await client.call_text("accessibility_tree", {"app_name": PROBE_SELECTOR})
    assert f'[frame] "{PROBE_TITLE}"' in tree, tree[:2000]
    return Path(log_match.group(1))


def _preserve_backend_record(artifact_dir: Path) -> None:
    (artifact_dir / "capture-backend.txt").write_text(
        "Requested screenshot capture backend: X11 scrot\n",
        encoding="utf-8",
    )


async def _launch_gui_probe(client: McpTestClient) -> None:
    output = await client.call_text(
        "launch_app",
        {
            "command": GUI_PROBE_COMMAND,
            "env": {
                "GDK_BACKEND": "wayland",
                "GTK_MODULES": "gail:atk-bridge",
                "NO_AT_BRIDGE": "0",
                "XDG_SESSION_TYPE": "wayland",
            },
        },
    )
    assert f"App launched: {GUI_PROBE_COMMAND}" in output, output
    waited = await client.call_text(
        "wait_for_element",
        {"query": "Animation Target", "app_name": GUI_PROBE_SELECTOR, "timeout_ms": 15_000},
    )
    assert '"Animation Target"' in waited, waited[:1500]


def _color_mask_bytes(path: Path, color: tuple[int, int, int]) -> tuple[tuple[int, int], bytes]:
    """Raw bytes of pixels within COLOR_TOLERANCE of ``color`` on every channel."""
    with Image.open(path) as image:
        bands = image.convert("RGB").split()
    masks = [
        band.point(lambda v, t=target: 255 if abs(v - t) <= COLOR_TOLERANCE else 0)
        for band, target in zip(bands, color, strict=True)
    ]
    mask = ImageChops.multiply(ImageChops.multiply(masks[0], masks[1]), masks[2])
    return mask.size, mask.tobytes()


def _largest_color_region(
    path: Path, color: tuple[int, int, int]
) -> tuple[int, int, int, int] | None:
    """Bounding box of the largest connected region of pixels matching ``color``.

    A global colour bbox is contaminated by tolerance hits on window edges and
    glyph specks unrelated to the widget (observed: 1px decoration lines and the
    Unicode/Tofu row widened the bbox far beyond the button, shifting the click
    onto a non-target). Flood-filling 4-connected components keeps only the
    solid painted surface of the unique #1f5f9f button.
    """
    (width, height), data = _color_mask_bytes(path, color)
    remaining = bytearray(data)
    best_count = 0
    best_bbox: tuple[int, int, int, int] | None = None
    scan = 0
    while True:
        start = remaining.find(255, scan)
        if start < 0:
            return best_bbox
        stack = [start]
        remaining[start] = 0
        count = 0
        min_x = max_x = start % width
        min_y = max_y = start // width
        while stack:
            pixel = stack.pop()
            count += 1
            x, y = pixel % width, pixel // width
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            if y > 0 and remaining[pixel - width] == 255:
                remaining[pixel - width] = 0
                stack.append(pixel - width)
            if y + 1 < height and remaining[pixel + width] == 255:
                remaining[pixel + width] = 0
                stack.append(pixel + width)
            if x > 0 and remaining[pixel - 1] == 255:
                remaining[pixel - 1] = 0
                stack.append(pixel - 1)
            if x + 1 < width and remaining[pixel + 1] == 255:
                remaining[pixel + 1] = 0
                stack.append(pixel + 1)
        if count > best_count:
            best_count = count
            best_bbox = (min_x, min_y, max_x + 1, max_y + 1)


async def _screenshot_showing(
    client: McpTestClient, color: tuple[int, int, int], destination: Path
) -> tuple[str, tuple[int, int, int, int]]:
    """Take screenshots until the probe's colour is rendered; keep the last one."""
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    while True:
        output = await client.call_text("screenshot")
        shutil.copy2(screenshot_path(output), destination)
        bbox = _largest_color_region(destination, color)
        if bbox is not None:
            return output, bbox
        if time.monotonic() > deadline:
            raise AssertionError(f"{color} never appeared in {destination}: {output}")
        await anyio.sleep(POLL_INTERVAL_SECONDS)


async def _wait_for_status(
    client: McpTestClient, prefix: str, expected: str, app_name: str
) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    current = ""
    while time.monotonic() < deadline:
        tree = await client.call_text("accessibility_tree", {"app_name": app_name})
        current = _status_name(tree, prefix)
        if current == expected:
            return
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"{prefix} stayed {current!r}, expected {expected!r}")


@pytest.mark.anyio
@pytest.mark.visual
@pytest.mark.parametrize(
    ("scale", "screen_size"),
    [
        pytest.param(1.0, SCREEN_SIZE, id="scale-1.0"),
        # 1920x1200 at 1.45 leaves a 1324x828 logical workspace, large enough
        # for the 920x700 probe window.
        pytest.param(1.45, (1920, 1200), id="scale-1.45"),
    ],
)
async def test_x11_screenshot_pixels_are_logical_click_targets(
    scale: float, screen_size: tuple[int, int]
) -> None:
    """A widget found by its pixels in a screenshot is a logical click target (issue #44).

    At a fractional scale the capture holds device pixels on unfixed builds.
    Under X11 the legacy input path still mirrors raw physical pixels, so a
    pixel-derived click can accidentally succeed; the regression is therefore
    asserted by comparing rendered pixel size against logical geometry, not by
    requiring the click to miss.
    """
    expected_logical = (screen_size[0] / scale, screen_size[1] / scale)
    with nested_visual_kwin(scale=scale, screen_size=screen_size) as visual:
        _preserve_backend_record(visual.artifact_dir)
        async with running_mcp_server(
            env={"DISPLAY": visual.x_display, "KWIN_MCP_X11_SCREENSHOT": "1"},
        ) as client:
            connected = False
            try:
                await _connect(
                    client,
                    visual.dbus_address,
                    visual.wayland_display,
                    keep_screenshots=False,
                )
                connected = True
                await _launch_gui_probe(client)
                before = _status_name(
                    await client.call_text("accessibility_tree", {"app_name": GUI_PROBE_SELECTOR}),
                    "animation_status:",
                )

                shot = visual.artifact_dir / "scaled-before-click.png"
                output, bbox = await _screenshot_showing(client, ANIMATION_BLUE, shot)
                spaces = coordinate_spaces(output)
                # A build without the coordinate line gets the assumption callers
                # made before #44 (pixel == input coordinate). The pixel region
                # is never derived from AT-SPI geometry: a wrong origin/scale is
                # caught by the independent logical-geometry assertions below,
                # not assumed to misclick.
                origin = spaces[0].origin if spaces else (0, 0)
                click_x = origin[0] + (bbox[0] + bbox[2]) // 2
                click_y = origin[1] + (bbox[1] + bbox[3]) // 2
                await client.call_text("mouse_click", {"x": click_x, "y": click_y})
                await _wait_for_status_change(
                    client, "animation_status:", before, GUI_PROBE_SELECTOR
                )

                # The button keeps its logical size in the image: pixel pixels,
                # AT-SPI rect, and the CSS minimum all agree within slack. The
                # rendered width exceeds the 320 minimum (homogeneous grid), so
                # the width floor is a lower bound only.
                rect = await _global_element_rect(client, "Animation Target", GUI_PROBE_SELECTOR)
                rendered = (bbox[2] - bbox[0], bbox[3] - bbox[1])
                assert rendered[0] >= ANIMATION_TARGET_MIN[0] - TARGET_SLACK_PX, bbox
                assert abs(rendered[1] - ANIMATION_TARGET_MIN[1]) <= TARGET_SLACK_PX, bbox
                assert abs(rendered[0] - rect[2]) <= TARGET_SLACK_PX, (bbox, rect)
                assert abs(rendered[1] - rect[3]) <= TARGET_SLACK_PX, (bbox, rect)
                assert rect[0] <= click_x < rect[0] + rect[2], (rect, click_x)
                assert rect[1] <= click_y < rect[1] + rect[3], (rect, click_y)
                await _wait_for_status(
                    client, "animation_status:", ANIMATION_DONE, GUI_PROBE_SELECTOR
                )

                # #44 contract: the screenshot names one logical space whose
                # origin, backend, coverage, and size drive the mapping.
                assert len(spaces) == 1, output
                space = spaces[0]
                assert (space.origin, space.backend, space.coverage) == (
                    (0, 0),
                    "x11-scrot",
                    "full",
                ), output
                assert all(
                    abs(actual - expected) <= LOGICAL_SLACK_PX
                    for actual, expected in zip(space.size, expected_logical, strict=True)
                ), (space, expected_logical)
                with Image.open(shot) as image:
                    assert image.size == space.size, (image.size, space)

                # Frame bursts share the mapping: same logical size, button in place.
                delays = [0, 200, 900]
                burst = await client.call_text(
                    "mouse_click",
                    {"x": click_x, "y": click_y, "screenshot_after_ms": delays},
                )
                frames = _frame_sources(burst, delays, space.size)
                for index, frame in enumerate(frames):
                    preserved = visual.artifact_dir / f"scaled-frame-{index:03d}.png"
                    shutil.copy2(frame, preserved)
                    frame_bbox = _largest_color_region(preserved, ANIMATION_BLUE)
                    assert frame_bbox is not None, (index, burst)
                    assert all(
                        abs(a - b) <= FRAME_SLACK_PX for a, b in zip(frame_bbox, bbox, strict=True)
                    ), (index, frame_bbox, bbox)
                await _wait_for_status(
                    client, "animation_status:", ANIMATION_DONE, GUI_PROBE_SELECTOR
                )

                # The mirrored X11 cursor lands on the logical point it was moved to.
                await client.call_text("mouse_move", {"x": click_x, "y": click_y})
                without_cursor = visual.artifact_dir / "scaled-without-cursor.png"
                with_cursor = visual.artifact_dir / "scaled-with-cursor.png"
                shutil.copy2(
                    screenshot_path(
                        await client.call_text("screenshot", {"include_cursor": False})
                    ),
                    without_cursor,
                )
                shutil.copy2(
                    screenshot_path(await client.call_text("screenshot", {"include_cursor": True})),
                    with_cursor,
                )
                with Image.open(without_cursor) as first, Image.open(with_cursor) as second:
                    difference = ImageChops.difference(first.convert("RGB"), second.convert("RGB"))
                red, green, blue = difference.split()
                mask = ImageChops.lighter(ImageChops.lighter(red, green), blue)
                image_x, image_y = click_x - origin[0], click_y - origin[1]
                radius = 40
                near = mask.crop(
                    (image_x - radius, image_y - radius, image_x + radius, image_y + radius)
                )
                near_changed = sum(near.histogram()[11:])
                total_changed = sum(mask.histogram()[11:])
                assert near_changed >= 20, (near_changed, total_changed)
                assert total_changed - near_changed <= max(10, total_changed // 50), (
                    near_changed,
                    total_changed,
                )
            finally:
                (visual.artifact_dir / "mcp-server.stderr.log").write_text(
                    client.stderr_text(), encoding="utf-8"
                )
                if connected:
                    stop_output = await client.call_text("session_stop")
                    assert stop_output == "Disconnected from live session.", stop_output


@pytest.mark.anyio
@pytest.mark.visual
async def test_x11_screenshots_cover_cursor_and_action_frame_paths() -> None:
    """Cursor pixels and each frame-producing input family cross installed MCP stdio."""
    with nested_visual_kwin() as visual:
        _preserve_backend_record(visual.artifact_dir)
        async with running_mcp_server(
            env={"DISPLAY": visual.x_display, "KWIN_MCP_X11_SCREENSHOT": "1"},
        ) as client:
            connected = False
            screenshot_dir: Path | None = None
            try:
                await _connect(
                    client,
                    visual.dbus_address,
                    visual.wayland_display,
                    keep_screenshots=False,
                )
                connected = True
                await _launch_probe(client)

                click_rect = await _global_element_rect(client, "Click Target")
                keyboard_rect = await _global_element_rect(client, "Keyboard Target")
                zoom_rect = await _global_element_rect(client, "Zoom Target")
                drag_rect = await _global_element_rect(client, "Drag Target")
                click_x, click_y = _center(click_rect)
                keyboard_x, keyboard_y = _center(keyboard_rect)
                zoom_x, zoom_y = _center(zoom_rect)

                await client.call_text("mouse_move", {"x": click_x, "y": click_y})
                without_cursor_source = _screenshot_source(
                    await client.call_text("screenshot", {"include_cursor": False})
                )
                screenshot_dir = without_cursor_source.parent
                without_cursor = _preserve_png(
                    without_cursor_source,
                    visual.artifact_dir,
                    "pointer-without-cursor.png",
                )
                with_cursor_source = _screenshot_source(
                    await client.call_text("screenshot", {"include_cursor": True})
                )
                assert with_cursor_source.parent == screenshot_dir
                with_cursor = _preserve_png(
                    with_cursor_source,
                    visual.artifact_dir,
                    "pointer-with-cursor.png",
                )
                with (
                    Image.open(without_cursor) as first_image,
                    Image.open(with_cursor) as second_image,
                ):
                    difference = ImageChops.difference(
                        first_image.convert("RGB"),
                        second_image.convert("RGB"),
                    )
                    red, green, blue = difference.split()
                    mask = ImageChops.lighter(ImageChops.lighter(red, green), blue)
                    radius = 48
                    nearby = mask.crop(
                        (
                            max(0, click_x - radius),
                            max(0, click_y - radius),
                            min(SCREEN_SIZE[0], click_x + radius),
                            min(SCREEN_SIZE[1], click_y + radius),
                        )
                    )
                    near_changed = sum(nearby.histogram()[11:])
                    total_changed = sum(mask.histogram()[11:])
                    keyboard_target = mask.crop(
                        (
                            keyboard_rect[0],
                            keyboard_rect[1],
                            keyboard_rect[0] + keyboard_rect[2],
                            keyboard_rect[1] + keyboard_rect[3],
                        )
                    )
                    keyboard_target_changed = sum(keyboard_target.histogram()[11:])
                    far_changed = total_changed - near_changed - keyboard_target_changed
                    assert near_changed >= 20, (near_changed, total_changed)
                    assert far_changed <= max(10, total_changed // 50), (
                        near_changed,
                        total_changed,
                        keyboard_target_changed,
                        far_changed,
                    )

                initial_tree = await client.call_text(
                    "accessibility_tree", {"app_name": PROBE_SELECTOR}
                )
                click_before = _status_name(initial_tree, "click_status:")
                click_output = await client.call_text(
                    "mouse_click",
                    {"x": click_x, "y": click_y, "screenshot_after_ms": [0]},
                )
                click_sources = _preserve_frames(
                    click_output, [0], visual.artifact_dir, "mouse-click-frame"
                )
                assert click_sources[0].parent == screenshot_dir
                await _wait_for_status_change(client, "click_status:", click_before)

                focus_output = await client.call_text(
                    "mouse_click", {"x": keyboard_x, "y": keyboard_y}
                )
                assert focus_output == f"Clicked left at ({keyboard_x}, {keyboard_y})", focus_output
                keyboard_output = await client.call_text(
                    "keyboard_type",
                    {"text": "frame-path", "screenshot_after_ms": [25]},
                )
                keyboard_sources = _preserve_frames(
                    keyboard_output, [25], visual.artifact_dir, "keyboard-type-frame"
                )
                assert keyboard_sources[0].parent == screenshot_dir
                await _wait_for_entry_text(client, "frame-path")

                zoom_before = _status_name(
                    await client.call_text("accessibility_tree", {"app_name": PROBE_SELECTOR}),
                    "zoom_status:",
                )
                touch_output = await client.call_text(
                    "touch_tap",
                    {
                        "x": zoom_x,
                        "y": zoom_y,
                        "hold_ms": 800,
                        "screenshot_after_ms": [50],
                    },
                )
                touch_sources = _preserve_frames(
                    touch_output, [50], visual.artifact_dir, "touch-tap-frame"
                )
                assert touch_sources[0].parent == screenshot_dir
                await _wait_for_status_change(client, "zoom_status:", zoom_before)

                drag_before = _status_name(
                    await client.call_text("accessibility_tree", {"app_name": PROBE_SELECTOR}),
                    "drag_status:",
                )
                drag_x, drag_y, drag_width, drag_height = drag_rect
                drag_from = (drag_x + drag_width // 4, drag_y + drag_height // 2)
                drag_to = (drag_x + 3 * drag_width // 4, drag_y + drag_height // 2)
                drag_output = await client.call_text(
                    "mouse_drag",
                    {
                        "from_x": drag_from[0],
                        "from_y": drag_from[1],
                        "to_x": drag_to[0],
                        "to_y": drag_to[1],
                        "screenshot_after_ms": [75],
                    },
                )
                drag_sources = _preserve_frames(
                    drag_output, [75], visual.artifact_dir, "mouse-drag-frame"
                )
                assert drag_sources[0].parent == screenshot_dir
                await _wait_for_status_change(client, "drag_status:", drag_before)
            finally:
                (visual.artifact_dir / "mcp-server.stderr.log").write_text(
                    client.stderr_text(), encoding="utf-8"
                )
                if connected:
                    stop_output = await client.call_text("session_stop")
                    assert stop_output == "Disconnected from live session.", stop_output

            assert screenshot_dir is not None
            assert not screenshot_dir.exists(), screenshot_dir


@pytest.mark.anyio
@pytest.mark.visual
async def test_live_screenshot_cleanup_respects_keep_screenshots() -> None:
    """Generated files disappear by default and survive stop only when requested."""
    with nested_visual_kwin() as visual:
        _preserve_backend_record(visual.artifact_dir)
        async with running_mcp_server(
            env={"DISPLAY": visual.x_display, "KWIN_MCP_X11_SCREENSHOT": "1"},
        ) as client:
            connected = False
            kept_dir: Path | None = None
            try:
                await _connect(
                    client,
                    visual.dbus_address,
                    visual.wayland_display,
                    keep_screenshots=False,
                )
                connected = True
                removed_source = _screenshot_source(await client.call_text("screenshot"))
                removed_dir = removed_source.parent
                _preserve_png(
                    removed_source,
                    visual.artifact_dir,
                    "keep-false-before-stop.png",
                )
                assert await client.call_text("session_stop") == "Disconnected from live session."
                connected = False
                assert not removed_source.exists(), removed_source
                assert not removed_dir.exists(), removed_dir

                await _connect(
                    client,
                    visual.dbus_address,
                    visual.wayland_display,
                    keep_screenshots=True,
                )
                connected = True
                kept_source = _screenshot_source(await client.call_text("screenshot"))
                kept_dir = kept_source.parent
                assert await client.call_text("session_stop") == "Disconnected from live session."
                connected = False
                assert kept_source.is_file() and kept_source.stat().st_size > 0, kept_source
                assert kept_dir.is_dir(), kept_dir
                _preserve_png(
                    kept_source,
                    visual.artifact_dir,
                    "keep-true-after-stop.png",
                )
            finally:
                (visual.artifact_dir / "mcp-server.stderr.log").write_text(
                    client.stderr_text(), encoding="utf-8"
                )
                if connected:
                    await client.call_result("session_stop")
                if kept_dir is not None:
                    shutil.rmtree(kept_dir, ignore_errors=True)

            assert kept_dir is not None and not kept_dir.exists()


@pytest.mark.anyio
async def test_virtual_screenshot_failures_name_each_attempt_and_server_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated headless failures retain diagnostics without leaking server resources."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("KWIN_MCP_X11_SCREENSHOT", raising=False)

    async with running_mcp_server() as client:
        session_running = False
        try:
            start = await client.call_text(
                "session_start",
                {"screen_width": SCREEN_SIZE[0], "screen_height": SCREEN_SIZE[1]},
            )
            session_running = True
            assert "Session started." in start, start
            assert "Input backend: KWin EIS" in start, start

            server_process = _server_process_dir()
            baseline_threads, baseline_fds = _process_resource_counts(server_process)
            resource_samples: list[tuple[int, int]] = []

            for _ in range(3):
                screenshot_result = await client.call_result("screenshot")
                screenshot_error = _result_text(screenshot_result)
                assert screenshot_result.isError is True, screenshot_error
                assert "Screenshot capture failed" in screenshot_error, screenshot_error
                assert "ScreenShot2" in screenshot_error, screenshot_error
                assert "Spectacle" in screenshot_error, screenshot_error
                resource_samples.append(_process_resource_counts(server_process))

            burst_result = await client.call_result(
                "mouse_move",
                {
                    "x": SCREEN_SIZE[0] // 2,
                    "y": SCREEN_SIZE[1] // 2,
                    "screenshot_after_ms": [0],
                },
            )
            burst_error = _result_text(burst_result)
            assert burst_result.isError is True, burst_error
            assert "Frame burst capture failed" in burst_error, burst_error
            assert "ScreenShot2" in burst_error, burst_error
            assert "Spectacle" in burst_error, burst_error
            resource_samples.append(_process_resource_counts(server_process))

            thread_counts = [count[0] for count in resource_samples]
            fd_counts = [count[1] for count in resource_samples]
            assert max(thread_counts) <= baseline_threads + 2, (
                baseline_threads,
                thread_counts,
            )
            assert max(fd_counts) <= baseline_fds + 2, (baseline_fds, fd_counts)

            assert await client.session.send_ping() is not None
            valid_output = await client.call_text(
                "mouse_move", {"x": SCREEN_SIZE[0] // 2, "y": SCREEN_SIZE[1] // 2}
            )
            assert valid_output.startswith("Mouse moved to"), valid_output
        finally:
            if session_running:
                assert await client.call_text("session_stop") == "Session stopped."
