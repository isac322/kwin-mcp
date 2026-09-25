"""Pixel-backed visual QA against the installed MCP server and nested KWin."""

from __future__ import annotations

import contextlib
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

import anyio
import pytest
from mcp_harness import running_mcp_server
from PIL import Image, ImageChops, ImageStat
from visual_harness import nested_visual_kwin

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mcp_harness import McpTestClient

SCREEN_SIZE = (1280, 800)
GUI_PROBE_COMMAND = "python3 /app/tests/e2e/gui_probe.py"
GUI_PROBE_ACCESSIBILITY_SELECTOR = "gui_probe.py"
GUI_PROBE_WINDOW_SELECTOR = "gui_probe.py"
GUI_PROBE_VISIBLE_NAME = "GUI Probe"
GUI_PROBE_CLIENT_SIZE = (920, 700)
POLL_INTERVAL_SECONDS = 0.1
STATE_TIMEOUT_SECONDS = 5.0
_RECT = r"\((-?\d+), (-?\d+), (\d+)x(\d+)\)"
_FRAME_LINE = re.compile(r"^\s+(\d+)ms: (.+?) \((\d+(?:\.\d+)?) KB\)$", re.MULTILINE)

pytestmark = [pytest.mark.anyio, pytest.mark.visual]


@pytest.fixture
def anyio_backend() -> str:
    """The installed MCP client runs on asyncio."""
    return "asyncio"


def _single_rect(output: str, pattern: str) -> tuple[int, int, int, int]:
    matches = re.findall(pattern, output, re.MULTILINE)
    assert len(matches) == 1, output[:1500]
    x, y, width, height = matches[0]
    return int(x), int(y), int(width), int(height)


async def _global_element_rect(
    client: McpTestClient,
    accessibility_app_name: str,
    name: str,
) -> tuple[int, int, int, int]:
    elements = await client.call_text(
        "find_ui_elements",
        {"query": name, "app_name": accessibility_app_name},
    )
    # find_ui_elements already reports global screen coordinates.
    x, y, width, height = _single_rect(
        elements,
        rf'^- \[[^]]+] "{re.escape(name)}" @ screen {_RECT}(?:\s|$)',
    )
    assert width > 0 and height > 0, elements[:1500]
    return x, y, width, height


def _center(rect: tuple[int, int, int, int]) -> tuple[int, int]:
    x, y, width, height = rect
    return x + width // 2, y + height // 2


def _screenshot_source(output: str) -> Path:
    prefix = "Screenshot saved: "
    assert output.startswith(prefix), output
    path = Path(output.removeprefix(prefix).rsplit(" (", 1)[0])
    assert path.is_file(), output
    return path


def _preserve_png(source: Path, artifact_dir: Path, name: str) -> Path:
    destination = artifact_dir / name
    shutil.copy2(source, destination)
    with Image.open(destination) as image:
        image.load()
        assert image.format == "PNG", destination
        assert image.size == SCREEN_SIZE, (destination, image.size)
    return destination


def _load_rgb(path: Path) -> Image.Image:
    with Image.open(path) as image:
        image.load()
        assert image.format == "PNG", path
        assert image.size == SCREEN_SIZE, (path, image.size)
        return image.convert("RGB")


def _difference_mask(first: Image.Image, second: Image.Image) -> tuple[Image.Image, Image.Image]:
    assert first.size == second.size, (first.size, second.size)
    difference = ImageChops.difference(first, second)
    red, green, blue = difference.split()
    maximum = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    return difference, maximum


def _material_difference_metrics(
    first: Image.Image,
    second: Image.Image,
) -> tuple[int, float]:
    difference, maximum = _difference_mask(first, second)
    changed_pixels = sum(maximum.histogram()[11:])
    absolute_difference = sum(ImageStat.Stat(difference).sum)
    return changed_pixels, absolute_difference


def _assert_material_difference(
    first: Image.Image,
    second: Image.Image,
    *,
    minimum_pixels: int,
    label: str,
) -> None:
    changed_pixels, absolute_difference = _material_difference_metrics(first, second)
    assert changed_pixels >= minimum_pixels, (
        f"{label}: only {changed_pixels} pixels changed by more than 10 levels"
    )
    assert absolute_difference >= minimum_pixels * 20, (
        f"{label}: total RGB difference was only {absolute_difference:.0f}"
    )


def _assert_nonempty_rect(
    image: Image.Image,
    rect: tuple[int, int, int, int],
    label: str,
) -> Image.Image:
    x, y, width, height = rect
    assert x >= 0 and y >= 0 and x + width <= image.width and y + height <= image.height, (
        label,
        rect,
        image.size,
    )
    crop = image.crop((x, y, x + width, y + height))
    extrema = ImageStat.Stat(crop).extrema
    assert crop.getbbox() is not None, f"{label} crop is empty"
    assert any(high - low >= 8 for low, high in extrema), (
        f"{label} crop has no visible foreground variation: {extrema}"
    )
    return crop


def _colorful_pixel_count(image: Image.Image) -> int:
    pixels = cast("Iterable[tuple[int, int, int]]", image.getdata())
    return sum(
        1 for red, green, blue in pixels if max(red, green, blue) - min(red, green, blue) >= 35
    )


def _app_log_path(launch_output: str) -> Path:
    match = re.search(r"^App log: (.+)$", launch_output, re.MULTILINE)
    assert match is not None, launch_output
    return Path(match.group(1))


def _preserve_runtime_evidence(
    client: McpTestClient,
    artifact_dir: Path,
    app_log_path: Path | None,
    app_log_name: str,
) -> None:
    (artifact_dir / "mcp-server.stderr.log").write_text(
        client.stderr_text(),
        encoding="utf-8",
    )
    if app_log_path is not None and app_log_path.is_file():
        shutil.copy2(app_log_path, artifact_dir / app_log_name)


def _preserve_capture_backend(
    artifact_dir: Path,
    screenshot_service_available: bool | None,
) -> None:
    assert screenshot_service_available is not None
    capability = "available" if screenshot_service_available else "unavailable"
    (artifact_dir / "capture-backend.txt").write_text(
        "Selected screenshot capture backend: X11 scrot\n"
        f"KWin ScreenShot2 capability: {capability}\n",
        encoding="utf-8",
    )


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
    assert f"D-Bus: {dbus_address}" in output, output
    assert f"Wayland: {wayland_display}" in output, output
    assert "Input backend: KWin EIS" in output, output


async def _wait_for_accessible_name(
    client: McpTestClient,
    app_name: str,
    expected_name: str,
) -> str:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    tree = ""
    while time.monotonic() < deadline:
        tree = await client.call_text("accessibility_tree", {"app_name": app_name})
        if f'"{expected_name}"' in tree:
            return tree
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"AT-SPI name {expected_name!r} never appeared:\n{tree[:2000]}")


async def _wait_for_binary(client: McpTestClient, expected: str) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    elements = ""
    exact_label = re.compile(rf'^- \[label] "{re.escape(expected)}"(?: |$)')
    while time.monotonic() < deadline:
        elements = await client.call_text(
            "find_ui_elements",
            {"query": expected, "app_name": "kcalc"},
        )
        element_lines = [line for line in elements.splitlines() if line.startswith("- [")]
        if any(exact_label.match(line) for line in element_lines):
            return
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"KCalc binary label {expected!r} never appeared:\n{elements[:1500]}")


async def _stop_session(
    client: McpTestClient,
    *,
    connected: bool,
    body_succeeded: bool,
    artifact_dir: Path,
    app_log_path: Path | None,
    app_log_name: str,
) -> None:
    try:
        if connected and body_succeeded:
            output = await client.call_text("session_stop")
            assert output == "Disconnected from live session.", output
        elif connected:
            with contextlib.suppress(BaseException):
                await client.call_result("session_stop")
    finally:
        _preserve_runtime_evidence(client, artifact_dir, app_log_path, app_log_name)


async def _capture_preserved(
    client: McpTestClient,
    artifact_dir: Path,
    name: str,
    *,
    include_cursor: bool = False,
) -> tuple[Path, Image.Image]:
    output = await client.call_text("screenshot", {"include_cursor": include_cursor})
    destination = _preserve_png(_screenshot_source(output), artifact_dir, name)
    return destination, _load_rgb(destination)


async def _wait_for_material_repaint(
    client: McpTestClient,
    artifact_dir: Path,
    name: str,
    initial_crop: Image.Image,
    rect: tuple[int, int, int, int],
    *,
    minimum_pixels: int,
    label: str,
) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    x, y, width, height = rect
    last_source: Path | None = None
    changed_pixels = 0
    absolute_difference = 0.0

    while True:
        output = await client.call_text("screenshot", {"include_cursor": False})
        source = _screenshot_source(output)
        candidate = _load_rgb(source)
        candidate_crop = candidate.crop((x, y, x + width, y + height))
        changed_pixels, absolute_difference = _material_difference_metrics(
            initial_crop,
            candidate_crop,
        )
        last_source = source
        if changed_pixels >= minimum_pixels and absolute_difference >= minimum_pixels * 20:
            _preserve_png(source, artifact_dir, name)
            return
        if time.monotonic() >= deadline:
            break
        await anyio.sleep(POLL_INTERVAL_SECONDS)

    assert last_source is not None
    _preserve_png(last_source, artifact_dir, name)
    raise AssertionError(
        f"{label}: repaint never became material within {STATE_TIMEOUT_SECONDS:.1f}s; "
        f"last capture changed {changed_pixels} pixels by more than 10 levels "
        f"(minimum {minimum_pixels}) with total RGB difference "
        f"{absolute_difference:.0f} (minimum {minimum_pixels * 20})"
    )


async def _wait_for_crop_match(
    client: McpTestClient,
    artifact_dir: Path,
    name: str,
    reference_crop: Image.Image,
    rect: tuple[int, int, int, int],
    *,
    maximum_pixels: int,
    maximum_absolute_difference: float,
    label: str,
) -> Image.Image:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    x, y, width, height = rect
    last_source: Path | None = None
    changed_pixels = 0
    absolute_difference = 0.0

    while True:
        output = await client.call_text("screenshot", {"include_cursor": False})
        source = _screenshot_source(output)
        candidate = _load_rgb(source)
        candidate_crop = candidate.crop((x, y, x + width, y + height))
        changed_pixels, absolute_difference = _material_difference_metrics(
            reference_crop,
            candidate_crop,
        )
        last_source = source
        if changed_pixels <= maximum_pixels and absolute_difference <= maximum_absolute_difference:
            _preserve_png(source, artifact_dir, name)
            return candidate
        if time.monotonic() >= deadline:
            break
        await anyio.sleep(POLL_INTERVAL_SECONDS)

    assert last_source is not None
    _preserve_png(last_source, artifact_dir, name)
    raise AssertionError(
        f"{label}: pixels never returned to the initial state within "
        f"{STATE_TIMEOUT_SECONDS:.1f}s; last capture changed {changed_pixels} pixels "
        f"by more than 10 levels (maximum {maximum_pixels}) with total RGB difference "
        f"{absolute_difference:.0f} (maximum {maximum_absolute_difference:.0f})"
    )


async def test_gui_probe_visual_semantics_pixels_cursor_and_frame_burst() -> None:
    """Prove the probe with AT-SPI, EIS, and explicitly selected X11 scrot pixels."""
    with nested_visual_kwin() as visual:
        _preserve_capture_backend(
            visual.artifact_dir,
            visual.screenshot_service_available,
        )
        async with running_mcp_server(
            env={
                "DISPLAY": visual.x_display,
                "KWIN_MCP_X11_SCREENSHOT": "1",
            },
        ) as client:
            connected = False
            body_succeeded = False
            app_log_path: Path | None = None
            try:
                await _connect(client, visual.dbus_address, visual.wayland_display)
                connected = True

                launch_output = await client.call_text(
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
                assert f"App launched: {GUI_PROBE_COMMAND}" in launch_output, launch_output
                app_log_path = _app_log_path(launch_output)
                waited = await client.call_text(
                    "wait_for_element",
                    {
                        "query": "Animation Target",
                        "app_name": GUI_PROBE_ACCESSIBILITY_SELECTOR,
                        "timeout_ms": 15_000,
                    },
                )
                assert '[button] "Animation Target"' in waited, waited[:1500]

                initial_tree = await client.call_text(
                    "accessibility_tree",
                    {"app_name": GUI_PROBE_ACCESSIBILITY_SELECTOR},
                )
                assert f'[frame] "{GUI_PROBE_VISIBLE_NAME}"' in initial_tree, initial_tree[:2500]
                assert f'[label] "{GUI_PROBE_VISIBLE_NAME}"' in initial_tree, initial_tree[:2500]

                probe_geometry = await client.call_text(
                    "window_geometry",
                    {"app_name": GUI_PROBE_WINDOW_SELECTOR},
                )
                assert (
                    f'- {GUI_PROBE_WINDOW_SELECTOR} "{GUI_PROBE_VISIBLE_NAME}"' in probe_geometry
                ), probe_geometry[:1500]
                _, _, client_width, client_height = _single_rect(
                    probe_geometry,
                    rf"client:\s+{_RECT}",
                )
                assert (client_width, client_height) == GUI_PROBE_CLIENT_SIZE, probe_geometry[:1500]
                exact_initial_names = (
                    "Hover Target",
                    "hover_status: idle",
                    "Unicode Sample",
                    "unicode_sample: GUI 검증 42 😀",
                    "Tofu Control",
                    "tofu_control: □□",
                    "Animation Target",
                    "animation_status: 0",
                )
                for name in exact_initial_names:
                    assert f'"{name}"' in initial_tree, initial_tree[:2500]

                rects = {
                    name: await _global_element_rect(
                        client,
                        GUI_PROBE_ACCESSIBILITY_SELECTOR,
                        name,
                    )
                    for name in exact_initial_names
                }
                _, initial_image = await _capture_preserved(
                    client,
                    visual.artifact_dir,
                    "gui-probe-initial.png",
                )
                crops = {
                    name: _assert_nonempty_rect(initial_image, rect, name)
                    for name, rect in rects.items()
                }

                unicode_crop = crops["unicode_sample: GUI 검증 42 😀"]
                tofu_crop = crops["tofu_control: □□"]
                assert unicode_crop.size == tofu_crop.size, (unicode_crop.size, tofu_crop.size)
                _assert_material_difference(
                    unicode_crop,
                    tofu_crop,
                    minimum_pixels=250,
                    label="Unicode sample versus tofu control",
                )
                unicode_color = _colorful_pixel_count(unicode_crop)
                tofu_color = _colorful_pixel_count(tofu_crop)
                assert unicode_color >= tofu_color + 25, (
                    "the Unicode sample lacks the expected independently rendered color emoji: "
                    f"sample={unicode_color}, tofu={tofu_color}"
                )

                hover_rect = rects["Hover Target"]
                hover_x, hover_y = _center(hover_rect)
                move_output = await client.call_text(
                    "mouse_move",
                    {"x": hover_x, "y": hover_y},
                )
                assert move_output == f"Mouse moved to ({hover_x}, {hover_y})", move_output
                hovered_tree = await _wait_for_accessible_name(
                    client,
                    GUI_PROBE_ACCESSIBILITY_SELECTOR,
                    "hover_status: entered",
                )
                assert '"hover_status: idle"' not in hovered_tree, hovered_tree[:2500]
                await _wait_for_material_repaint(
                    client,
                    visual.artifact_dir,
                    "gui-probe-hovered.png",
                    crops["Hover Target"],
                    hover_rect,
                    minimum_pixels=1000,
                    label="mouse_move hover target",
                )

                static_x, static_y = _center(rects["Unicode Sample"])
                move_output = await client.call_text(
                    "mouse_move",
                    {"x": static_x, "y": static_y},
                )
                assert move_output == f"Mouse moved to ({static_x}, {static_y})", move_output
                idle_tree = await _wait_for_accessible_name(
                    client,
                    GUI_PROBE_ACCESSIBILITY_SELECTOR,
                    "hover_status: idle",
                )
                assert '"hover_status: entered"' not in idle_tree, idle_tree[:2500]
                no_cursor_image = await _wait_for_crop_match(
                    client,
                    visual.artifact_dir,
                    "gui-probe-pointer-without-cursor.png",
                    crops["Hover Target"],
                    hover_rect,
                    maximum_pixels=32,
                    maximum_absolute_difference=2000,
                    label="mouse_move away from hover target",
                )
                _, with_cursor_image = await _capture_preserved(
                    client,
                    visual.artifact_dir,
                    "gui-probe-pointer-with-cursor.png",
                    include_cursor=True,
                )
                cursor_difference, cursor_mask = _difference_mask(
                    no_cursor_image,
                    with_cursor_image,
                )
                cursor_bbox = cursor_difference.getbbox()
                assert cursor_bbox is not None, "include_cursor produced no pixel difference"
                radius = 48
                near_box = (
                    max(0, static_x - radius),
                    max(0, static_y - radius),
                    min(SCREEN_SIZE[0], static_x + radius),
                    min(SCREEN_SIZE[1], static_y + radius),
                )
                near_cursor = cursor_mask.crop(near_box)
                near_changed = sum(near_cursor.histogram()[11:])
                total_changed = sum(cursor_mask.histogram()[11:])
                assert near_changed >= 20, (
                    f"cursor changed only {near_changed} localized pixels near "
                    f"{(static_x, static_y)}"
                )
                assert total_changed - near_changed <= max(10, total_changed // 50), (
                    f"cursor diff was not localized: total={total_changed}, near={near_changed}, "
                    f"bbox={cursor_bbox}"
                )

                animation_rect = rects["Animation Target"]
                animation_x, animation_y = _center(animation_rect)
                requested_delays = [650, 0, 250, 450]
                burst_output = await client.call_text(
                    "mouse_click",
                    {
                        "x": animation_x,
                        "y": animation_y,
                        "screenshot_after_ms": requested_delays,
                    },
                )
                assert burst_output.startswith(
                    f"Clicked left at ({animation_x}, {animation_y})\nCaptured 4 frames:"
                ), burst_output
                frame_matches = _FRAME_LINE.findall(burst_output)
                assert len(frame_matches) == len(requested_delays), burst_output
                sorted_delays = sorted(requested_delays)
                assert [int(delay) for delay, _, _ in frame_matches] == sorted_delays, burst_output

                frame_images: list[Image.Image] = []
                for index, (delay_text, path_text, _) in enumerate(frame_matches):
                    delay = int(delay_text)
                    source = Path(path_text)
                    preserved = _preserve_png(
                        source,
                        visual.artifact_dir,
                        f"gui-probe-animation-{index:03d}-{delay}ms.png",
                    )
                    frame_images.append(_load_rgb(preserved))

                x, y, width, height = animation_rect
                animation_crops = [
                    image.crop((x, y, x + width, y + height)) for image in frame_images
                ]
                burst_materially_changed = False
                for first_index in range(len(animation_crops) - 1):
                    for second_index in range(first_index + 1, len(animation_crops)):
                        difference, maximum = _difference_mask(
                            animation_crops[first_index],
                            animation_crops[second_index],
                        )
                        changed_pixels = sum(maximum.histogram()[11:])
                        absolute_difference = sum(ImageStat.Stat(difference).sum)
                        if changed_pixels >= 1000 and absolute_difference >= 20_000:
                            burst_materially_changed = True
                            break
                    if burst_materially_changed:
                        break
                assert burst_materially_changed, (
                    "X11 scrot burst animation frames were all materially identical"
                )
                await _wait_for_accessible_name(
                    client,
                    GUI_PROBE_ACCESSIBILITY_SELECTOR,
                    "animation_status: 12",
                )

                body_succeeded = True
            finally:
                await _stop_session(
                    client,
                    connected=connected,
                    body_succeeded=body_succeeded,
                    artifact_dir=visual.artifact_dir,
                    app_log_path=app_log_path,
                    app_log_name="gui-probe.log",
                )


async def test_kcalc_binary_values_have_significant_screenshot_transitions() -> None:
    """Pair KCalc's binary AT-SPI value with preserved before/after pixels."""
    with nested_visual_kwin() as visual:
        _preserve_capture_backend(
            visual.artifact_dir,
            visual.screenshot_service_available,
        )
        async with running_mcp_server(
            env={
                "DISPLAY": visual.x_display,
                "KWIN_MCP_X11_SCREENSHOT": "1",
            },
        ) as client:
            connected = False
            body_succeeded = False
            app_log_path: Path | None = None
            try:
                await _connect(client, visual.dbus_address, visual.wayland_display)
                connected = True

                launch_output = await client.call_text("launch_app", {"command": "kcalc"})
                assert "App launched: kcalc" in launch_output, launch_output
                app_log_path = _app_log_path(launch_output)
                waited = await client.call_text(
                    "wait_for_element",
                    {"query": "Seven", "app_name": "kcalc", "timeout_ms": 15_000},
                )
                assert '[button] "Seven"' in waited, waited[:1500]

                await _wait_for_binary(client, "0")
                _, initial_image = await _capture_preserved(
                    client,
                    visual.artifact_dir,
                    "kcalc-initial-binary-0.png",
                )

                seven_rect = await _global_element_rect(client, "kcalc", "Seven")
                seven_x, seven_y = _center(seven_rect)
                seven_output = await client.call_text(
                    "mouse_click",
                    {"x": seven_x, "y": seven_y},
                )
                assert seven_output == f"Clicked left at ({seven_x}, {seven_y})", seven_output
                await _wait_for_binary(client, "111")
                _, seven_image = await _capture_preserved(
                    client,
                    visual.artifact_dir,
                    "kcalc-after-seven-binary-111.png",
                )
                _assert_material_difference(
                    initial_image,
                    seven_image,
                    minimum_pixels=15,
                    label="KCalc initial to Seven",
                )

                zero_rect = await _global_element_rect(client, "kcalc", "Zero")
                zero_x, zero_y = _center(zero_rect)
                zero_output = await client.call_text(
                    "mouse_click",
                    {"x": zero_x, "y": zero_y},
                )
                assert zero_output == f"Clicked left at ({zero_x}, {zero_y})", zero_output
                await _wait_for_binary(client, "1000110")
                _, zero_image = await _capture_preserved(
                    client,
                    visual.artifact_dir,
                    "kcalc-after-zero-binary-1000110.png",
                )
                _assert_material_difference(
                    seven_image,
                    zero_image,
                    minimum_pixels=50,
                    label="KCalc Seven to Zero",
                )

                body_succeeded = True
            finally:
                await _stop_session(
                    client,
                    connected=connected,
                    body_succeeded=body_succeeded,
                    artifact_dir=visual.artifact_dir,
                    app_log_path=app_log_path,
                    app_log_name="kcalc.log",
                )
