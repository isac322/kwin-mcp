"""--screenshot-images over stdio: one ImageContent per frame on every capture tool.

Successful frames need the nested X11 KWin (``KWIN_MCP_X11_SCREENSHOT=1`` plus
``session_connect``): the virtual KWin in the container never answers ScreenShot2.
A window_zoo started outside kwin-mcp gives the window and element carriers a target.
"""

from __future__ import annotations

import io
import itertools
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from mcp.types import ImageContent, TextContent
from mcp_harness import (
    ERAS,
    TOOL_NAMES,
    ProgressLog,
    ScriptedUser,
    images,
    render_structured,
    running_client,
    running_mcp_server,
    text_of,
)
from PIL import Image
from visual_harness import nested_visual_kwin
from window_zoo import APP_ID, ZooProcess

from kwin_mcp.core import NO_SESSION_TEXT

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Iterator

    from mcp.types import CallToolResult
    from mcp_harness import McpTestClient, ModernMcpTestClient
    from visual_harness import NestedVisualKWin

    Client = McpTestClient | ModernMcpTestClient

IMAGES_FLAG = "--screenshot-images"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
FRAME_DELAYS = [0, 100]
# Where the zoo's main window is pinned before any carrier runs.
ZOO_ORIGIN = {"x": 40, "y": 40}
# A point on the empty workspace, right of the pinned zoo window.
FREE_POINT = {"x": 1150, "y": 700}
WINDOW_TIMEOUT_S = 15.0
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
OK_BUTTON = {"app": APP_ID, "query": "OK", "role": "button"}


@pytest.fixture
def anyio_backend() -> str:
    """The installed MCP client runs on asyncio."""
    return "asyncio"


def _visual_env(visual: NestedVisualKWin) -> dict[str, str]:
    return {"DISPLAY": visual.x_display, "KWIN_MCP_X11_SCREENSHOT": "1"}


def _frames_of(name: str, structured: dict[str, Any]) -> list[dict[str, Any]]:
    """The Frame dicts of a result: the result itself for screen_capture, else ``frames``."""
    if name == "screen_capture":
        return [structured]
    frames = structured["frames"]
    assert isinstance(frames, list), structured
    return frames


def _png_size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        assert image.format == "PNG", image.format
        return image.size


def _checked(name: str, result: CallToolResult) -> tuple[dict[str, Any], str]:
    """A successful result: the text first, equal to render() of the structured content."""
    assert not result.is_error, text_of(result)
    first = result.content[0]
    assert isinstance(first, TextContent), result.content
    assert [c for c in result.content if isinstance(c, TextContent)] == [first], result.content
    structured = result.structured_content
    assert structured is not None, first.text
    assert first.text == render_structured(name, structured), first.text
    return structured, first.text


def _assert_images_are_frames(name: str, result: CallToolResult) -> list[dict[str, Any]]:
    """content == [Text, Image x len(frames)]; each image/png equals its Frame.path bytes."""
    structured, _ = _checked(name, result)
    frames = _frames_of(name, structured)
    assert frames, f"{name} captured no frame: {structured}"
    assert [type(c) for c in result.content] == [TextContent] + [ImageContent] * len(frames), (
        name,
        result.content,
    )
    assert all(
        isinstance(c, ImageContent) and c.mime_type == "image/png" for c in result.content[1:]
    ), result.content
    decoded = images(result)
    for frame, data in zip(frames, decoded, strict=True):
        assert data.startswith(PNG_SIGNATURE), data[:16]
        assert data == Path(frame["path"]).read_bytes(), frame["path"]
        assert _png_size(data) == (frame["width"], frame["height"]), frame
    return frames


def _assert_no_images(name: str, result: CallToolResult) -> list[dict[str, Any]]:
    structured, _ = _checked(name, result)
    assert len(result.content) == 1, result.content
    assert images(result) == []
    frames = _frames_of(name, structured)
    assert all(Path(frame["path"]).is_file() for frame in frames), frames
    return frames


async def _connect(client: Client, visual: NestedVisualKWin) -> None:
    info, _ = await client.call_structured(
        "session_connect",
        {"dbus_address": visual.dbus_address, "wayland_display": visual.wayland_display},
    )
    assert info["kind"] == "live", info
    assert info["input_backend"] == "eis", info


async def _stop(client: Client) -> None:
    stopped, _ = await client.call_structured("session_stop")
    assert stopped["stopped"] is True and stopped["kind"] == "live", stopped


async def _zoo_windows(client: Client, caption: str) -> list[dict[str, Any]]:
    """The zoo's windows whose caption is exactly ``caption``, once KWin lists one."""
    with anyio.fail_after(WINDOW_TIMEOUT_S):
        while True:
            listing, _ = await client.call_structured(
                "window_list", {"filter": {"app_id": APP_ID, "caption": caption}}
            )
            found = [w for w in listing["windows"] if w["caption"] == caption]
            if found:
                return found
            await anyio.sleep(0.2)


@dataclass
class _Desk:
    """The connected session and its zoo: what every carrier call needs."""

    client: Client
    zoo: ZooProcess
    user: ScriptedUser
    main_id: str


@asynccontextmanager
async def _desk(visual: NestedVisualKWin, era: str, *server_args: str) -> AsyncGenerator[_Desk]:
    """A fresh zoo plus a server connected to ``visual``, the zoo window pinned and on AT-SPI."""
    user = ScriptedUser()
    with ZooProcess(
        wayland_display=visual.wayland_display, dbus_address=visual.dbus_address
    ) as zoo:
        zoo.expect("window: Zoo")
        async with running_client(
            era, *server_args, env=_visual_env(visual), elicitation_callback=user
        ) as client:
            await _connect(client, visual)
            try:
                (main,) = await _zoo_windows(client, "Zoo")
                placed, _ = await client.call_structured(
                    "window_place", {"window": {"id": main["id"]}, "frame": ZOO_ORIGIN}
                )
                frame = placed["window"]["frame"]
                assert frame["x"] + frame["width"] < FREE_POINT["x"] - 50, placed
                found, _ = await client.call_structured(
                    "element_query",
                    {"locator": OK_BUTTON, "wait": {"until": "present", "timeout_ms": 15_000}},
                )
                if not found["wait"]["satisfied"]:
                    dump = await client.call_text(
                        "element_query", {"locator": {"query": "OK"}, "max_depth": 64}
                    )
                    raise AssertionError(f"{found}\n{dump[:3000]}")
                yield _Desk(client, zoo, user, main["id"])
            finally:
                (visual.artifact_dir / "mcp-server.stderr.log").write_text(
                    client.stderr_text(), encoding="utf-8"
                )
                await _stop(client)


async def _carrier_call(
    desk: _Desk,
    name: str,
    capture: dict[str, Any],
    check: Callable[[str, CallToolResult], list[dict[str, Any]]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run carrier ``name`` once with ``capture``; cleanup calls (releases) carry no capture."""
    client = desk.client
    arguments: dict[str, Any]
    match name:
        case "pointer_click" | "pointer_move" | "touch_tap":
            arguments = {"target": FREE_POINT}
        case "pointer_scroll":
            arguments = {"target": FREE_POINT, "delta": 2}
        case "pointer_drag":
            arguments = {"from": FREE_POINT, "to": {"x": FREE_POINT["x"] + 40, "y": 600}}
        case "pointer_hold":
            arguments = {"phase": "press", "target": FREE_POINT}
        case "keyboard_press":
            arguments = {"keys": "shift"}
        case "keyboard_type":
            arguments = {"text": "a"}
        case "keyboard_hold":
            arguments = {"phase": "press", "keys": "shift"}
        case "touch_swipe":
            arguments = {"from": FREE_POINT, "to": {"x": FREE_POINT["x"] - 100, "y": 650}}
        case "touch_pinch":
            arguments = {"center": FREE_POINT, "start_distance": 120, "end_distance": 40}
        case "element_act":
            arguments = {"element": {"strict": OK_BUTTON}, "action": "click"}
        case "window_focus":
            arguments = {"window": {"id": desk.main_id}}
        case "window_place":
            arguments = {"window": {"id": desk.main_id}, "frame": {"x": 60, "y": 60}}
        case "window_close":
            desk.zoo.command("utility", "window: Zoo Utility")
            (utility,) = await _zoo_windows(client, "Zoo Utility")
            desk.user.answer = "confirm"
            arguments = {"window": {"id": utility["id"]}}
        case _:
            raise AssertionError(name)
    result = await client.call_result(name, {**arguments, "capture": capture})
    frames = check(name, result)
    assert [frame["delay_ms"] for frame in frames] == capture["delays_ms"], frames
    structured = result.structured_content
    assert structured is not None
    match name:
        case "pointer_hold":
            await client.call_structured("pointer_hold", {"phase": "release", "target": FREE_POINT})
        case "keyboard_hold":
            await client.call_structured("keyboard_hold", {"phase": "release", "keys": "shift"})
        case "window_close":
            assert structured["requested"] is True, structured
            assert structured["consent"] == "accepted", structured
            desk.zoo.expect("closed: Zoo Utility")
        case _:
            pass
    return structured, frames


@pytest.fixture(scope="module")
def visual() -> Iterator[NestedVisualKWin]:
    """One nested X11 KWin for the module; each test starts its own zoo and server."""
    with nested_visual_kwin() as nested:
        yield nested


# ── ported from 0.11 test_mcp_features.py ───────────────────────────────


def _no_session_lines(tool: str) -> tuple[str, ...]:
    """The two-line no-session ToolError, as the SDK prefixes it with the tool name."""
    what, remedy = NO_SESSION_TEXT.split("\n")
    return (f"Error executing tool {tool}: {what}", remedy)


@pytest.mark.anyio
async def test_images_flag_error_result_has_no_images_and_server_survives() -> None:
    async with running_mcp_server(IMAGES_FLAG) as client:
        shot = await client.call_result("screen_capture")
        assert shot.is_error, shot.content
        assert images(shot) == [], shot.content
        assert await client.call_tool_error("screen_capture") == _no_session_lines("screen_capture")

        moved_args = {"target": {"x": 1, "y": 1}, "capture": {"delays_ms": [0]}}
        moved = await client.call_result("pointer_move", moved_args)
        assert moved.is_error, moved.content
        assert images(moved) == [], moved.content
        assert await client.call_tool_error("pointer_move", moved_args) == _no_session_lines(
            "pointer_move"
        )

        tools = await client.list_tools()
        assert frozenset(tool.name for tool in tools) == frozenset(TOOL_NAMES)


@pytest.mark.anyio
@pytest.mark.visual
async def test_screen_capture_images_flag_attaches_captured_pngs_in_order(
    visual: NestedVisualKWin,
) -> None:
    """With --screenshot-images, each captured PNG follows the unchanged text block."""
    async with running_mcp_server(IMAGES_FLAG, env=_visual_env(visual)) as client:
        await _connect(client, visual)
        try:
            shot = await client.call_result("screen_capture")
            (frame,) = _assert_images_are_frames("screen_capture", shot)
            assert frame["coverage"] == "full", frame

            frame_progress = ProgressLog()
            moved = await client.call_result(
                "pointer_move",
                {"target": {"x": 200, "y": 200}, "capture": {"delays_ms": FRAME_DELAYS}},
                progress_callback=frame_progress,
            )
            frames = _assert_images_are_frames("pointer_move", moved)
            assert [f["delay_ms"] for f in frames] == FRAME_DELAYS, frames
            assert len({f["path"] for f in frames}) == len(FRAME_DELAYS), frames

            await frame_progress.settle()
            events = frame_progress.events
            assert len(events) == len(FRAME_DELAYS), events
            values = frame_progress.values
            assert all(later > earlier for earlier, later in itertools.pairwise(values)), values
            assert all(total == len(FRAME_DELAYS) for _, total, _ in events), events
            assert [message for _, _, message in events] == [
                f"Frame {index}/{len(FRAME_DELAYS)} at {delay}ms"
                for index, delay in enumerate(FRAME_DELAYS, start=1)
            ], events
        finally:
            (visual.artifact_dir / "mcp-server.stderr.log").write_text(
                client.stderr_text(), encoding="utf-8"
            )
            await _stop(client)


@pytest.mark.anyio
@pytest.mark.visual
async def test_screen_capture_results_carry_no_images_without_flag(
    visual: NestedVisualKWin,
) -> None:
    """Without the opt-in flag, capturing tools return only their text block."""
    async with running_mcp_server(env=_visual_env(visual)) as client:
        await _connect(client, visual)
        try:
            shot = await client.call_result("screen_capture")
            _assert_no_images("screen_capture", shot)

            moved = await client.call_result(
                "pointer_move",
                {"target": {"x": 200, "y": 200}, "capture": {"delays_ms": FRAME_DELAYS}},
            )
            frames = _assert_no_images("pointer_move", moved)
            assert [f["delay_ms"] for f in frames] == FRAME_DELAYS, frames
        finally:
            await _stop(client)


# ── QA ─────────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.visual
@pytest.mark.qa("QA-CAP-018")
@pytest.mark.parametrize("carrier", CARRIERS)
async def test_images_on_every_carrier(visual: NestedVisualKWin, carrier: str) -> None:
    """Under --screenshot-images each carrier attaches one image/png per frame, bytes == file."""
    async with _desk(visual, ERAS[0], IMAGES_FLAG) as desk:
        _, frames = await _carrier_call(
            desk, carrier, {"delays_ms": [0]}, _assert_images_are_frames
        )
        assert len(frames) == 1, frames


# Per-call values: frame files and sizes, KWin window ids, AT-SPI refs.
_VOLATILE: dict[str, object] = {
    "path": "<path>",
    "uri": "<uri>",
    "size_kb": 0.0,
    "id": "<id>",
    "window_id": "<id>",
    "ref": "<ref>",
}


def _normalized(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _VOLATILE[key] if key in _VOLATILE else _normalized(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    return value


def _normalized_content(structured: dict[str, Any]) -> dict[str, Any]:
    """``structured`` with its volatile fields (paths, times, refs) replaced."""
    normalized = _normalized(structured)
    assert isinstance(normalized, dict), normalized
    return normalized


async def _capture_run(
    visual: NestedVisualKWin, era: str, *, flag: bool
) -> dict[str, tuple[dict[str, Any], str]]:
    """Run screen_capture and every carrier; return normalized structured content and text."""
    check = _assert_images_are_frames if flag else _assert_no_images
    args = (IMAGES_FLAG,) if flag else ()
    outcome: dict[str, tuple[dict[str, Any], str]] = {}
    async with _desk(visual, era, *args) as desk:
        shot = await desk.client.call_result("screen_capture")
        check("screen_capture", shot)
        structured, _ = _checked("screen_capture", shot)
        normalized = _normalized_content(structured)
        outcome["screen_capture"] = (
            normalized,
            render_structured("screen_capture", normalized),
        )
        for name in CARRIERS:
            structured, frames = await _carrier_call(desk, name, {"delays_ms": [0, 50]}, check)
            assert len(frames) == 2, (name, frames)
            normalized = _normalized_content(structured)
            outcome[name] = (normalized, render_structured(name, normalized))

        failed = await desk.client.call_result(
            "window_focus",
            {"window": {"id": "kwin-mcp-no-such-window"}, "capture": {"delays_ms": [0]}},
        )
        assert failed.is_error, failed.content
        assert failed.structured_content is None, failed.structured_content
        assert images(failed) == [], failed.content
    return outcome


@pytest.mark.anyio
@pytest.mark.visual
@pytest.mark.qa("QA-MCP-005")
@pytest.mark.parametrize("era", ERAS)
async def test_images_flag_attaches_frames_on_every_capture_tool(
    visual: NestedVisualKWin, era: str
) -> None:
    """The flag adds only ImageContent: structured content and text match without it."""
    plain = await _capture_run(visual, era, flag=False)
    flagged = await _capture_run(visual, era, flag=True)
    assert list(flagged) == ["screen_capture", *CARRIERS]
    for name, (structured, text) in plain.items():
        assert flagged[name][0] == structured, (name, flagged[name][0], structured)
        assert flagged[name][1] == text, (name, flagged[name][1], text)
