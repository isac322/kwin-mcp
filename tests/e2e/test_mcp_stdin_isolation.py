"""The installed MCP server's stdio transport must stay private to the server.

Over stdio, the server's stdin carries every JSON-RPC request. Session processes
used to inherit it: the compositor wrapper and every ``launch_app`` program shared
the server's stdin pipe, so a launched program that reads stdin (a shell script,
``cat``, a CLI tool) consumed request lines and those calls never got a response.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import anyio
import pytest
from mcp_harness import running_mcp_server

CALL_TIMEOUT_SECONDS = 15
FOLLOW_UP_CALLS = 8


@pytest.fixture
def anyio_backend() -> str:
    """The MCP stdio client is exercised on asyncio."""
    return "asyncio"


def _parent_pid(pid: str) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # The command name may contain spaces or parentheses; fields follow the last ")".
    return int(stat.rsplit(")", 1)[1].split()[1])


def _stdin_target(pid: str) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/fd/0")
    except OSError:
        return None


def _server_pid() -> str:
    """The installed ``kwin-mcp`` process spawned by this test's stdio client."""
    own_pid = os.getpid()
    candidates = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or _parent_pid(entry.name) != own_pid:
            continue
        cmdline = (entry / "cmdline").read_bytes().split(b"\0")
        if any(Path(arg.decode(errors="replace")).name == "kwin-mcp" for arg in cmdline):
            candidates.append(entry.name)
    assert len(candidates) == 1, candidates
    return candidates[0]


def _processes_sharing_stdin(server_pid: str) -> dict[str, str]:
    """Every other process whose stdin is the server's JSON-RPC pipe."""
    transport = _stdin_target(server_pid)
    assert transport is not None and transport.startswith("pipe:"), transport
    sharing: dict[str, str] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or entry.name == server_pid:
            continue
        if _stdin_target(entry.name) == transport:
            try:
                comm = (entry / "comm").read_text(encoding="utf-8").strip()
            except OSError:
                comm = "?"
            sharing[entry.name] = comm
    return sharing


@pytest.mark.anyio
async def test_session_processes_cannot_read_the_json_rpc_stdin(tmp_path: Path) -> None:
    stolen = tmp_path / "stolen.bin"
    reader = f"sh -c {shlex.quote(f'cat > {shlex.quote(str(stolen))}')}"

    async with running_mcp_server() as client:
        session_running = False
        try:
            start_output = await client.call_text("session_start", {})
            session_running = True
            assert "Session started." in start_output, start_output

            launch_output = await client.call_text("launch_app", {"command": reader})
            assert launch_output.startswith("App launched:"), launch_output

            # The compositor, D-Bus, AT-SPI2, and the launched reader are all
            # running now; none of them may hold the transport as stdin.
            assert _processes_sharing_stdin(_server_pid()) == {}

            completed = 0
            for _ in range(FOLLOW_UP_CALLS):
                with anyio.move_on_after(CALL_TIMEOUT_SECONDS) as scope:
                    await client.call_text("list_windows")
                if scope.cancelled_caught:
                    break
                completed += 1
            assert completed == FOLLOW_UP_CALLS, (
                f"only {completed}/{FOLLOW_UP_CALLS} list_windows calls answered within "
                f"{CALL_TIMEOUT_SECONDS}s; the reader captured "
                f"{stolen.read_bytes()[:200]!r}"
            )
            assert not stolen.exists() or stolen.read_bytes() == b""
        finally:
            if session_running:
                with anyio.fail_after(CALL_TIMEOUT_SECONDS):
                    assert await client.call_text("session_stop") == "Session stopped."
