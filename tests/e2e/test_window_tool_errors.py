"""Invalid window-tool arguments are tool errors, decided before any session lookup.

These tests need no KWin session: an argument error must not depend on session
state, so the engine raises ValueError with its own message even when no session
is running, and over MCP the call comes back with ``isError`` and that message
instead of the generic "No active session" guidance.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest
from mcp.types import TextContent
from mcp_harness import running_mcp_server

from kwin_mcp.core import AutomationEngine

if TYPE_CHECKING:
    from mcp.types import CallToolResult

FOCUS_SELECTOR_REQUIRED = "focus_window needs app_name or window_id (see window_geometry)."
CLOSE_ID_REQUIRED = "window_close needs a window_id (see window_geometry)."
DESKTOP_WINDOW_REQUIRED = "window_set_desktop needs a window_id (see window_geometry)."
DESKTOP_ID_REQUIRED = "window_set_desktop needs a desktop_id (see virtual_desktops)."


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _result_text(result: CallToolResult) -> str:
    return "\n".join(content.text for content in result.content if isinstance(content, TextContent))


@pytest.mark.parametrize("arguments", [{}, {"app_name": ""}], ids=["no-arguments", "empty-name"])
def test_focus_window_without_a_selector_is_rejected_without_a_session(
    arguments: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match=re.escape(FOCUS_SELECTOR_REQUIRED)):
        AutomationEngine().focus_window(**arguments)


def test_window_close_without_an_id_is_rejected_without_a_session() -> None:
    with pytest.raises(ValueError, match=re.escape(CLOSE_ID_REQUIRED)):
        AutomationEngine().window_close("")


@pytest.mark.parametrize(
    ("window_id", "desktop_id", "message"),
    [
        ("", "desktop", DESKTOP_WINDOW_REQUIRED),
        ("{window}", "", DESKTOP_ID_REQUIRED),
    ],
    ids=["no-window-id", "no-desktop-id"],
)
def test_window_set_desktop_without_an_id_is_rejected_without_a_session(
    window_id: str, desktop_id: str, message: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        AutomationEngine().window_set_desktop(window_id, desktop_id)


@pytest.mark.anyio
@pytest.mark.parametrize("arguments", [{}, {"app_name": ""}], ids=["no-arguments", "empty-name"])
async def test_focus_window_without_a_selector_is_a_tool_error_over_mcp(
    arguments: dict[str, Any],
) -> None:
    async with running_mcp_server() as client:
        result = await client.call_result("focus_window", arguments)

    assert result.is_error is True, _result_text(result)
    # isError alone also holds for "No active session"; the message proves the
    # argument check ran first.
    assert "needs app_name or window_id" in _result_text(result), _result_text(result)


@pytest.mark.anyio
async def test_window_set_geometry_zero_width_is_a_tool_error_over_mcp() -> None:
    async with running_mcp_server() as client:
        result = await client.call_result("window_set_geometry", {"window_id": "x", "width": 0})

    assert result.is_error is True, _result_text(result)
    # The published schema (gt=0) rejects the value before the engine runs, so
    # only the field name is pinned, not the engine's wording.
    assert "width" in _result_text(result), _result_text(result)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"window_id": "", "desktop_id": "desktop"}, DESKTOP_WINDOW_REQUIRED),
        ({"window_id": "{window}", "desktop_id": ""}, DESKTOP_ID_REQUIRED),
    ],
    ids=["no-window-id", "no-desktop-id"],
)
async def test_window_set_desktop_without_an_id_is_a_tool_error_over_mcp(
    arguments: dict[str, str], message: str
) -> None:
    async with running_mcp_server() as client:
        result = await client.call_result("window_set_desktop", arguments)

    assert result.is_error is True, _result_text(result)
    assert result.structured_content is None, result.structured_content
    # The engine's own message, not "No active session", proves the argument
    # check ran first.
    assert message in _result_text(result), _result_text(result)
