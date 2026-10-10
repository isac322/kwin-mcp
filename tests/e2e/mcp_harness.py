"""Async helpers for exercising the installed kwin-mcp server over stdio."""

from __future__ import annotations

import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping
    from typing import Any, TextIO

    from mcp.types import CallToolResult, InitializeResult

EXPECTED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "accessibility_tree",
        "active_window",
        "clipboard_get",
        "clipboard_set",
        "dbus_call",
        "find_ui_elements",
        "focus_window",
        "keyboard_key",
        "keyboard_key_down",
        "keyboard_key_up",
        "keyboard_type",
        "keyboard_type_unicode",
        "launch_app",
        "list_windows",
        "mouse_button_down",
        "mouse_button_up",
        "mouse_click",
        "mouse_drag",
        "mouse_move",
        "mouse_scroll",
        "read_app_log",
        "screenshot",
        "session_connect",
        "session_start",
        "session_stop",
        "touch_multi_swipe",
        "touch_pinch",
        "touch_swipe",
        "touch_tap",
        "wait_for_element",
        "wayland_info",
        "window_close",
        "window_geometry",
    }
)


class ProgressLog:
    """Progress callback that records every ``notifications/progress`` of one call."""

    def __init__(self) -> None:
        self.events: list[tuple[float, float | None, str | None]] = []

    async def __call__(self, progress: float, total: float | None, message: str | None) -> None:
        self.events.append((progress, total, message))

    @property
    def values(self) -> list[float]:
        return [progress for progress, _, _ in self.events]

    async def settle(self, seconds: float = 0.2) -> None:
        """Let callbacks the client spawned for already-received notifications run.

        The client dispatches each progress notification to its own task, so the
        last callbacks can still be pending when ``call_tool`` returns.
        """
        await anyio.sleep(seconds)


class McpTestClient:
    """Initialized MCP client plus assertions tailored to text-returning tools."""

    def __init__(
        self,
        session: ClientSession,
        stderr_path: Path,
        initialize_result: InitializeResult,
    ) -> None:
        self.session = session
        self.initialize_result = initialize_result
        self._stderr_path = stderr_path

    def stderr_text(self) -> str:
        """Return stderr emitted by the installed server process so far."""
        try:
            return self._stderr_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    async def call_result(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        progress_callback: ProgressLog | None = None,
    ) -> CallToolResult:
        """Call a tool over JSON-RPC and return the unmodified MCP result.

        Passing ``progress_callback`` makes the client send a progressToken, so the
        server's ``notifications/progress`` for this call are recorded in it.
        """
        return await self.session.call_tool(name, arguments, progress_callback=progress_callback)

    async def call_text(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Call a text tool, failing immediately when MCP reports a tool error."""
        result = await self.call_result(name, arguments)
        text = "\n".join(
            content.text for content in result.content if isinstance(content, TextContent)
        )
        if result.is_error:
            stderr = self.stderr_text().strip()
            details = text or repr(result.content)
            if stderr:
                details = f"{details}\n\nkwin-mcp stderr:\n{stderr[-8000:]}"
            raise AssertionError(f"MCP tool {name!r} failed: {details}")
        return text


def _add_stderr_note(error: BaseException, stderr_path: Path) -> None:
    try:
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return
    if not stderr:
        return

    note = f"kwin-mcp stderr:\n{stderr[-8000:]}"
    if note not in getattr(error, "__notes__", ()):
        error.add_note(note)


@asynccontextmanager
async def running_mcp_server(
    *extra_args: str, env: Mapping[str, str] | None = None
) -> AsyncGenerator[McpTestClient]:
    """Start and initialize the installed kwin-mcp entrypoint over real stdio.

    Environment overrides apply only to the spawned server process.
    """
    stderr_file = cast(
        "TextIO",
        tempfile.NamedTemporaryFile(  # noqa: SIM115 - kept open through async yield
            mode="w+",
            encoding="utf-8",
            prefix="kwin-mcp-e2e-stderr-",
            delete=False,
        ),
    )
    stderr_path = Path(stderr_file.name)
    server_env = os.environ.copy()
    if env is not None:
        server_env.update(env)
    parameters = StdioServerParameters(
        command="kwin-mcp",
        args=list(extra_args),
        env=server_env,
    )

    try:
        async with (
            stdio_client(parameters, errlog=stderr_file) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialize_result = await session.initialize()
            yield McpTestClient(session, stderr_path, initialize_result)
    except BaseException as error:
        stderr_file.flush()
        _add_stderr_note(error, stderr_path)
        raise
    finally:
        stderr_file.close()
        stderr_path.unlink(missing_ok=True)
