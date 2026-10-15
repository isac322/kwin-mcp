"""screen_capture: Frame fields, regions, max_edge defaults and multi-output layouts.

Pixel captures run against the nested X11 KWin of ``visual_harness`` with
``KWIN_MCP_X11_SCREENSHOT=1``: the container's virtual KWin never answers ScreenShot2.
The mixed-scale refusals run on a test-owned virtual KWin with two outputs, where
ScreenShot2 fails and Spectacle is the only remaining backend.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from _asserts import CoordinateSpace, coordinate_spaces, frame_png, screenshot_path
from mcp_harness import running_mcp_server
from PIL import Image, ImageChops, ImageDraw
from pydantic import ValidationError
from session_harness import live_kwin
from test_screen_capture_visual import (
    ANIMATION_BLUE,
    GUI_PROBE_APP,
    GUI_PROBE_COMMAND,
    X11_BACKEND,
    assert_full_frame,
    blue_in,
    capture,
    connect,
    element_rect,
    kwin_screens,
    largest_color_region,
    launch_gui_probe,
    status,
    stop_live,
    wait_status_change,
    write_stderr,
    x11_engine,
    x11_env,
)
from visual_harness import nested_visual_kwin

from kwin_mcp.models import Capture, Frame, ImageMapping, Locator, Point, Rect, Wait

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from mcp_harness import McpTestClient
    from visual_harness import NestedVisualKWin

    from kwin_mcp.core import AutomationEngine

SCREEN_SIZE = (1280, 800)
SCREENSHOT_NAME = re.compile(r"screenshot_\d{8}_\d{6}(_\d+)?\.png")
OUTPUT_TIMEOUT_SECONDS = 15.0


@pytest.fixture
def anyio_backend() -> str:
    """The installed MCP client runs on asyncio."""
    return "asyncio"


def launch_gui_probe_engine(engine: AutomationEngine) -> Rect:
    """Launch gui_probe.py in the engine's session; return its Animation Target rect."""
    engine.app_launch(
        GUI_PROBE_COMMAND,
        env={
            "GDK_BACKEND": "wayland",
            "GTK_MODULES": "gail:atk-bridge",
            "NO_AT_BRIDGE": "0",
            "XDG_SESSION_TYPE": "wayland",
        },
    )
    found = engine.element_query(
        Locator(app=GUI_PROBE_APP, query="Animation Target"), wait=Wait(timeout_ms=15_000)
    )
    assert found.wait is not None and found.wait.satisfied, found.render()
    exact = [e for e in found.elements if e.name == "Animation Target"]
    assert len(exact) == 1 and exact[0].rect is not None, found.render()
    return exact[0].rect


def png_header(path: Path) -> tuple[str, tuple[int, int]]:
    with Image.open(path) as image:
        return image.format or "", image.size


def directory_files(directory: Path) -> list[Path]:
    return sorted(directory.iterdir())


# ── QA-CAP-001: the Frame fields ──────────────────────────────────────────


@pytest.mark.visual
@pytest.mark.qa("QA-CAP-001")
def test_frame_fields_mapping_coverage_size_kb_uri(
    engine: AutomationEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    with x11_engine(engine, monkeypatch):
        frame = engine.screen_capture()

        path = Path(frame.path)
        assert path.is_absolute() and path.is_file(), frame
        assert SCREENSHOT_NAME.fullmatch(path.name), path.name
        assert frame.uri == f"kwin://screenshots/{path.name}", frame
        assert png_header(path) == ("PNG", SCREEN_SIZE), frame
        assert (frame.width, frame.height) == SCREEN_SIZE, frame
        assert frame.mapping == ImageMapping(scale=1.0, origin=Point(x=0, y=0)), frame
        assert frame.coverage == "full", frame
        assert frame.size_kb == round(path.stat().st_size / 1024, 1), frame
        assert frame.delay_ms is None, frame

        text = frame.render()
        lines = text.splitlines()
        assert lines[0] == f"Screenshot saved: {path} ({frame.size_kb:.1f} KB)", text
        assert screenshot_path(text) == path, text
        assert coordinate_spaces(text) == [
            CoordinateSpace((0, 0), SCREEN_SIZE, X11_BACKEND, "full")
        ], text
        assert lines[1] == frame.coordinate_space, text
        assert lines[2] == f"Image: 1280x800 px; scale 1; resource {frame.uri}", text

        # A second capture in the same second never reuses the name.
        again = engine.screen_capture()
        assert again.path != frame.path, (again, frame)
        assert SCREENSHOT_NAME.fullmatch(Path(again.path).name), again.path
        assert path.is_file(), path


# ── QA-CAP-003: regions inside, partly outside and fully outside ─────────


@pytest.mark.visual
@pytest.mark.qa("QA-CAP-003")
def test_region_outside_workspace_dropped_partially_intersected(
    engine: AutomationEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    with x11_engine(engine, monkeypatch):
        launch_gui_probe_engine(engine)
        full = engine.screen_capture(max_edge=0)
        full_path = assert_full_frame(full, SCREEN_SIZE)
        assert largest_color_region(full_path, ANIMATION_BLUE) is not None, full

        def assert_crop_matches_full(frame: Frame, box: tuple[int, int, int, int]) -> None:
            with Image.open(full_path) as whole, Image.open(frame_png(frame)) as part:
                expected = whole.convert("RGB").crop(box)
                assert part.size == expected.size, (part.size, box)
                assert ImageChops.difference(expected, part.convert("RGB")).getbbox() is None

        inside = engine.screen_capture(region=Rect(x=100, y=100, width=200, height=150))
        assert (inside.width, inside.height) == (200, 150), inside
        assert inside.mapping == ImageMapping(scale=1.0, origin=Point(x=100, y=100)), inside
        assert inside.coverage == "full", inside
        assert coordinate_spaces(inside.render()) == [
            CoordinateSpace((100, 100), (200, 150), X11_BACKEND, "full")
        ], inside.render()
        assert_crop_matches_full(inside, (100, 100, 300, 250))

        # The part outside the 1280x800 workspace is dropped: 80x100 remains.
        partial = engine.screen_capture(region=Rect(x=1200, y=700, width=200, height=200))
        assert (partial.width, partial.height) == (80, 100), partial
        assert partial.mapping == ImageMapping(scale=1.0, origin=Point(x=1200, y=700)), partial
        assert [(s.origin, s.size) for s in coordinate_spaces(partial.render())] == [
            ((1200, 700), (80, 100))
        ], partial.render()
        assert_crop_matches_full(partial, (1200, 700, 1280, 800))
        # Every pixel of the clipped crop was captured, so it reports full coverage
        # (coverage describes the image's pixels, as the 0.11 coordinate line did).
        assert partial.coverage == "full", partial

        # Fully outside: an error naming the workspace, and no file is left behind.
        shots = Path(full.path).parent
        before = directory_files(shots)
        with pytest.raises(ValueError, match=r"outside the captured workspace \(0, 0, 1280x800\)"):
            engine.screen_capture(region=Rect(x=5000, y=5000, width=10, height=10))
        assert directory_files(shots) == before

        # A zero or negative size is a validation error before any capture.
        for width, height in ((0, 10), (10, -5), (-1, -1)):
            with pytest.raises(ValidationError, match="greater than 0"):
                Rect(x=0, y=0, width=width, height=height)
        assert directory_files(shots) == before


# ── QA-CAP-004: max_edge 0 / null / N and the server default ─────────────


@asynccontextmanager
async def max_edge_client(visual: NestedVisualKWin, max_edge: int) -> AsyncGenerator[McpTestClient]:
    async with running_mcp_server(
        "--screenshot-max-edge", str(max_edge), env=x11_env(visual)
    ) as client:
        connected = False
        try:
            await connect(client, visual)
            connected = True
            yield client
        finally:
            write_stderr(visual, client)
            if connected:
                await stop_live(client)


def assert_scaled(frame: Frame, size: tuple[int, int], scale: float) -> None:
    assert png_header(frame_png(frame)) == ("PNG", size), frame
    assert (frame.width, frame.height) == size, frame
    assert frame.mapping == ImageMapping(scale=scale, origin=Point(x=0, y=0)), frame
    assert frame.coverage == "full", frame
    assert [(s.origin, s.size) for s in coordinate_spaces(frame.render())] == [
        ((0, 0), SCREEN_SIZE)
    ], frame.render()


@pytest.mark.anyio
@pytest.mark.visual
@pytest.mark.qa("QA-CAP-004")
async def test_max_edge_zero_full_null_server_default(tmp_path: Path) -> None:
    with nested_visual_kwin() as visual:
        async with max_edge_client(visual, 640) as client:
            await launch_gui_probe(client)
            rect = await element_rect(client, GUI_PROBE_APP, "Animation Target")

            # null = the server's --screenshot-max-edge.
            default, _, bbox = await blue_in(client, {}, tmp_path / "default.png")
            assert_scaled(default, (640, 400), 0.5)
            null_frame, _ = await capture(client, {"max_edge": None})
            assert_scaled(null_frame, (640, 400), 0.5)
            # 0 = full resolution, N = this call's bound.
            full, _ = await capture(client, {"max_edge": 0})
            assert_scaled(full, SCREEN_SIZE, 1.0)
            small, _ = await capture(client, {"max_edge": 320})
            assert_scaled(small, (320, 200), 0.25)

            # logical = origin + image_px / scale, checked on the button's centre.
            assert default.mapping is not None
            scale = default.mapping.scale
            centre_x = default.mapping.origin.x + round((bbox[0] + bbox[2]) / 2 / scale)
            centre_y = default.mapping.origin.y + round((bbox[1] + bbox[3]) / 2 / scale)
            assert rect[0] <= centre_x < rect[0] + rect[2], (rect, centre_x)
            assert rect[1] <= centre_y < rect[1] + rect[3], (rect, centre_y)

            # A capture option without max_edge follows the server default too.
            before = await status(client, GUI_PROBE_APP, "animation_status:")
            clicked, _ = await client.call_structured(
                "pointer_click",
                {"target": {"x": centre_x, "y": centre_y}, "capture": {"delays_ms": [0]}},
            )
            assert clicked["at"] == {"x": centre_x, "y": centre_y}, clicked
            (burst_frame,) = (Frame.model_validate(f) for f in clicked["frames"])
            assert burst_frame.delay_ms == 0, burst_frame
            assert_scaled(burst_frame, (640, 400), 0.5)
            await wait_status_change(client, GUI_PROBE_APP, "animation_status:", before)

            moved, _ = await client.call_structured(
                "pointer_move",
                {"target": {"x": 10, "y": 10}, "capture": {"delays_ms": [0], "max_edge": 320}},
            )
            (bounded,) = (Frame.model_validate(f) for f in moved["frames"])
            assert_scaled(bounded, (320, 200), 0.25)

            error = "\n".join(
                await client.call_tool_error("screen_capture", {"max_edge": -1}, lines=None)
            )
            assert "max_edge" in error, error
            capture_error = "\n".join(
                await client.call_tool_error(
                    "pointer_move",
                    {"target": {"x": 10, "y": 10}, "capture": {"delays_ms": [0], "max_edge": -1}},
                    lines=None,
                )
            )
            assert "max_edge" in capture_error, capture_error

        async with max_edge_client(visual, 400) as client:
            default, _ = await capture(client)
            assert_scaled(default, (400, 250), 0.3125)
            full, _ = await capture(client, {"max_edge": 0})
            assert_scaled(full, SCREEN_SIZE, 1.0)
            moved, _ = await client.call_structured(
                "pointer_move", {"target": {"x": 20, "y": 20}, "capture": {"delays_ms": [0]}}
            )
            (burst_frame,) = (Frame.model_validate(f) for f in moved["frames"])
            assert_scaled(burst_frame, (400, 250), 0.3125)


# ── multi-output layouts ─────────────────────────────────────────────────


def kscreen_doctor(*arguments: str, dbus_address: str, wayland_display: str, home: Path) -> None:
    """Run kscreen-doctor against a test-owned compositor with an isolated config home."""
    for subdir in (".config", ".local/share", ".local/state"):
        (home / subdir).mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "DBUS_SESSION_BUS_ADDRESS": dbus_address,
        "WAYLAND_DISPLAY": wayland_display,
        "QT_QPA_PLATFORM": "wayland",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
    }
    env.pop("DISPLAY", None)
    result = subprocess.run(
        ["kscreen-doctor", *arguments],
        env=env,
        capture_output=True,
        text=True,
        timeout=OUTPUT_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, (arguments, result.stdout, result.stderr)
    # kscreen-doctor reports a refused configuration on stdout and still exits 0.
    assert "applying config failed" not in result.stdout, (arguments, result.stdout)


def wait_for_screens(
    dbus_address: str, predicate: Callable[[list[dict[str, Any]]], bool]
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + OUTPUT_TIMEOUT_SECONDS
    screens = kwin_screens(dbus_address)
    while not predicate(screens):
        if time.monotonic() >= deadline:
            raise AssertionError(f"KWin outputs never reached the layout: {screens}")
        time.sleep(0.2)
        screens = kwin_screens(dbus_address)
    return screens


def screen_box(screen: dict[str, Any]) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of a KWin output in logical px."""
    left, top = round(screen["x"]), round(screen["y"])
    return left, top, left + round(screen["width"]), top + round(screen["height"])


MIXED_SCALE = 1.45


@pytest.mark.qa("QA-CAP-007")
def test_mixed_scale_refusal_live_kwin(
    engine: AutomationEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A live mixed-scale layout: KWin keeps it composable, so no refusal is issued.

    The 0.11 refusal ("Spectacle X cannot capture this layout: mixed output scales
    with ... outside its canvas anchored at logical (0, 0)") needs an output at a
    negative origin. A live KWin never produces one: KWin 6.3 re-normalizes a
    negative position so the layout's top-left is (0, 0), and newer kscreen-doctor
    refuses a negative position outright. The test therefore asks for the layout
    KWin 6.3 normalizes to (scaled output left, at the origin); the unit test
    ``test_spectacle_mixed_scale_refused`` (fake topology) is the refusal oracle.
    Here ScreenShot2 is absent (virtual backend), the layout guard lets Spectacle
    start, and the capture fails with both backends named and no file left.
    """
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("KWIN_MCP_X11_SCREENSHOT", raising=False)
    with live_kwin(output_count=2) as live:
        screens = wait_for_screens(live.dbus_address, lambda s: len(s) == 2)
        name = next(s for s in screens if round(s["x"]) != 0)["name"]
        other = next(s for s in screens if s["name"] != name)["name"]
        kscreen_doctor(
            f"output.{name}.scale.{MIXED_SCALE:g}",
            f"output.{name}.position.0,0",
            f"output.{other}.position.{round(1280 / MIXED_SCALE)},0",
            dbus_address=live.dbus_address,
            wayland_display=live.wayland_display,
            home=tmp_path / "kscreen-home",
        )
        screens = wait_for_screens(
            live.dbus_address,
            lambda s: any(x["name"] == name and abs(x["scale"] - MIXED_SCALE) < 1e-3 for x in s),
        )
        # Mixed scales, with the layout's top-left at (0, 0).
        assert sorted(round(s["scale"], 2) for s in screens) == [1.0, MIXED_SCALE], screens
        boxes = [screen_box(s) for s in screens]
        assert (min(b[0] for b in boxes), min(b[1] for b in boxes)) == (0, 0), screens

        info = engine.session_connect(
            dbus_address=live.dbus_address, wayland_display=live.wayland_display
        )
        assert info.kind == "live", info.render()
        try:
            shots = Path(info.artifacts_dir)
            before = directory_files(shots)
            with pytest.raises(RuntimeError) as failure:
                engine.screen_capture()
            message = str(failure.value)
            assert message.startswith("Screenshot capture failed: ScreenShot2 ("), message
            assert "; Spectacle (spectacle " in message, message
            assert "cannot capture this layout" not in message, message
            assert directory_files(shots) == before
        finally:
            engine.session_stop()


@pytest.mark.visual
@pytest.mark.qa("QA-CAP-008")
def test_region_unproven_mapping_live(
    engine: AutomationEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A region on a layout whose mapping cannot be proven is an error, never a Frame.

    Two X11 outputs of equal physical size (the second at scale 2, so the layout is
    mixed-scale) cannot be told apart in the X root capture, ScreenShot2 is absent
    and Spectacle cannot run: no backend proves a mapping, so the region is refused.
    """
    with nested_visual_kwin(output_count=2) as visual:
        screens = wait_for_screens(visual.dbus_address, lambda s: len(s) == 2)
        name = next(s for s in screens if round(s["x"]) != 0)["name"]
        kscreen_doctor(
            f"output.{name}.scale.2",
            dbus_address=visual.dbus_address,
            wayland_display=visual.wayland_display,
            home=visual.artifact_dir / "home",
        )
        wait_for_screens(
            visual.dbus_address,
            lambda s: any(x["name"] == name and abs(x["scale"] - 2) < 1e-3 for x in s),
        )
        monkeypatch.setenv("DISPLAY", visual.x_display)
        monkeypatch.setenv("KWIN_MCP_X11_SCREENSHOT", "1")
        info = engine.session_connect(
            dbus_address=visual.dbus_address, wayland_display=visual.wayland_display
        )
        assert info.kind == "live", info.render()
        try:
            shots = Path(info.artifacts_dir)
            before = directory_files(shots)
            region = Rect(x=0, y=0, width=200, height=100)
            with pytest.raises(RuntimeError) as failure:
                engine.screen_capture(region=region)
            message = str(failure.value)
            assert message.startswith("Screenshot capture failed: X11/scrot ("), message
            assert "coordinate mapping cannot be proven" in message, message
            assert "; ScreenShot2 (" in message and "; Spectacle (" in message, message
            assert directory_files(shots) == before
            # The capture option of an action fails as a warning: its frames come from
            # the burst path, whose unproven mapping is refused at the crop.
            pressed = engine.keyboard_press("shift", capture=Capture(delays_ms=[0], region=region))
            assert pressed.frames == [], pressed.render()
            assert len(pressed.warnings) == 1, pressed.render()
            assert pressed.warnings[0].startswith(
                "frame capture failed after the action: Cannot crop to a region: "
                "Coordinate space: unavailable (cannot locate exactly one X window per KWin output"
            ), pressed.render()
            assert directory_files(shots) == before
        finally:
            engine.session_stop()


SHORT_HEIGHT = 560
_XWININFO_CHILD = re.compile(r"^\s+(0x[0-9a-f]+) .*\s(\d+)x(\d+)\+(-?\d+)\+(-?\d+)\s", re.MULTILINE)


def x11_window_at(display: str, x: int, y: int) -> str:
    """The id of the one top-level X window whose top-left corner is at (x, y)."""
    tree = subprocess.run(
        ["xwininfo", "-root", "-children", "-display", display],
        capture_output=True,
        text=True,
        timeout=OUTPUT_TIMEOUT_SECONDS,
        check=True,
    ).stdout
    ids = [
        window
        for window, _, _, wx, wy in _XWININFO_CHILD.findall(tree)
        if (int(wx), int(wy)) == (x, y)
    ]
    assert len(ids) == 1, tree
    return ids[0]


@pytest.mark.slow
@pytest.mark.timeout(600)
@pytest.mark.visual
@pytest.mark.qa("QA-CAP-009")
def test_uncaptured_areas_transparent(
    engine: AutomationEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two outputs of different heights: the gap of the union is transparent.

    KWin's X11 windowed backend sizes each output by its X window, so shrinking the
    second output's window makes it 1280x560 next to the first 1280x800. (Equal
    physical sizes would make the X11 mapping ambiguous, as in 0.11.)
    """
    with nested_visual_kwin(output_count=2) as visual:
        screens = wait_for_screens(visual.dbus_address, lambda s: len(s) == 2)
        second = next(s for s in screens if round(s["x"]) != 0)
        name = second["name"]
        window = x11_window_at(visual.x_display, round(second["x"]), round(second["y"]))
        subprocess.run(
            ["xdotool", "windowsize", window, str(round(second["width"])), str(SHORT_HEIGHT)],
            env={**os.environ, "DISPLAY": visual.x_display},
            check=True,
            timeout=OUTPUT_TIMEOUT_SECONDS,
        )
        screens = wait_for_screens(
            visual.dbus_address,
            lambda s: any(x["name"] == name and round(x["height"]) == SHORT_HEIGHT for x in s),
        )
        boxes = [screen_box(s) for s in screens]
        heights = {bottom - top for _, top, _, bottom in boxes}
        assert len(heights) == 2, screens
        left = min(b[0] for b in boxes)
        top = min(b[1] for b in boxes)
        union = (max(b[2] for b in boxes) - left, max(b[3] for b in boxes) - top)

        monkeypatch.setenv("DISPLAY", visual.x_display)
        monkeypatch.setenv("KWIN_MCP_X11_SCREENSHOT", "1")
        info = engine.session_connect(
            dbus_address=visual.dbus_address, wayland_display=visual.wayland_display
        )
        assert info.kind == "live", info.render()
        try:
            frame = engine.screen_capture(max_edge=0)
            with Image.open(frame_png(frame)) as image:
                mode = image.mode
                alpha = image.getchannel("A") if mode == "RGBA" else None
        finally:
            engine.session_stop()
        assert mode == "RGBA" and alpha is not None, mode

        assert (frame.width, frame.height) == union, (frame, screens)
        assert frame.mapping == ImageMapping(scale=1.0, origin=Point(x=left, y=top)), frame
        # Coverage is per output, as the 0.11 coordinate line stated it: every output
        # was captured whole, so it is "full" even though the union has a gap. The
        # gap, which belongs to no output, is transparent instead (checked below).
        assert frame.coverage == "full", frame
        assert [(s.origin, s.size, s.coverage) for s in coordinate_spaces(frame.render())] == [
            ((left, top), union, "full")
        ], frame.render()

        # Expected alpha: opaque inside every output, transparent everywhere else.
        expected = Image.new("L", union, 0)
        draw = ImageDraw.Draw(expected)
        for box_left, box_top, box_right, box_bottom in boxes:
            draw.rectangle(
                (box_left - left, box_top - top, box_right - left - 1, box_bottom - top - 1),
                fill=255,
            )
        opaque = alpha.point(lambda value: 255 if value else 0)
        assert ImageChops.difference(opaque, expected).getbbox() is None, (
            screens,
            ImageChops.difference(opaque, expected).getbbox(),
        )
        gap = ImageChops.invert(expected)
        assert ImageChops.multiply(alpha, gap).getbbox() is None, "uncaptured pixels not alpha 0"
