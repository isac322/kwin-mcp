"""screen_capture pixels against real KWin backends: nested X11, virtual, Spectacle stubs.

Ported from the 0.11 ``test_screenshot_behavior.py`` (ledger row 32). Every capture is
asserted structurally as a ``Frame`` (path, uri, width, height, mapping, coverage,
size_kb); the coordinate-space line is still parsed from the text (F143). The container's
virtual KWin never answers ScreenShot2, so pixel tests run against the nested X11 KWin of
``visual_harness`` with ``KWIN_MCP_X11_SCREENSHOT=1`` (the 0.11 mechanism).
"""

from __future__ import annotations

import io
import os
import re
import shutil
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from _asserts import CoordinateSpace, coordinate_spaces, frame_png
from kwin_oracle import KWinOracle
from mcp.types import ImageContent, TextContent
from mcp_harness import images, running_mcp_server, text_of
from PIL import Image, ImageChops
from probe_session import PROBE_APP, PROBE_COMMAND, PROBE_ENV, launch_probe
from visual_harness import NestedVisualKWin, nested_visual_kwin

from kwin_mcp.models import Frame, ImageMapping, Point, Rect

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

    from mcp_harness import McpTestClient

    from kwin_mcp.core import AutomationEngine

E2E_DIR = Path(__file__).resolve().parent
SCREEN_SIZE = (1280, 800)
POLL_INTERVAL_SECONDS = 0.1
STATE_TIMEOUT_SECONDS = 5.0
APP_WAIT_TIMEOUT_MS = 15_000
GUI_PROBE_COMMAND = f"python3 {E2E_DIR / 'gui_probe.py'}"
GUI_PROBE_APP = "gui_probe.py"
# gui_probe.py paints its Animation Target button #1f5f9f, TARGET_WIDTH/HEIGHT
# are set_size_request minima: the homogeneous grid stretches the rendered
# button wider (observed ~432x84 logical), so only a minimum width is asserted.
ANIMATION_BLUE = (0x1F, 0x5F, 0x9F)
ANIMATION_TARGET_MIN = (320, 84)
ANIMATION_DONE = "animation_status: 12"
COLOR_TOLERANCE = 12
TARGET_SLACK_PX = 16
FRAME_SLACK_PX = 3
LOGICAL_SLACK_PX = 2
X11_BACKEND = "x11-scrot"


@pytest.fixture
def anyio_backend() -> str:
    """The installed MCP client runs on asyncio."""
    return "asyncio"


# ── pixels ───────────────────────────────────────────────────────────────


def color_mask_bytes(path: Path, color: tuple[int, int, int]) -> tuple[tuple[int, int], bytes]:
    """Raw bytes of pixels within COLOR_TOLERANCE of ``color`` on every channel."""
    with Image.open(path) as image:
        bands = image.convert("RGB").split()
    masks = [
        band.point(lambda v, t=target: 255 if abs(v - t) <= COLOR_TOLERANCE else 0)
        for band, target in zip(bands, color, strict=True)
    ]
    mask = ImageChops.multiply(ImageChops.multiply(masks[0], masks[1]), masks[2])
    return mask.size, mask.tobytes()


def largest_color_region(
    path: Path, color: tuple[int, int, int]
) -> tuple[int, int, int, int] | None:
    """Bounding box (left, top, right, bottom) of the largest 4-connected ``color`` region.

    A global colour bbox is contaminated by tolerance hits on window edges and glyph
    specks unrelated to the widget; flood-filling keeps only the solid painted surface.
    """
    (width, height), data = color_mask_bytes(path, color)
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
        scan = start + 1


def changed_pixels(first: Path, second: Path) -> Image.Image:
    """Mask (L mode) of pixels whose RGB differs between two equally sized PNGs."""
    with Image.open(first) as a, Image.open(second) as b:
        difference = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    red, green, blue = difference.split()
    return ImageChops.lighter(ImageChops.lighter(red, green), blue)


def count_changed(mask: Image.Image, box: tuple[int, int, int, int] | None = None) -> int:
    """Pixels of ``mask`` (inside ``box`` when given) that changed by more than 10."""
    region = mask if box is None else mask.crop(box)
    return sum(region.histogram()[11:])


def preserve_png(source: Path, artifact_dir: Path, name: str) -> Path:
    destination = artifact_dir / name
    shutil.copy2(source, destination)
    return destination


def png_size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        return image.size


_IMAGE_TERM = re.compile(
    r"image (\d+)x(\d+) downscaled: pixel \(px, py\) shows "
    r"\((-?\d+) \+ \(px \+ 0\.5\) \* (\d+) / (\d+) - 0\.5, "
    r"(-?\d+) \+ \(py \+ 0\.5\) \* (\d+) / (\d+) - 0\.5\)"
)


def image_term(line: str) -> tuple[tuple[int, int], tuple[float, float]]:
    """Image pixel size and per-axis logical-per-pixel factors stated by ``line``."""
    match = _IMAGE_TERM.search(line)
    assert match is not None, line
    iw, ih, _, width, iw2, _, height, ih2 = (int(group) for group in match.groups())
    assert (iw, ih) == (iw2, ih2), line
    return (iw, ih), (width / iw, height / ih)


def assert_full_frame(frame: Frame, size: tuple[int, int]) -> Path:
    """A full-resolution workspace frame: logical size, origin 0,0, scale 1, full coverage."""
    path = frame_png(frame)
    assert (frame.width, frame.height) == size, frame
    assert frame.mapping == ImageMapping(scale=1.0, origin=Point(x=0, y=0)), frame
    assert frame.coverage == "full", frame
    assert frame.uri == f"kwin://screenshots/{path.name}", frame
    assert frame.size_kb == round(path.stat().st_size / 1024, 1), frame
    return path


# ── MCP calls ─────────────────────────────────────────────────────────────


def x11_env(visual: NestedVisualKWin) -> dict[str, str]:
    return {"DISPLAY": visual.x_display, "KWIN_MCP_X11_SCREENSHOT": "1"}


async def connect(
    client: McpTestClient, visual: NestedVisualKWin, *, retention: dict[str, bool] | None = None
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "dbus_address": visual.dbus_address,
        "wayland_display": visual.wayland_display,
    }
    if retention is not None:
        arguments["retention"] = retention
    info, _ = await client.call_structured("session_connect", arguments)
    assert info["kind"] == "live", info
    assert info["input_backend"] == "eis", info
    return info


async def stop_live(client: McpTestClient) -> dict[str, Any]:
    stopped, text = await client.call_structured("session_stop")
    assert stopped["stopped"] is True, stopped
    assert text.splitlines()[0] == "Disconnected from live session.", text
    return stopped


async def capture(
    client: McpTestClient, arguments: dict[str, Any] | None = None
) -> tuple[Frame, str]:
    structured, text = await client.call_structured("screen_capture", arguments or {})
    return Frame.model_validate(structured), text


async def launch(client: McpTestClient, command: str) -> None:
    launched, _ = await client.call_structured(
        "app_launch", {"command": command, "env": dict(PROBE_ENV)}
    )
    assert launched["command"] == command, launched


async def wait_element(client: McpTestClient, app: str, name: str) -> dict[str, Any]:
    """The one element of ``app`` named exactly ``name``, waiting for it to appear."""
    found, text = await client.call_structured(
        "element_query",
        {
            "locator": {"app": app, "query": name},
            "wait": {"timeout_ms": APP_WAIT_TIMEOUT_MS},
        },
    )
    assert found["wait"]["satisfied"] is True, text[:2000]
    exact = [e for e in found["elements"] if e["name"] == name]
    assert len(exact) == 1, text[:2000]
    return exact[0]


async def element_rect(client: McpTestClient, app: str, name: str) -> tuple[int, int, int, int]:
    rect = (await wait_element(client, app, name))["rect"]
    assert rect is not None, name
    return rect["x"], rect["y"], rect["width"], rect["height"]


def center(rect: tuple[int, int, int, int]) -> tuple[int, int]:
    x, y, width, height = rect
    return x + width // 2, y + height // 2


async def status(client: McpTestClient, app: str, prefix: str) -> str:
    found, text = await client.call_structured(
        "element_query", {"locator": {"app": app, "query": prefix}}
    )
    names = [e["name"] for e in found["elements"] if e["name"].startswith(prefix)]
    assert len(names) == 1, text[:2000]
    return names[0]


async def wait_status_change(client: McpTestClient, app: str, prefix: str, previous: str) -> str:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    current = previous
    while time.monotonic() < deadline:
        current = await status(client, app, prefix)
        if current != previous:
            return current
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"{prefix} did not change from {previous!r}")


async def wait_status(client: McpTestClient, app: str, prefix: str, expected: str) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    current = ""
    while time.monotonic() < deadline:
        current = await status(client, app, prefix)
        if current == expected:
            return
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"{prefix} stayed {current!r}, expected {expected!r}")


async def launch_gui_probe(client: McpTestClient) -> None:
    await launch(client, GUI_PROBE_COMMAND)
    await wait_element(client, GUI_PROBE_APP, "Animation Target")


async def blue_in(
    client: McpTestClient, arguments: dict[str, Any], destination: Path
) -> tuple[Frame, str, tuple[int, int, int, int]]:
    """Capture with ``arguments`` until the probe's blue button is drawn; keep the last one."""
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    while True:
        frame, text = await capture(client, arguments)
        shutil.copy2(frame_png(frame), destination)
        bbox = largest_color_region(destination, ANIMATION_BLUE)
        if bbox is not None:
            return frame, text, bbox
        if time.monotonic() > deadline:
            raise AssertionError(f"{ANIMATION_BLUE} never appeared in {destination}: {text}")
        await anyio.sleep(POLL_INTERVAL_SECONDS)


def action_frames(structured: dict[str, Any]) -> list[Frame]:
    return [Frame.model_validate(frame) for frame in structured["frames"]]


def burst_sources(
    frames: list[Frame], text: str, delays: list[int], size: tuple[int, int]
) -> list[Path]:
    """Frames of one burst: sorted delays, the full logical mapping, one directory."""
    assert [frame.delay_ms for frame in frames] == sorted(delays), frames
    assert coordinate_spaces(text) == [CoordinateSpace((0, 0), size, X11_BACKEND, "full")] * len(
        delays
    ), text
    sources = [assert_full_frame(frame, size) for frame in frames]
    assert len({source.parent for source in sources}) == 1, sources
    return sources


def write_stderr(visual: NestedVisualKWin, client: McpTestClient) -> None:
    (visual.artifact_dir / "mcp-server.stderr.log").write_text(
        client.stderr_text(), encoding="utf-8"
    )


@asynccontextmanager
async def live_client(
    visual: NestedVisualKWin, *server_args: str, retention: dict[str, bool] | None = None
) -> AsyncGenerator[McpTestClient]:
    """An installed server connected to ``visual``; disconnected (and checked) on exit."""
    async with running_mcp_server(*server_args, env=x11_env(visual)) as client:
        connected = False
        try:
            await connect(client, visual, retention=retention)
            connected = True
            yield client
        finally:
            write_stderr(visual, client)
            if connected:
                await stop_live(client)


# ── nested X11: pixels are logical click targets ─────────────────────────


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
async def test_x11_screen_capture_pixels_are_logical_click_targets(
    scale: float, screen_size: tuple[int, int]
) -> None:
    """A widget found by its pixels in a capture is a logical click target (issue #44).

    At a fractional scale the capture holds device pixels on unfixed builds. Under X11
    the legacy input path still mirrors raw physical pixels, so a pixel-derived click
    can accidentally succeed; the regression is therefore asserted by comparing
    rendered pixel size against logical geometry, not by requiring the click to miss.
    """
    expected_logical = (screen_size[0] / scale, screen_size[1] / scale)
    with nested_visual_kwin(scale=scale, screen_size=screen_size) as visual:
        async with live_client(visual) as client:
            await launch_gui_probe(client)
            before = await status(client, GUI_PROBE_APP, "animation_status:")
            shot = visual.artifact_dir / "scaled-before-click.png"
            frame, text, bbox = await blue_in(client, {}, shot)

            # The text line and the structured Frame state the same logical space.
            spaces = coordinate_spaces(text)
            assert len(spaces) == 1, text
            space = spaces[0]
            assert (space.origin, space.backend, space.coverage) == (
                (0, 0),
                X11_BACKEND,
                "full",
            ), text
            assert all(
                abs(actual - expected) <= LOGICAL_SLACK_PX
                for actual, expected in zip(space.size, expected_logical, strict=True)
            ), (space, expected_logical)
            assert_full_frame(frame, space.size)
            with Image.open(shot) as image:
                assert image.size == space.size, (image.size, space)

            # logical = origin + image_px / scale; nothing is assumed about the pixels.
            assert frame.mapping is not None
            origin = (frame.mapping.origin.x, frame.mapping.origin.y)
            click_x = origin[0] + round((bbox[0] + bbox[2]) / 2 / frame.mapping.scale)
            click_y = origin[1] + round((bbox[1] + bbox[3]) / 2 / frame.mapping.scale)
            clicked, _ = await client.call_structured(
                "pointer_click", {"target": {"x": click_x, "y": click_y}}
            )
            assert clicked["at"] == {"x": click_x, "y": click_y}, clicked
            await wait_status_change(client, GUI_PROBE_APP, "animation_status:", before)

            # The button keeps its logical size in the image: pixel size, AT-SPI rect
            # and the CSS minimum all agree within slack. The rendered width exceeds
            # the 320 minimum (homogeneous grid), so the width floor is a lower bound.
            rect = await element_rect(client, GUI_PROBE_APP, "Animation Target")
            rendered = (bbox[2] - bbox[0], bbox[3] - bbox[1])
            assert rendered[0] >= ANIMATION_TARGET_MIN[0] - TARGET_SLACK_PX, bbox
            assert abs(rendered[1] - ANIMATION_TARGET_MIN[1]) <= TARGET_SLACK_PX, bbox
            assert abs(rendered[0] - rect[2]) <= TARGET_SLACK_PX, (bbox, rect)
            assert abs(rendered[1] - rect[3]) <= TARGET_SLACK_PX, (bbox, rect)
            assert rect[0] <= click_x < rect[0] + rect[2], (rect, click_x)
            assert rect[1] <= click_y < rect[1] + rect[3], (rect, click_y)
            await wait_status(client, GUI_PROBE_APP, "animation_status:", ANIMATION_DONE)

            # Frame bursts share the mapping: same logical size, button in place.
            delays = [0, 200, 900]
            burst, burst_text = await client.call_structured(
                "pointer_click",
                {"target": {"x": click_x, "y": click_y}, "capture": {"delays_ms": delays}},
            )
            sources = burst_sources(action_frames(burst), burst_text, delays, space.size)
            for index, source in enumerate(sources):
                preserved = preserve_png(source, visual.artifact_dir, f"scaled-frame-{index}.png")
                frame_bbox = largest_color_region(preserved, ANIMATION_BLUE)
                assert frame_bbox is not None, (index, burst_text)
                assert all(
                    abs(a - b) <= FRAME_SLACK_PX for a, b in zip(frame_bbox, bbox, strict=True)
                ), (index, frame_bbox, bbox)
            await wait_status(client, GUI_PROBE_APP, "animation_status:", ANIMATION_DONE)

            # The mirrored X11 cursor lands on the logical point it was moved to.
            await client.call_structured("pointer_move", {"target": {"x": click_x, "y": click_y}})
            without_frame, _ = await capture(client, {"include_cursor": False})
            with_frame, _ = await capture(client, {"include_cursor": True})
            without_cursor = preserve_png(
                frame_png(without_frame), visual.artifact_dir, "scaled-without-cursor.png"
            )
            with_cursor = preserve_png(
                frame_png(with_frame), visual.artifact_dir, "scaled-with-cursor.png"
            )
            mask = changed_pixels(without_cursor, with_cursor)
            image_x, image_y = click_x - origin[0], click_y - origin[1]
            radius = 40
            near_changed = count_changed(
                mask, (image_x - radius, image_y - radius, image_x + radius, image_y + radius)
            )
            total_changed = count_changed(mask)
            assert near_changed >= 20, (near_changed, total_changed)
            assert total_changed - near_changed <= max(10, total_changed // 50), (
                near_changed,
                total_changed,
            )


@pytest.mark.anyio
@pytest.mark.visual
async def test_x11_screen_captures_cover_cursor_and_action_frame_paths() -> None:
    """Cursor pixels and each frame-producing input family cross installed MCP stdio."""
    with nested_visual_kwin() as visual:
        async with running_mcp_server(env=x11_env(visual)) as client:
            connected = False
            screenshot_dir: Path | None = None
            try:
                await connect(client, visual)
                connected = True
                await launch(client, PROBE_COMMAND)
                await wait_element(client, PROBE_APP, "Keyboard Target")
                click_rect = await element_rect(client, PROBE_APP, "Click Target")
                keyboard_rect = await element_rect(client, PROBE_APP, "Keyboard Target")
                zoom_rect = await element_rect(client, PROBE_APP, "Zoom Target")
                drag_rect = await element_rect(client, PROBE_APP, "Drag Target")
                click_x, click_y = center(click_rect)
                keyboard_x, keyboard_y = center(keyboard_rect)
                zoom_x, zoom_y = center(zoom_rect)

                await client.call_structured(
                    "pointer_move", {"target": {"x": click_x, "y": click_y}}
                )
                without_frame, _ = await capture(client, {"include_cursor": False})
                screenshot_dir = assert_full_frame(without_frame, SCREEN_SIZE).parent
                without_cursor = preserve_png(
                    Path(without_frame.path), visual.artifact_dir, "pointer-without-cursor.png"
                )
                with_frame, _ = await capture(client, {"include_cursor": True})
                assert Path(with_frame.path).parent == screenshot_dir
                with_cursor = preserve_png(
                    assert_full_frame(with_frame, SCREEN_SIZE),
                    visual.artifact_dir,
                    "pointer-with-cursor.png",
                )
                mask = changed_pixels(without_cursor, with_cursor)
                radius = 48
                near_changed = count_changed(
                    mask,
                    (
                        max(0, click_x - radius),
                        max(0, click_y - radius),
                        min(SCREEN_SIZE[0], click_x + radius),
                        min(SCREEN_SIZE[1], click_y + radius),
                    ),
                )
                total_changed = count_changed(mask)
                keyboard_changed = count_changed(
                    mask,
                    (
                        keyboard_rect[0],
                        keyboard_rect[1],
                        keyboard_rect[0] + keyboard_rect[2],
                        keyboard_rect[1] + keyboard_rect[3],
                    ),
                )
                far_changed = total_changed - near_changed - keyboard_changed
                assert near_changed >= 20, (near_changed, total_changed)
                assert far_changed <= max(10, total_changed // 50), (
                    near_changed,
                    total_changed,
                    keyboard_changed,
                    far_changed,
                )

                click_before = await status(client, PROBE_APP, "click_status:")
                clicked, click_text = await client.call_structured(
                    "pointer_click",
                    {"target": {"x": click_x, "y": click_y}, "capture": {"delays_ms": [0]}},
                )
                assert clicked["at"] == {"x": click_x, "y": click_y}, clicked
                click_sources = burst_sources(action_frames(clicked), click_text, [0], SCREEN_SIZE)
                preserve_png(click_sources[0], visual.artifact_dir, "mouse-click-frame-000.png")
                assert click_sources[0].parent == screenshot_dir
                await wait_status_change(client, PROBE_APP, "click_status:", click_before)

                focused, _ = await client.call_structured(
                    "pointer_click", {"target": {"x": keyboard_x, "y": keyboard_y}}
                )
                assert focused["at"] == {"x": keyboard_x, "y": keyboard_y}, focused
                typed, typed_text = await client.call_structured(
                    "keyboard_type", {"text": "frame-path", "capture": {"delays_ms": [25]}}
                )
                keyboard_sources = burst_sources(
                    action_frames(typed), typed_text, [25], SCREEN_SIZE
                )
                preserve_png(keyboard_sources[0], visual.artifact_dir, "keyboard-type-frame.png")
                assert keyboard_sources[0].parent == screenshot_dir
                await wait_entry_text(client, "frame-path")

                zoom_before = await status(client, PROBE_APP, "zoom_status:")
                tapped, tap_text = await client.call_structured(
                    "touch_tap",
                    {
                        "target": {"x": zoom_x, "y": zoom_y},
                        "hold_ms": 800,
                        "capture": {"delays_ms": [50]},
                    },
                )
                touch_sources = burst_sources(action_frames(tapped), tap_text, [50], SCREEN_SIZE)
                preserve_png(touch_sources[0], visual.artifact_dir, "touch-tap-frame.png")
                assert touch_sources[0].parent == screenshot_dir
                await wait_status_change(client, PROBE_APP, "zoom_status:", zoom_before)

                drag_before = await status(client, PROBE_APP, "drag_status:")
                drag_x, drag_y, drag_width, drag_height = drag_rect
                drag_from = {"x": drag_x + drag_width // 4, "y": drag_y + drag_height // 2}
                drag_to = {"x": drag_x + 3 * drag_width // 4, "y": drag_y + drag_height // 2}
                dragged, drag_text = await client.call_structured(
                    "pointer_drag",
                    {"from": drag_from, "to": drag_to, "capture": {"delays_ms": [75]}},
                )
                drag_sources = burst_sources(action_frames(dragged), drag_text, [75], SCREEN_SIZE)
                preserve_png(drag_sources[0], visual.artifact_dir, "mouse-drag-frame.png")
                assert drag_sources[0].parent == screenshot_dir
                await wait_status_change(client, PROBE_APP, "drag_status:", drag_before)
            finally:
                write_stderr(visual, client)
                if connected:
                    await stop_live(client)
            # Without retention the session's screenshot directory goes with it.
            assert screenshot_dir is not None
            assert not screenshot_dir.exists(), screenshot_dir


async def wait_entry_text(client: McpTestClient, expected: str) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    actual = ""
    while time.monotonic() < deadline:
        actual = (await wait_element(client, PROBE_APP, "Keyboard Target"))["text"]
        if actual == expected:
            return
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"Keyboard Target text {actual!r}, expected {expected!r}")


@pytest.mark.anyio
@pytest.mark.visual
async def test_live_screen_capture_cleanup_respects_keep_screen_captures() -> None:
    """Generated files disappear by default and survive stop only with retention.artifacts."""
    with nested_visual_kwin() as visual:
        async with running_mcp_server(env=x11_env(visual)) as client:
            connected = False
            kept_dir: Path | None = None
            try:
                info = await connect(client, visual)
                connected = True
                assert info["retention"] == {"artifacts": False, "home": False}, info
                removed, _ = await capture(client)
                removed_source = assert_full_frame(removed, SCREEN_SIZE)
                removed_dir = removed_source.parent
                preserve_png(removed_source, visual.artifact_dir, "keep-false-before-stop.png")
                stopped = await stop_live(client)
                connected = False
                assert stopped["kept"]["artifacts_dir"] is None, stopped
                assert not removed_source.exists(), removed_source
                assert not removed_dir.exists(), removed_dir

                info = await connect(client, visual, retention={"artifacts": True})
                connected = True
                assert info["retention"] == {"artifacts": True, "home": False}, info
                kept, _ = await capture(client)
                kept_source = assert_full_frame(kept, SCREEN_SIZE)
                kept_dir = kept_source.parent
                stopped, stop_text = await client.call_structured("session_stop")
                connected = False
                assert stopped["kept"]["artifacts_dir"] == str(kept_dir), stopped
                assert stop_text == (
                    f"Disconnected from live session.\nArtifacts kept: {kept_dir}"
                ), stop_text
                assert kept_source.is_file() and kept_source.stat().st_size > 0, kept_source
                assert kept_dir.is_dir(), kept_dir
                preserve_png(kept_source, visual.artifact_dir, "keep-true-after-stop.png")
            finally:
                write_stderr(visual, client)
                if connected:
                    await client.call_result("session_stop")
                if kept_dir is not None:
                    shutil.rmtree(kept_dir, ignore_errors=True)
            assert kept_dir is not None and not kept_dir.exists()


# ── virtual session: failures and the Spectacle fallback ─────────────────


def _server_process_dir() -> Path:
    """The /proc directory of this test's MCP server child."""
    matches: list[Path] = []
    for process_dir in Path("/proc").iterdir():
        if not process_dir.name.isdigit():
            continue
        try:
            status_text = (process_dir / "status").read_text(encoding="utf-8")
            parent_line = next(
                line for line in status_text.splitlines() if line.startswith("PPid:")
            )
            parent_pid = int(parent_line.split(":", 1)[1])
            command = (process_dir / "cmdline").read_bytes().replace(b"\0", b" ")
        except (FileNotFoundError, PermissionError, ProcessLookupError, StopIteration):
            continue
        if parent_pid == os.getpid() and b"kwin-mcp" in command:
            matches.append(process_dir)
    assert len(matches) == 1, f"expected one kwin-mcp child, found {matches}"
    return matches[0]


def _process_resource_counts(process_dir: Path) -> tuple[int, int]:
    """The process's live thread and file-descriptor counts."""
    return (
        sum(1 for _ in (process_dir / "task").iterdir()),
        sum(1 for _ in (process_dir / "fd").iterdir()),
    )


async def start_virtual(client: McpTestClient) -> None:
    info, _ = await client.call_structured(
        "session_start", {"screen_width": SCREEN_SIZE[0], "screen_height": SCREEN_SIZE[1]}
    )
    assert info["kind"] == "virtual", info
    assert info["input_backend"] == "eis", info


async def stop_virtual(client: McpTestClient) -> None:
    stopped, text = await client.call_structured("session_stop")
    assert stopped["stopped"] is True, stopped
    assert text.splitlines()[0] == "Session stopped.", text


async def assert_server_alive(client: McpTestClient) -> None:
    assert await client.session.send_ping() is not None
    moved, _ = await client.call_structured(
        "pointer_move", {"target": {"x": SCREEN_SIZE[0] // 2, "y": SCREEN_SIZE[1] // 2 + 3}}
    )
    assert moved["at"] == {"x": SCREEN_SIZE[0] // 2, "y": SCREEN_SIZE[1] // 2 + 3}, moved


@pytest.mark.anyio
async def test_virtual_screen_capture_failures_name_each_attempt_and_server_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated headless failures retain diagnostics without leaking server resources."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("KWIN_MCP_X11_SCREENSHOT", raising=False)

    async with running_mcp_server() as client:
        session_running = False
        try:
            await start_virtual(client)
            session_running = True

            server_process = _server_process_dir()
            baseline_threads, baseline_fds = _process_resource_counts(server_process)
            resource_samples: list[tuple[int, int]] = []

            for _ in range(3):
                error = "\n".join(await client.call_tool_error("screen_capture", lines=None))
                assert "Screenshot capture failed" in error, error
                assert "ScreenShot2" in error, error
                assert "Spectacle" in error, error
                resource_samples.append(_process_resource_counts(server_process))

                # An action's frames are taken after the action is committed: their
                # failure is a warning on a successful result, never an error.
                burst, burst_text = await client.call_structured(
                    "pointer_move",
                    {
                        "target": {"x": SCREEN_SIZE[0] // 2, "y": SCREEN_SIZE[1] // 2},
                        "capture": {"delays_ms": [0]},
                    },
                )
                assert burst["frames"] == [], burst
                assert len(burst["warnings"]) == 1, burst_text
                warning = burst["warnings"][0]
                assert warning.startswith(
                    "frame capture failed after the action: Frame burst capture failed"
                ), warning
                assert "ScreenShot2" in warning, warning
                assert "Spectacle" in warning, warning
                resource_samples.append(_process_resource_counts(server_process))

            thread_counts = [count[0] for count in resource_samples]
            fd_counts = [count[1] for count in resource_samples]
            assert max(thread_counts) <= baseline_threads + 2, (baseline_threads, thread_counts)
            assert max(fd_counts) <= baseline_fds + 2, (baseline_fds, fd_counts)
            await assert_server_alive(client)
        finally:
            if session_running:
                await stop_virtual(client)


# Issue #65: a real mixed-scale Spectacle capture under QEMU TCG completed
# successfully in 11.3 s while the fallback was killed at a fixed 10 s. The stub
# stands in for that slow but healthy Spectacle: it answers after this delay
# with a valid full-workspace PNG, exactly once per capture.
SLOW_SPECTACLE_SECONDS = 12.0
# Topology observations and the kill of a stuck capture fit well inside this.
STUCK_CAPTURE_SLACK_SECONDS = 10.0
_SLOW_SPECTACLE_STUB = """#!/usr/bin/env python3
import struct
import sys
import time
import zlib
from pathlib import Path

args = sys.argv[1:]
with Path(sys.argv[0] + ".calls").open("a", encoding="utf-8") as calls:
    calls.write(" ".join(args) + "\\n")
time.sleep({delay})
width, height = {width}, {height}

def chunk(tag, data):
    body = tag + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

rows = (b"\\x00" + b"\\x1f\\x5f\\x9f" * width) * height
Path(args[args.index("-o") + 1]).write_bytes(
    b"\\x89PNG\\r\\n\\x1a\\n"
    + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    + chunk(b"IDAT", zlib.compress(rows))
    + chunk(b"IEND", b"")
)
"""


def _install_spectacle_stub(directory: Path, delay: float) -> Path:
    """Put a ``spectacle`` that answers after ``delay`` first on a PATH; return its call log."""
    stub = directory / "spectacle"
    stub.write_text(
        _SLOW_SPECTACLE_STUB.format(delay=delay, width=SCREEN_SIZE[0], height=SCREEN_SIZE[1]),
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return directory / "spectacle.calls"


@pytest.mark.anyio
async def test_virtual_spectacle_fallback_waits_for_slow_healthy_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Spectacle capture slower than 10 s but inside the deadline is returned (#65)."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("KWIN_MCP_X11_SCREENSHOT", raising=False)
    calls = _install_spectacle_stub(tmp_path, SLOW_SPECTACLE_SECONDS)

    async with running_mcp_server(
        env={"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    ) as client:
        session_running = False
        try:
            await start_virtual(client)
            session_running = True
            # Headless virtual KWin exposes no working ScreenShot2, so this
            # exercises the real Spectacle fallback end to end.
            started = time.monotonic()
            frame, text = await capture(client)
            elapsed = time.monotonic() - started
            assert coordinate_spaces(text) == [
                CoordinateSpace((0, 0), SCREEN_SIZE, "spectacle", "full")
            ], text
            assert_full_frame(frame, SCREEN_SIZE)
            # One slow capture, not a retry that happened to fit a short bound.
            invocations = calls.read_text(encoding="utf-8").splitlines()
            assert len(invocations) == 1, invocations
            assert elapsed >= SLOW_SPECTACLE_SECONDS, elapsed
        finally:
            if session_running:
                await stop_virtual(client)


@pytest.mark.anyio
async def test_virtual_spectacle_fallback_bounds_stuck_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Spectacle that never finishes is killed at the reported deadline (#65)."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("KWIN_MCP_X11_SCREENSHOT", raising=False)
    calls = _install_spectacle_stub(tmp_path, 3600)

    async with running_mcp_server(
        env={"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    ) as client:
        session_running = False
        try:
            await start_virtual(client)
            session_running = True
            started = time.monotonic()
            error = "\n".join(await client.call_tool_error("screen_capture", lines=None))
            elapsed = time.monotonic() - started
            match = re.search(r"Spectacle \(spectacle timed out after (\d+(?:\.\d+)?)s", error)
            assert match is not None, error
            deadline = float(match.group(1))
            # The deadline must leave room for the recorded slow capture, and the
            # reported value must be the one actually enforced.
            assert deadline > SLOW_SPECTACLE_SECONDS, error
            assert deadline <= elapsed <= deadline + STUCK_CAPTURE_SLACK_SECONDS, (elapsed, error)
            assert len(calls.read_text(encoding="utf-8").splitlines()) == 1
            await assert_server_alive(client)
        finally:
            if session_running:
                await stop_virtual(client)


# Spectacle 6.3's own failure mode observed on the mixed-scale QEMU session
# (#65): its CaptureScreen request misses its 4 s reply window, it prints the
# reason and exits 0 without writing the file.
_SILENT_SPECTACLE_REASON = (
    "KWin screenshot request failed: Did not receive a reply. - Method: CaptureScreen"
)


@pytest.mark.anyio
async def test_virtual_spectacle_fallback_reports_why_no_file_was_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Spectacle exit 0 without output surfaces Spectacle's own reason (#65)."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("KWIN_MCP_X11_SCREENSHOT", raising=False)
    stub = tmp_path / "spectacle"
    stub.write_text(f"#!/bin/sh\necho '{_SILENT_SPECTACLE_REASON}' >&2\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)

    async with running_mcp_server(
        env={"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    ) as client:
        session_running = False
        try:
            await start_virtual(client)
            session_running = True
            error = "\n".join(await client.call_tool_error("screen_capture", lines=None))
            assert f"Spectacle (spectacle produced no output: {_SILENT_SPECTACLE_REASON})" in (
                error
            ), error
        finally:
            if session_running:
                await stop_virtual(client)


# ── cropping and downscaling (region, max_edge, --screenshot-max-edge) ──
# The session is wider than tall, like an ultrawide desktop. A widget is located
# by its own pixels in the reframed image, then mapped back with the formula the
# coordinate line states; the click has to reach the widget, so a wrong scale or
# crop origin fails behaviourally rather than only by size.

WIDE_SCREEN_SIZE = (2560, 800)
MAX_EDGE = 1280
SCALED_SIZE = (1280, 400)
FLAG_MAX_EDGE = 640
FLAG_SCALED_SIZE = (640, 200)
FRAME_DELAYS = [0, 100]
# One scaled pixel covers two logical pixels; Lanczos softens edges by about one more.
SCALED_SLACK_PX = 4


@asynccontextmanager
async def wide_session(*server_args: str) -> AsyncGenerator[McpTestClient]:
    """An installed server connected to a wide nested KWin, captured through X11.

    The container's virtual KWin has no ScreenShot2, so pixel tests use the same
    nested visual compositor as the other tests in this module.
    """
    with nested_visual_kwin(screen_size=WIDE_SCREEN_SIZE) as visual:
        async with live_client(visual, *server_args) as client:
            yield client


@pytest.mark.anyio
@pytest.mark.visual
async def test_downscaled_pixels_map_back_to_a_working_click(tmp_path: Path) -> None:
    """The stated pixel-to-logical formula turns a scaled pixel into a hit."""
    async with wide_session() as client:
        await launch_gui_probe(client)
        rect = await element_rect(client, GUI_PROBE_APP, "Animation Target")

        full, full_text, full_bbox = await blue_in(client, {}, tmp_path / "full.png")
        assert [(s.origin, s.size) for s in coordinate_spaces(full_text)] == [
            ((0, 0), WIDE_SCREEN_SIZE)
        ], full_text
        assert "downscaled" not in full_text, full_text
        assert_full_frame(full, WIDE_SCREEN_SIZE)

        scaled, text, bbox = await blue_in(client, {"max_edge": MAX_EDGE}, tmp_path / "scaled.png")
        # The logical space is unchanged; only the pixel grid shrank.
        assert [(s.origin, s.size) for s in coordinate_spaces(text)] == [
            ((0, 0), WIDE_SCREEN_SIZE)
        ], text
        image_size, (fx, fy) = image_term(text)
        assert image_size == SCALED_SIZE, text
        assert (scaled.width, scaled.height) == SCALED_SIZE, scaled
        assert scaled.mapping == ImageMapping(scale=0.5, origin=Point(x=0, y=0)), scaled
        assert scaled.coverage == "full", scaled
        frame_png(scaled)
        assert all(
            abs(value * 2 - full_value) <= SCALED_SLACK_PX
            for value, full_value in zip(bbox, full_bbox, strict=True)
        ), (bbox, full_bbox)

        # The stated formula: a scaled pixel shows its source pixel center.
        click_x = round(((bbox[0] + bbox[2]) / 2 + 0.5) * fx - 0.5)
        click_y = round(((bbox[1] + bbox[3]) / 2 + 0.5) * fy - 0.5)
        assert rect[0] <= click_x < rect[0] + rect[2], (rect, click_x)
        assert rect[1] <= click_y < rect[1] + rect[3], (rect, click_y)
        before = await status(client, GUI_PROBE_APP, "animation_status:")
        await client.call_structured("pointer_click", {"target": {"x": click_x, "y": click_y}})
        await wait_status_change(client, GUI_PROBE_APP, "animation_status:", before)


@pytest.mark.anyio
@pytest.mark.visual
async def test_region_crop_reports_its_origin_and_clips_to_the_workspace(tmp_path: Path) -> None:
    async with wide_session() as client:
        await launch_gui_probe(client)
        _, _, full_bbox = await blue_in(client, {}, tmp_path / "full.png")

        # A full-resolution crop around the button: pixel (px, py) is origin + (px, py).
        region = {"x": full_bbox[0] - 40, "y": full_bbox[1] - 30, "width": 600, "height": 200}
        cropped, text, bbox = await blue_in(client, {"region": region}, tmp_path / "crop.png")
        assert [(s.origin, s.size) for s in coordinate_spaces(text)] == [
            ((region["x"], region["y"]), (region["width"], region["height"]))
        ], text
        assert "downscaled" not in text, text
        assert (cropped.width, cropped.height) == (region["width"], region["height"]), cropped
        assert cropped.mapping == ImageMapping(
            scale=1.0, origin=Point(x=region["x"], y=region["y"])
        ), cropped
        frame_png(cropped)
        with Image.open(tmp_path / "crop.png") as image:
            assert image.size == (region["width"], region["height"])
        assert all(
            abs(crop + offset - full) <= 1
            for crop, offset, full in zip(
                bbox[:2], (region["x"], region["y"]), full_bbox[:2], strict=True
            )
        ), (bbox, full_bbox)

        # A region hanging off the top-left corner keeps only the on-screen part.
        clipped, clipped_text = await capture(
            client, {"region": {"x": -100, "y": -50, "width": 300, "height": 150}}
        )
        assert [(s.origin, s.size) for s in coordinate_spaces(clipped_text)] == [
            ((0, 0), (200, 100))
        ], clipped_text
        assert (clipped.width, clipped.height) == (200, 100), clipped
        assert clipped.mapping == ImageMapping(scale=1.0, origin=Point(x=0, y=0)), clipped

        # Crop and downscale compose: the crop is bounded after cutting.
        both, both_text = await capture(
            client,
            {"region": {"x": 0, "y": 0, "width": 2000, "height": 500}, "max_edge": 1000},
        )
        assert [(s.origin, s.size) for s in coordinate_spaces(both_text)] == [
            ((0, 0), (2000, 500))
        ], both_text
        assert image_term(both_text)[0] == (1000, 250), both_text
        assert (both.width, both.height) == (1000, 250), both
        assert both.mapping == ImageMapping(scale=0.5, origin=Point(x=0, y=0)), both

        # Entirely off-screen: a tool error that names the workspace, no stray file.
        shots_dir = Path(both.path).parent
        before_files = sorted(shots_dir.iterdir())
        message = "\n".join(
            await client.call_tool_error(
                "screen_capture",
                {"region": {"x": 3000, "y": 0, "width": 100, "height": 100}},
                lines=None,
            )
        )
        assert "outside the captured workspace (0, 0, 2560x800)" in message, message
        assert sorted(shots_dir.iterdir()) == before_files

        # Schema validation refuses a malformed region before the engine runs.
        malformed = "\n".join(
            await client.call_tool_error(
                "screen_capture", {"region": {"x": 0, "y": 0, "width": 10}}, lines=None
            )
        )
        assert "height" in malformed, malformed
        assert sorted(shots_dir.iterdir()) == before_files


@pytest.mark.anyio
@pytest.mark.visual
async def test_max_edge_flag_bounds_screen_captures_frames_and_attached_images() -> None:
    """--screenshot-max-edge applies to every capture; a call can ask for full size."""
    async with wide_session(
        "--screenshot-max-edge", str(FLAG_MAX_EDGE), "--screenshot-images"
    ) as client:
        shot = await client.call_result("screen_capture", {})
        assert not shot.is_error, text_of(shot)
        text = text_of(shot)
        frame = Frame.model_validate(shot.structured_content)
        assert frame.render() == text, text
        assert image_term(text)[0] == FLAG_SCALED_SIZE, text
        assert (frame.width, frame.height) == FLAG_SCALED_SIZE, frame
        assert frame.mapping == ImageMapping(scale=0.25, origin=Point(x=0, y=0)), frame
        attached = images(shot)
        assert len(attached) == 1, shot.content
        assert isinstance(shot.content[0], TextContent), shot.content
        assert isinstance(shot.content[1], ImageContent), shot.content
        assert png_size(attached[0]) == FLAG_SCALED_SIZE
        assert attached[0] == frame_png(frame).read_bytes()

        full = await client.call_result("screen_capture", {"max_edge": 0})
        assert not full.is_error, text_of(full)
        full_text = text_of(full)
        assert "downscaled" not in full_text, full_text
        full_frame = Frame.model_validate(full.structured_content)
        assert (full_frame.width, full_frame.height) == WIDE_SCREEN_SIZE, full_frame
        assert png_size(images(full)[0]) == WIDE_SCREEN_SIZE

        moved = await client.call_result(
            "pointer_move",
            {"target": {"x": 200, "y": 200}, "capture": {"delays_ms": FRAME_DELAYS}},
        )
        assert not moved.is_error, text_of(moved)
        moved_text = text_of(moved)
        frames = action_frames(moved.structured_content or {})
        assert [frame.delay_ms for frame in frames] == FRAME_DELAYS, frames
        assert [(f.width, f.height) for f in frames] == [FLAG_SCALED_SIZE] * len(FRAME_DELAYS)
        assert [png_size(data) for data in images(moved)] == [FLAG_SCALED_SIZE] * len(FRAME_DELAYS)
        frame_lines = [line for line in moved_text.splitlines() if "Coordinate space" in line]
        assert len(frame_lines) == len(FRAME_DELAYS), moved_text
        assert all(image_term(line)[0] == FLAG_SCALED_SIZE for line in frame_lines), moved_text


@pytest.mark.anyio
@pytest.mark.visual
async def test_later_frame_burst_never_overwrites_earlier_frames() -> None:
    """Each capture burst publishes new paths; earlier frames keep their pixels."""
    with nested_visual_kwin() as visual:
        async with live_client(visual) as client:
            screen, _ = await capture(client)
            screenshot_dir = assert_full_frame(screen, SCREEN_SIZE).parent

            first_burst, first_text = await client.call_structured(
                "pointer_move", {"target": {"x": 10, "y": 10}, "capture": {"delays_ms": [0]}}
            )
            (first,) = burst_sources(action_frames(first_burst), first_text, [0], SCREEN_SIZE)
            first_bytes = first.read_bytes()
            first_mtime = first.stat().st_mtime_ns
            preserve_png(first, visual.artifact_dir, "burst-before-probe.png")

            # Change the screen between the bursts: the probe window appears.
            await launch(client, PROBE_COMMAND)
            await wait_element(client, PROBE_APP, "Keyboard Target")

            second_burst, second_text = await client.call_structured(
                "pointer_move", {"target": {"x": 20, "y": 20}, "capture": {"delays_ms": [0]}}
            )
            (second,) = burst_sources(action_frames(second_burst), second_text, [0], SCREEN_SIZE)
            preserve_png(second, visual.artifact_dir, "burst-after-probe.png")

            assert second != first, (first, second)
            assert first.parent == screenshot_dir, (first, screenshot_dir)
            assert second.parent == screenshot_dir, (second, screenshot_dir)
            assert first.read_bytes() == first_bytes
            assert first.stat().st_mtime_ns == first_mtime
            assert second.read_bytes() != first_bytes


# ── engine runner: cursor pixels and coordinates on a scaled output ──────


@contextmanager
def x11_engine(
    engine: AutomationEngine,
    monkeypatch: pytest.MonkeyPatch,
    *,
    scale: float = 1.0,
    screen_size: tuple[int, int] = SCREEN_SIZE,
) -> Generator[NestedVisualKWin]:
    """The engine connected to a nested X11 KWin, captures selecting X11/scrot."""
    with nested_visual_kwin(scale=scale, screen_size=screen_size) as visual:
        monkeypatch.setenv("DISPLAY", visual.x_display)
        monkeypatch.setenv("KWIN_MCP_X11_SCREENSHOT", "1")
        info = engine.session_connect(
            dbus_address=visual.dbus_address, wayland_display=visual.wayland_display
        )
        assert info.kind == "live" and info.input_backend == "eis", info.render()
        try:
            yield visual
        finally:
            engine.session_stop()


@pytest.mark.visual
@pytest.mark.qa("QA-CAP-002")
def test_include_cursor_draws_pointer(
    engine: AutomationEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """include_cursor defaults to false; true draws the pointer at its logical position."""
    with x11_engine(engine, monkeypatch) as visual:
        target = Point(x=640, y=400)
        moved = engine.pointer_move(target)
        assert moved.at == target, moved.render()
        plain = preserve_png(
            assert_full_frame(engine.screen_capture(), SCREEN_SIZE),
            visual.artifact_dir,
            "cursor-default.png",
        )
        drawn = preserve_png(
            assert_full_frame(engine.screen_capture(include_cursor=True), SCREEN_SIZE),
            visual.artifact_dir,
            "cursor-drawn.png",
        )
        plain_again = preserve_png(
            assert_full_frame(engine.screen_capture(include_cursor=False), SCREEN_SIZE),
            visual.artifact_dir,
            "cursor-off.png",
        )
        radius = 40
        cursor_box = (
            target.x - radius,
            target.y - radius,
            target.x + radius,
            target.y + radius,
        )
        # The two captures without the cursor are identical around the pointer...
        assert count_changed(changed_pixels(plain, plain_again), cursor_box) == 0
        # ...and only the include_cursor capture differs there, and only there.
        drawn_mask = changed_pixels(plain, drawn)
        near_changed = count_changed(drawn_mask, cursor_box)
        total_changed = count_changed(drawn_mask)
        assert near_changed >= 20, (near_changed, total_changed)
        assert total_changed - near_changed <= max(10, total_changed // 50), (
            near_changed,
            total_changed,
        )


@pytest.mark.visual
@pytest.mark.qa("QA-INP-010")
def test_global_logical_px_on_scaled_output(
    engine: AutomationEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a 1.45-scaled output, targets, window frames, crops and clicks share logical px."""
    scale = 1.45
    with x11_engine(engine, monkeypatch, scale=scale, screen_size=(1920, 1200)) as visual:
        screens = kwin_screens(visual.dbus_address)
        assert [s["scale"] for s in screens] == [pytest.approx(scale)], screens
        probe = launch_probe(engine, tmp_path)

        # Window frame: kwin-mcp's window_list agrees with KWin's own report.
        oracle_window = probe.oracle().window(probe.window_id)
        assert oracle_window is not None and oracle_window.client is not None
        assert oracle_window.frame is not None
        windows = [w for w in engine.window_list().windows if w.id == probe.window_id]
        assert len(windows) == 1, engine.window_list().render()
        window = windows[0]
        assert (window.frame.x, window.frame.y, window.frame.width, window.frame.height) == (
            oracle_window.frame.as_ints()
        ), (window, oracle_window)
        assert (window.client.x, window.client.y) == probe.origin, (window, probe.origin)

        # Element rect: global logical px inside the window's client area.
        element = probe.element("Click Target")
        rect = element.rect
        assert rect is not None, element
        assert window.client.x <= rect.x
        assert rect.x + rect.width <= window.client.x + window.client.width, (rect, window)
        assert window.client.y <= rect.y
        assert rect.y + rect.height <= window.client.y + window.client.height, (rect, window)

        # Region crop: a crop of the rect is exactly that rect of a full capture.
        full = engine.screen_capture(max_edge=0)
        assert full.mapping == ImageMapping(scale=1.0, origin=Point(x=0, y=0)), full
        crop = engine.screen_capture(
            region=Rect(x=rect.x, y=rect.y, width=rect.width, height=rect.height)
        )
        assert crop.mapping == ImageMapping(scale=1.0, origin=Point(x=rect.x, y=rect.y)), crop
        assert (crop.width, crop.height) == (rect.width, rect.height), crop
        with Image.open(frame_png(full)) as whole, Image.open(frame_png(crop)) as part:
            expected = whole.convert("RGB").crop(
                (rect.x, rect.y, rect.x + rect.width, rect.y + rect.height)
            )
            assert ImageChops.difference(expected, part.convert("RGB")).getbbox() is None
        preserve_png(Path(crop.path), visual.artifact_dir, "click-target-crop.png")

        # Click: the probe's raw events land on the rect's logical centre.
        mark = probe.park()
        target = probe.center("Click Target")
        clicked = engine.pointer_click(target)
        assert clicked.at == target, clicked.render()
        events = probe.wait_events(
            lambda evs: any(e["kind"] == "button_release" for e in evs), since=mark
        )
        buttons = [e for e in events if e["kind"] in ("button_press", "button_release")]
        assert [(e["kind"], e["button"]) for e in buttons] == [
            ("button_press", "left"),
            ("button_release", "left"),
        ], events
        for event in buttons:
            hit = probe.to_global(int(event["x"]), int(event["y"]))
            assert abs(hit.x - target.x) <= 1 and abs(hit.y - target.y) <= 1, (event, target)
        probe.wait_status("click_status:", lambda s: "button=left count=1" in s)


def kwin_screens(dbus_address: str) -> list[dict[str, Any]]:
    """KWin's outputs (logical geometry and scale) read with a test-owned script."""
    raw = KWinOracle(dbus_address).run(
        "var out = [];\n"
        "var screens = workspace.screens;\n"
        "for (var i = 0; i < screens.length; i++) {\n"
        "    var g = screens[i].geometry;\n"
        "    out.push({name: screens[i].name, x: g.x, y: g.y, width: g.width,\n"
        "              height: g.height, scale: screens[i].devicePixelRatio});\n"
        "}\n"
        "report(out);"
    )
    assert isinstance(raw, list), raw
    return raw
