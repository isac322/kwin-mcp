"""Post-action ``capture`` frames on the 15 GUI-state mutators (S §3.3, G4).

The virtual KWin in the container never answers ScreenShot2, so every frame here
comes from the 0.11 frame-burst path that works in the container: a nested
Xvfb/X11 KWin (``visual_harness.nested_visual_kwin``) attached with
``session_connect``, with ``KWIN_MCP_X11_SCREENSHOT=1`` selecting scrot.

Engine tests share one module-scoped KCalc session; the stdio tests
(QA-CAP-010/011/017) start their own server against their own nested KWin.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import itertools
import json
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from _asserts import frame_png, has_kcalc_binary
from kwin_oracle import KWinOracle
from kwin_oracle import Rect as OracleRect
from mcp_harness import TOOL_NAMES, ProgressLog, running_mcp_server
from PIL import Image, ImageChops
from visual_harness import _apply_output_scale, nested_visual_kwin

from kwin_mcp import screenshot
from kwin_mcp.core import AutomationEngine
from kwin_mcp.models import (
    Capture,
    Frame,
    Locator,
    PartialRect,
    Point,
    PointerClickResult,
    Rect,
    StrictAddr,
    Wait,
    Window,
    WindowSelector,
)
from kwin_mcp.server import build_server

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from mcp_harness import McpTestClient
    from visual_harness import NestedVisualKWin

# Server/engine default --screenshot-max-edge for the module session (QA-CAP-012).
SERVER_MAX_EDGE = 640
SCREEN_SIZE = (1280, 800)
APP_WAIT = Wait(timeout_ms=15_000)
QT_ACCESSIBILITY_ENV = {"QT_ACCESSIBILITY": "1", "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1"}
PROBE_COMMAND = f"{sys.executable} /app/tests/e2e/interaction_probe.py"
PROBE_APP = "interaction_probe.py"
PROBE_ENV = {
    "GDK_BACKEND": "wayland",
    "GTK_MODULES": "gail:atk-bridge",
    "NO_AT_BRIDGE": "0",
    "XDG_SESSION_TYPE": "wayland",
}
FRAME_NAME = re.compile(r"^frame_\d{8}_\d{6}_(\d{3})_(\d+)ms(?:_\d+)?\.png$")
PROGRESS_FRAME = re.compile(r"^Frame (\d+)/(\d+) at (\d+)ms$")

# S §3.3: the 15 tools that mutate GUI-visible state take ``capture``.
CARRIERS = (
    "pointer_click",
    "pointer_move",
    "pointer_scroll",
    "pointer_drag",
    "pointer_hold",
    "keyboard_press",
    "keyboard_type",
    "keyboard_hold",
    "touch_tap",
    "touch_swipe",
    "touch_pinch",
    "element_act",
    "window_focus",
    "window_place",
    "window_close",
)


def _x11_env(visual: NestedVisualKWin) -> dict[str, str]:
    """The opt-in X11/scrot frame backend of the nested compositor (0.11 mechanism)."""
    return {"DISPLAY": visual.x_display, "KWIN_MCP_X11_SCREENSHOT": "1"}


@dataclass
class KCalcScene:
    """One nested KWin with KCalc, driven in-process by one engine."""

    engine: AutomationEngine
    visual: NestedVisualKWin
    oracle: KWinOracle
    artifacts_dir: Path

    def kcalc_window(self) -> Window:
        windows = [w for w in self.engine.window_list().windows if "kcalc" in w.app_id.lower()]
        assert len(windows) == 1, windows
        return windows[0]

    def button(self, name: str) -> Point:
        """Centre of a KCalc button, read live over AT-SPI2."""
        found = self.engine.element_query(Locator(app="kcalc", query=name, role="button"))
        rects = [el.rect for el in found.elements if el.name == name and el.rect is not None]
        assert len(rects) == 1, found.render()
        rect = rects[0]
        return Point(x=rect.x + rect.width // 2, y=rect.y + rect.height // 2)

    def wait_binary(self, expected: str) -> None:
        deadline = time.monotonic() + 10
        found = self.engine.element_query(Locator(app="kcalc", role="label"))
        while not has_kcalc_binary(found, expected):
            assert time.monotonic() < deadline, f"KCalc never showed {expected}:\n{found.render()}"
            time.sleep(0.2)
            found = self.engine.element_query(Locator(app="kcalc", role="label"))

    def clear(self) -> None:
        self.engine.element_act(
            StrictAddr(strict=Locator(app="kcalc", query="Clear All", role="button")), "Press"
        )
        self.wait_binary("0")

    def frame_files(self) -> set[Path]:
        return {p for p in self.artifacts_dir.rglob("*") if p.is_file()}


@pytest.fixture(scope="module")
def scene() -> Iterator[KCalcScene]:
    """Module-scoped nested KWin + KCalc, frames through the X11/scrot backend."""
    with pytest.MonkeyPatch.context() as patch, nested_visual_kwin() as visual:
        for key, value in _x11_env(visual).items():
            patch.setenv(key, value)
        engine = AutomationEngine(screenshot_max_edge=SERVER_MAX_EDGE)
        try:
            info = engine.session_connect(
                dbus_address=visual.dbus_address, wayland_display=visual.wayland_display
            )
            engine.app_launch("kcalc", env=QT_ACCESSIBILITY_ENV)
            ready = engine.element_query(
                Locator(app="kcalc", query="Seven", role="button"), wait=APP_WAIT
            )
            assert ready.wait is not None and ready.wait.satisfied, ready.render()
            built = KCalcScene(
                engine=engine,
                visual=visual,
                oracle=KWinOracle(visual.dbus_address),
                artifacts_dir=Path(info.artifacts_dir),
            )
            built.wait_binary("0")
            yield built
        finally:
            engine.session_stop()


def _frame_name(path: Path) -> tuple[int, int]:
    """``(index, delay_ms)`` from a ``frame_<ts>_<i>_<delay>ms.png`` name."""
    match = FRAME_NAME.match(path.name)
    assert match is not None, path.name
    return int(match.group(1)), int(match.group(2))


def _assert_frame(frame: Frame, delay_ms: int) -> Path:
    """A published burst frame: PNG on disk, sized as reported, size_kb from the file."""
    path = frame_png(frame)
    assert frame.delay_ms == delay_ms, frame
    assert _frame_name(path)[1] == delay_ms, path.name
    assert frame.size_kb == round(path.stat().st_size / 1024, 1), (frame, path.stat().st_size)
    assert frame.size_kb > 0, frame
    assert frame.uri == f"kwin://screenshots/{path.name}", frame
    return path


def _material_difference(first: Image.Image, second: Image.Image) -> int:
    """Pixels whose RGB difference exceeds 32 on any channel."""
    diff = ImageChops.difference(first.convert("RGB"), second.convert("RGB"))
    red, green, blue = diff.split()
    mask = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    return sum(mask.point(lambda v: 255 if v > 32 else 0).histogram()[255:])


# ── QA-CAP-015: every carrier takes capture; the others do not ───────────


@functools.cache
def _capture_parameters() -> dict[str, bool]:
    """Whether each published tool's input schema has a ``capture`` property."""
    tools = asyncio.run(build_server().list_tools())
    listing = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools]
    return {
        tool["name"]: "capture" in tool["inputSchema"].get("properties", {}) for tool in listing
    }


def _run_carrier(scene: KCalcScene, name: str, capture: Capture) -> Any:
    engine = scene.engine
    window = scene.kcalc_window()
    seven = scene.button("Seven")
    eight = scene.button("Eight")
    if name == "pointer_click":
        return engine.pointer_click(seven, capture=capture)
    if name == "pointer_move":
        return engine.pointer_move(eight, capture=capture)
    if name == "pointer_scroll":
        return engine.pointer_scroll(seven, 1, capture=capture)
    if name == "pointer_drag":
        return engine.pointer_drag(seven, eight, capture=capture)
    if name == "pointer_hold":
        try:
            return engine.pointer_hold("press", seven, capture=capture)
        finally:
            engine.pointer_hold("release", seven)
    if name == "keyboard_press":
        return engine.keyboard_press("Escape", capture=capture)
    if name == "keyboard_type":
        return engine.keyboard_type("1", capture=capture)
    if name == "keyboard_hold":
        try:
            return engine.keyboard_hold("press", "shift", capture=capture)
        finally:
            engine.keyboard_hold("release", "shift")
    if name == "touch_tap":
        return engine.touch_tap(seven, capture=capture)
    if name == "touch_swipe":
        return engine.touch_swipe(seven, eight, capture=capture)
    if name == "touch_pinch":
        return engine.touch_pinch(seven, 40, 80, capture=capture)
    if name == "element_act":
        return engine.element_act(
            StrictAddr(strict=Locator(app="kcalc", query="Seven", role="button")),
            "Press",
            capture=capture,
        )
    if name == "window_focus":
        return engine.window_focus(WindowSelector(id=window.id), capture=capture)
    if name == "window_place":
        return engine.window_place(
            WindowSelector(id=window.id),
            frame=PartialRect(x=window.frame.x, y=window.frame.y),
            capture=capture,
        )
    msg = f"unhandled carrier {name}"
    raise AssertionError(msg)


@contextmanager
def _probe_window(scene: KCalcScene) -> Generator[Window]:
    """A second, closeable window (the interaction probe) for window_close."""
    launched = scene.engine.app_launch(PROBE_COMMAND, env=PROBE_ENV)
    deadline = time.monotonic() + 15
    while True:
        windows = [w for w in scene.engine.window_list().windows if PROBE_APP in w.app_id]
        if windows:
            break
        assert time.monotonic() < deadline, scene.engine.app_log(launched.pid).render()
        time.sleep(0.2)
    try:
        yield windows[0]
    finally:
        scene.oracle.close(windows[0].id)


@pytest.mark.qa("QA-CAP-015")
@pytest.mark.parametrize("carrier", CARRIERS)
def test_capture_on_all_15_carriers(scene: KCalcScene, carrier: str) -> None:
    published = _capture_parameters()
    carriers = {name for name, has in published.items() if has}
    assert carriers == set(CARRIERS), sorted(carriers)
    for name in ("clipboard_write", "app_launch", "screen_capture", "element_query"):
        assert not published[name], name
    for name in TOOL_NAMES:
        parameters = inspect.signature(getattr(scene.engine, name)).parameters
        has_engine_capture = "capture" in parameters
        assert has_engine_capture == (name in CARRIERS), name

    capture = Capture(delays_ms=[0])
    if carrier == "window_close":
        with _probe_window(scene) as probe:
            # The MCP server passes the user's confirmation as consent="accepted"
            # (server.window_close -> engine._close); a live session refuses without it.
            result = scene.engine._close(WindowSelector(id=probe.id), capture, "accepted")
            assert result.requested, result
            assert result.window_id == probe.id, result
    elif carrier == "window_focus":
        window = scene.kcalc_window()
        title = (
            window.frame.x,
            window.frame.y,
            window.frame.x + window.frame.width,
            window.client.y,
        )
        assert title[3] > title[1], window
        # Newer Breeze draws active and inactive windows alike, so activation alone
        # changes no pixel there. Focusing also raises: cover KCalc's titlebar with
        # the probe window, then the frame after window_focus must show KCalc on top.
        with _probe_window(scene) as probe:
            # The probe is fixed-size: KWin moves it to the titlebar but keeps its size.
            cover = OracleRect(window.frame.x, window.frame.y, window.frame.width, 200)
            assert scene.oracle.set_frame(probe.id, cover)
            assert scene.oracle.activate(probe.id)
            scene.oracle.wait_for_windows(
                lambda ws: any(
                    w.id == probe.id
                    and w.active
                    and w.frame is not None
                    and (round(w.frame.x), round(w.frame.y)) == (round(cover.x), round(cover.y))
                    for w in ws
                )
            )
            covered = scene.engine.screen_capture(max_edge=0)
            result = scene.engine.window_focus(
                WindowSelector(id=window.id), capture=Capture(delays_ms=[100], max_edge=0)
            )
            assert result.window is not None and result.window.active, result
            [frame] = result.frames
            path = _assert_frame(frame, 100)
            with Image.open(covered.path) as before, Image.open(path) as after:
                changed = _material_difference(before.crop(title), after.crop(title))
        assert changed >= 50, (changed, title)
        return
    else:
        result = _run_carrier(scene, carrier, capture)
    assert not result.warnings, result
    [frame] = result.frames
    _assert_frame(frame, 0)
    assert frame.mapping is not None, frame
    assert frame.coverage == "full", frame


# ── QA-CAP-012: capture options are screen_capture's ─────────────────────


@pytest.mark.qa("QA-CAP-012")
def test_capture_options_match_screen_capture(scene: KCalcScene) -> None:
    engine = scene.engine
    region = Rect(x=10, y=20, width=300, height=200)
    cursor = Point(x=100, y=100)
    engine.pointer_move(cursor)

    burst = engine.keyboard_press(
        "a", capture=Capture(delays_ms=[0], region=region, max_edge=100, include_cursor=True)
    )
    assert not burst.warnings, burst
    [framed] = burst.frames
    framed_path = _assert_frame(framed, 0)
    shot = engine.screen_capture(region=region, max_edge=100, include_cursor=True)
    bare = engine.screen_capture(region=region, max_edge=100, include_cursor=False)

    for frame in (framed, shot, bare):
        assert frame.width == 100, frame
        assert frame.height == round(100 * region.height / region.width), frame
        assert frame.mapping is not None, frame
        assert frame.mapping.origin == Point(x=10, y=20), frame
        assert frame.mapping.scale == pytest.approx(100 / 300), frame
        assert frame.coverage == "full", frame
    assert framed.coordinate_space == shot.coordinate_space, (framed, shot)

    # Cursor drawn in both: each with-cursor image differs from the bare one near
    # the pointer, and the burst frame equals the screen_capture image there.
    scale = 100 / 300
    cx = round((cursor.x - region.x) * scale)
    cy = round((cursor.y - region.y) * scale)
    box = (max(0, cx - 8), max(0, cy - 8), min(100, cx + 12), min(67, cy + 12))
    with Image.open(framed_path) as a, Image.open(shot.path) as b, Image.open(bare.path) as c:
        assert _material_difference(a.crop(box), c.crop(box)) >= 3, box
        assert _material_difference(b.crop(box), c.crop(box)) >= 3, box
        assert _material_difference(a, b) <= 10

    # Omitted options: whole workspace, no cursor, the engine's (server's) max_edge.
    default = engine.keyboard_press("a", capture=Capture(delays_ms=[0]))
    [plain] = default.frames
    plain_path = _assert_frame(plain, 0)
    full = engine.screen_capture()
    assert (plain.width, plain.height) == (SERVER_MAX_EDGE, 400), plain
    assert (full.width, full.height) == (plain.width, plain.height), full
    assert plain.mapping is not None and full.mapping is not None
    assert plain.mapping.origin == Point(x=0, y=0), plain
    assert plain.mapping.scale == pytest.approx(SERVER_MAX_EDGE / SCREEN_SIZE[0]), plain
    assert plain.coverage == "full", plain
    with Image.open(plain_path) as a, Image.open(full.path) as b:
        assert _material_difference(a, b) <= 10


# ── QA-CAP-013: [] = no frames ───────────────────────────────────────────


@pytest.mark.qa("QA-CAP-013")
def test_empty_delays_means_no_frames(scene: KCalcScene) -> None:
    before = scene.frame_files()
    empty = scene.engine.keyboard_press("a", capture=Capture(delays_ms=[]))
    assert empty.frames == [], empty
    assert empty.warnings == [], empty
    assert "Captured" not in empty.render(), empty.render()
    none = scene.engine.keyboard_press("a", capture=None)
    assert none.frames == [], none
    assert none.warnings == [], none
    assert scene.frame_files() == before


# ── QA-CAP-014: a failure after commit is a warning ──────────────────────


@pytest.mark.qa("QA-CAP-014")
def test_capture_failure_after_commit_is_warning_not_error(
    scene: KCalcScene, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_scrot = screenshot._capture_via_scrot
    scrot_calls: list[Path] = []

    def scrot_fails_after_first(output_path: Path, *, include_cursor: bool = False) -> None:
        scrot_calls.append(output_path)
        if len(scrot_calls) > 1:
            msg = "scrot failed after the first frame in this test"
            raise RuntimeError(msg)
        real_scrot(output_path, include_cursor=include_cursor)

    def unavailable(*_args: object, **_kwargs: object) -> None:
        msg = "unavailable in this test"
        raise RuntimeError(msg)

    scene.clear()
    before = scene.frame_files()
    monkeypatch.setattr(screenshot, "_capture_via_scrot", scrot_fails_after_first)
    monkeypatch.setattr(screenshot, "_capture_frame_burst_dbus", unavailable)
    monkeypatch.setattr(screenshot, "_capture_frame_burst_spectacle", unavailable)

    pressed = scene.engine.keyboard_press("7", capture=Capture(delays_ms=[0, 200]))
    assert len(scrot_calls) == 2, scrot_calls
    assert pressed.frames == [], pressed
    [warning] = pressed.warnings
    assert warning.startswith(
        "frame capture failed after the action: Frame burst capture failed: X11/scrot ("
    ), warning
    assert "scrot failed after the first frame in this test" in warning, warning
    assert f"Warning: {warning}" in pressed.render().splitlines(), pressed.render()
    # The press committed exactly once: KCalc shows 7 (binary 111), not 77.
    scene.wait_binary("111")

    scrot_calls.clear()
    acted = scene.engine.element_act(
        StrictAddr(strict=Locator(app="kcalc", query="Seven", role="button")),
        "Press",
        capture=Capture(delays_ms=[0, 200]),
    )
    assert acted.dispatch in {"confirmed", "sent_unconfirmed"}, acted
    assert acted.frames == [], acted
    [warning] = acted.warnings
    assert "scrot failed after the first frame in this test" in warning, warning
    # One activation: 77 = binary 1001101.
    scene.wait_binary("1001101")
    # The failed burst published nothing.
    assert {p for p in scene.frame_files() - before if p.suffix == ".png"} == set()


# ── QA-CAP-016: a topology shift mid-burst ───────────────────────────────


@pytest.mark.qa("QA-CAP-016")
@pytest.mark.slow
@pytest.mark.timeout(600)
def test_topology_shift_mid_burst(monkeypatch: pytest.MonkeyPatch) -> None:
    with nested_visual_kwin() as visual:
        for key, value in _x11_env(visual).items():
            monkeypatch.setenv(key, value)
        home = visual.artifact_dir / "home"
        isolated_home = {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
        }
        real_scrot = screenshot._capture_via_scrot
        calls: list[float] = []

        def scrot_with_rescale(output_path: Path, *, include_cursor: bool = False) -> None:
            calls.append(time.monotonic())
            if len(calls) == 2:
                # KWin's output scale changes while the second frame is captured.
                _apply_output_scale(
                    dbus_address=visual.dbus_address,
                    wayland_display=visual.wayland_display,
                    scale=1.25,
                    log_path=visual.artifact_dir / "kscreen-doctor-mid-burst.log",
                    isolated_home=isolated_home,
                )
            real_scrot(output_path, include_cursor=include_cursor)

        monkeypatch.setattr(screenshot, "_capture_via_scrot", scrot_with_rescale)
        engine = AutomationEngine()
        try:
            engine.session_connect(
                dbus_address=visual.dbus_address, wayland_display=visual.wayland_display
            )
            result = engine.keyboard_press("shift", capture=Capture(delays_ms=[0, 500]))
            first, second = result.frames
            _assert_frame(first, 0)
            _assert_frame(second, 500)
        finally:
            engine.session_stop()
    assert not result.warnings, result
    assert calls[1] - calls[0] >= 0.45, calls
    assert first.mapping is not None, first
    assert second.mapping is None, second
    assert second.coordinate_space == (
        "Coordinate space: unavailable (output topology or windows changed while this frame "
        "was captured); backend x11-scrot"
    ), second.coordinate_space
    text = result.render()
    assert f"    {second.coordinate_space}" in text.splitlines(), text
    assert "scale unavailable" in text, text


# ── stdio tests ──────────────────────────────────────────────────────────


class TimedProgress(ProgressLog):
    """ProgressLog that also stamps each notification with CLOCK_REALTIME on receipt."""

    def __init__(self) -> None:
        super().__init__()
        self.received_ns: list[int] = []

    async def __call__(self, progress: float, total: float | None, message: str | None) -> None:
        self.received_ns.append(time.clock_gettime_ns(time.CLOCK_REALTIME))
        await super().__call__(progress, total, message)


async def _connect(client: McpTestClient, visual: NestedVisualKWin) -> None:
    info, _ = await client.call_structured(
        "session_connect",
        {"dbus_address": visual.dbus_address, "wayland_display": visual.wayland_display},
    )
    assert info["kind"] == "live", info


def _probe_events(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.qa("QA-CAP-010")
@pytest.mark.anyio
async def test_capture_returns_frames_sorted_with_duplicates(tmp_path: Path) -> None:
    log_path = tmp_path / "probe-events.jsonl"
    with nested_visual_kwin() as visual:
        async with running_mcp_server(env=_x11_env(visual)) as client:
            await _connect(client, visual)
            try:
                await client.call_structured(
                    "app_launch",
                    {
                        "command": PROBE_COMMAND,
                        "env": {**PROBE_ENV, "KWIN_MCP_PROBE_EVENT_LOG": str(log_path)},
                    },
                )
                found, _ = await client.call_structured(
                    "element_query",
                    {
                        "locator": {"app": PROBE_APP, "query": "Click Target"},
                        "wait": {"timeout_ms": 15_000},
                    },
                )
                [target] = [
                    el
                    for el in found["elements"]
                    if el["name"] == "Click Target" and el["rect"] is not None
                ]
                rect = target["rect"]
                point = {"x": rect["x"] + rect["width"] // 2, "y": rect["y"] + rect["height"] // 2}
                progress = TimedProgress()
                started_ns = time.clock_gettime_ns(time.CLOCK_REALTIME)
                structured, text = await client.call_structured(
                    "pointer_click",
                    {"target": point, "capture": {"delays_ms": [300, 0, 300, 1200]}},
                    progress_callback=progress,
                )
                await progress.settle()
                frames = PointerClickResult.model_validate(structured).frames
                assert [f.delay_ms for f in frames] == [0, 300, 300, 1200], text
                # Session files are removed by session_stop; check them while they exist.
                paths = [_assert_frame(f, f.delay_ms or 0) for f in frames]
                mtimes = [path.stat().st_mtime_ns for path in paths]

                # Anchor check: delays run from the end of the action, not the call. A
                # 1.5 s hold puts a call-anchored 2.5 s frame only ~1 s after the release.
                held_ns = time.clock_gettime_ns(time.CLOCK_REALTIME)
                held, _ = await client.call_structured(
                    "pointer_click",
                    {"target": point, "hold_ms": 1500, "capture": {"delays_ms": [2500]}},
                )
                [held_frame] = PointerClickResult.model_validate(held).frames
                held_mtime = _assert_frame(held_frame, 2500).stat().st_mtime_ns
            finally:
                await client.call_structured("session_stop")
    # The two 300 ms frames are two distinct files.
    assert len(set(paths)) == 4, paths
    indexes = [_frame_name(path)[0] for path in paths]
    assert indexes == [0, 1, 2, 3], paths
    assert "Captured 4 frames:" in text.splitlines(), text
    assert [line.split(": ", 1)[0].strip() for line in text.splitlines() if "ms: " in line] == [
        "0ms",
        "300ms",
        "300ms",
        "1200ms",
    ], text

    # Oracle: the probe stamps every raw event with CLOCK_REALTIME; each frame's
    # capture time is its PNG mtime (published by hard link, so scrot's write time).
    events = _probe_events(log_path)
    clicked = [e for e in events if started_ns <= e["real_ns"] < held_ns]
    kinds = [e["kind"] for e in clicked if e["kind"] != "motion"]
    assert kinds == ["button_press", "button_release"], clicked
    last_event_ns = max(e["real_ns"] for e in clicked)
    # Frames are captured one after another, never batched: strictly increasing,
    # including the two 300 ms duplicates, and each frame's progress arrives in order.
    assert all(a < b for a, b in itertools.pairwise(mtimes)), mtimes
    assert all(a <= b for a, b in itertools.pairwise(progress.received_ns)), progress.received_ns
    offsets_ms = [(m - last_event_ns) / 1e6 for m in mtimes]
    # A15 timing: lower bound = requested delay - 10 %; upper bound = 3x the delay plus
    # the backend's one-frame latency (the 0 ms frame's offset: topology observation
    # and scrot), so a drifting schedule fails without a fixed +-N ms window.
    latency_ms = offsets_ms[0]
    for delay, offset in zip([0, 300, 300, 1200], offsets_ms, strict=True):
        assert offset >= 0.9 * delay, (delay, offsets_ms)
        assert offset <= 3 * (delay + latency_ms), (delay, offsets_ms)
    held_events = [e for e in events if e["real_ns"] >= held_ns]
    held_kinds = [e["kind"] for e in held_events if e["kind"] != "motion"]
    assert held_kinds == ["button_press", "button_release"], held_events
    press_ns, release_ns = (e["real_ns"] for e in held_events if e["kind"] != "motion")
    assert (release_ns - press_ns) / 1e6 >= 0.9 * 1500, held_events
    assert (held_mtime - release_ns) / 1e6 >= 0.9 * 2500, (held_mtime, release_ns)

    frame_messages = [m for _, _, m in progress.events if m and PROGRESS_FRAME.match(m)]
    assert frame_messages == [
        "Frame 1/4 at 0ms",
        "Frame 2/4 at 300ms",
        "Frame 3/4 at 300ms",
        "Frame 4/4 at 1200ms",
    ], progress.events


@pytest.mark.qa("QA-CAP-011")
@pytest.mark.slow
@pytest.mark.timeout(600)
@pytest.mark.anyio
async def test_large_burst_no_cap() -> None:
    delays = list(range(51))
    with nested_visual_kwin() as visual:
        async with running_mcp_server(env=_x11_env(visual)) as client:
            await _connect(client, visual)
            try:
                progress = ProgressLog()
                structured, text = await client.call_structured(
                    "keyboard_press",
                    {"keys": "a", "capture": {"delays_ms": delays}},
                    progress_callback=progress,
                )
                await progress.settle()
                started = time.monotonic()
                late, _ = await client.call_structured(
                    "keyboard_press", {"keys": "a", "capture": {"delays_ms": [40_000]}}
                )
                elapsed = time.monotonic() - started
                late_files = [Path(f["path"]).is_file() for f in late["frames"]]
                burst_files = [Path(f["path"]).is_file() for f in structured["frames"]]
            finally:
                await client.call_structured("session_stop")
    frames = structured["frames"]
    assert [f["delay_ms"] for f in frames] == delays, text
    assert len({f["path"] for f in frames}) == 51
    assert "Captured 51 frames:" in text.splitlines(), text
    messages = [m for _, _, m in progress.events]
    assert messages == [f"Frame {i + 1}/51 at {d}ms" for i, d in enumerate(delays)], messages
    assert progress.values == sorted(progress.values), progress.values
    assert all(total == 51 for _, total, _ in progress.events), progress.events
    [late_frame] = late["frames"]
    assert late_frame["delay_ms"] == 40_000, late
    assert late_files == [True], late
    assert burst_files == [True] * 51
    assert elapsed >= 36.0, elapsed


@pytest.mark.qa("QA-CAP-017")
@pytest.mark.anyio
async def test_negative_and_huge_delays() -> None:
    with nested_visual_kwin() as visual:
        async with running_mcp_server(env=_x11_env(visual)) as client:
            await _connect(client, visual)
            try:
                # SPEC-GAP-10 pin: a negative delay is clamped to 0, not rejected.
                negative, text = await client.call_structured(
                    "keyboard_press", {"keys": "a", "capture": {"delays_ms": [-100]}}
                )
                timings: dict[str, float] = {}

                async def burst() -> None:
                    start = time.monotonic()
                    held, _ = await client.call_structured(
                        "keyboard_press", {"keys": "a", "capture": {"delays_ms": [3000]}}
                    )
                    timings["burst"] = time.monotonic() - start
                    timings["frames"] = len(held["frames"])

                async def info() -> None:
                    await anyio.sleep(0.05)
                    start = time.monotonic()
                    await client.call_structured("session_info")
                    timings["info"] = time.monotonic() - start

                async with anyio.create_task_group() as group:
                    group.start_soon(burst)
                    group.start_soon(info)
            finally:
                await client.call_structured("session_stop")
    [frame] = negative["frames"]
    assert frame["delay_ms"] == 0, negative
    assert _frame_name(Path(frame["path"])) == (0, 0), frame
    assert negative["warnings"] == [], negative
    assert "  0ms: " in text, text
    assert timings["frames"] == 1, timings
    assert timings["burst"] >= 2.7, timings
    # A15: the stated 2.9 s lower bound minus 10 %.
    assert timings["info"] >= 0.9 * 2.9, timings


# ── ScreenShot2 burst scheduling (QA-CAP-010, boundary stubs) ────────────
#
# No compositor in the container answers ScreenShot2, so only its I/O
# boundary (the D-Bus connection, the per-frame topology read, and the raw
# frame fetch) is stubbed here; the real ``_capture_frame_burst_dbus`` loop
# runs and the oracle is the mtime each frame's PNG gets inside the loop.

_STUB_SIZE = (64, 64)
_STUB_TOPOLOGY = screenshot.WorkspaceTopology(
    outputs=(screenshot.OutputGeometry("stub", 0, 0, 64, 64, 1.0),),
    virtual=(0, 0, 64, 64),
)
_STUB_FRAME = screenshot._RawFrame(
    data=bytes(_STUB_SIZE[0] * _STUB_SIZE[1] * 4),
    width=_STUB_SIZE[0],
    height=_STUB_SIZE[1],
    stride=_STUB_SIZE[0] * 4,
    format=4,
    scale=1.0,
)


class _StubBus:
    """D-Bus connection stand-in; only the object lookup is ever called."""

    def get_object(self, *_args: object, **_kwargs: object) -> object:
        return object()


@pytest.mark.qa("QA-CAP-010")
def test_screenshot2_burst_delays_run_from_burst_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Frame delays are offsets from the burst's start, not gaps between frames.

    Two 300 ms frames are captured back to back: the second adds no wait. A
    loop that re-reads the start time per frame lands [0,300,300,1200] at
    ~0/300/600/1800 ms instead of ~0/300/300/1200 ms and fails the bounds.
    """
    monkeypatch.delenv("KWIN_MCP_X11_SCREENSHOT", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(screenshot.dbus.bus, "BusConnection", lambda _address: _StubBus())
    monkeypatch.setattr(screenshot.dbus, "Interface", lambda _obj, _name: object())
    monkeypatch.setattr(screenshot, "query_topology", lambda _address: _STUB_TOPOLOGY)
    monkeypatch.setattr(screenshot, "_capture_raw_frame", lambda _iface, _opts: _STUB_FRAME)

    delays = [0, 300, 300, 1200]
    started = time.monotonic()
    started_wall = time.time()
    frames = screenshot.capture_frame_burst("unix:path=/nonexistent", tmp_path, delays)
    elapsed = time.monotonic() - started

    assert [delay for delay, _, _ in frames] == delays
    assert [mapping.backend for _, _, mapping in frames] == ["screenshot2"] * len(delays)
    mtimes = [path.stat().st_mtime for _, path, _ in frames]
    # No frame is written before its offset from the call: a loop that skipped the
    # wait would write every frame at once (20 ms covers file-time rounding). The
    # first frame's own encoding time lies inside each later offset, so the bound
    # is taken from the call, not from the first frame.
    for delay, mtime in zip(delays, mtimes, strict=True):
        assert mtime - started_wall >= delay / 1000 - 0.02, (started_wall, delays, mtimes)
    assert elapsed >= 1.2, elapsed
    # The two equal delays ran back to back; a per-frame start adds 0.3 s.
    assert mtimes[2] - mtimes[1] <= 0.2, mtimes
    # The last frame lands at ~1.2 s; a per-frame start sums the delays to >= 1.8 s.
    assert elapsed <= 1.5, elapsed
