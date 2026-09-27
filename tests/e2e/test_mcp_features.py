"""Installed-package coverage for tool annotations, progress, and opt-in screenshot images."""

from __future__ import annotations

import base64
import io
import itertools
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from _asserts import coordinate_spaces, screenshot_path
from mcp.types import ImageContent, TextContent
from mcp_harness import EXPECTED_TOOL_NAMES, ProgressLog, running_mcp_server
from PIL import Image
from visual_harness import nested_visual_kwin

if TYPE_CHECKING:
    from mcp.types import CallToolResult, Tool
    from mcp_harness import McpTestClient

SCREENSHOT_IMAGES_FLAG = "--screenshot-images"
SCREENSHOT_PREFIX = "Screenshot saved: "
SESSION_REQUIRED_GUIDANCE = "Call session_start or session_connect first."
SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800
MISSING_ELEMENT = "kwin-mcp-e2e-element-that-never-exists"
WAIT_TIMEOUT_MS = 1500
FRAME_DELAYS = [0, 100]
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_FRAME_LINE = re.compile(r"^\s+(\d+)ms: (.+?) \((\d+(?:\.\d+)?) KB\)$", re.MULTILINE)
_WAYLAND_SOCKET = re.compile(r"wayland-mcp-\S+")

# Wire (camelCase) ToolAnnotations per tool. Read-only tools deliberately omit
# destructiveHint/idempotentHint: they are meaningless when readOnlyHint is true.
EXPECTED_ANNOTATIONS: dict[str, dict[str, bool]] = {
    "session_start": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
    "session_connect": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
    "session_stop": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "screenshot": {"readOnlyHint": True, "openWorldHint": False},
    "accessibility_tree": {"readOnlyHint": True, "openWorldHint": False},
    "find_ui_elements": {"readOnlyHint": True, "openWorldHint": False},
    "mouse_click": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "mouse_move": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "mouse_scroll": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "mouse_drag": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "mouse_button_down": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "mouse_button_up": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "keyboard_type": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "keyboard_type_unicode": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "keyboard_key": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "keyboard_key_down": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "keyboard_key_up": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "touch_tap": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "touch_swipe": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "touch_pinch": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "touch_multi_swipe": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "clipboard_get": {"readOnlyHint": True, "openWorldHint": False},
    "clipboard_set": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "wait_for_element": {"readOnlyHint": True, "openWorldHint": False},
    "launch_app": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
    "list_windows": {"readOnlyHint": True, "openWorldHint": False},
    "focus_window": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "window_geometry": {"readOnlyHint": True, "openWorldHint": False},
    "active_window": {"readOnlyHint": True, "openWorldHint": False},
    "window_close": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "dbus_call": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
    "read_app_log": {"readOnlyHint": True, "openWorldHint": False},
    "wayland_info": {"readOnlyHint": True, "openWorldHint": False},
}


@pytest.fixture
def anyio_backend() -> str:
    """The installed MCP client runs on asyncio."""
    return "asyncio"


def _text_blocks(result: CallToolResult) -> list[str]:
    return [block.text for block in result.content if isinstance(block, TextContent)]


def _image_blocks(result: CallToolResult) -> list[ImageContent]:
    return [block for block in result.content if isinstance(block, ImageContent)]


def _only_text(result: CallToolResult) -> str:
    """The single text block of a successful result that carries no images."""
    assert not result.is_error, result.content
    assert len(result.content) == 1, result.content
    block = result.content[0]
    assert isinstance(block, TextContent), result.content
    return block.text


def _tool_table(tools: list[Tool]) -> dict[str, dict[str, object]]:
    """Wire form of everything a client sees per tool, keyed by tool name."""
    return {tool.name: tool.model_dump(by_alias=True, mode="json") for tool in tools}


def _assert_strictly_increasing(values: list[float]) -> None:
    assert all(later > earlier for earlier, later in itertools.pairwise(values)), values


def _normalized_start_text(text: str) -> str:
    """session_start text minus the per-session socket name."""
    return _WAYLAND_SOCKET.sub("wayland-mcp-*", text)


def _decoded_png(block: ImageContent) -> bytes:
    assert block.mime_type == "image/png", block.mime_type
    data = base64.b64decode(block.data, validate=True)
    assert data.startswith(PNG_SIGNATURE), data[:16]
    return data


def _png_size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        assert image.format == "PNG"
        return image.size


def _frame_paths(output: str) -> list[Path]:
    matches = _FRAME_LINE.findall(output)
    assert [int(delay) for delay, _, _ in matches] == FRAME_DELAYS, output
    return [Path(path) for _, path, _ in matches]


@pytest.mark.anyio
async def test_tools_publish_annotations_and_keep_schemas_with_and_without_images_flag() -> None:
    """Every tool carries its hint table; the images flag changes no tool definition."""
    assert frozenset(EXPECTED_ANNOTATIONS) == EXPECTED_TOOL_NAMES

    async with running_mcp_server() as client:
        plain = (await client.session.list_tools()).tools
    async with running_mcp_server(SCREENSHOT_IMAGES_FLAG) as client:
        flagged = (await client.session.list_tools()).tools
        # The flag is stripped before the SDK parses argv, so the server stays usable.
        assert await client.session.send_ping() is not None

    plain_table = _tool_table(plain)
    assert frozenset(plain_table) == EXPECTED_TOOL_NAMES
    assert _tool_table(flagged) == plain_table

    for tool in plain:
        assert tool.annotations is not None, tool.name
        published = tool.annotations.model_dump(by_alias=True, exclude_none=True)
        assert published == EXPECTED_ANNOTATIONS[tool.name], (tool.name, published)

        # Context injection must stay invisible in the published input schema.
        properties = tool.input_schema.get("properties", {})
        assert "ctx" not in properties, (tool.name, tool.input_schema)
        assert "ctx" not in tool.input_schema.get("required", []), (tool.name, tool.input_schema)

        # Plain text tools keep the SDK's wrapped `{"result": string}` output schema.
        output_schema = tool.output_schema
        assert isinstance(output_schema, dict), (tool.name, output_schema)
        assert output_schema.get("type") == "object", (tool.name, output_schema)
        assert output_schema.get("required") == ["result"], (tool.name, output_schema)
        output_properties = output_schema.get("properties")
        assert isinstance(output_properties, dict), (tool.name, output_schema)
        assert list(output_properties) == ["result"], (tool.name, output_schema)
        assert output_properties["result"].get("type") == "string", (tool.name, output_schema)


@pytest.mark.anyio
async def test_images_flag_error_result_has_no_images_and_server_survives() -> None:
    async with running_mcp_server(SCREENSHOT_IMAGES_FLAG) as client:
        result = await client.call_result("screenshot")
        assert result.is_error, result.content
        assert _image_blocks(result) == [], result.content
        error_text = "\n".join(_text_blocks(result))
        assert SESSION_REQUIRED_GUIDANCE in error_text, error_text

        frames = await client.call_result(
            "mouse_move", {"x": 1, "y": 1, "screenshot_after_ms": [0]}
        )
        assert frames.is_error, frames.content
        assert _image_blocks(frames) == [], frames.content

        tools = await client.session.list_tools()
        assert frozenset(tool.name for tool in tools.tools) == EXPECTED_TOOL_NAMES


@pytest.mark.anyio
async def test_long_running_tools_report_increasing_progress_only_when_requested() -> None:
    """session_start and wait_for_element stream progress; without a token nothing changes."""
    session_arguments = {"screen_width": SCREEN_WIDTH, "screen_height": SCREEN_HEIGHT}
    wait_arguments = {"query": MISSING_ELEMENT, "timeout_ms": WAIT_TIMEOUT_MS}

    async with running_mcp_server() as client:
        session_running = False
        try:
            start_progress = ProgressLog()
            start_result = await client.call_result(
                "session_start", session_arguments, progress_callback=start_progress
            )
            session_running = not start_result.is_error
            started = _only_text(start_result)
            assert started.startswith("Session started."), started
            await start_progress.settle()
            assert len(start_progress.events) >= 4, start_progress.events
            _assert_strictly_increasing(start_progress.values)
            with_message = [message for _, _, message in start_progress.events if message]
            assert len(with_message) >= 4, start_progress.events

            wait_progress = ProgressLog()
            waited = _only_text(
                await client.call_result(
                    "wait_for_element", wait_arguments, progress_callback=wait_progress
                )
            )
            assert waited.startswith(f"Timeout after {WAIT_TIMEOUT_MS}ms"), waited
            await wait_progress.settle()
            assert len(wait_progress.events) >= 3, wait_progress.events
            _assert_strictly_increasing(wait_progress.values)

            # No progressToken: the same calls succeed with the same text.
            unreported = await client.call_result("wait_for_element", wait_arguments)
            assert _only_text(unreported) == waited

            stopped = await client.call_text("session_stop")
            session_running = False
            assert stopped == "Session stopped.", stopped

            restart_result = await client.call_result("session_start", session_arguments)
            session_running = not restart_result.is_error
            restarted = _only_text(restart_result)
            assert _normalized_start_text(restarted) == _normalized_start_text(started), (
                started,
                restarted,
            )
        finally:
            if session_running:
                await client.call_text("session_stop")


async def _connect_visual(client: McpTestClient, dbus_address: str, wayland_display: str) -> None:
    output = await client.call_text(
        "session_connect",
        {"dbus_address": dbus_address, "wayland_display": wayland_display},
    )
    assert output.startswith("Connected to live KWin session."), output


@pytest.mark.anyio
@pytest.mark.visual
async def test_screenshot_images_flag_attaches_captured_pngs_in_order() -> None:
    """With --screenshot-images, each captured PNG follows the unchanged text block."""
    with nested_visual_kwin() as visual:
        async with running_mcp_server(
            SCREENSHOT_IMAGES_FLAG,
            env={"DISPLAY": visual.x_display, "KWIN_MCP_X11_SCREENSHOT": "1"},
        ) as client:
            connected = False
            try:
                await _connect_visual(client, visual.dbus_address, visual.wayland_display)
                connected = True

                shot = await client.call_result("screenshot")
                assert not shot.is_error, shot.content
                assert len(shot.content) == 2, shot.content
                text_block, image_block = shot.content
                assert isinstance(text_block, TextContent), shot.content
                assert isinstance(image_block, ImageContent), shot.content
                text = text_block.text
                assert text.startswith(SCREENSHOT_PREFIX), text
                assert shot.structured_content == {"result": text}
                png = _decoded_png(image_block)
                assert png == screenshot_path(text).read_bytes()
                spaces = coordinate_spaces(text)
                assert len(spaces) == 1, text
                assert _png_size(png) == spaces[0].size, (_png_size(png), spaces[0])

                frame_progress = ProgressLog()
                moved = await client.call_result(
                    "mouse_move",
                    {"x": 200, "y": 200, "screenshot_after_ms": FRAME_DELAYS},
                    progress_callback=frame_progress,
                )
                assert not moved.is_error, moved.content
                assert len(moved.content) == 1 + len(FRAME_DELAYS), moved.content
                moved_text = moved.content[0]
                assert isinstance(moved_text, TextContent), moved.content
                assert moved.structured_content == {"result": moved_text.text}
                frames = _image_blocks(moved)
                assert moved.content[1:] == frames, moved.content
                frame_paths = _frame_paths(moved_text.text)
                assert [_decoded_png(frame) for frame in frames] == [
                    path.read_bytes() for path in frame_paths
                ]

                await frame_progress.settle()
                assert len(frame_progress.events) == len(FRAME_DELAYS), frame_progress.events
                _assert_strictly_increasing(frame_progress.values)
                assert all(
                    total == len(FRAME_DELAYS) and message
                    for _, total, message in frame_progress.events
                ), frame_progress.events
            finally:
                (visual.artifact_dir / "mcp-server.stderr.log").write_text(
                    client.stderr_text(), encoding="utf-8"
                )
                if connected:
                    stop_output = await client.call_text("session_stop")
                    assert stop_output == "Disconnected from live session.", stop_output


@pytest.mark.anyio
@pytest.mark.visual
async def test_screenshot_results_carry_no_images_without_flag() -> None:
    """Without the opt-in flag, capturing tools return only their text block."""
    with nested_visual_kwin() as visual:
        async with running_mcp_server(
            env={"DISPLAY": visual.x_display, "KWIN_MCP_X11_SCREENSHOT": "1"},
        ) as client:
            connected = False
            try:
                await _connect_visual(client, visual.dbus_address, visual.wayland_display)
                connected = True

                shot = await client.call_result("screenshot")
                text = _only_text(shot)
                assert text.startswith(SCREENSHOT_PREFIX), text
                assert shot.structured_content == {"result": text}

                # No progress_callback: the frame burst still succeeds unchanged.
                moved = await client.call_result(
                    "mouse_move", {"x": 200, "y": 200, "screenshot_after_ms": FRAME_DELAYS}
                )
                moved_text = _only_text(moved)
                assert moved.structured_content == {"result": moved_text}
                assert all(path.is_file() for path in _frame_paths(moved_text)), moved_text
            finally:
                (visual.artifact_dir / "mcp-server.stderr.log").write_text(
                    client.stderr_text(), encoding="utf-8"
                )
                if connected:
                    stop_output = await client.call_text("session_stop")
                    assert stop_output == "Disconnected from live session.", stop_output


@pytest.mark.anyio
@pytest.mark.visual
async def test_action_then_frame_burst_reports_strictly_increasing_progress() -> None:
    """The gesture's step count and the frame burst restart at 1; emitted progress must not."""
    with nested_visual_kwin() as visual:
        async with running_mcp_server(
            env={"DISPLAY": visual.x_display, "KWIN_MCP_X11_SCREENSHOT": "1"},
        ) as client:
            connected = False
            try:
                await _connect_visual(client, visual.dbus_address, visual.wayland_display)
                connected = True

                swipe_progress = ProgressLog()
                swiped = await client.call_result(
                    "touch_swipe",
                    {
                        "from_x": 200,
                        "from_y": 600,
                        "to_x": 600,
                        "to_y": 600,
                        "duration_ms": 300,
                        "screenshot_after_ms": FRAME_DELAYS,
                    },
                    progress_callback=swipe_progress,
                )
                assert not swiped.is_error, swiped.content

                await swipe_progress.settle()
                assert len(swipe_progress.events) > len(FRAME_DELAYS), swipe_progress.events
                _assert_strictly_increasing(swipe_progress.values)
                # The action's counter and the burst each start at 1; the burst's
                # raw values are offset so the last two events are its frames.
                messages = [message for _, _, message in swipe_progress.events]
                assert messages[-2:] == [
                    f"Frame {index}/{len(FRAME_DELAYS)} at {delay}ms"
                    for index, delay in enumerate(FRAME_DELAYS, 1)
                ], swipe_progress.events
                progress, total, _ = swipe_progress.events[-1]
                assert progress == total, swipe_progress.events
            finally:
                (visual.artifact_dir / "mcp-server.stderr.log").write_text(
                    client.stderr_text(), encoding="utf-8"
                )
                if connected:
                    stop_output = await client.call_text("session_stop")
                    assert stop_output == "Disconnected from live session.", stop_output
