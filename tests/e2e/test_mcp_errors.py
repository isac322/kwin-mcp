"""Tool errors versus non-error results over the installed server, in both protocol eras.

S §3.5: a ``ToolError`` is ``isError`` with one ``TextContent``, no
``structuredContent`` and no image: after the SDK prefix, line 1 says what failed
and line 2 is a literal remedy. Absence and refusals are data, not errors. The
server survives every error and refuses queued work once shutdown begins.
"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from mcp.types import ImageContent, TextContent
from mcp_harness import (
    ERAS,
    LEGACY_PROTOCOL_VERSION,
    MAX_LINE_BYTES,
    TOOL_NAMES,
    McpTestClient,
    ModernMcpTestClient,
    ScriptedUser,
    raw_mcp_client,
    running_client,
    running_mcp_server,
)
from probe_session import PROBE_APP, PROBE_COMMAND, probe_env, read_events
from session_harness import live_kwin
from window_zoo import APP_ID as ZOO_APP_ID
from window_zoo import ZooProcess

from kwin_mcp.server import EXIT_DRAIN_SECONDS, EXIT_REPORT_SECONDS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcp.types import CallToolResult
    from session_harness import LiveKWin

pytestmark = pytest.mark.anyio

Client = McpTestClient | ModernMcpTestClient
SESSION_ARGUMENTS = {"screen_width": 1280, "screen_height": 800}
SCREENSHOT_IMAGES_FLAG = "--screenshot-images"
APP_WAIT = {"timeout_ms": 15_000}
UNKNOWN_WINDOW_ID = "{00000000-0000-0000-0000-000000000000}"
BAD_BINARY = "definitely-not-a-real-binary"
STALE_REF = "e999.1"

NO_SESSION = (
    "No session running.",
    "Call session_start (virtual) or session_connect (live desktop).",
)
SESSION_RUNNING = ("Session already running.", "Call session_stop first.")
NO_CLIPBOARD = (
    "Clipboard not enabled.",
    "Pass enable_clipboard=true to session_start, or use session_connect "
    "(clipboard is always enabled for live sessions).",
)
NO_INPUT = (
    "No input backend (KWin EIS unavailable); install kwin-common or reconnect.",
    "Call session_stop, then session_start or session_connect again.",
)
SHUTDOWN_TEXT = "Server is shutting down; tool not executed."

# The tools that never gate on a running session.
UNGATED = frozenset({"session_start", "session_connect", "session_stop", "session_info"})
POINT = {"x": 10, "y": 10}
# The smallest valid arguments of every tool.
MINIMAL_ARGUMENTS: dict[str, dict[str, Any]] = {
    "session_start": {},
    "session_connect": {},
    "session_stop": {},
    "session_info": {},
    "app_launch": {"command": "true"},
    "app_log": {"pid": 1},
    "screen_capture": {},
    "element_query": {},
    "element_act": {"element": {"ref": "e1.1"}, "action": "Press"},
    "window_list": {},
    "desktop_list": {},
    "window_focus": {"window": {"id": "{x}"}},
    "window_place": {"window": {"id": "{x}"}, "desktop": "1"},
    "window_close": {"window": {"id": "{x}"}},
    "pointer_click": {"target": POINT},
    "pointer_move": {"target": POINT},
    "pointer_scroll": {"target": POINT, "delta": 1},
    "pointer_drag": {"from": POINT, "to": {"x": 20, "y": 20}},
    "pointer_hold": {"phase": "press", "target": POINT},
    "keyboard_press": {"keys": "a"},
    "keyboard_type": {"text": "a"},
    "keyboard_hold": {"phase": "press", "keys": "a"},
    "touch_tap": {"target": POINT},
    "touch_swipe": {"from": POINT, "to": {"x": 20, "y": 20}},
    "touch_pinch": {"center": POINT, "start_distance": 10, "end_distance": 40},
    "clipboard_read": {},
    "clipboard_write": {"text": "a"},
    "dbus_call": {"service": "org.kde.KWin", "path": "/", "interface": "a.b", "method": "M"},
    "wayland_list": {},
}
assert tuple(MINIMAL_ARGUMENTS) == TOOL_NAMES

# Loaded into the server process through PYTHONPATH: libei cannot be loaded, so
# sessions start without an input backend (the server's own code is untouched).
_NO_LIBEI_SITECUSTOMIZE = """\
import kwin_mcp.input as _input


def _missing_libei():
    raise OSError("libei.so.1: cannot open shared object file (test fault injection)")


_input._load_libei = _missing_libei
"""


@pytest.fixture
def anyio_backend() -> str:
    """The installed MCP client runs on asyncio."""
    return "asyncio"


def _assert_tool_error_shape(name: str, result: CallToolResult) -> tuple[str, ...]:
    """The lines of a ToolError after the SDK prefix; the result shape is S §3.5's."""
    assert result.is_error, (name, result)
    assert result.structured_content is None, (name, result)
    assert len(result.content) == 1, (name, result.content)
    assert isinstance(result.content[0], TextContent), (name, result.content)
    assert not any(isinstance(c, ImageContent) for c in result.content)
    prefix = f"Error executing tool {name}: "
    text = result.content[0].text
    assert text.startswith(prefix), (name, text)
    return tuple(text.removeprefix(prefix).split("\n"))


async def _tool_error(
    client: Client, name: str, arguments: dict[str, Any] | None = None
) -> tuple[str, ...]:
    return _assert_tool_error_shape(name, await client.call_result(name, arguments or {}))


async def _wait_for_zoo_windows(client: Client, count: int) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 15
    while True:
        listing, _ = await client.call_structured("window_list", {"filter": {"app_id": ZOO_APP_ID}})
        if len(listing["windows"]) >= count:
            return listing["windows"]
        assert time.monotonic() < deadline, listing
        await anyio.sleep(0.2)


async def _session_endpoints(client: Client) -> tuple[str, str]:
    info, _ = await client.call_structured("session_info")
    assert info["session"] is not None, info
    return info["session"]["wayland_socket"], info["session"]["dbus_address"]


def _start_zoo(wayland_display: str, dbus_address: str, *commands: str) -> ZooProcess:
    zoo = ZooProcess(
        wayland_display=wayland_display, dbus_address=dbus_address, open_commands=commands
    )
    zoo.expect("window: Zoo")
    return zoo


# ── QA-MCP-013 / QA-MCP-015: the ToolError table ─────────────────────────


async def _error_table(client: Client, after_each: Callable[[], Awaitable[None]]) -> None:
    """Trigger each ToolError class and check its exact lines; ``after_each`` runs after each."""
    assert await _tool_error(client, "pointer_click", {"target": POINT}) == NO_SESSION
    await after_each()

    info, _ = await client.call_structured("session_start", SESSION_ARGUMENTS)
    assert info["input_backend"] == "eis", info
    zoo: ZooProcess | None = None
    try:
        assert await _tool_error(client, "session_start", SESSION_ARGUMENTS) == SESSION_RUNNING
        await after_each()
        assert await _tool_error(client, "clipboard_read") == NO_CLIPBOARD
        await after_each()
        # S §3.5 states the stale-ref text as one line carrying its own remedy.
        assert await _tool_error(
            client, "element_act", {"element": {"ref": STALE_REF}, "action": "Press"}
        ) == (f"Stale ref {STALE_REF} (AT-SPI worker restarted); re-run element_query.",)
        await after_each()
        assert await _tool_error(client, "keyboard_type", {"text": "é"}) == (
            "keyboard_type cannot type 'é'.",
            'Retry with method="unicode".',
        )
        await after_each()

        wayland_display, dbus_address = await _session_endpoints(client)
        zoo = await anyio.to_thread.run_sync(
            _start_zoo, wayland_display, dbus_address, "same-caption"
        )
        windows = await _wait_for_zoo_windows(client, 2)
        selector = {"app_id": ZOO_APP_ID, "caption": "Zoo"}
        line, remedy = await _tool_error(client, "window_focus", {"window": selector})
        assert line.startswith("2 windows match "), line
        for window in windows:
            assert f'{window["id"]} {ZOO_APP_ID} "Zoo" ({window["kind"]})' in line, line
        assert remedy == 'Narrow the selector: add kind:["normal"] or use id.'
        await after_each()

        await client.call_structured("pointer_hold", {"phase": "press", "target": POINT})
        try:
            assert await _tool_error(client, "pointer_move", {"target": POINT}) == (
                "pointer_move refused: left is held by pointer_hold.",
                "To continue: use pointer_hold(phase='move', button, target) to move a held "
                "button.",
            )
            await after_each()
            assert await _tool_error(client, "pointer_click", {"target": POINT}) == (
                "pointer_click refused: left is held by pointer_hold; release it first.",
                "Release with pointer_hold(phase='release', button='left', target={x, y}), "
                "then retry.",
            )
            await after_each()
        finally:
            await client.call_structured("pointer_hold", {"phase": "release", "target": POINT})

        # The contract keeps 0.11's single-line text for an unknown key (contract §2 row 20).
        assert await _tool_error(client, "keyboard_press", {"keys": "nosuchkey"}) == (
            "Unknown key: nosuchkey",
        )
        await after_each()

        assert await _tool_error(
            client, "pointer_click", {"target": POINT, "modifers": "ctrl"}
        ) == ("Unknown argument 'modifers' for pointer_click.", "Did you mean 'modifiers'?")
        await after_each()
    finally:
        if zoo is not None:
            await anyio.to_thread.run_sync(zoo.stop)
        await client.call_structured("session_stop")


async def _no_input_backend_error(era: str, tmp_path: Path) -> None:
    """A session whose input backend could not start: every input tool names EIS."""
    (tmp_path / "sitecustomize.py").write_text(_NO_LIBEI_SITECUSTOMIZE, encoding="utf-8")
    env = {"PYTHONPATH": str(tmp_path)}
    async with running_client(era, SCREENSHOT_IMAGES_FLAG, env=env) as client:
        info, _ = await client.call_structured("session_start", SESSION_ARGUMENTS)
        try:
            assert info["input_backend"] is None, info
            for tool in ("pointer_click", "keyboard_press", "touch_tap"):
                assert await _tool_error(client, tool, MINIMAL_ARGUMENTS[tool]) == NO_INPUT
        finally:
            await client.call_structured("session_stop")


@pytest.mark.qa("QA-MCP-013")
@pytest.mark.parametrize("era", ERAS)
async def test_tool_error_two_line_format_table(era: str, tmp_path: Path) -> None:
    async with running_client(era, SCREENSHOT_IMAGES_FLAG) as client:

        async def listing_unchanged() -> None:
            assert [tool.name for tool in await client.list_tools()] == list(TOOL_NAMES)

        await _error_table(client, listing_unchanged)
    await _no_input_backend_error(era, tmp_path)


# ── QA-MCP-014: absence and refusals are data ─────────────────────────────


def _kill_compositor(live: LiveKWin) -> None:
    """Kill the live compositor only (its D-Bus stays up), like a KWin crash."""
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            argv = cmdline.read_bytes().split(b"\0")
        except OSError:
            continue
        if argv and argv[0].endswith(b"kwin_wayland") and live.wayland_display.encode() in argv:
            os.kill(int(cmdline.parent.name), signal.SIGKILL)
            return
    msg = f"no kwin_wayland process serves {live.wayland_display}"
    raise AssertionError(msg)


async def _virtual_absence(client: Client, tmp_path: Path) -> None:
    await client.call_structured("session_start", SESSION_ARGUMENTS)
    zoo: ZooProcess | None = None
    try:
        await client.call_structured(
            "app_launch",
            {"command": PROBE_COMMAND, "env": probe_env(tmp_path / "probe-events.jsonl")},
        )
        found, _ = await client.call_structured(
            "element_query",
            {"locator": {"app": PROBE_APP, "query": "Keyboard Target"}, "wait": APP_WAIT},
        )
        assert found["wait"]["satisfied"], found

        nothing, _ = await client.call_structured(
            "element_query", {"locator": {"app": "no-such-app-for-kwin-mcp"}}
        )
        assert nothing["elements"] == [], nothing
        no_windows, _ = await client.call_structured(
            "window_list", {"filter": {"app_id": "no-such-app-for-kwin-mcp"}}
        )
        assert no_windows["windows"] == [], no_windows
        timed_out, text = await client.call_structured(
            "element_query",
            {"locator": {"query": "No Such Element Anywhere"}, "wait": {"timeout_ms": 500}},
        )
        assert timed_out["wait"]["satisfied"] is False, timed_out
        assert timed_out["elements"] == [], timed_out
        assert text.startswith("NOT SATISFIED:"), text

        missing, text = await client.call_structured(
            "window_close", {"window": {"id": UNKNOWN_WINDOW_ID}}
        )
        assert (missing["requested"], missing["refusal"]) == (False, "no_window"), missing
        assert text == f"No window with id '{UNKNOWN_WINDOW_ID}'."

        wayland_display, dbus_address = await _session_endpoints(client)
        zoo = await anyio.to_thread.run_sync(_start_zoo, wayland_display, dbus_address)
        await anyio.to_thread.run_sync(zoo.command, "closer", "window: Zoo Closer")
        windows = await _wait_for_zoo_windows(client, 2)
        closer = next(w for w in windows if w["caption"] == "Zoo Closer")
        # The closer must still be open (only window_place's resize may close it).
        await anyio.sleep(1.0)
        assert not any(line == "closed: Zoo Closer" for line in zoo.lines), zoo.lines
        placed, text = await client.call_structured(
            "window_place",
            {"window": {"id": closer["id"]}, "frame": {"width": closer["frame"]["width"] + 40}},
        )
        await anyio.to_thread.run_sync(zoo.expect, "closed: Zoo Closer")
        # The closer destroys itself once resized: the readback finds no window.
        assert placed["window"] is None, placed
        assert placed["applied"]["frame"] is False, placed
        assert "observed: window no longer exists (it closed or its client exited)" in (
            text.splitlines()
        ), text
    finally:
        if zoo is not None:
            await anyio.to_thread.run_sync(zoo.stop)
        await client.call_structured("session_stop")


async def _live_refusals(era: str) -> None:
    with live_kwin() as live:
        zoo = await anyio.to_thread.run_sync(_start_zoo, live.wayland_display, live.dbus_address)
        try:
            user = ScriptedUser()
            async with running_client(era, elicitation_callback=user) as client:
                await client.call_structured(
                    "session_connect",
                    {"dbus_address": live.dbus_address, "wayland_display": live.wayland_display},
                )
                (window,) = await _wait_for_zoo_windows(client, 1)
                for answer, consent in (
                    ("unconfirmed", "not_confirmed"),
                    ("decline", "declined"),
                    ("cancel", "cancelled"),
                ):
                    user.answer = answer
                    refused, _ = await client.call_structured(
                        "window_close", {"window": {"id": window["id"]}}
                    )
                    assert (refused["requested"], refused["consent"]) == (False, consent)
                    assert refused["refusal"] == "no_consent", refused
                assert len(user.requests) == 3

                _kill_compositor(live)
                failure = await client.call_result("desktop_list")
                lines = _assert_tool_error_shape("desktop_list", failure)
                assert lines[0], lines
                await client.call_structured("session_stop")

            # A client without elicitation: the unsupported refusal, never an error.
        finally:
            await anyio.to_thread.run_sync(zoo.stop)

    with live_kwin() as live:
        zoo = await anyio.to_thread.run_sync(_start_zoo, live.wayland_display, live.dbus_address)
        try:
            async with running_client(era) as client:
                await client.call_structured(
                    "session_connect",
                    {"dbus_address": live.dbus_address, "wayland_display": live.wayland_display},
                )
                (window,) = await _wait_for_zoo_windows(client, 1)
                refused, _ = await client.call_structured(
                    "window_close", {"window": {"id": window["id"]}}
                )
                assert (refused["requested"], refused["consent"], refused["refusal"]) == (
                    False,
                    "unsupported",
                    "no_consent",
                ), refused
                await client.call_structured("session_stop")
        finally:
            await anyio.to_thread.run_sync(zoo.stop)


@pytest.mark.qa("QA-MCP-014")
@pytest.mark.parametrize("era", ERAS)
async def test_absence_and_refusals_are_non_error(era: str, tmp_path: Path) -> None:
    async with running_client(era) as client:
        await _virtual_absence(client, tmp_path)
    await _live_refusals(era)


# ── QA-MCP-015: the server survives every error class ────────────────────


@pytest.mark.qa("QA-MCP-015")
async def test_server_survives_every_error_class(tmp_path: Path) -> None:
    async with running_mcp_server() as client:

        async def alive() -> None:
            assert await client.session.send_ping() is not None
            assert [tool.name for tool in await client.list_tools()] == list(TOOL_NAMES)

        await _error_table(client, alive)
        await _virtual_absence(client, tmp_path)
        await alive()

        # An engine exception (a launch that fails) is a ToolError too, and keeps the session.
        await client.call_structured("session_start", SESSION_ARGUMENTS)
        try:
            lines = await _tool_error(client, "app_launch", {"command": BAD_BINARY})
            assert BAD_BINARY in lines[0], lines
            assert "session_stop" in "\n".join(lines), lines
            await alive()
            info, _ = await client.call_structured("session_info")
            assert info["session"] is not None, info
        finally:
            stopped, text = await client.call_structured("session_stop")
        assert stopped["stopped"] is True, stopped
        assert text == "Session stopped."
        await alive()


# ── QA-GATE-001: the no-session gate ──────────────────────────────────────


@pytest.mark.qa("QA-GATE-001")
@pytest.mark.parametrize("era", ERAS)
async def test_no_session_gate_every_tool(era: str) -> None:
    gated = [name for name in TOOL_NAMES if name not in UNGATED]
    assert len(gated) == 25
    user = ScriptedUser(answer="confirm")
    async with running_client(era, elicitation_callback=user) as client:
        for name in gated:
            assert await _tool_error(client, name, MINIMAL_ARGUMENTS[name]) == NO_SESSION, name
        # window_close asks nothing without a session.
        assert user.requests == []
        assert [tool.name for tool in await client.list_tools()] == list(TOOL_NAMES)
        info, _ = await client.call_structured("session_info")
        assert info["session"] is None, info


# ── QA-MCP-026: the shutdown gate ─────────────────────────────────────────


async def _read_until_eof(client: Any, timeout_s: float) -> list[dict[str, Any]]:
    """Every message the server writes until it closes stdout."""
    messages: list[dict[str, Any]] = []
    with anyio.fail_after(timeout_s):
        while True:
            try:
                line = await client._stdout.receive_until(b"\n", MAX_LINE_BYTES)
            except (anyio.EndOfStream, anyio.IncompleteRead):
                return messages
            messages.append(json.loads(line))


def _server_pid() -> int:
    """PID of the kwin-mcp server the harness spawned (this test's only child).

    The entry point is a script, so exec resolves to the interpreter: argv[0]
    is ``python`` and the ``kwin-mcp`` name appears as a later argument.
    """
    me = os.getpid()
    pids: list[int] = []
    for proc_dir in Path("/proc").glob("[0-9]*"):
        try:
            # /proc/<pid>/stat: "pid (comm) state ppid ..." — comm may hold spaces or
            # parens, so the fields after the last ")" start with state then ppid.
            fields = (proc_dir / "stat").read_bytes().rpartition(b")")[2].split()
            args = (proc_dir / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError):
            continue
        if fields[0] == b"Z" or int(fields[1]) != me:
            continue
        if any(os.path.basename(arg) == b"kwin-mcp" for arg in args):
            pids.append(int(proc_dir.name))
    assert len(pids) == 1, f"expected one kwin-mcp child process, found {pids}"
    return pids[0]


@pytest.mark.qa("QA-MCP-026")
async def test_shutdown_gate_queued_call_not_executed(tmp_path: Path) -> None:
    log_path = tmp_path / "probe-events.jsonl"
    # Shutdown comes from SIGTERM, not stdin EOF: on EOF the SDK's dispatcher cancels
    # every pending handler task before the exit path sets _shutdown, so a queued
    # call's tool-thread submission is destroyed before it can run and the gate
    # inside it is never reached. A signal leaves the handlers (and their queued
    # submissions) pending, so each queued call starts on the tool thread only after
    # _shutdown is set — exactly the work the gate refuses.
    # The in-flight wait finishes inside the drain, then the serialized session_stop
    # and the report run: 3x that sum bounds the exit.
    exit_bound = 3 * (5.0 + EXIT_DRAIN_SECONDS + EXIT_REPORT_SECONDS)
    with live_kwin() as live:
        oracle = live.oracle()
        async with raw_mcp_client(
            {"elicitation": {"form": {}}}, era=LEGACY_PROTOCOL_VERSION
        ) as client:
            await client.call_text(
                "session_connect",
                {"dbus_address": live.dbus_address, "wayland_display": live.wayland_display},
            )
            await client.call_text(
                "app_launch", {"command": PROBE_COMMAND, "env": probe_env(log_path)}
            )
            waited = await client.call_tool(
                "element_query",
                {"locator": {"app": PROBE_APP, "query": "Keyboard Target"}, "wait": APP_WAIT},
            )
            assert waited["structuredContent"]["wait"]["satisfied"], waited
            # The window_close target is the test's own zoo: server exit leaves it running.
            zoo = await anyio.to_thread.run_sync(
                _start_zoo, live.wayland_display, live.dbus_address
            )
            try:
                windows = await anyio.to_thread.run_sync(
                    lambda: live.wait_for_windows(
                        lambda ws: any(w.resource_class == ZOO_APP_ID for w in ws)
                    )
                )
                zoo_window = next(w for w in windows if w.resource_class == ZOO_APP_ID)
                listing = await client.call_tool("window_list", {"filter": {"app_id": PROBE_APP}})
                probes = listing["structuredContent"]["windows"]
                assert len(probes) == 1, listing
                probe_window = probes[0]
                await client.call_text("window_focus", {"window": {"id": probe_window["id"]}})
                events_before = read_events(log_path)
                requests_before = len(client.server_requests)

                # One call still in flight when the signal lands, three queued behind it
                # on the tool thread. Its 1.5 s wait outlives the signal yet finishes
                # inside the exit drain, so the queued submissions start after
                # _shutdown is set and the serialized session_stop still runs.
                base = client._next_id
                in_flight = {
                    "name": "element_query",
                    "arguments": {
                        "locator": {"query": "No Such Element"},
                        "wait": {"timeout_ms": 1500},
                    },
                }
                queued = [
                    {"name": "keyboard_press", "arguments": {"keys": "z"}},
                    {"name": "window_close", "arguments": {"window": {"id": zoo_window.id}}},
                    {"name": "desktop_list", "arguments": {}},
                ]
                for offset, params in enumerate([in_flight, *queued], start=1):
                    await client._send(
                        {
                            "jsonrpc": "2.0",
                            "id": base + offset,
                            "method": "tools/call",
                            "params": params,
                        }
                    )
                    if offset == 1:
                        await anyio.sleep(0.5)
                client._next_id = base + 1 + len(queued)
                await anyio.sleep(0.3)
                started = time.monotonic()
                os.kill(_server_pid(), signal.SIGTERM)
                messages = await _read_until_eof(client, exit_bound)
                elapsed = time.monotonic() - started

                queued_ids = {base + offset for offset in range(2, 2 + len(queued))}
                for message in messages:
                    assert message.get("method") != "elicitation/create", message
                    if message.get("id") in queued_ids:
                        # The shutdown refusal, or no answer at all when the exit
                        # won the write race: either way the tool never ran.
                        if "error" in message:
                            continue
                        result = message["result"]
                        assert result.get("isError") is True, message
                        texts = [c["text"] for c in result["content"]]
                        assert len(texts) == 1, message
                        assert texts[0].endswith(SHUTDOWN_TEXT), message
                assert len(client.server_requests) == requests_before
                assert elapsed <= exit_bound, elapsed

                new_events = read_events(log_path)[len(events_before) :]
                assert not [e for e in new_events if e.get("key") in ("z", "Z")], new_events
                assert oracle.window(zoo_window.id) is not None
            finally:
                await anyio.to_thread.run_sync(zoo.stop)
