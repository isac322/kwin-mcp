"""MCP protocol-layer smoke tests.

Distro-independent but run inside the cross-distro matrix so each distro's
Python + Pydantic + MCP SDK stack is exercised end-to-end.
"""

from __future__ import annotations

from tests.integration._helpers import mcp_stdio_session

# Tools every kwin-mcp build must expose. Checked individually instead of by
# exact count so adding a new tool does not force a test edit in lockstep.
EXPECTED_TOOLS: frozenset[str] = frozenset(
    {
        "session_start",
        "session_connect",
        "session_stop",
        "screenshot",
        "accessibility_tree",
        "find_ui_elements",
        "wait_for_element",
        "launch_app",
        "list_windows",
        "focus_window",
        "mouse_click",
        "mouse_move",
        "mouse_scroll",
        "mouse_drag",
        "keyboard_type",
        "keyboard_type_unicode",
        "keyboard_key",
        "clipboard_get",
        "clipboard_set",
        "dbus_call",
        "read_app_log",
        "wayland_info",
    }
)


async def test_list_tools_exposes_expected_inventory() -> None:
    """Every expected tool is registered and advertised via ``tools/list``."""
    async with mcp_stdio_session() as client:
        resp = await client.list_tools()
    names = {t.name for t in resp.tools}
    missing = EXPECTED_TOOLS - names
    assert not missing, f"Missing expected tools: {missing} (got {names})"


async def test_tool_descriptions_are_non_empty() -> None:
    """Every advertised tool has a non-empty description.

    Guards against ``@mcp.tool()`` docstrings being stripped in packaging or
    regressed to empty.
    """
    async with mcp_stdio_session() as client:
        resp = await client.list_tools()
    empty = [t.name for t in resp.tools if not (t.description and t.description.strip())]
    assert not empty, f"Tools with empty description: {empty}"


async def test_session_start_rejects_invalid_type() -> None:
    """Pydantic ``Field`` validation surfaces through the MCP layer.

    Passes a string where ``screen_width`` expects an integer. FastMCP should
    either raise over the transport or return a tool-side error result. In
    either case the tool must not silently succeed.
    """
    async with mcp_stdio_session() as client:
        try:
            result = await client.call_tool("session_start", {"screen_width": "not-an-int"})
        except Exception:
            # Transport / schema validation raised — acceptable.
            return

        joined = "".join(getattr(c, "text", "") for c in result.content)
        assert result.isError or "Session started" not in joined, (
            "Invalid screen_width was accepted without error"
        )
