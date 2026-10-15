"""The 1.0 cutover over the installed server: 29 tools, no aliases, removed names explained.

The expected names come from ``tests/tool_surface.py`` and the per-name replacement
calls from the design's capability table (S §5.1), written out here, never read
from ``kwin_mcp.server``.
"""

from __future__ import annotations

import pytest
from mcp_harness import REMOVED_TOOL_NAMES, TOOL_NAMES, running_mcp_server, text_of

from kwin_mcp.server import build_server

pytestmark = pytest.mark.anyio

# The 1.0 call that replaces each removed 0.11 / PR #103 tool (S §5.1), plus a
# fragment of its remedy that pins the parameter mapping where the name alone is
# ambiguous (press vs release, the unicode route, the active-window filter...).
REPLACEMENTS: dict[str, tuple[str, str]] = {
    "accessibility_tree": ("element_query", "max_depth"),
    "active_window": ("window_list", "active:true"),
    "clipboard_get": ("clipboard_read", "clipboard_read()"),
    "clipboard_set": ("clipboard_write", "text"),
    "find_ui_elements": ("element_query", "locator="),
    "focus_window": ("window_focus", "window="),
    "invoke_ui_action": ("element_act", "action"),
    "keyboard_key": ("keyboard_press", "keys"),
    "keyboard_key_down": ("keyboard_hold", "phase='press'"),
    "keyboard_key_up": ("keyboard_hold", "phase='release'"),
    "keyboard_type_unicode": ("keyboard_type", "method='unicode'"),
    "launch_app": ("app_launch", "command"),
    "list_windows": ("element_query", "max_depth=1"),
    "mouse_button_down": ("pointer_hold", "phase='press'"),
    "mouse_button_up": ("pointer_hold", "phase='release'"),
    "mouse_click": ("pointer_click", "target={x,y}"),
    "mouse_drag": ("pointer_drag", "from={x,y}"),
    "mouse_move": ("pointer_move", "target={x,y}"),
    "mouse_scroll": ("pointer_scroll", "target={x,y}"),
    "read_app_log": ("app_log", "pid"),
    "screenshot": ("screen_capture", "region"),
    "touch_multi_swipe": ("touch_swipe", "fingers"),
    "virtual_desktops": ("desktop_list", "desktop_list()"),
    "wait_for_element": ("element_query", "wait="),
    "wayland_info": ("wayland_list", "filter"),
    "window_geometry": ("window_list", "filter="),
    "window_set_desktop": ("window_place", "desktop"),
    "window_set_geometry": ("window_place", "frame="),
}

# 0.11 argument shapes for some removed tools: the removed-name remedy must win
# over argument checks, so these arguments must not change the answer.
OLD_ARGUMENTS: dict[str, dict[str, object]] = {
    "mouse_click": {"x": 1, "y": 1},
    "screenshot": {},
    "accessibility_tree": {},
    "virtual_desktops": {},
    "keyboard_key": {"key": "Return"},
    "launch_app": {"command": "true"},
    "window_set_geometry": {"window_id": "{x}", "x": 0, "y": 0},
}


@pytest.fixture
def anyio_backend() -> str:
    """The installed MCP client runs on asyncio."""
    return "asyncio"


@pytest.mark.qa("QA-SURF-001")
async def test_exactly_29_tools_no_old_names() -> None:
    assert len(TOOL_NAMES) == 29
    assert len(REMOVED_TOOL_NAMES) == 28
    assert frozenset(REPLACEMENTS) == frozenset(REMOVED_TOOL_NAMES)

    async with running_mcp_server() as client:
        stdio_names = [tool.name for tool in await client.list_tools()]
    in_process_names = [tool.name for tool in await build_server().list_tools()]

    for names in (stdio_names, in_process_names):
        assert len(names) == 29, names
        assert len(set(names)) == len(names), names
        assert set(names) == set(TOOL_NAMES), set(names) ^ set(TOOL_NAMES)
        assert not set(names) & set(REMOVED_TOOL_NAMES), set(names) & set(REMOVED_TOOL_NAMES)
    assert stdio_names == in_process_names


@pytest.mark.qa("QA-GATE-008")
async def test_old_name_call_is_error_pointing_to_migration_table() -> None:
    async with running_mcp_server() as client:
        for name in REMOVED_TOOL_NAMES:
            # The SDK turns this into an isError result; a JSON-RPC error would raise here.
            result = await client.call_result(name, OLD_ARGUMENTS.get(name, {}))
            assert result.is_error, (name, result)
            assert result.structured_content is None, (name, result)
            text = text_of(result)
            assert "\n" not in text, (name, text)
            replacement, fragment = REPLACEMENTS[name]
            prefix = f"Unknown tool: {name}. Removed in kwin-mcp 1.0; use {replacement}("
            assert text.startswith(prefix), (name, text)
            assert text.endswith(")."), (name, text)
            assert fragment in text.removeprefix(f"Unknown tool: {name}."), (name, text)
        # The exact text the design quotes.
        lines = await client.call_tool_error("mouse_click", {"x": 1, "y": 1}, lines=1)
        assert lines == (
            "Unknown tool: mouse_click. Removed in kwin-mcp 1.0; "
            "use pointer_click(target={x,y}, ...).",
        )
        # A name that never existed keeps the SDK's plain text.
        assert await client.call_tool_error("no_such_tool", {}, lines=1) == (
            "Unknown tool: no_such_tool",
        )

        assert await client.session.send_ping() is not None
        assert [tool.name for tool in await client.list_tools()] == list(TOOL_NAMES)
        structured, _ = await client.call_structured("session_info")
        assert structured["session"] is None, structured
