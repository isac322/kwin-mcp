"""The kwin-mcp server stops a live session when the process itself exits.

The live-session half of the exit-cleanup coverage (``test_exit_cleanup_virtual.py``
holds the virtual and session-less half and the stdio helpers reused here). For a
live session every exit drops the connection with its launched apps stopped while
the test-owned KWin and any pre-existing app are never signalled — both when the
final ``session_stop`` runs on the tool thread and when a busy tool forces the
owned-process registry path. The registry path must also remove exactly what
``session_stop`` would (the unretained artifacts dir) and restore the desktop's
accessibility switch.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from session_harness import live_kwin
from test_exit_cleanup_virtual import (
    CLEANUP_SETTLE_SECONDS,
    SERVER_EXIT_TIMEOUT_TOOL_OVERRUN_SECONDS,
    _app_pid_from_response,
    _connect_live_session,
    _fire_overrun_tool,
    _is_live,
    _LineReader,
    _rpc,
    _spawn,
    _spawn_with_stderr,
    _stdin,
    _stdout,
    _wait_until,
)

from kwin_mcp.session import accessibility_enabled, set_accessibility_enabled

if TYPE_CHECKING:
    import subprocess


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
            {"name": "app_launch", "arguments": {"command": command}},
        )
        pids.append(_app_pid_from_response(response))
    return pids


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

    A long ``capture={delays_ms:[...]}`` tool holds the tool thread, so the queued
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


def test_live_session_busy_exit_removes_screen_capture_dir_keeps_kwin() -> None:
    """Busy exit on a live session removes its artifacts dir; KWin survives (F7).

    The registry must remove exactly what ``session_stop`` would remove. A live
    session with the default retention has its artifacts dir removed by
    ``session_stop``; the busy-exit registry path must do the same. The live session
    creates its artifacts dir (prefix ``kwin-mcp-screenshots-``) on connect; it is
    the only new such dir in the temp directory and the one ``SessionInfo`` names.
    Then an overrun tool is fired and stdin closed: the dir must be removed and KWin
    must survive.
    """
    with live_kwin() as live:
        server = _spawn_with_stderr([sys.executable, "-m", "kwin_mcp"])
        reader = _LineReader(_stdout(server))
        try:
            # Record existing artifacts dirs before connecting (the live session
            # creates a new one with prefix kwin-mcp-screenshots-).
            temp_dir = Path(tempfile.gettempdir())
            before = {p for p in temp_dir.glob("kwin-mcp-screenshots-*") if p.is_dir()}
            info = _connect_live_session(server, reader, live)
            assert info["retention"] == {"artifacts": False, "home": False}, info
            # Find the new artifacts dir created by the live session.
            after = {p for p in temp_dir.glob("kwin-mcp-screenshots-*") if p.is_dir()}
            new_dirs = after - before
            assert new_dirs == {Path(info["artifacts_dir"])}, (
                f"expected exactly the reported artifacts dir, got {new_dirs}"
            )
            screenshot_dir = Path(info["artifacts_dir"])
            assert screenshot_dir.is_dir(), f"artifacts dir does not exist: {screenshot_dir}"

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
            # The artifacts dir must be removed (retention.artifacts is false by default).
            assert not screenshot_dir.exists(), (
                f"the live session's screenshot dir survived the busy exit: {screenshot_dir}"
            )
            assert live.process.poll() is None, "the live KWin was signalled"
        finally:
            if server.poll() is None:
                server.kill()
                server.wait()


def test_live_session_busy_exit_restores_accessibility_keeps_kwin() -> None:
    """Busy exit turns the live desktop's accessibility switch back off; KWin survives.

    ``session_connect(enable_accessibility=true)`` turns ``org.a11y.Status.IsEnabled``
    on and ``session_stop`` turns it back off. When a tool still holds the tool thread
    at exit, the stop never runs and the registry path cleans up instead; it must
    restore the switch too, or the desktop keeps accessibility on after the server is
    gone (and a later connection, seeing it on, would never turn it off).
    """
    with live_kwin() as live:
        # The switch outlives a bus, so start from a known off state.
        set_accessibility_enabled(live.dbus_address, False)
        assert not accessibility_enabled(live.dbus_address)
        server = _spawn_with_stderr([sys.executable, "-m", "kwin_mcp"])
        reader = _LineReader(_stdout(server))
        try:
            info = _connect_live_session(server, reader, live, enable_accessibility=True)
            assert info["accessibility"] == "switched_on", info
            assert accessibility_enabled(live.dbus_address)

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
            assert not accessibility_enabled(live.dbus_address), (
                "the busy exit left the live desktop's accessibility switch on"
            )
            assert live.process.poll() is None, "the live KWin was signalled"
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
