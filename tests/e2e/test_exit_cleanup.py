"""The kwin-mcp server and CLI stop the session when the process itself exits.

A client that closes stdin (stdio EOF), or a supervisor that sends SIGTERM or
SIGHUP, must tear the session down exactly as ``session_stop`` would: for a
virtual session the KWin compositor, its ``dbus-run-session`` wrapper bus, and
every launched app terminate; for a live session the connection is dropped with
its launched apps stopped and pre-existing apps untouched.

Before the fix, ``server.main`` only called ``mcp.run()`` and never stopped the
engine's session, so any of those exits left the compositor, wrapper, and the
launched app running. The CLI only handled ``quit``/EOF/Ctrl-C, not SIGTERM or
SIGHUP.

The final ``session_stop`` always runs on the single tool thread (the engine is
not thread-safe), so the main thread never calls into the engine while a tool may
be running. When the tool thread is free the full ``session_stop`` runs. When a
tool still holds it, the queued stop is cancelled before it runs and the process
groups the server spawned (the virtual wrapper and each launched app, virtual or
live) are terminated directly: ``SIGTERM`` to every group, a shared grace wait,
``SIGKILL`` to the survivors, and a second shared grace wait. KWin of a live
session and any pre-existing app are never signalled.

Three further guarantees are covered here: a tool whose remaining work outlives
the exit drain (a long ``screenshot_after_ms`` burst) must not delay the exit -
the owned groups are terminated and the process exits within the bound - ; a live
session with several slow-exiting apps must lose every launched app while KWin
survives, both when the stop runs and when the busy registry path runs; and a
signal that arrives while the CLI is tearing the session down must not interrupt
the teardown and skip the launched apps.

The remaining coverage needs no session at all: a signal that lands inside the
asyncio loop (mid tool submission, or idle) must still exit 128+signum with the
cleanup outcome line on stderr — a raising handler there reaches ``main``
wrapped or swallowed by the SDK's task machinery instead; a CLI exit must sweep
owned process groups no session claimed; an in-escalation leader must not be
reaped by a concurrent registry poll (the SIGKILL step would then skip the
group); and a temp dir registered after ``close()`` must be removed, not
recorded.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import IO, TYPE_CHECKING

import pytest
from session_harness import LiveKWin, live_kwin

# Bounds for the real (non-stub) session lifecycle.
SESSION_START_TIMEOUT_SECONDS = 90.0
SERVER_EXIT_TIMEOUT_SECONDS = 30.0
CLEANUP_SETTLE_SECONDS = 10.0
# Disconnecting while session_start is still executing: the stop waits for the in-flight
# tool (up to ~20s) plus its own bounded waits, so allow more than the plain signal path.
SERVER_EXIT_TIMEOUT_TOOL_INFLIGHT_SECONDS = 60.0
# Disconnecting while a tool's remaining work outlives the server's exit drain
# (2 s in the server): the queued stop is cancelled before it runs and the owned
# process groups are terminated directly, so the exit must not wait the tool out.
# The bound sits above drain + TERM grace + KILL grace + margin and below the tool's
# own 40 s duration.
SERVER_EXIT_TIMEOUT_TOOL_OVERRUN_SECONDS = 15.0
# No-session exits (idle signal, signal mid-submission): only the exit cleanup
# itself — the drain plus the registry sweep — can consume time.
SERVER_EXIT_TIMEOUT_IDLE_SECONDS = 15.0


# A distinctive app so its PID, parsed from the launch output, identifies it.
_APP_COMMAND = "sleep 600"
# The tool used to overrun the exit drain: a frame burst whose first (only) capture
# is 40 s out, one uninterrupted sleep, so the tool body is still running when the
# client disconnects.
_OVERRUN_TOOL_DELAY_MS = 40000
# First app of the CLI teardown regression: it traps SIGTERM and takes
# _SLOW_APP_TERM_DELAY_S to exit, so the stop's per-app wait blocks on it and the
# signal stream is guaranteed to land while the stop is still running (before the
# remaining apps are terminated).
_SLOW_APP_TERM_DELAY_S = 2.0
_SLOW_APP_COMMAND = (
    f'python3 -c "import signal,sys,time;'
    f"signal.signal(signal.SIGTERM,lambda s,f:"
    f"(time.sleep({_SLOW_APP_TERM_DELAY_S:g}),sys.exit(0)));"
    f'time.sleep(600)"'
)
# Instant apps after the slow one: they are the leak candidates a signal-interrupted
# stop leaves running.
_CLI_STOP_TEST_APP_COUNT = 5
# Live-session exit regression (c, c2): every launched app traps SIGTERM and takes
# this long to exit, so the shared escalation's TERM grace is fully used. Five of
# them would exceed any per-app budget, so only a shared escalation (or the registry
# path) terminates them all within the bound.
_LIVE_APP_TERM_DELAY_S = 2.8
_LIVE_SLOW_APP_COMMAND = (
    f'python3 -c "import signal,sys,time;'
    f"signal.signal(signal.SIGTERM,lambda s,f:"
    f"(time.sleep({_LIVE_APP_TERM_DELAY_S:g}),sys.exit(0)));"
    f'time.sleep(600)"'
)
_LIVE_SLOW_APP_COUNT = 5

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# /proc helpers (zombies excluded: the container runs pytest as PID 1 without an
# orphan reaper, so a leaked descendant would stay a zombie, not a survivor)
# ---------------------------------------------------------------------------


def _proc_stat(pid: str | int) -> tuple[str, int] | None:
    """Return (state, pgrp) from /proc/<pid>/stat, or None once it is gone."""
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
    except OSError:
        return None
    fields = stat.rpartition(")")[2].split()
    return fields[0], int(fields[2])


def _is_live(pid: str | int) -> bool:
    stat = _proc_stat(pid)
    return stat is not None and stat[0] not in ("Z", "X")


def _cmdline(pid: str | int) -> list[str]:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _parent_pid(pid: str | int) -> int | None:
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
    except OSError:
        return None
    return int(stat.rpartition(")")[2].split()[1])


def _children(pid: int) -> list[int]:
    """Live pids whose parent is ``pid``."""
    result = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or not _is_live(entry.name):
            continue
        if _parent_pid(entry.name) == pid:
            result.append(int(entry.name))
    return result


def _session_pgid(server_pid: int) -> int | None:
    """The virtual session's process group id (the wrapper, a new-session leader).

    ``Session.start`` launches ``dbus-run-session`` with ``start_new_session=True``
    and no explicit pgid, so the wrapper's pid doubles as its group id.
    """
    for child in _children(server_pid):
        argv = _cmdline(child)
        if argv and os.path.basename(argv[0]) == "dbus-run-session":
            return child
    return None


def _group_pids(pgid: int) -> set[int]:
    """Live pids in the process group ``pgid``."""
    result = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or not _is_live(entry.name):
            continue
        stat = _proc_stat(entry.name)
        if stat is not None and stat[1] == pgid:
            result.add(int(entry.name))
    return result


def _wait_until(predicate: Callable[[], object], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return bool(predicate())


# ---------------------------------------------------------------------------
# Raw stdio client for the installed server and CLI (mirrors the leak check)
# ---------------------------------------------------------------------------


class _LineReader:
    """Read stdout lines on a background thread so the caller can time out.

    ``get`` returns the next line, ``None`` when no line arrives within the
    timeout, and raises ``EOFError`` once the stream is closed.
    """

    def __init__(self, stream: IO[str]) -> None:
        self._queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, args=(stream,), daemon=True).start()

    def _run(self, stream: IO[str]) -> None:
        for line in stream:
            self._queue.put(line)
        self._queue.put(None)

    def get(self, timeout: float) -> str | None:
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is None:
            raise EOFError("process closed stdout")
        return item


def _spawn(argv: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _spawn_new_session(argv: list[str]) -> subprocess.Popen[str]:
    """Spawn in a new session (its own process group) so only the PID is signalled.

    The CLI and the apps it launches share a process group (launched apps are not yet
    in their own group upstream), so the teardown regression must signal the CLI PID
    only, never a process group. A new session keeps the CLI and its apps isolated from
    the test's group so the group is never the delivery vehicle.
    """
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )


def _spawn_with_stderr(argv: list[str]) -> subprocess.Popen[str]:
    """Spawn the server with stderr piped, so the exit outcome line can be read.

    The server writes a single ``kwin-mcp: exit cleanup: ...`` line to stderr before
    ``os._exit``; the pipe buffer is far larger than that, so reading it after the
    process has exited is safe.
    """
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _stdin(process: subprocess.Popen[str]) -> IO[str]:
    assert process.stdin is not None
    return process.stdin


def _stdout(process: subprocess.Popen[str]) -> IO[str]:
    assert process.stdout is not None
    return process.stdout


def _rpc(
    reader: _LineReader,
    process: subprocess.Popen[str],
    request_id: int,
    method: str,
    params: dict[str, object],
) -> dict:
    request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    stdin = _stdin(process)
    stdin.write(json.dumps(request) + "\n")
    stdin.flush()
    deadline = time.monotonic() + SESSION_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        line = reader.get(timeout=min(deadline - time.monotonic(), 0.1))
        if line is None:
            continue
        message = json.loads(line)
        if message.get("id") == request_id:
            return message
    msg = f"no response to {method!r} within {SESSION_START_TIMEOUT_SECONDS:g}s"
    raise AssertionError(msg)


def _app_pid_from_response(response: dict) -> int:
    """The launched app's PID, from the ``App launched: ... (PID=...)`` line."""
    content = response["result"]["content"]
    text = "".join(block.get("text", "") for block in content if isinstance(block, dict))
    match = re.search(r"App launched:.*\(PID=(\d+)\)", text)
    assert match, f"no app PID in session_start response: {text[:200]}"
    return int(match.group(1))


def _start_server_session(process: subprocess.Popen[str], reader: _LineReader) -> int:
    """Initialize the server, start a virtual session with the tracked app.

    Returns the launched app's PID.
    """
    _rpc(
        reader,
        process,
        1,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "exit-cleanup", "version": "0"},
        },
    )
    initialized = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    stdin = _stdin(process)
    stdin.write(initialized + "\n")
    stdin.flush()
    response = _rpc(
        reader,
        process,
        2,
        "tools/call",
        {"name": "session_start", "arguments": {"app_command": _APP_COMMAND}},
    )
    return _app_pid_from_response(response)


def _response_text(response: dict) -> str:
    """The concatenated text of a tools/call response."""
    content = response.get("result", {}).get("content", [])
    return "".join(block.get("text", "") for block in content if isinstance(block, dict))


def _connect_live_session(
    process: subprocess.Popen[str], reader: _LineReader, live: LiveKWin
) -> None:
    """Initialize the server and connect it to the test-owned live KWin."""
    _rpc(
        reader,
        process,
        1,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "exit-cleanup", "version": "0"},
        },
    )
    initialized = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    stdin = _stdin(process)
    stdin.write(initialized + "\n")
    stdin.flush()
    response = _rpc(
        reader,
        process,
        2,
        "tools/call",
        {
            "name": "session_connect",
            "arguments": {
                "dbus_address": live.dbus_address,
                "wayland_display": live.wayland_display,
            },
        },
    )
    assert "Connected to live KWin session" in _response_text(response), (
        f"server did not connect to the live session: {_response_text(response)[:200]}"
    )


def _launch_live_apps(
    process: subprocess.Popen[str],
    reader: _LineReader,
    count: int,
    command: str,
    first_id: int,
) -> list[int]:
    """Launch ``count`` apps in the live session; return their PIDs in launch order."""
    pids: list[int] = []
    for i in range(count):
        response = _rpc(
            reader,
            process,
            first_id + i,
            "tools/call",
            {"name": "launch_app", "arguments": {"command": command}},
        )
        pids.append(_app_pid_from_response(response))
    return pids


def _wait_for_app_pid(reader: _LineReader, timeout: float) -> int | None:
    """Read the CLI's stdout until it reports a launched app's PID."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = reader.get(timeout=min(deadline - time.monotonic(), 0.1))
        if line is None:
            continue
        match = re.search(r"App launched:.*\(PID=(\d+)\)", line)
        if match:
            return int(match.group(1))
    return None


def _wait_for_pgid(server_pid: int, timeout: float) -> int | None:
    """Wait until the session's wrapper process group is up, then return its pgid."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pgid = _session_pgid(server_pid)
        if pgid is not None:
            return pgid
        time.sleep(0.1)
    return None


def _fire_overrun_tool(server: subprocess.Popen[str]) -> None:
    """Fire the tool whose remaining work outlives the server's exit drain bound.

    ``mouse_move`` with a single frame captured ``_OVERRUN_TOOL_DELAY_MS`` out spends
    the whole time in one uninterrupted sleep in the frame capture, so its body is
    still running when the client disconnects.
    """
    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "mouse_move",
            "arguments": {"x": 100, "y": 100, "screenshot_after_ms": [_OVERRUN_TOOL_DELAY_MS]},
        },
    }
    stdin = _stdin(server)
    stdin.write(json.dumps(request) + "\n")
    stdin.flush()


def _wait_for_line(reader: _LineReader, substring: str, timeout: float) -> bool:
    """Read the process's stdout until a line contains ``substring`` (or the timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = reader.get(timeout=min(deadline - time.monotonic(), 0.1))
        if line is None:
            continue
        if substring in line:
            return True
    return False


def _assert_session_gone(pgid: int | None, app_pid: int | None = None) -> None:
    """No live member of the session group (and no live app) survive the shutdown.

    Waits for disappearance within the grace period: teardown is not synchronous, so
    the killed processes take a moment to actually die after the server exits.
    """
    assert pgid is not None, "could not identify the session process group"
    assert _wait_until(lambda: not _group_pids(pgid), CLEANUP_SETTLE_SECONDS), (
        f"session process group {pgid} still has live members: {sorted(_group_pids(pgid))}"
    )
    if app_pid is not None:
        assert _wait_until(lambda: not _is_live(app_pid), CLEANUP_SETTLE_SECONDS), (
            f"launched app {app_pid} is still running"
        )


def _initialize_server(process: subprocess.Popen[str], reader: _LineReader) -> None:
    """Run the MCP initialize handshake without starting a session.

    The exit-behavior regressions below exercise servers with no session, so the
    handshake stops after ``notifications/initialized``; the caller drives the
    interesting request (or signal) itself.
    """
    _rpc(
        reader,
        process,
        1,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "exit-cleanup", "version": "0"},
        },
    )
    initialized = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    stdin = _stdin(process)
    stdin.write(initialized + "\n")
    stdin.flush()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_stdio_eof_stops_the_virtual_session_and_app() -> None:
    """Closing the client's stdin stops the compositor, wrapper, and app."""
    server = _spawn([sys.executable, "-m", "kwin_mcp"])
    reader = _LineReader(_stdout(server))
    try:
        app_pid = _start_server_session(server, reader)
        pgid = _session_pgid(server.pid)
        assert pgid is not None, "session started but the wrapper process group was not found"
        assert _is_live(app_pid), f"session_start launched {app_pid} but it is not running"

        # EOF: closing stdin ends the MCP stdio transport; the server must exit 0.
        _stdin(server).close()
        exit_code = server.wait(timeout=SERVER_EXIT_TIMEOUT_SECONDS)
        assert exit_code == 0, f"server did not exit 0 on stdin EOF, got {exit_code}"
        _assert_session_gone(pgid, app_pid)
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()


def test_server_sigterm_stops_the_virtual_session_and_app() -> None:
    """SIGTERM to the server stops the compositor, wrapper, and app (exit 143)."""
    server = _spawn([sys.executable, "-m", "kwin_mcp"])
    reader = _LineReader(_stdout(server))
    try:
        app_pid = _start_server_session(server, reader)
        pgid = _session_pgid(server.pid)
        assert pgid is not None, "session started but the wrapper process group was not found"
        assert _is_live(app_pid), f"session_start launched {app_pid} but it is not running"

        os.kill(server.pid, signal.SIGTERM)
        exit_code = server.wait(timeout=SERVER_EXIT_TIMEOUT_SECONDS)
        assert exit_code == 143, f"expected exit 143 on SIGTERM, got {exit_code}"
        _assert_session_gone(pgid, app_pid)
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()


def test_cli_sigterm_stops_the_virtual_session_and_app() -> None:
    """SIGTERM to the CLI stops the compositor, wrapper, and app."""
    cli = _spawn([sys.executable, "-m", "kwin_mcp.cli"])
    reader = _LineReader(_stdout(cli))
    try:
        # Pipe mode: the CLI reads commands from stdin until EOF.
        stdin = _stdin(cli)
        stdin.write(f"session_start app_command={shlex.quote(_APP_COMMAND)}\n")
        stdin.flush()
        app_pid = _wait_for_app_pid(reader, SESSION_START_TIMEOUT_SECONDS)
        assert app_pid is not None, "CLI did not report the launched app"
        pgid = _session_pgid(cli.pid)
        assert pgid is not None, "session started but the wrapper process group was not found"
        assert _is_live(app_pid), f"session_start launched {app_pid} but it is not running"

        os.kill(cli.pid, signal.SIGTERM)
        cli.wait(timeout=SERVER_EXIT_TIMEOUT_SECONDS)
        _assert_session_gone(pgid, app_pid)
    finally:
        if cli.poll() is None:
            cli.kill()
            cli.wait()


def test_eof_while_session_start_running_stops_the_session() -> None:
    """Closing stdin while session_start is still executing stops the whole session.

    Contract: a client that disconnects mid-startup must not leak the compositor,
    wrapper, or app, whatever phase session_start is in.
    """
    server = _spawn([sys.executable, "-m", "kwin_mcp"])
    reader = _LineReader(_stdout(server))
    try:
        _rpc(
            reader,
            server,
            1,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "exit-cleanup", "version": "0"},
            },
        )
        initialized = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        stdin = _stdin(server)
        stdin.write(initialized + "\n")
        stdin.flush()
        # Fire session_start (a lifecycle tool) but do not wait for its response, so it
        # is still executing on the tool thread when the client disconnects.
        request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "session_start", "arguments": {"app_command": _APP_COMMAND}},
        }
        stdin.write(json.dumps(request) + "\n")
        stdin.flush()
        pgid = _wait_for_pgid(server.pid, SESSION_START_TIMEOUT_SECONDS)
        assert pgid is not None, "session_start did not bring up the wrapper process group"

        # Disconnect (EOF) while session_start is still executing; the server must stop
        # the session (waiting for the in-flight tool) and then exit 0.
        stdin.close()
        exit_code = server.wait(timeout=SERVER_EXIT_TIMEOUT_TOOL_INFLIGHT_SECONDS)
        assert exit_code == 0, f"server did not exit 0 on stdin EOF, got {exit_code}"
        _assert_session_gone(pgid)
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()


def test_eof_while_tool_running_exits_bounded_and_clean() -> None:
    """Closing stdin while a tool is running still exits bounded and leaves no survivors.

    Regression: on stdio EOF the SDK cancels in-flight handlers without stopping the
    executor job, so the old cleanup ran session_stop on the main thread concurrently
    with the tool body still running on the tool thread — the engine is not
    thread-safe, and the two could tear the session down in opposite orders (the stop
    racing the in-flight tool, or the tool's a11y worker being killed mid-exchange).
    The final stop must run on the tool thread, serialized with the in-flight tool, so
    the process always exits within the bound with the session fully torn down.
    """
    server = _spawn([sys.executable, "-m", "kwin_mcp"])
    reader = _LineReader(_stdout(server))
    try:
        app_pid = _start_server_session(server, reader)
        pgid = _session_pgid(server.pid)
        assert pgid is not None, "session started but the wrapper process group was not found"
        assert _is_live(app_pid), f"session_start launched {app_pid} but it is not running"

        # Occupy the tool thread with a long read-only wait on an element that never
        # appears; the response is not awaited, so the tool is still running when the
        # client disconnects.
        request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "wait_for_element",
                "arguments": {"query": "no-such-element-xyz", "timeout_ms": 15000},
            },
        }
        stdin = _stdin(server)
        stdin.write(json.dumps(request) + "\n")
        stdin.flush()
        # The tool is now running on the tool thread.
        time.sleep(1.0)
        assert _group_pids(pgid), "session group died before the client disconnected"

        # Disconnect (EOF) while the tool is still running: the server must exit 0
        # within the bound (no hang on the in-flight tool) and tear the whole session
        # down (compositor, wrapper, app).
        stdin.close()
        exit_code = server.wait(timeout=SERVER_EXIT_TIMEOUT_TOOL_INFLIGHT_SECONDS)
        assert exit_code == 0, f"server did not exit 0 on stdin EOF, got {exit_code}"
        _assert_session_gone(pgid, app_pid)
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()


def test_second_sigterm_during_cleanup_still_exits() -> None:
    """A second SIGTERM while the stop is running does not hang the shutdown.

    Regression: the signal handlers used to stay active during session_stop, so a second
    SIGTERM raised _Shutdown through the cleanup and left the process half-torn-down.
    Stdin is left open (no EOF) so the only thing that can unblock the stop is the
    one-way signal transition.
    """
    server = _spawn([sys.executable, "-m", "kwin_mcp"])
    reader = _LineReader(_stdout(server))
    try:
        app_pid = _start_server_session(server, reader)
        pgid = _session_pgid(server.pid)
        assert pgid is not None, "session started but the wrapper process group was not found"
        assert _is_live(app_pid), f"session_start launched {app_pid} but it is not running"

        # First SIGTERM: the server begins the bounded stop.
        os.kill(server.pid, signal.SIGTERM)
        # Re-signal every 50 ms while the server is still up: at least one of these
        # lands while the stop is running (the stop's bounded waits always outlast a
        # 50 ms tick), and a second signal must not throw through the cleanup.
        re_signal_deadline = time.monotonic() + 2.0
        while server.poll() is None and time.monotonic() < re_signal_deadline:
            time.sleep(0.05)
            if server.poll() is None:
                os.kill(server.pid, signal.SIGTERM)
        # The process must exit within the bound (not hang on the second signal).
        exit_code = server.wait(timeout=SERVER_EXIT_TIMEOUT_SECONDS)
        assert exit_code == 143, f"expected exit 143 on SIGTERM, got {exit_code}"
        _assert_session_gone(pgid, app_pid)
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()


def test_eof_while_overrunning_tool_running_exits_bounded_and_clean() -> None:
    """Closing stdin while a tool outlives the drain bound still exits bounded and clean.

    The in-flight tool holds the tool thread, so the queued session_stop cannot start
    within the exit drain: the server cancels it before it runs and terminates the owned
    process groups directly (the wrapper and the launched app), without touching the
    engine. The exit must not wait the 40 s tool out: it os._exits 0 within the bound
    with no member of the wrapper group or the app surviving.
    """
    server = _spawn([sys.executable, "-m", "kwin_mcp"])
    reader = _LineReader(_stdout(server))
    try:
        app_pid = _start_server_session(server, reader)
        pgid = _session_pgid(server.pid)
        assert pgid is not None, "session started but the wrapper process group was not found"
        assert _is_live(app_pid), f"session_start launched {app_pid} but it is not running"

        # Occupy the tool thread with a burst whose remaining work (a 40 s frame wait)
        # outlives the server's exit drain bound; the response is not awaited.
        _fire_overrun_tool(server)
        # The tool is now running on the tool thread (in its long frame wait).
        time.sleep(1.0)
        assert _group_pids(pgid), "session group died before the client disconnected"

        # Disconnect (EOF) while the overrunning tool is still running: the server must
        # exit 0 within the bound (not wait the tool out) and tear the session down.
        _stdin(server).close()
        exit_code = server.wait(timeout=SERVER_EXIT_TIMEOUT_TOOL_OVERRUN_SECONDS)
        assert exit_code == 0, f"server did not exit 0 on stdin EOF, got {exit_code}"
        _assert_session_gone(pgid, app_pid)
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()


def test_eof_with_closed_stderr_while_overrunning_tool_exits_bounded_and_clean() -> None:
    """A client that closes its stderr reader before disconnecting still gets a bounded exit.

    A disconnecting client (or a crashing one) closes every pipe it holds, so the
    server's exit outcome line hits a broken pipe. That write is diagnostic only: it
    must not skip the forced exit, or interpreter shutdown joins the tool thread, still
    busy with the overrunning tool, and the exit waits the 40 s tool out. The server
    must exit 0 within the bound with no member of the wrapper group or the app
    surviving.
    """
    server = _spawn_with_stderr([sys.executable, "-m", "kwin_mcp"])
    reader = _LineReader(_stdout(server))
    try:
        app_pid = _start_server_session(server, reader)
        pgid = _session_pgid(server.pid)
        assert pgid is not None, "session started but the wrapper process group was not found"
        assert _is_live(app_pid), f"session_start launched {app_pid} but it is not running"

        _fire_overrun_tool(server)
        time.sleep(1.0)
        assert _group_pids(pgid), "session group died before the client disconnected"

        # Close the only stderr reader, then disconnect (EOF): the outcome write now
        # raises BrokenPipeError in the server.
        assert server.stderr is not None
        server.stderr.close()
        _stdin(server).close()
        exit_code = server.wait(timeout=SERVER_EXIT_TIMEOUT_TOOL_OVERRUN_SECONDS)
        assert exit_code == 0, f"server did not exit 0 on stdin EOF, got {exit_code}"
        _assert_session_gone(pgid, app_pid)
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()


def test_sigterm_while_overrunning_tool_running_exits_bounded_and_clean() -> None:
    """SIGTERM while a tool outlives the drain bound still exits bounded and clean.

    The in-flight tool holds the tool thread, so the queued session_stop cannot start
    within the exit drain: the server cancels it before it runs and terminates the owned
    process groups directly (the wrapper and the launched app), without touching the
    engine. The exit must not wait the 40 s tool out: it os._exits 143 within the bound
    with no member of the wrapper group or the app surviving.
    """
    server = _spawn([sys.executable, "-m", "kwin_mcp"])
    reader = _LineReader(_stdout(server))
    try:
        app_pid = _start_server_session(server, reader)
        pgid = _session_pgid(server.pid)
        assert pgid is not None, "session started but the wrapper process group was not found"
        assert _is_live(app_pid), f"session_start launched {app_pid} but it is not running"

        # Occupy the tool thread with a burst whose remaining work (a 40 s frame wait)
        # outlives the server's exit drain bound; the response is not awaited.
        _fire_overrun_tool(server)
        # The tool is now running on the tool thread (in its long frame wait).
        time.sleep(1.0)
        assert _group_pids(pgid), "session group died before the client disconnected"

        # SIGTERM while the overrunning tool is still running: the server must exit 143
        # within the bound (not wait the old 35 s executor timeout) and tear the
        # session down.
        os.kill(server.pid, signal.SIGTERM)
        exit_code = server.wait(timeout=SERVER_EXIT_TIMEOUT_TOOL_OVERRUN_SECONDS)
        assert exit_code == 143, f"expected exit 143 on SIGTERM, got {exit_code}"
        _assert_session_gone(pgid, app_pid)
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()


def test_cli_sigterm_during_session_stop_still_stops_launched_apps() -> None:
    """A SIGTERM while the CLI is tearing down a live session cannot skip launched apps.

    Regression: the signal raised KeyboardInterrupt inside LiveSession.stop(), which
    clears its running flag before terminating the launched apps; the remaining apps
    were left running and the signal-path cleanup, seeing the session as not running,
    skipped them. Teardown now defers the signals until it completes, then the CLI
    exits through its normal path.

    The first launched app traps SIGTERM and takes ~_SLOW_APP_TERM_DELAY_S seconds to
    exit, so the stop's per-app wait blocks on it; the signal stream below is
    therefore guaranteed to land while the command-driven stop is still running,
    before the remaining (instant) apps are terminated.
    """
    with live_kwin() as live:
        cli = _spawn_new_session([sys.executable, "-m", "kwin_mcp.cli"])
        reader = _LineReader(_stdout(cli))
        app_pids: list[int] = []
        try:
            stdin = _stdin(cli)
            stdin.write(
                f"session_connect dbus_address={shlex.quote(live.dbus_address)} "
                f"wayland_display={shlex.quote(live.wayland_display)}\n"
            )
            stdin.flush()
            assert _wait_for_line(
                reader, "Connected to live KWin session", SESSION_START_TIMEOUT_SECONDS
            ), "CLI did not connect to the live session"

            # First app: traps SIGTERM and dies slowly, so the stop blocks on it.
            stdin.write(f"launch_app command={shlex.quote(_SLOW_APP_COMMAND)}\n")
            stdin.flush()
            app_pid = _wait_for_app_pid(reader, SESSION_START_TIMEOUT_SECONDS)
            assert app_pid is not None, "CLI did not report the slow app"
            app_pids.append(app_pid)

            for _ in range(_CLI_STOP_TEST_APP_COUNT):
                stdin.write(f"launch_app command={shlex.quote(_APP_COMMAND)}\n")
                stdin.flush()
                app_pid = _wait_for_app_pid(reader, SESSION_START_TIMEOUT_SECONDS)
                assert app_pid is not None, "CLI did not report the launched app"
                app_pids.append(app_pid)

            # Start the teardown, then signal the CLI while the stop is running. The
            # pause lets the CLI read the line and begin the stop (blocked on the slow
            # app), so the signals land mid-stop, not before it.
            stdin.write("session_stop\n")
            stdin.flush()
            time.sleep(1.0)
            re_signal_deadline = time.monotonic() + 3.0
            while cli.poll() is None and time.monotonic() < re_signal_deadline:
                time.sleep(0.02)
                if cli.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(cli.pid, signal.SIGTERM)
            # Close stdin so the CLI exits even if it is still blocked on input().
            with contextlib.suppress(BrokenPipeError, ValueError, OSError):
                stdin.close()
            exit_code = cli.wait(timeout=SERVER_EXIT_TIMEOUT_SECONDS)
            assert exit_code == 0, f"expected the CLI to exit 0, got {exit_code}"
            for app_pid in app_pids:
                assert _wait_until(lambda p=app_pid: not _is_live(p), CLEANUP_SETTLE_SECONDS), (
                    f"launched app {app_pid} survived a signal-interrupted session_stop"
                )
        finally:
            if cli.poll() is None:
                cli.kill()
                cli.wait()


def test_live_session_eof_stops_launched_apps_keeps_kwin() -> None:
    """EOF on a live session with slow-exiting apps stops every app; KWin survives.

    The reviewer's r3 probe as a test: a live connection with five launched apps that
    each take ~2.8 s to exit after SIGTERM. The tool thread is free, so the full
    ``session_stop`` starts within the drain and runs on it; the shared escalation
    terminates all five within the TERM grace. KWin and any pre-existing app are never
    signalled.
    """
    with live_kwin() as live:
        server = _spawn([sys.executable, "-m", "kwin_mcp"])
        reader = _LineReader(_stdout(server))
        try:
            _connect_live_session(server, reader, live)
            app_pids = _launch_live_apps(
                server, reader, _LIVE_SLOW_APP_COUNT, _LIVE_SLOW_APP_COMMAND, 10
            )
            for app_pid in app_pids:
                assert _is_live(app_pid), f"launched app {app_pid} is not running"

            # EOF: the full session_stop runs on the tool thread and terminates every
            # launched app group with the shared escalation.
            _stdin(server).close()
            exit_code = server.wait(timeout=SERVER_EXIT_TIMEOUT_TOOL_OVERRUN_SECONDS)
            assert exit_code == 0, f"server did not exit 0 on stdin EOF, got {exit_code}"
            for app_pid in app_pids:
                assert _wait_until(lambda p=app_pid: not _is_live(p), CLEANUP_SETTLE_SECONDS), (
                    f"launched app {app_pid} survived the live-session EOF"
                )
            assert live.process.poll() is None, "the live KWin was signalled"
        finally:
            if server.poll() is None:
                server.kill()
                server.wait()


def test_live_session_busy_exit_terminates_launched_apps_keeps_kwin() -> None:
    """EOF on a live session with a busy tool takes the registry path; KWin survives.

    A long ``screenshot_after_ms`` tool holds the tool thread, so the queued
    ``session_stop`` cannot start within the drain and is cancelled before it runs. The
    main thread then terminates the owned app groups directly (the registry path), never
    touching the engine. All five slow-exiting apps must be gone, the stderr outcome line
    must name the registry path, and the live KWin must survive.
    """
    with live_kwin() as live:
        server = _spawn_with_stderr([sys.executable, "-m", "kwin_mcp"])
        reader = _LineReader(_stdout(server))
        try:
            _connect_live_session(server, reader, live)
            app_pids = _launch_live_apps(
                server, reader, _LIVE_SLOW_APP_COUNT, _LIVE_SLOW_APP_COMMAND, 10
            )
            for app_pid in app_pids:
                assert _is_live(app_pid), f"launched app {app_pid} is not running"

            # Occupy the tool thread with a burst whose 40 s frame wait outlives the exit
            # drain, so the stop is cancelled and the registry path runs.
            _fire_overrun_tool(server)
            time.sleep(1.0)
            for app_pid in app_pids:
                assert _is_live(app_pid), f"launched app {app_pid} died before EOF"

            _stdin(server).close()
            exit_code = server.wait(timeout=SERVER_EXIT_TIMEOUT_TOOL_OVERRUN_SECONDS)
            assert exit_code == 0, f"server did not exit 0 on stdin EOF, got {exit_code}"
            stderr = server.stderr.read() if server.stderr is not None else ""
            assert "registry" in stderr, (
                f"expected the stderr outcome line to name the registry path, got: {stderr!r}"
            )
            for app_pid in app_pids:
                assert _wait_until(lambda p=app_pid: not _is_live(p), CLEANUP_SETTLE_SECONDS), (
                    f"launched app {app_pid} survived the busy live-session exit"
                )
            assert live.process.poll() is None, "the live KWin was signalled"
        finally:
            if server.poll() is None:
                server.kill()
                server.wait()


def test_registry_spawn_after_close_and_close_waits_for_in_progress_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn after close() is refused; close() waits for an in-progress spawn.

    Unit-level, no KWin: the registry's spawn/close atomicity is the core of the
    restructure, so it is exercised directly. A Popen that blocks on an event (no
    sleeps-as-synchronization) holds the registry lock mid-spawn, so close() must not
    proceed until that spawn finishes registering its group.
    """
    import kwin_mcp.session as session_module
    from kwin_mcp.session import OwnedProcessRegistry

    # (1) A spawn after close() must not start a process.
    closed_registry = OwnedProcessRegistry()
    closed_registry.close()
    with pytest.raises(RuntimeError):
        closed_registry.spawn(["true"])

    # (2) close() must wait for an in-progress spawn to register.
    registry = OwnedProcessRegistry()
    spawn_in_progress = threading.Event()
    release = threading.Event()
    close_done = threading.Event()
    fake_pid = os.getpid() + 1

    class _BlockingPopen:
        def __init__(self, args: object, **kwargs: object) -> None:
            spawn_in_progress.set()
            release.wait()
            self.pid = fake_pid

    monkeypatch.setattr(session_module.subprocess, "Popen", _BlockingPopen)
    spawn_thread = threading.Thread(target=lambda: registry.spawn(["true"]), daemon=True)
    spawn_thread.start()
    assert spawn_in_progress.wait(timeout=5), "the spawn did not start"

    # Start close() in a thread; it blocks on the registry lock the spawn holds.
    close_thread = threading.Thread(
        target=lambda: (registry.close(), close_done.set()), daemon=True
    )
    close_thread.start()
    # While the spawn still holds the registry lock, close() must stay blocked: this
    # pin catches a close() that proceeds before the in-progress spawn registered.
    assert not close_done.wait(timeout=0.3), "close() proceeded while the spawn held the lock"

    # Release the spawn: it registers its group and releases the lock, so close()
    # (still blocked) can proceed only after the registration.
    release.set()
    spawn_thread.join(timeout=5)
    assert close_done.wait(timeout=5), "close() did not complete after the spawn finished"

    # The in-progress spawn finished registering before close() completed.
    assert fake_pid in registry._processes, "the in-progress spawn did not register before close"


def test_registry_terminate_all_does_not_signal_a_reaped_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leader that was reaped (unregistered) is not signalled by terminate_all.

    Unit-level, no KWin (F6): the registry must never signal a group whose leader is
    no longer an unreaped child of the process, because the kernel may have recycled
    that pgid into an unrelated group. Spawn a short-lived child through the registry,
    reap it through the same path ``Session.is_running`` uses (registry.poll), then run
    ``terminate_all`` and assert no signal was attempted for that pgid.
    """
    import kwin_mcp.session as session_module
    from kwin_mcp.session import OwnedProcessRegistry, Session

    registry = OwnedProcessRegistry()
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(session_module, "process_registry", registry)
    monkeypatch.setattr(
        session_module,
        "_signal_process_group",
        lambda pgid, sig: signals.append((pgid, sig)),
    )

    session = Session()
    proc = registry.spawn(["true"], start_new_session=True)
    session._process = proc
    pid = proc.pid

    # Reap it through the same path Session.is_running uses, until it exits.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and session.is_running:
        time.sleep(0.01)
    assert not session.is_running, "the reaped leader still reports as running"

    registry.close()
    registry.terminate_all()
    attempted = [sig for (pgid, sig) in signals if pgid == pid]
    assert not attempted, f"terminate_all signalled a reaped leader {pid}: {attempted}"


def test_live_session_busy_exit_removes_screenshot_dir_keeps_kwin() -> None:
    """Busy exit on a live session removes its screenshot dir; KWin survives (F7).

    The registry must remove exactly what ``session_stop`` would remove. A live
    session with default ``keep_screenshots`` has its screenshot dir removed by
    ``session_stop``; the busy-exit registry path must do the same. The live session
    creates its screenshot dir (prefix ``kwin-mcp-screenshots-``) on connect, so find
    it by listing the temp directory, then fire an overrun tool and close stdin: the
    dir must be removed and KWin must survive.
    """
    with live_kwin() as live:
        server = _spawn_with_stderr([sys.executable, "-m", "kwin_mcp"])
        reader = _LineReader(_stdout(server))
        try:
            # Record existing screenshot dirs before connecting (the live session
            # creates a new one with prefix kwin-mcp-screenshots-).
            temp_dir = Path(tempfile.gettempdir())
            before = {p for p in temp_dir.glob("kwin-mcp-screenshots-*") if p.is_dir()}
            _connect_live_session(server, reader, live)
            # Find the new screenshot dir created by the live session.
            after = {p for p in temp_dir.glob("kwin-mcp-screenshots-*") if p.is_dir()}
            new_dirs = after - before
            assert len(new_dirs) == 1, f"expected exactly one new screenshot dir, got {new_dirs}"
            screenshot_dir = next(iter(new_dirs))
            assert screenshot_dir.is_dir(), f"screenshot dir does not exist: {screenshot_dir}"

            # Occupy the tool thread so the busy (registry) path runs.
            _fire_overrun_tool(server)
            time.sleep(1.0)

            _stdin(server).close()
            exit_code = server.wait(timeout=SERVER_EXIT_TIMEOUT_TOOL_OVERRUN_SECONDS)
            assert exit_code == 0, f"server did not exit 0 on stdin EOF, got {exit_code}"
            stderr = server.stderr.read() if server.stderr is not None else ""
            assert "registry" in stderr, (
                f"expected the stderr outcome line to name the registry path, got: {stderr!r}"
            )
            # The screenshot dir must be removed (keep_screenshots is False by default).
            assert not screenshot_dir.exists(), (
                f"the live session's screenshot dir survived the busy exit: {screenshot_dir}"
            )
            assert live.process.poll() is None, "the live KWin was signalled"
        finally:
            if server.poll() is None:
                server.kill()
                server.wait()


def test_virtual_keep_home_busy_exit_removes_screenshots_keeps_home() -> None:
    """Busy exit on a virtual keep_home session removes home/.screenshots; home kept (F7).

    The registry must remove exactly what ``session_stop`` would remove. A virtual
    session with ``keep_home=True`` and default ``keep_screenshots`` has its
    ``home/.screenshots`` removed by ``session_stop`` (the home itself is kept); the
    busy-exit registry path must do the same. Start the session with
    ``isolate_home=True, keep_home=True``, take a screenshot (so ``home/.screenshots``
    exists), then fire an overrun tool and close stdin: ``home/.screenshots`` must be
    removed and the home kept.
    """
    server = _spawn_with_stderr([sys.executable, "-m", "kwin_mcp"])
    reader = _LineReader(_stdout(server))
    try:
        _rpc(
            reader,
            server,
            1,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "exit-cleanup", "version": "0"},
            },
        )
        initialized = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        stdin = _stdin(server)
        stdin.write(initialized + "\n")
        stdin.flush()
        # Start a virtual session with an isolated, kept home.
        response = _rpc(
            reader,
            server,
            2,
            "tools/call",
            {
                "name": "session_start",
                "arguments": {"isolate_home": True, "keep_home": True},
            },
        )
        text = _response_text(response)
        match = re.search(r"Isolated home: (\S+)", text)
        assert match, f"no isolated home in response: {text[:200]}"
        home_dir = Path(match.group(1))
        assert home_dir.is_dir(), f"isolated home does not exist: {home_dir}"

        # Take a screenshot so home/.screenshots exists with content.
        _rpc(reader, server, 3, "tools/call", {"name": "screenshot", "arguments": {}})
        screenshots = home_dir / ".screenshots"
        assert screenshots.is_dir(), f"home/.screenshots does not exist: {screenshots}"

        # Occupy the tool thread so the busy (registry) path runs.
        _fire_overrun_tool(server)
        time.sleep(1.0)

        _stdin(server).close()
        exit_code = server.wait(timeout=SERVER_EXIT_TIMEOUT_TOOL_OVERRUN_SECONDS)
        assert exit_code == 0, f"server did not exit 0 on stdin EOF, got {exit_code}"
        stderr = server.stderr.read() if server.stderr is not None else ""
        assert "registry" in stderr, (
            f"expected the stderr outcome line to name the registry path, got: {stderr!r}"
        )
        # home/.screenshots must be removed; the home itself is kept.
        assert not screenshots.exists(), f"home/.screenshots survived the busy exit: {screenshots}"
        assert home_dir.is_dir(), f"the kept home was removed: {home_dir}"
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()


# The app locks a subdirectory of its isolated home, so the busy-exit registry path
# cannot remove that home: the removal error must not cost the bounded exit.
_LOCKING_APP_COMMAND = (
    'sh -c \'mkdir -p "$HOME/locked/inner" && chmod 0 "$HOME/locked" && exec sleep 600\''
)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_busy_exit_with_undeletable_home_dir_still_exits_bounded() -> None:
    """A temp dir the registry cannot remove does not block the bounded busy exit.

    The launched app leaves an inaccessible subdirectory in the isolated home. On the
    busy-exit registry path the home's removal fails; the server must still terminate
    the owned groups and exit 0 within the bound instead of letting the error escape
    past the forced exit (interpreter shutdown would then wait the 40 s tool out).
    """
    server = _spawn_with_stderr([sys.executable, "-m", "kwin_mcp"])
    reader = _LineReader(_stdout(server))
    home_dir: Path | None = None
    try:
        _rpc(
            reader,
            server,
            1,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "exit-cleanup", "version": "0"},
            },
        )
        initialized = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        stdin = _stdin(server)
        stdin.write(initialized + "\n")
        stdin.flush()
        response = _rpc(
            reader,
            server,
            2,
            "tools/call",
            {
                "name": "session_start",
                "arguments": {"isolate_home": True, "app_command": _LOCKING_APP_COMMAND},
            },
        )
        text = _response_text(response)
        match = re.search(r"Isolated home: (\S+)", text)
        assert match, f"no isolated home in response: {text[:200]}"
        home_dir = Path(match.group(1))
        app_pid = _app_pid_from_response(response)
        pgid = _session_pgid(server.pid)
        assert pgid is not None, "session started but the wrapper process group was not found"
        locked = home_dir / "locked"
        assert _wait_until(lambda: locked.exists() and not os.access(locked, os.R_OK), 10), (
            f"the app did not lock {locked}"
        )

        _fire_overrun_tool(server)
        time.sleep(1.0)

        _stdin(server).close()
        exit_code = server.wait(timeout=SERVER_EXIT_TIMEOUT_TOOL_OVERRUN_SECONDS)
        assert exit_code == 0, f"server did not exit 0 on stdin EOF, got {exit_code}"
        stderr = server.stderr.read() if server.stderr is not None else ""
        assert "registry" in stderr, (
            f"expected the stderr outcome line to name the registry path, got: {stderr!r}"
        )
        _assert_session_gone(pgid, app_pid)
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()
        if home_dir is not None and (home_dir / "locked").exists():
            (home_dir / "locked").chmod(0o700)
            shutil.rmtree(home_dir, ignore_errors=True)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_registry_terminate_all_removes_remaining_dirs_past_a_failing_one(
    tmp_path: Path,
) -> None:
    """One temp dir that cannot be removed does not stop the removal of the others.

    Unit-level, no KWin: ``terminate_all`` runs on the exit path, so it must neither
    raise nor stop at the first directory it cannot remove.
    """
    from kwin_mcp.session import OwnedProcessRegistry

    stuck = tmp_path / "stuck"
    (stuck / "locked" / "inner").mkdir(parents=True)
    (stuck / "locked").chmod(0)
    removable = tmp_path / "removable"
    removable.mkdir()
    registry = OwnedProcessRegistry()
    registry.register_temp_dir(stuck)
    registry.register_temp_dir(removable)
    try:
        registry.close()
        registry.terminate_all()
        assert not removable.exists(), "a dir after the failing one was not removed"
    finally:
        (stuck / "locked").chmod(0o700)


_FAILING_CLEANUP_SERVER = """
import sys
import time

import kwin_mcp.server as server


def failing_terminate_all():
    raise RuntimeError("injected cleanup failure")


def fake_run():
    server._tool_executor.submit(time.sleep, 30)
    print("ready", flush=True)
    sys.stdin.read()


server.process_registry.terminate_all = failing_terminate_all
server.mcp.run = fake_run
server.main()
"""


def test_exit_cleanup_failure_still_exits_bounded() -> None:
    """An exception from the exit cleanup is reported, and the forced exit still runs.

    No KWin: the transport is replaced by one that occupies the tool thread with a 30 s
    job and waits for EOF, and the registry's ``terminate_all`` raises. The server must
    exit 0 within the bound with the failure named on stderr, instead of letting the
    exception skip the forced exit and wait the job out.
    """
    server = _spawn_with_stderr([sys.executable, "-c", _FAILING_CLEANUP_SERVER])
    reader = _LineReader(_stdout(server))
    try:
        assert _wait_for_line(reader, "ready", 30), "the fake transport did not start"
        _stdin(server).close()
        exit_code = server.wait(timeout=SERVER_EXIT_TIMEOUT_TOOL_OVERRUN_SECONDS)
        assert exit_code == 0, f"server did not exit 0 on stdin EOF, got {exit_code}"
        stderr = server.stderr.read() if server.stderr is not None else ""
        assert "injected cleanup failure" in stderr, (
            f"expected the stderr outcome line to report the failure, got: {stderr!r}"
        )
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()


def test_live_session_serialized_stop_completes_beyond_drain() -> None:
    """An already-started final stop that outlives the drain completes; the process
    waits for it.

    Candidate coverage for the serialized path, not a fail-before oracle for an
    abandoned stop (the path check rests on the outcome wording). A live session with
    five apps that each trap SIGTERM and take ~2.8 s to exit. The tool thread is
    free, so the stop starts within
    the 2 s drain and runs on it. The stop's shared escalation takes ~2.8 s (the apps'
    TERM delay) plus the grace waits — longer than the drain. The main thread must wait
    for the stop to finish (not exit before it does), so every app is gone and the
    stderr outcome line names the serialized path.
    """
    with live_kwin() as live:
        server = _spawn_with_stderr([sys.executable, "-m", "kwin_mcp"])
        reader = _LineReader(_stdout(server))
        try:
            _connect_live_session(server, reader, live)
            app_pids = _launch_live_apps(
                server, reader, _LIVE_SLOW_APP_COUNT, _LIVE_SLOW_APP_COMMAND, 10
            )
            for app_pid in app_pids:
                assert _is_live(app_pid), f"launched app {app_pid} is not running"

            # EOF: the tool thread is free, so the stop starts within the drain and
            # runs on it. The stop's shared escalation (~2.8 s + grace) outlives the
            # drain; the main thread must wait for it to finish.
            _stdin(server).close()
            exit_code = server.wait(timeout=SERVER_EXIT_TIMEOUT_TOOL_OVERRUN_SECONDS)
            assert exit_code == 0, f"server did not exit 0 on stdin EOF, got {exit_code}"
            stderr = server.stderr.read() if server.stderr is not None else ""
            assert "serialized" in stderr, (
                f"expected the stderr outcome line to name the serialized path, got: {stderr!r}"
            )
            for app_pid in app_pids:
                assert _wait_until(lambda p=app_pid: not _is_live(p), CLEANUP_SETTLE_SECONDS), (
                    f"launched app {app_pid} survived the serialized live-session exit"
                )
            assert live.process.poll() is None, "the live KWin was signalled"
        finally:
            if server.poll() is None:
                server.kill()
                server.wait()


# A server whose first tool submission raises SIGTERM into the asyncio loop, at the
# exact awaitable boundary the signal used to be thrown through.
_SIGTERM_ON_FIRST_SUBMIT_SERVER = """
import os
import signal

import kwin_mcp.server as server

real_submit = server._tool_executor.submit


def sigterm_then_submit(*args, **kwargs):
    server._tool_executor.submit = real_submit
    os.kill(os.getpid(), signal.SIGTERM)
    return real_submit(*args, **kwargs)


server._tool_executor.submit = sigterm_then_submit
server.main()
"""


def test_sigterm_during_tool_submission_exits_143_with_stdin_open() -> None:
    """SIGTERM raised inside the asyncio loop during a tool submission still exits 143.

    The first ``_tool_executor.submit`` (our ``session_stop`` call) kills the process
    mid-``mcp.run()``. A handler that raises there reaches ``main`` wrapped by the
    SDK's request task group, so the old ``except _Shutdown`` missed it and the
    forced exit ran with code 0. Stdin stays open so only the signal path can end
    the server; exit 143 plus the stderr outcome line proves the signal was
    recorded and the shared cleanup ran.
    """
    server = _spawn_with_stderr([sys.executable, "-c", _SIGTERM_ON_FIRST_SUBMIT_SERVER])
    reader = _LineReader(_stdout(server))
    try:
        _initialize_server(server, reader)

        # The tool needs no session; its submit is what delivers the SIGTERM. The
        # response is not awaited: the signal path may os._exit before writing it.
        request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "session_stop", "arguments": {}},
        }
        stdin = _stdin(server)
        stdin.write(json.dumps(request) + "\n")
        stdin.flush()

        exit_code = server.wait(timeout=SERVER_EXIT_TIMEOUT_IDLE_SECONDS)
        assert exit_code == 143, (
            f"expected exit 143 when a signal lands mid-submission, got {exit_code}"
        )
        stderr = server.stderr.read() if server.stderr is not None else ""
        assert "kwin-mcp: exit cleanup:" in stderr, (
            f"the exit outcome line was not written to stderr: {stderr!r}"
        )
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()


def test_sigint_idle_exits_130_with_stdin_open() -> None:
    """SIGINT on an idle, session-less server exits 130 while stdin stays open.

    Same class of regression as the mid-submission SIGTERM: a signal that unwinds
    the asyncio loop must land as the conventional 128+signum exit with the exit
    cleanup still run, not as a bare loop unwind that exits 0 or wedges. Stdin
    stays open so EOF cannot end the server first.
    """
    server = _spawn([sys.executable, "-m", "kwin_mcp"])
    reader = _LineReader(_stdout(server))
    try:
        _initialize_server(server, reader)

        os.kill(server.pid, signal.SIGINT)
        exit_code = server.wait(timeout=SERVER_EXIT_TIMEOUT_IDLE_SECONDS)
        assert exit_code == 130, f"expected exit 130 on SIGINT, got {exit_code}"
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()


def test_registry_escalation_kills_term_ignoring_member_despite_concurrent_poll() -> None:
    """A registry poll during escalation must not reap the leader before SIGKILL.

    Unit-level, no KWin. The leader's group holds a member that ignores SIGTERM,
    so the TERM grace expires with the group still alive and the SIGKILL step is
    what actually kills it. A concurrent ``registry.poll`` that reaped the leader
    (what ``proc.poll()`` does) would leave ``_leader_reaped`` True and the KILL
    step would skip the group, leaking the member; the contract requires the
    escalation's polls to observe the exit without reaping it.
    """
    from kwin_mcp.session import OwnedProcessRegistry

    registry = OwnedProcessRegistry()
    leader = registry.spawn(
        ["bash", "-c", "bash -c 'trap \"\" TERM; exec sleep 60' & wait"],
        start_new_session=True,
    )
    pgid = leader.pid

    def term_ignoring_member_is_sleeping() -> bool:
        return any("sleep" in _cmdline(pid) for pid in _group_pids(pgid) if pid != leader.pid)

    try:
        # The member's ``trap`` runs before its ``exec sleep``, so a live member
        # already running as ``sleep`` is guaranteed to ignore the pending SIGTERM.
        assert _wait_until(term_ignoring_member_is_sleeping, 10), (
            f"the TERM-ignoring member never exec'd sleep; group: {sorted(_group_pids(pgid))}"
        )

        escalator = threading.Thread(
            target=lambda: registry.terminate_leaders([leader], reap=False), daemon=True
        )
        escalator.start()
        while escalator.is_alive():
            registry.poll(leader)
            time.sleep(0.01)
        escalator.join()

        # The leader is dead by now, so the registry's poll must report the exit
        # (without having reaped it mid-escalation, which is what the poll loop
        # above was hammering on).
        assert registry.poll(leader) is not None, (
            "the leader exited during escalation but the registry's poll never saw it"
        )
        assert _wait_until(lambda: not _group_pids(pgid), CLEANUP_SETTLE_SECONDS), (
            f"a TERM-ignoring member survived the escalation: {sorted(_group_pids(pgid))}"
        )
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            leader.wait(timeout=5)


def test_registry_register_temp_dir_after_close_removes_dir_and_raises() -> None:
    """register_temp_dir on a closed registry removes the dir and raises.

    Unit-level, no KWin. After ``close()`` a newly-registered dir would never be
    visited by ``terminate_all`` (the registry path already ran or is past the
    point of taking new work), so it must be removed on the spot rather than
    recorded — the failure mode being asserted is a silent leak, not just a
    missing RuntimeError.
    """
    from kwin_mcp.session import OwnedProcessRegistry

    registry = OwnedProcessRegistry()
    registry.close()
    orphan_dir = Path(tempfile.mkdtemp())
    try:
        with pytest.raises(RuntimeError):
            registry.register_temp_dir(orphan_dir)
        assert not orphan_dir.exists(), (
            f"register_temp_dir on a closed registry left {orphan_dir} behind"
        )
    finally:
        shutil.rmtree(orphan_dir, ignore_errors=True)


# A CLI that spawns an owned process group but never attaches it to a session:
# on exit the registry sweep is the only thing that can stop it.
_CLI_OWNED_GROUP_LEAK = """
import kwin_mcp.cli as cli
from kwin_mcp.session import process_registry

proc = process_registry.spawn(["sleep", "60"], start_new_session=True)


def cmdloop_with_pid(self, *args, **kwargs):
    print(proc.pid, flush=True)
    return cli.cmd.Cmd.cmdloop(self, *args, **kwargs)


cli.KwinMcpShell.cmdloop = cmdloop_with_pid
cli.main()
"""


@pytest.mark.parametrize("exit_mode", ["eof", "sigterm"])
def test_cli_exit_terminates_owned_process_not_attached_to_a_session(exit_mode: str) -> None:
    """A CLI exit kills an owned group that no session ever claimed.

    The spawned ``sleep`` is a registry leader but never becomes a session app
    (the launch is orphaned between spawn and registration, e.g. by a failed
    launch_app), so ``session_stop`` has nothing to stop. Every CLI exit path —
    EOF on stdin or a shutdown signal — must run the registry sweep so the group
    is terminated rather than leaked. The pid marker is printed only once the
    CLI has installed its signal handlers, so the SIGTERM variant cannot race
    the handler installation.
    """
    cli = _spawn([sys.executable, "-c", _CLI_OWNED_GROUP_LEAK])
    reader = _LineReader(_stdout(cli))
    sleep_pid: int | None = None
    try:
        line = reader.get(timeout=SERVER_EXIT_TIMEOUT_IDLE_SECONDS)
        assert line is not None, "the CLI never reported the owned process pid"
        sleep_pid = int(line.strip())
        assert _is_live(sleep_pid), f"the spawned process {sleep_pid} is not running"

        if exit_mode == "sigterm":
            # Stdin stays open, so the CLI exits through the signal path alone.
            os.kill(cli.pid, signal.SIGTERM)
        else:
            _stdin(cli).close()
        exit_code = cli.wait(timeout=SERVER_EXIT_TIMEOUT_IDLE_SECONDS)
        assert exit_code == 0, f"the CLI did not exit 0 on {exit_mode}, got {exit_code}"
        assert _wait_until(lambda: not _is_live(sleep_pid), CLEANUP_SETTLE_SECONDS), (
            f"the owned process {sleep_pid} survived the CLI's {exit_mode} exit"
        )
    finally:
        if sleep_pid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(sleep_pid, signal.SIGKILL)
        if cli.poll() is None:
            cli.kill()
            cli.wait()
