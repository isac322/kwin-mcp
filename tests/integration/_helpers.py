"""Shared helpers for kwin-mcp integration tests.

All MCP client plumbing lives in async context managers here rather than
pytest fixtures. ``anyio`` (used by the MCP SDK) requires entering and exiting
cancel scopes in the same task; pytest-asyncio fixtures with ``yield`` violate
that constraint because setup and teardown run in separate tasks. Context
managers invoked inline via ``async with`` inside each test keep the whole
lifecycle on one task and avoid the "cancel scope exited in a different task"
``RuntimeError``.
"""

from __future__ import annotations

import os
import re
import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mcp.types import CallToolResult

# Pass the full parent env explicitly. MCP's ``StdioServerParameters`` with
# ``env=None`` provides only a minimal default environment to the server
# subprocess, which strips ``XDG_RUNTIME_DIR``, ``DISPLAY``, and others that
# ``dbus-run-session`` / ``kwin_wayland --virtual`` rely on to boot. Without
# these the wrapper script blocks waiting for D-Bus / AT-SPI bootstrap that
# never happens.
MCP_SERVER_PARAMS = StdioServerParameters(
    command=sys.executable,
    args=["-m", "kwin_mcp"],
    env=dict(os.environ),
)

_COORD_RE = re.compile(r"@ \((\d+), (\d+), (\d+)x(\d+)\)")


def text_of(result: CallToolResult) -> str:
    """Concatenate ``text`` attributes from every TextContent in a tool result."""
    return "\n".join(c.text for c in result.content if isinstance(c, TextContent))


async def call_text(client: ClientSession, tool: str, args: dict[str, object] | None = None) -> str:
    """Call ``tool`` via the MCP client and return its text output."""
    result = await client.call_tool(tool, args or {})
    return text_of(result)


async def coords_of(client: ClientSession, query: str, app_name: str) -> tuple[int, int]:
    """Return the center ``(x, y)`` of the first widget matching ``query``.

    Parses lines like ``- [push button] "2" @ (320, 450, 60x50) [actions: click]``.
    """
    text = await call_text(client, "find_ui_elements", {"query": query, "app_name": app_name})
    for line in text.splitlines():
        m = _COORD_RE.search(line)
        if m:
            x, y, w, h = map(int, m.groups())
            return x + w // 2, y + h // 2
    msg = f"No coords for query={query!r} in app={app_name!r}. Raw output:\n{text}"
    raise AssertionError(msg)


async def coords_of_any(
    client: ClientSession, queries: list[str], app_name: str
) -> tuple[int, int]:
    """Try each query in order; return coords of the first match."""
    last_error = ""
    for q in queries:
        try:
            return await coords_of(client, q, app_name)
        except AssertionError as exc:
            last_error = str(exc)
    msg = f"None of {queries!r} found in {app_name!r}. Last error:\n{last_error}"
    raise AssertionError(msg)


@asynccontextmanager
async def mcp_stdio_session() -> AsyncIterator[ClientSession]:
    """Spawn kwin-mcp as a real MCP stdio subprocess, connect, initialize."""
    async with (
        stdio_client(MCP_SERVER_PARAMS) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


@asynccontextmanager
async def running_kwin() -> AsyncIterator[ClientSession]:
    """Open an MCP session and bring up a virtual KWin session via ``session_start``.

    ``keep_screenshots=True`` retains the session's screenshot dir past
    ``session_stop`` so CI can upload it as an artifact on failure.
    """
    async with mcp_stdio_session() as client:
        result = await client.call_tool(
            "session_start", {"enable_clipboard": True, "keep_screenshots": True}
        )
        text = text_of(result)
        assert "Session started" in text, f"session_start failed: {text}"
        try:
            yield client
        finally:
            await client.call_tool("session_stop", {})
