"""KWin-free coverage for re-registering the recorded tool bodies with new flags."""

from __future__ import annotations

import pytest

from kwin_mcp import server


@pytest.fixture
def anyio_backend() -> str:
    """The SDK's tool listing is exercised on asyncio."""
    return "asyncio"


@pytest.mark.anyio
async def test_register_tools_twice_replaces_each_tool_with_latest_flags() -> None:
    """A second registration replaces every tool once and applies its live descriptions."""
    try:
        server._register_tools(live_session_mode=False, screenshot_images=False)
        server._register_tools(live_session_mode=True, screenshot_images=False)
        tools = await server.mcp.list_tools()
        names = [tool.name for tool in tools]
        assert len(names) == len(server._tool_bodies), names
        assert len(set(names)) == len(names), names
        descriptions = {tool.name: tool.description for tool in tools}
        assert (
            descriptions["session_connect"] == server._LIVE_SESSION_DESCRIPTIONS["session_connect"]
        )
    finally:
        server._register_tools(live_session_mode=False, screenshot_images=False)
