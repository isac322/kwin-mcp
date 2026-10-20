"""End-to-end coverage for the ``kwin-mcp-cli`` surface.

The CLI's commands are the 29 tools with the same parameters (object, list and
dict parameters take JSON, validated into the same models), and each command
prints the same text the MCP server returns: both call ``result.render()``. Tool
errors print ``Error: <message>`` on stderr. The gates live in the engine, so the
CLI, which has no MCP resolver layer, refuses exactly what the server refuses.

The CLI runs as a real ``kwin-mcp-cli`` process. Interactive steps go through
``_Cli``, which writes one command plus an unknown-command marker and reads stdout
up to the marker's ``Unknown command`` line, so each command's output is exactly
the lines before it. Oracles: the server's text for the same call (stdio), the
interaction probe's raw event log, KWin itself and the process table.
"""

from __future__ import annotations

import json
import os
import pty
import queue
import re
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from kwin_oracle import KWinOracle
from mcp_harness import REMOVED_TOOL_NAMES, TOOL_NAMES, running_mcp_server
from probe_session import (
    PROBE_APP,
    PROBE_COMMAND,
    Probe,
    of_kind,
    probe_env,
    spawn_probe,
    start_probe_session,
    stop_process,
)
from session_harness import live_kwin

from kwin_mcp.cli import KwinMcpShell
from kwin_mcp.core import AutomationEngine
from kwin_mcp.models import (
    ElementPoint,
    Locator,
    Point,
    Rect,
    TypedArg,
    Wait,
    WindowSelector,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mcp_harness import McpTestClient

CLI_TIMEOUT_S = 60.0
STOP_TIMEOUT_S = 60.0
SCREEN = {"screen_width": 1280, "screen_height": 800}
_REF = re.compile(r"\be\d+\.\d+\b")
_ELAPSED = re.compile(r"after \d+ ms")
_REPR = re.compile(r"\b[A-Z][A-Za-z]+\((?:[a-z_]+=)")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── CLI process helpers ─────────────────────────────────────────────────────


def _cli_path() -> str:
    path = shutil.which("kwin-mcp-cli")
    assert path is not None, "kwin-mcp-cli is not installed on PATH"
    return path


def _cli_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    return {**os.environ, "PYTHONUNBUFFERED": "1", **(extra or {})}


def _unknown(name: str) -> str:
    return f"Unknown command: {name}. Type 'help' for available commands."


def _line(tool: str, args: dict[str, Any] | None = None) -> str:
    """A CLI command taking every argument as one JSON object."""
    return f"{tool} {json.dumps(args)}" if args else tool


def _error_blocks(stderr: str) -> list[str]:
    """Split stderr into one block per ``Error:`` line plus its continuation lines."""
    blocks: list[str] = []
    for line in stderr.splitlines():
        if line.startswith("Error: "):
            blocks.append(line)
        else:
            assert blocks, f"stderr does not start with an Error: line: {stderr!r}"
            blocks[-1] += "\n" + line
    return blocks


def _pipe(stdin: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run ``kwin-mcp-cli`` in pipe mode on ``stdin`` to completion."""
    return subprocess.run(
        [_cli_path(), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env if env is not None else _cli_env(),
        timeout=CLI_TIMEOUT_S * 3,
        check=False,
    )


class _Cli:
    """One interactive pipe-mode ``kwin-mcp-cli`` process."""

    def __init__(self, *args: str) -> None:
        self._stderr = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed in close()
            mode="w+", encoding="utf-8", prefix="kwin-mcp-cli-stderr-"
        )
        self.proc = subprocess.Popen(
            [_cli_path(), *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            env=_cli_env(),
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self._marker = 0
        self._stderr_pos = 0

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line.rstrip("\n"))
        self._lines.put(None)

    def next_line(self, timeout_s: float = CLI_TIMEOUT_S) -> str | None:
        try:
            return self._lines.get(timeout=timeout_s)
        except queue.Empty:
            pytest.fail(f"kwin-mcp-cli printed nothing for {timeout_s} s: {self.stderr_tail()}")

    def stderr_tail(self) -> str:
        self._stderr.flush()
        return Path(self._stderr.name).read_text(encoding="utf-8")[self._stderr_pos :]

    def run(self, command: str, timeout_s: float = CLI_TIMEOUT_S) -> tuple[str, str]:
        """Run one command; return (its stdout, its stderr)."""
        assert self.proc.stdin is not None
        self._marker += 1
        marker = f"zz_marker_{self._marker}"
        self.proc.stdin.write(f"{command}\n{marker}\n")
        self.proc.stdin.flush()
        out: list[str] = []
        while True:
            line = self.next_line(timeout_s)
            assert line is not None, f"kwin-mcp-cli exited during {command!r}: {out}"
            if line == _unknown(marker):
                break
            out.append(line)
        err = self.stderr_tail()
        self._stderr_pos += len(err)
        return "\n".join(out), err

    def ok(self, command: str, timeout_s: float = CLI_TIMEOUT_S) -> str:
        out, err = self.run(command, timeout_s)
        assert err == "", (command, err)
        assert not _REPR.search(out), (command, out)
        return out

    def close(self, timeout_s: float = STOP_TIMEOUT_S) -> tuple[str, str, int]:
        """Send EOF and wait; return (remaining stdout, remaining stderr, exit code)."""
        assert self.proc.stdin is not None
        if not self.proc.stdin.closed:
            self.proc.stdin.close()
        code = self.proc.wait(timeout=timeout_s)
        self._reader.join(timeout=5)
        rest: list[str] = []
        while True:
            try:
                line = self._lines.get_nowait()
            except queue.Empty:
                break
            if line is not None:
                rest.append(line)
        err = self.stderr_tail()
        self._stderr.close()
        return "\n".join(rest), err, code

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if not self._stderr.closed:
            self._stderr.close()


@pytest.fixture
def cli() -> Iterator[list[_Cli]]:
    """Owns every ``_Cli`` a test opens and kills any still running at the end."""
    opened: list[_Cli] = []
    yield opened
    for process in opened:
        process.kill()


def _open(opened: list[_Cli], *args: str) -> _Cli:
    process = _Cli(*args)
    opened.append(process)
    return process


def _connect(process: _Cli, dbus_address: str, wayland_display: str) -> None:
    out = process.ok(
        f"session_connect dbus_address={dbus_address} wayland_display={wayland_display}"
    )
    assert out.startswith("Connected to live KWin session."), out


def _normalized(text: str) -> str:
    return _ELAPSED.sub("after <n> ms", _REF.sub("e<ref>", text))


def _proc_state(pid: int) -> str | None:
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None
    return stat.rpartition(")")[2].split()[0]


def _is_live(pid: int) -> bool:
    """Running and not a zombie (the container's PID 1 does not reap orphans)."""
    state = _proc_state(pid)
    return state is not None and state not in ("Z", "X")


def _live_kwin_pids() -> set[int]:
    pids: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text().strip() != "kwin_wayland":
                continue
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if _is_live(int(entry.name)):
            pids.add(int(entry.name))
    return pids


def _wait_dead(pids: set[int], timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while any(_is_live(pid) for pid in pids):
        assert time.monotonic() < deadline, {pid: _proc_state(pid) for pid in pids}
        time.sleep(0.1)


# ── QA-CLI-001: the command set ─────────────────────────────────────────────


def _help_listing(stdout: str) -> dict[str, str]:
    lines = stdout.splitlines()
    assert lines[:2] == ["Available commands:", ""], stdout[:500]
    listing: dict[str, str] = {}
    for line in lines[2:]:
        if not line:
            break
        match = re.fullmatch(r"  (\S+) +(.*)", line)
        assert match is not None, line
        listing[match.group(1)] = match.group(2)
    return listing


@pytest.mark.qa("QA-CLI-001")
def test_cli_has_29_commands_and_rejects_old_names() -> None:
    # No KWin on PATH: listing the commands must not need a session or KWin.
    env = _cli_env({"PATH": str(Path(_cli_path()).parent)})
    assert shutil.which("kwin_wayland", path=env["PATH"]) is None
    old_names = [*REMOVED_TOOL_NAMES, "screenshot_data", "accessibility_tree_data"]
    result = _pipe("help\n" + "".join(f"{name}\n" for name in old_names) + "quit\n", env=env)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "kwin-mcp>" not in result.stdout
    listing = _help_listing(result.stdout)
    assert set(listing) == set(TOOL_NAMES), set(listing) ^ set(TOOL_NAMES)
    assert len(listing) == 29
    assert not [name for name in listing if name.endswith("_data")]
    for name, doc in listing.items():
        method = getattr(AutomationEngine, name)
        assert doc == (method.__doc__ or "").split("\n")[0], (name, doc)
    tail = result.stdout.split("\n\n")[2].splitlines()[:2]
    assert [line.split()[0] for line in tail] == ["help", "quit"], tail
    assert result.stdout.split("\n\n")[2].splitlines()[2:] == [_unknown(n) for n in old_names]

    shell_commands = {
        name.removeprefix("do_") for name in dir(KwinMcpShell) if name.startswith("do_")
    }
    assert shell_commands == {"help", "quit", "exit", "EOF"}, shell_commands


# ── QA-GATE-007: the engine re-checks every gate ────────────────────────────

_POINT = {"x": 10, "y": 10}
_GATED_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("app_launch", {"command": "true"}),
    ("app_log", {"pid": 1}),
    ("screen_capture", {}),
    ("element_query", {}),
    ("element_act", {"element": {"ref": "e1.1"}, "action": "Press"}),
    ("window_list", {}),
    ("desktop_list", {}),
    ("window_focus", {"window": {"id": "x"}}),
    ("window_place", {"window": {"id": "x"}, "frame": {"x": 0}}),
    ("window_close", {"window": {"id": "x"}}),
    ("pointer_click", {"target": _POINT}),
    ("pointer_move", {"target": _POINT}),
    ("pointer_scroll", {"target": _POINT, "delta": 1}),
    ("pointer_drag", {"from": _POINT, "to": {"x": 20, "y": 20}}),
    ("pointer_hold", {"phase": "press", "target": _POINT}),
    ("keyboard_press", {"keys": "a"}),
    ("keyboard_type", {"text": "a"}),
    ("keyboard_hold", {"phase": "press", "keys": "a"}),
    ("touch_tap", {"target": _POINT}),
    ("touch_swipe", {"from": _POINT, "to": {"x": 20, "y": 20}}),
    ("touch_pinch", {"center": _POINT, "start_distance": 10, "end_distance": 20}),
    ("clipboard_read", {}),
    ("clipboard_write", {"text": "x"}),
    (
        "dbus_call",
        {
            "service": "org.kde.KWin",
            "path": "/KWin",
            "interface": "org.kde.KWin",
            "method": "currentDesktop",
        },
    ),
    ("wayland_list", {}),
]


async def _server_errors(
    client: McpTestClient, calls: list[tuple[str, dict[str, Any]]]
) -> list[str]:
    """The server's isError texts as the CLI prints them: the SDK's ``Error executing
    tool <name>: `` prefix (kept on MCP, QA-MCP-016) becomes the CLI's ``Error: ``."""
    errors: list[str] = []
    for name, args in calls:
        text = "\n".join(await client.call_tool_error(name, args, lines=None))
        prefix = f"Error executing tool {name}: "
        assert text.startswith(prefix), (name, text)
        errors.append("Error: " + text.removeprefix(prefix))
    return errors


@pytest.mark.anyio
@pytest.mark.qa("QA-GATE-007")
async def test_engine_rechecks_every_gate_without_resolver() -> None:
    assert {name for name, _ in _GATED_CALLS} == set(TOOL_NAMES) - {
        "session_start",
        "session_connect",
        "session_stop",
        "session_info",
    }

    # No session: every gated tool, including the input tools (session gate first).
    no_session = _pipe("".join(_line(name, args) + "\n" for name, args in _GATED_CALLS))
    assert no_session.returncode == 0, no_session.stderr
    assert no_session.stdout == "", no_session.stdout
    assert "Traceback" not in no_session.stderr, no_session.stderr
    cli_blocks = _error_blocks(no_session.stderr)

    # A running session: the double start and the disabled-clipboard gates.
    session_calls: list[tuple[str, dict[str, Any]]] = [
        ("session_start", SCREEN),
        ("clipboard_read", {}),
        ("clipboard_write", {"text": "x"}),
    ]
    with_session = _pipe(
        _line("session_start", SCREEN)
        + "\n"
        + "".join(_line(name, args) + "\n" for name, args in session_calls)
    )
    assert "Traceback" not in with_session.stderr, with_session.stderr
    assert with_session.stdout.startswith("Session started."), with_session.stdout[:500]
    session_blocks = _error_blocks(with_session.stderr)

    async with running_mcp_server() as client:
        server_no_session = await _server_errors(client, _GATED_CALLS)
        await client.call_structured("session_start", SCREEN)
        try:
            server_session = await _server_errors(client, session_calls)
        finally:
            await client.call_structured("session_stop")

    for (name, _), cli_text, server_text in zip(
        _GATED_CALLS, cli_blocks, server_no_session, strict=True
    ):
        assert cli_text == server_text, (name, cli_text, server_text)
        assert cli_text.splitlines() == [
            "Error: No session running.",
            "Call session_start (virtual) or session_connect (live desktop).",
        ], name
    assert session_blocks == server_session, (session_blocks, server_session)
    assert session_blocks[0] == "Error: Session already running.\nCall session_stop first."
    assert all(block.startswith("Error: Clipboard not enabled.\n") for block in session_blocks[1:])


# ── QA-CLI-002: JSON model parameters ───────────────────────────────────────


def _settled_press(probe: Probe, mark: int) -> tuple[int, int]:
    events = probe.wait_events(lambda ev: bool(of_kind(ev, "button_release")), since=mark)
    press = of_kind(events, "button_press")[0]
    return press["x"], press["y"]


@pytest.mark.qa("QA-CLI-002")
def test_cli_json_model_params(engine: AutomationEngine, tmp_path: Path, cli: list[_Cli]) -> None:
    probe = start_probe_session(engine, tmp_path)
    info = engine.session_info().session
    assert info is not None
    process = _open(cli)
    _connect(process, info.dbus_address, info.wayland_socket)

    # Point target: the click lands where the engine's own click lands.
    center = probe.center("Click Target")
    mark = probe.park()
    out = process.ok(f"pointer_click target='{json.dumps({'x': center.x, 'y': center.y})}'")
    assert _settled_press(probe, mark) == probe.to_local(center.x, center.y)
    mark = probe.park()
    assert out == engine.pointer_click(Point(x=center.x, y=center.y)).render()
    assert _settled_press(probe, mark) == probe.to_local(center.x, center.y)

    # ElementPoint target: a ref the CLI process itself minted.
    locator = {"app": PROBE_APP, "query": "Click Target"}
    listed = process.ok(f"element_query locator='{json.dumps(locator)}'")
    engine_listed = engine.element_query(Locator.model_validate(locator))
    assert _normalized(listed) == _normalized(engine_listed.render())
    ref_lines = [line for line in listed.splitlines() if '"Click Target"' in line]
    assert len(ref_lines) == 1, listed
    ref_match = re.search(r"ref=(e\d+\.\d+)", ref_lines[0])
    assert ref_match is not None, ref_lines
    mark = probe.park()
    target = {"ref": ref_match.group(1), "dx": 0, "dy": 0}
    out = process.ok(f"pointer_click target='{json.dumps(target)}'")
    assert _settled_press(probe, mark) == probe.to_local(center.x, center.y)
    engine_ref = next(e.ref for e in engine_listed.elements if e.name == "Click Target")
    mark = probe.park()
    engine_out = engine.pointer_click(ElementPoint(ref=engine_ref, dx=0, dy=0)).render()
    assert _normalized(out) == _normalized(engine_out)
    _settled_press(probe, mark)

    # element_act with a strict locator, window_list with a filter.
    strict = {"strict": {"app": PROBE_APP, "query": "Click Target", "role": "button"}}
    out = process.ok(f"element_act element='{json.dumps(strict)}' action=click")
    assert out.splitlines()[0] == (
        f'Invoked AT-SPI action: click on [button] "Click Target" in {PROBE_APP}'
    ), out
    selector = {"kind": ["normal"]}
    out = process.ok(f"window_list filter='{json.dumps(selector)}'")
    assert out == engine.window_list(WindowSelector.model_validate(selector)).render()

    # element_query with locator and wait.
    wait = {"timeout_ms": 5000}
    out = process.ok(
        f"element_query locator='{json.dumps({'app': PROBE_APP, 'query': 'raw_events:'})}' "
        f"wait='{json.dumps(wait)}'"
    )
    engine_out = engine.element_query(
        Locator(app=PROBE_APP, query="raw_events:"), wait=Wait.model_validate(wait)
    ).render()
    assert _normalized(out) == _normalized(engine_out)
    assert out.startswith("Found"), out

    # dbus_call args: a list mixing a dbus-send string and a typed argument.
    args = ["string:org.kde.KWin"]
    out = process.ok(
        "dbus_call service=org.freedesktop.DBus path=/org/freedesktop/DBus "
        f"interface=org.freedesktop.DBus method=GetNameOwner args='{json.dumps(args)}'"
    )
    engine_reply = engine.dbus_call(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "GetNameOwner",
        [TypedArg(type="string", value="org.kde.KWin")],
    )
    assert out == engine_reply.render()

    # app_launch env: a dict parameter reaching the process.
    out = process.ok("""app_launch command="sh -c 'printf %s $A'" env='{"A":"1"}'""")
    pid_match = re.search(r"\(PID=(\d+)\)", out)
    assert pid_match is not None, out
    pid = int(pid_match.group(1))
    deadline = time.monotonic() + 10
    log = ""
    while time.monotonic() < deadline:
        log = process.ok(f"app_log pid={pid} last_n_lines=0")
        if log.endswith("\n1"):
            break
        time.sleep(0.1)
    assert log.splitlines()[1:] == ["1"], log

    # screen_capture region: the same outcome (frame or error) as the engine.
    region = {"x": 0, "y": 0, "width": 10, "height": 10}
    cli_out, cli_err = process.run(f"screen_capture region='{json.dumps(region)}'", timeout_s=120)
    try:
        frame = engine.screen_capture(region=Rect.model_validate(region))
    except Exception as exc:
        assert cli_out == ""
        assert cli_err == f"Error: {exc}\n", (cli_err, exc)
    else:
        assert cli_err == ""
        assert re.sub(r"\S+\.png", "<png>", cli_out) == re.sub(r"\S+\.png", "<png>", frame.render())

    # window_place window/frame: KWin reports the frame the CLI asked for.
    window = {"id": probe.window_id}
    frame_arg = {"x": 100, "y": 80}
    out = process.ok(f"window_place window='{json.dumps(window)}' frame='{json.dumps(frame_arg)}'")
    placed = probe.oracle().window(probe.window_id)
    assert placed is not None and placed.frame is not None, placed
    assert placed.frame.as_ints()[:2] == (100, 80), (placed, out)

    rest, err, code = process.close()
    assert code == 0, err
    assert "Disconnected from live session." in rest, rest


# ── QA-CLI-003: keyword-named parameters ────────────────────────────────────


def _drag_effect(probe: Probe, mark: int) -> list[tuple[str, int, int]]:
    events = probe.wait_events(lambda ev: bool(of_kind(ev, "button_release")), since=mark)
    return [(e["kind"], e["x"], e["y"]) for e in of_kind(events, "button_press", "button_release")]


def _swipe_effect(probe: Probe, mark: int) -> list[tuple[str, int, int]]:
    events = probe.wait_events(lambda ev: bool(of_kind(ev, "touch_end")), since=mark)
    touches = of_kind(events, "touch_begin", "touch_update", "touch_end")
    begin = of_kind(touches, "touch_begin")
    updates = of_kind(touches, "touch_update")
    assert len(begin) == 1 and updates, touches
    return [
        ("touch_begin", begin[0]["x"], begin[0]["y"]),
        ("touch_update", updates[-1]["x"], updates[-1]["y"]),
    ]


@pytest.mark.qa("QA-CLI-003")
def test_cli_keyword_params_from_to(
    engine: AutomationEngine, tmp_path: Path, cli: list[_Cli]
) -> None:
    probe = start_probe_session(engine, tmp_path)
    info = engine.session_info().session
    assert info is not None
    process = _open(cli)
    _connect(process, info.dbus_address, info.wayland_socket)

    drag = probe.element("Drag Target").rect
    assert drag is not None
    start = Point(x=drag.x + drag.width // 4, y=drag.y + drag.height // 2)
    end = Point(x=drag.x + 3 * drag.width // 4, y=drag.y + drag.height // 2)
    from_arg = json.dumps({"x": start.x, "y": start.y})
    to_arg = json.dumps({"x": end.x, "y": end.y})

    mark = probe.park()
    # Populating the status text must not move or resize the real KWin window.
    before_drag = probe.oracle().window(probe.window_id)
    assert before_drag is not None and before_drag.client is not None
    assert before_drag.frame is not None
    process.ok(f"pointer_drag from='{from_arg}' to='{to_arg}'")
    cli_drag = _drag_effect(probe, mark)
    after_cli_drag = probe.oracle().window(probe.window_id)
    assert after_cli_drag is not None
    assert (after_cli_drag.client, after_cli_drag.frame) == (
        before_drag.client,
        before_drag.frame,
    ), (before_drag, after_cli_drag)
    mark = probe.park()
    engine.pointer_drag(start, end)
    assert cli_drag == _drag_effect(probe, mark)
    after_engine_drag = probe.oracle().window(probe.window_id)
    assert after_engine_drag is not None
    assert (after_engine_drag.client, after_engine_drag.frame) == (
        before_drag.client,
        before_drag.frame,
    ), (before_drag, after_engine_drag)
    assert cli_drag == [
        ("button_press", *probe.to_local(start.x, start.y)),
        ("button_release", *probe.to_local(end.x, end.y)),
    ]

    mark = probe.park()
    process.ok(f"touch_swipe from='{from_arg}' to='{to_arg}'")
    cli_swipe = _swipe_effect(probe, mark)
    mark = probe.park()
    engine.touch_swipe(start, end)
    assert cli_swipe == _swipe_effect(probe, mark)
    assert cli_swipe[0] == ("touch_begin", *probe.to_local(start.x, start.y))

    help_text = process.ok("help pointer_drag")
    names = [line.strip().split(":")[0] for line in help_text.splitlines() if line.startswith("  ")]
    assert "from" in names, help_text
    assert "from_" not in help_text, help_text
    process.close()


# ── QA-CLI-004: CLI text == server text ─────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.qa("QA-CLI-004")
async def test_cli_prints_render_identical_to_server_text(tmp_path: Path, cli: list[_Cli]) -> None:
    async with running_mcp_server() as client:
        started, _ = await client.call_structured("session_start", SCREEN)
        try:
            await client.call_structured(
                "app_launch",
                {"command": PROBE_COMMAND, "env": probe_env(tmp_path / "events.jsonl")},
            )
            waited, _ = await client.call_structured(
                "element_query",
                {
                    "locator": {"app": PROBE_APP, "query": "raw_events:"},
                    "wait": {"timeout_ms": 15000},
                },
            )
            assert waited["wait"]["satisfied"], waited

            process = _open(cli)
            _connect(process, started["dbus_address"], started["wayland_socket"])
            calls: list[tuple[str, dict[str, Any]]] = [
                ("desktop_list", {}),
                ("window_list", {}),
                ("wayland_list", {"filter": "wl_seat"}),
                ("element_query", {"locator": {"app": PROBE_APP}}),
            ]
            cli_texts = [process.ok(_line(name, args)) for name, args in calls]
            cli_info = process.ok("session_info")
            for (name, args), cli_text in zip(calls, cli_texts, strict=True):
                _, server_text = await client.call_structured(name, args)
                assert _normalized(cli_text) == _normalized(server_text), (name, cli_text)
                assert cli_text.strip(), name
                assert not _REPR.search(cli_text), (name, cli_text)
            # The CLI's own session is the live connection to the server's KWin.
            assert cli_info.splitlines()[:2] == [
                f"Session: live. Wayland socket: {started['wayland_socket']}",
                f"D-Bus: {started['dbus_address']}",
            ], cli_info
            assert "Apps: none" in cli_info.splitlines(), cli_info
            _, _, code = process.close()
            assert code == 0
        finally:
            await client.call_structured("session_stop")


# ── QA-CLI-005: errors, pipe mode and exit cleanup ──────────────────────────


@pytest.mark.qa("QA-CLI-005")
def test_cli_errors_pipe_mode_and_cleanup(cli: list[_Cli]) -> None:
    bad_lines = [
        "pointer_click target='{bad json'",
        """pointer_click target='{"x":"a","y":1}'""",
        "screen_capture max_edge=abc",
        "pointer_click nosuchparam=1",
        "pointer_click target",
        "keyboard_type text='unterminated",
    ]
    result = _pipe("\n".join([*bad_lines, "nosuchcommand", "session_info"]) + "\n")

    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    blocks = _error_blocks(result.stderr)
    assert len(blocks) == len(bad_lines), blocks
    for line, block in zip(bad_lines, blocks, strict=True):
        assert block.startswith("Error: "), (line, block)
    # The pipe continued past every error: the unknown command and session_info ran.
    assert result.stdout == f"{_unknown('nosuchcommand')}\nNo session running.\n", result.stdout
    assert "kwin-mcp>" not in result.stdout

    # EOF runs session_stop and the orphan sweep, even with signals during teardown.
    kwin_before = _live_kwin_pids()
    process = _open(cli)
    started = process.ok(_line("session_start", SCREEN), timeout_s=120)
    assert started.startswith("Session started."), started
    session_kwin = _live_kwin_pids() - kwin_before
    assert session_kwin
    launched = process.ok("app_launch command='sleep 300'")
    pid_match = re.search(r"\(PID=(\d+)\)", launched)
    assert pid_match is not None, launched
    app_pid = int(pid_match.group(1))
    assert _is_live(app_pid)

    assert process.proc.stdin is not None
    process.proc.stdin.close()
    line = process.next_line()
    assert line == "Stopping session...", line
    process.proc.send_signal(signal.SIGINT)
    process.proc.send_signal(signal.SIGTERM)
    rest, err, _code = process.close()
    assert "Session stopped." in rest.splitlines(), (rest, err)
    assert "Traceback" not in err, err
    _wait_dead({app_pid, *session_kwin})


# ── QA-CLI-007: --default-live-session changes only the intro ───────────────


def _tty(args: list[str], commands: str, timeout_s: float = 180.0) -> str:
    """Run ``kwin-mcp-cli`` on a pseudo-terminal (interactive mode); return its output."""
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [_cli_path(), *args],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=_cli_env(),
        close_fds=True,
    )
    os.close(slave)
    os.write(master, commands.encode())
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            assert time.monotonic() < deadline, b"".join(chunks).decode(errors="replace")
            readable, _, _ = select.select([master], [], [], 0.5)
            if readable:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
            elif process.poll() is not None:
                break
        process.wait(timeout=30)
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    return b"".join(chunks).decode(errors="replace").replace("\r\n", "\n")


@pytest.mark.slow
@pytest.mark.timeout(600)
@pytest.mark.qa("QA-CLI-007")
def test_cli_default_live_session_intro_only() -> None:
    live = _tty(
        ["--default-live-session"],
        f"{_line('session_start', SCREEN)}\nsession_info\nquit\n",
    )
    assert "kwin-mcp CLI (live session mode). Type 'help' for commands, 'quit' to exit." in live
    assert "kwin-mcp[live]> " in live, live
    assert "Session started. Wayland socket:" in live, live
    assert "Session: virtual. Wayland socket:" in live, live
    assert "Session stopped." in live, live

    plain = _tty([], "quit\n")
    assert "kwin-mcp CLI. Type 'help' for commands, 'quit' to exit." in plain, plain
    assert "kwin-mcp> " in plain, plain
    assert "live session mode" not in plain

    # Only the intro and prompt change: the commands and their help are identical.
    with_flag = _pipe("help\nhelp session_start\n", "--default-live-session")
    without_flag = _pipe("help\nhelp session_start\n")
    assert with_flag.stdout == without_flag.stdout
    assert with_flag.stderr == without_flag.stderr == ""


# ── QA-CLI-008: a CLI live window_close is refused ──────────────────────────


@pytest.mark.qa("QA-CLI-008")
def test_cli_live_window_close_refusal(
    engine: AutomationEngine, tmp_path: Path, cli: list[_Cli]
) -> None:
    with live_kwin() as live:
        probe = spawn_probe(
            dbus_address=live.dbus_address,
            wayland_display=live.wayland_display,
            log_path=tmp_path / "events.jsonl",
        )
        try:
            windows = live.wait_for_windows(
                lambda ws: any(w.pid == probe.pid for w in ws), timeout_s=30.0
            )
            window_id = next(w.id for w in windows if w.pid == probe.pid)

            process = _open(cli)
            _connect(process, live.dbus_address, live.wayland_display)
            out = process.ok(f"window_close window='{json.dumps({'id': window_id})}'")
            _, _, code = process.close()
            assert code == 0

            engine.session_connect(
                dbus_address=live.dbus_address, wayland_display=live.wayland_display
            )
            refused = engine.window_close(WindowSelector(id=window_id))
            assert refused.requested is False, refused
            assert refused.consent == "unsupported", refused
            assert out == refused.render()

            time.sleep(1.0)
            assert KWinOracle(live.dbus_address).window(window_id) is not None
            assert probe.poll() is None
        finally:
            engine.session_stop()
            stop_process(probe)
