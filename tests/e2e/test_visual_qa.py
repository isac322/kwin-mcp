"""Pixel-backed visual QA against the installed MCP server and nested KWin."""

from __future__ import annotations

import contextlib
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
import pytest
from _asserts import coordinate_spaces, frame_png, has_kcalc_binary
from mcp_harness import MODELS, running_mcp_server
from PIL import Image, ImageChops, ImageStat
from visual_harness import nested_visual_kwin

from kwin_mcp.models import (
    AppLaunchResult,
    ElementList,
    Frame,
    PointerClickResult,
    PointerMoveResult,
    SessionInfo,
    SessionStopResult,
    UIElement,
    WindowList,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mcp_harness import McpTestClient
    from pydantic import BaseModel

SCREEN_SIZE = (1280, 800)
GUI_PROBE_COMMAND = "python3 /app/tests/e2e/gui_probe.py"
GUI_PROBE_ACCESSIBILITY_SELECTOR = "gui_probe.py"
GUI_PROBE_WINDOW_SELECTOR = "gui_probe.py"
GUI_PROBE_VISIBLE_NAME = "GUI Probe"
GUI_PROBE_CLIENT_SIZE = (920, 700)
POLL_INTERVAL_SECONDS = 0.1
STATE_TIMEOUT_SECONDS = 5.0

pytestmark = [pytest.mark.anyio, pytest.mark.visual]


@pytest.fixture
def anyio_backend() -> str:
    """The installed MCP client runs on asyncio."""
    return "asyncio"


async def _call[M: BaseModel](
    client: McpTestClient, name: str, model_cls: type[M], arguments: dict[str, Any]
) -> tuple[M, str]:
    """Call a tool; return its validated result model and its text."""
    assert MODELS[name] is model_cls, (name, MODELS[name])
    structured, text = await client.call_structured(name, arguments)
    return model_cls.model_validate(structured), text


async def _elements(client: McpTestClient, app: str, query: str | None = None) -> ElementList:
    locator: dict[str, str] = {"app": app}
    if query is not None:
        locator["query"] = query
    found, _ = await _call(client, "element_query", ElementList, {"locator": locator})
    return found


def _names(found: ElementList) -> set[str]:
    return {element.name for element in found.elements}


async def _global_element_rect(
    client: McpTestClient,
    accessibility_app_name: str,
    name: str,
) -> tuple[int, int, int, int]:
    found = await _elements(client, accessibility_app_name, name)
    exact: list[UIElement] = [element for element in found.elements if element.name == name]
    assert len(exact) == 1, found.render()[:1500]
    # element_query reports global logical screen coordinates.
    rect = exact[0].rect
    assert rect is not None, exact[0].render()
    assert rect.width > 0 and rect.height > 0, exact[0].render()
    return rect.x, rect.y, rect.width, rect.height


def _center(rect: tuple[int, int, int, int]) -> tuple[int, int]:
    x, y, width, height = rect
    return x + width // 2, y + height // 2


def _frame_source(frame: Frame, *, capture_backend: str) -> Path:
    """The PNG of a whole-workspace capture mapped 1:1 onto the logical screen."""
    path = frame_png(frame)
    spaces = coordinate_spaces(frame.coordinate_space)
    assert [(s.origin, s.size, s.backend) for s in spaces] == [
        ((0, 0), SCREEN_SIZE, capture_backend)
    ], frame.coordinate_space
    assert frame.mapping is not None, frame
    assert frame.mapping.scale == 1.0, frame
    assert (frame.mapping.origin.x, frame.mapping.origin.y) == (0, 0), frame
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
    capture_backend: str,
    screenshot_service_available: bool | None,
) -> None:
    assert screenshot_service_available is not None
    capability = "available" if screenshot_service_available else "unavailable"
    (artifact_dir / "capture-backend.txt").write_text(
        f"Selected screenshot capture backend: {capture_backend}\n"
        f"KWin ScreenShot2 capability: {capability}\n",
        encoding="utf-8",
    )


async def _connect(client: McpTestClient, dbus_address: str, wayland_display: str) -> None:
    info, text = await _call(
        client,
        "session_connect",
        SessionInfo,
        {
            "dbus_address": dbus_address,
            "wayland_display": wayland_display,
            "retention": {"artifacts": True},
        },
    )
    assert info.kind == "live", text
    assert info.dbus_address == dbus_address, text
    assert info.input_backend == "eis", text
    assert text.startswith("Connected to live KWin session."), text
    assert f"D-Bus: {dbus_address}" in text, text
    assert f"Wayland: {wayland_display}" in text, text
    assert "Input backend: KWin EIS" in text, text


async def _launch(
    client: McpTestClient, command: str, env: dict[str, str] | None = None
) -> AppLaunchResult:
    arguments: dict[str, Any] = {"command": command}
    if env is not None:
        arguments["env"] = env
    launched, text = await _call(client, "app_launch", AppLaunchResult, arguments)
    assert launched.command == command, text
    assert text.startswith(f"App launched: {command} (PID="), text
    return launched


async def _wait_for_element(client: McpTestClient, app: str, name: str, role: str) -> None:
    found, text = await _call(
        client,
        "element_query",
        ElementList,
        {"locator": {"app": app, "query": name}, "wait": {"timeout_ms": 15_000}},
    )
    assert found.wait is not None and found.wait.satisfied, text[:1500]
    assert any(e.role == role and e.name == name for e in found.elements), text[:1500]


async def _wait_for_accessible_name(
    client: McpTestClient,
    app_name: str,
    expected_name: str,
) -> ElementList:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    found: ElementList | None = None
    while time.monotonic() < deadline:
        found = await _elements(client, app_name)
        if expected_name in _names(found):
            return found
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    tree = "" if found is None else found.render()
    raise AssertionError(f"AT-SPI name {expected_name!r} never appeared:\n{tree[:2000]}")


async def _wait_for_binary(client: McpTestClient, expected: str) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    found: ElementList | None = None
    while time.monotonic() < deadline:
        found = await _elements(client, "kcalc")
        if has_kcalc_binary(found, expected):
            return
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    elements = "" if found is None else found.render()
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
            stopped, text = await _call(client, "session_stop", SessionStopResult, {})
            assert stopped.stopped and stopped.kind == "live", text
            assert text.splitlines()[0] == "Disconnected from live session.", text
            assert stopped.kept.artifacts_dir is not None, text
        elif connected:
            with contextlib.suppress(BaseException):
                await client.call_result("session_stop")
    finally:
        _preserve_runtime_evidence(client, artifact_dir, app_log_path, app_log_name)


async def _screen_capture(client: McpTestClient, *, capture_backend: str) -> Path:
    frame, text = await _call(client, "screen_capture", Frame, {})
    assert frame.delay_ms is None, text
    return _frame_source(frame, capture_backend=capture_backend)


async def _capture_preserved(
    client: McpTestClient,
    artifact_dir: Path,
    name: str,
    *,
    capture_backend: str,
) -> tuple[Path, Image.Image]:
    destination = _preserve_png(
        await _screen_capture(client, capture_backend=capture_backend), artifact_dir, name
    )
    return destination, _load_rgb(destination)


async def _wait_for_material_repaint(
    client: McpTestClient,
    artifact_dir: Path,
    name: str,
    initial_crop: Image.Image,
    rect: tuple[int, int, int, int],
    *,
    capture_backend: str,
    minimum_pixels: int,
    label: str,
) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    x, y, width, height = rect
    last_source: Path | None = None
    changed_pixels = 0
    absolute_difference = 0.0

    while True:
        source = await _screen_capture(client, capture_backend=capture_backend)
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
    capture_backend: str,
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
        source = await _screen_capture(client, capture_backend=capture_backend)
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


async def _pointer_move(
    client: McpTestClient, x: int, y: int, capture: dict[str, Any] | None = None
) -> PointerMoveResult:
    arguments: dict[str, Any] = {"target": {"x": x, "y": y}}
    if capture is not None:
        arguments["capture"] = capture
    moved, text = await _call(client, "pointer_move", PointerMoveResult, arguments)
    assert moved.warnings == [], text
    assert moved.at is not None and (moved.at.x, moved.at.y) == (x, y), text
    assert text.splitlines()[0] == f"Mouse moved to ({x}, {y})", text
    return moved


async def _pointer_click(
    client: McpTestClient, x: int, y: int, capture: dict[str, Any] | None = None
) -> tuple[PointerClickResult, str]:
    arguments: dict[str, Any] = {"target": {"x": x, "y": y}}
    if capture is not None:
        arguments["capture"] = capture
    clicked, text = await _call(client, "pointer_click", PointerClickResult, arguments)
    assert clicked.warnings == [], text
    assert clicked.button == "left" and clicked.count == 1, text
    assert text.splitlines()[0] == f"Clicked left at ({x}, {y})", text
    return clicked, text


async def test_gui_probe_visual_semantics_pixels_cursor_and_frame_burst() -> None:
    """Prove the probe with AT-SPI, EIS, and the explicitly selected capture backend."""
    with nested_visual_kwin() as visual:
        _preserve_capture_backend(
            visual.artifact_dir,
            visual.capture_backend,
            visual.screenshot_service_available,
        )
        async with running_mcp_server(env=visual.capture_env) as client:
            connected = False
            body_succeeded = False
            app_log_path: Path | None = None
            try:
                await _connect(client, visual.dbus_address, visual.wayland_display)
                connected = True

                launched = await _launch(
                    client,
                    GUI_PROBE_COMMAND,
                    {
                        "GDK_BACKEND": "wayland",
                        "GTK_MODULES": "gail:atk-bridge",
                        "NO_AT_BRIDGE": "0",
                        "XDG_SESSION_TYPE": "wayland",
                    },
                )
                app_log_path = Path(launched.log_path)
                await _wait_for_element(
                    client, GUI_PROBE_ACCESSIBILITY_SELECTOR, "Animation Target", "button"
                )

                initial_tree = await _elements(client, GUI_PROBE_ACCESSIBILITY_SELECTOR)
                roles_and_names = {(e.role, e.name) for e in initial_tree.elements}
                tree_text = initial_tree.render()[:2500]
                assert ("frame", GUI_PROBE_VISIBLE_NAME) in roles_and_names, tree_text
                assert ("label", GUI_PROBE_VISIBLE_NAME) in roles_and_names, tree_text

                windows, windows_text = await _call(
                    client,
                    "window_list",
                    WindowList,
                    {"filter": {"app_id": GUI_PROBE_WINDOW_SELECTOR}},
                )
                probe_windows = [
                    window
                    for window in windows.windows
                    if window.app_id == GUI_PROBE_WINDOW_SELECTOR
                    and window.caption == GUI_PROBE_VISIBLE_NAME
                ]
                assert len(probe_windows) == 1, windows_text[:1500]
                client_rect = probe_windows[0].client
                assert (client_rect.width, client_rect.height) == GUI_PROBE_CLIENT_SIZE, (
                    windows_text[:1500]
                )
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
                initial_names = _names(initial_tree)
                for name in exact_initial_names:
                    assert name in initial_names, (name, tree_text)

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
                    capture_backend=visual.capture_backend,
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
                await _pointer_move(client, hover_x, hover_y)
                hovered_tree = await _wait_for_accessible_name(
                    client,
                    GUI_PROBE_ACCESSIBILITY_SELECTOR,
                    "hover_status: entered",
                )
                assert "hover_status: idle" not in _names(hovered_tree), hovered_tree.render()[
                    :2500
                ]
                await _wait_for_material_repaint(
                    client,
                    visual.artifact_dir,
                    "gui-probe-hovered.png",
                    crops["Hover Target"],
                    hover_rect,
                    capture_backend=visual.capture_backend,
                    minimum_pixels=1000,
                    label="pointer_move hover target",
                )

                static_x, static_y = _center(rects["Unicode Sample"])
                await _pointer_move(client, static_x, static_y)
                idle_tree = await _wait_for_accessible_name(
                    client,
                    GUI_PROBE_ACCESSIBILITY_SELECTOR,
                    "hover_status: idle",
                )
                assert "hover_status: entered" not in _names(idle_tree), idle_tree.render()[:2500]
                no_cursor_image = await _wait_for_crop_match(
                    client,
                    visual.artifact_dir,
                    "gui-probe-pointer-without-cursor.png",
                    crops["Hover Target"],
                    hover_rect,
                    capture_backend=visual.capture_backend,
                    maximum_pixels=32,
                    maximum_absolute_difference=2000,
                    label="pointer_move away from hover target",
                )
                # Re-moving onto the same point leaves the scene unchanged; its
                # include_cursor frame differs from the plain capture only by the cursor.
                cursor_move = await _pointer_move(
                    client,
                    static_x,
                    static_y,
                    {"delays_ms": [0], "include_cursor": True},
                )
                assert [frame.delay_ms for frame in cursor_move.frames] == [0], cursor_move
                with_cursor_path = _preserve_png(
                    _frame_source(cursor_move.frames[0], capture_backend=visual.capture_backend),
                    visual.artifact_dir,
                    "gui-probe-pointer-with-cursor.png",
                )
                with_cursor_image = _load_rgb(with_cursor_path)
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
                burst, burst_text = await _pointer_click(
                    client,
                    animation_x,
                    animation_y,
                    {"delays_ms": requested_delays},
                )
                assert burst_text.startswith(
                    f"Clicked left at ({animation_x}, {animation_y})\nCaptured 4 frames:"
                ), burst_text
                sorted_delays = sorted(requested_delays)
                assert [frame.delay_ms for frame in burst.frames] == sorted_delays, burst_text
                # Frames are cropped below with logical element rectangles, which is
                # only valid because every frame reports the logical workspace at (0, 0).
                frame_sources = [
                    _frame_source(frame, capture_backend=visual.capture_backend)
                    for frame in burst.frames
                ]

                frame_images: list[Image.Image] = []
                for index, (delay, source) in enumerate(
                    zip(sorted_delays, frame_sources, strict=True)
                ):
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
                    f"{visual.capture_backend} burst animation frames were all materially identical"
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


async def test_kcalc_binary_values_have_significant_screen_capture_transitions() -> None:
    """Pair KCalc's binary AT-SPI value with preserved before/after pixels."""
    with nested_visual_kwin() as visual:
        _preserve_capture_backend(
            visual.artifact_dir,
            visual.capture_backend,
            visual.screenshot_service_available,
        )
        async with running_mcp_server(env=visual.capture_env) as client:
            connected = False
            body_succeeded = False
            app_log_path: Path | None = None
            try:
                await _connect(client, visual.dbus_address, visual.wayland_display)
                connected = True

                launched = await _launch(client, "kcalc")
                app_log_path = Path(launched.log_path)
                await _wait_for_element(client, "kcalc", "Seven", "button")

                await _wait_for_binary(client, "0")
                _, initial_image = await _capture_preserved(
                    client,
                    visual.artifact_dir,
                    "kcalc-initial-binary-0.png",
                    capture_backend=visual.capture_backend,
                )

                seven_rect = await _global_element_rect(client, "kcalc", "Seven")
                seven_x, seven_y = _center(seven_rect)
                await _pointer_click(client, seven_x, seven_y)
                await _wait_for_binary(client, "111")
                _, seven_image = await _capture_preserved(
                    client,
                    visual.artifact_dir,
                    "kcalc-after-seven-binary-111.png",
                    capture_backend=visual.capture_backend,
                )
                _assert_material_difference(
                    initial_image,
                    seven_image,
                    minimum_pixels=15,
                    label="KCalc initial to Seven",
                )

                zero_rect = await _global_element_rect(client, "kcalc", "Zero")
                zero_x, zero_y = _center(zero_rect)
                await _pointer_click(client, zero_x, zero_y)
                await _wait_for_binary(client, "1000110")
                _, zero_image = await _capture_preserved(
                    client,
                    visual.artifact_dir,
                    "kcalc-after-zero-binary-1000110.png",
                    capture_backend=visual.capture_backend,
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
