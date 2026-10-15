"""Held input end to end: the Q10 (a) rule table, held reporting, cleanup on exit/kill.

Engine-level cases drive a virtual session with the interaction probe; the exit and
kill cases drive the installed ``kwin-mcp`` over stdio against a test-owned live KWin
(``live_kwin``) whose probe the test starts itself, so ``session_stop`` and server
exit never kill the oracle. Oracle: the probe's raw event log and status labels.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import tempfile
import time
from typing import IO, TYPE_CHECKING, Any

import anyio
import pytest
from _suite_policy import require_or_skip
from mcp_harness import running_mcp_server
from probe_session import (
    BUTTON_KINDS,
    HOLD_SETTLE_S,
    KEY_KINDS,
    POLL_S,
    STATE_TIMEOUT_S,
    Probe,
    field,
    int_field,
    of_kind,
    probe_env,
    read_events,
    spawn_probe,
    start_probe_session,
    stop_process,
)
from session_harness import live_kwin

from kwin_mcp.input import HeldControlError
from kwin_mcp.models import (
    ElementPoint,
    HeldInput,
    Locator,
    Point,
    RefAddr,
    Wait,
    WindowSelector,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from session_harness import LiveKWin

    from kwin_mcp.core import AutomationEngine

LEFT_AND_SHIFT = HeldInput(buttons=["left"], keys=["shift"])
SERVER_EXIT_TIMEOUT_S = 30.0
RPC_TIMEOUT_S = 60.0
LEAK_LIMIT = "KWin limit: emulated state survives client disconnect"
CRASH_LIMIT = "KWin limit: KWin crashes when a killed EIS client held input"


def _left_refusal(tool: str) -> str:
    return (
        f"{tool} refused: left is held by pointer_hold; release it first.\n"
        "Release with pointer_hold(phase='release', button='left', target={x, y}), then retry."
    )


def _key_refusal(tool: str, key: str) -> str:
    return (
        f"{tool} refused: {key} is held by keyboard_hold; release it first.\n"
        f"Release with keyboard_hold(phase='release', keys='{key}'), then retry."
    )


@pytest.fixture
def probe(engine: AutomationEngine, tmp_path: Path) -> Probe:
    return start_probe_session(engine, tmp_path)


def _raw_count(probe: Probe, name: str) -> int:
    return int_field(probe.status("raw_events:"), name)


def _key_events(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(e["kind"], e["key"]) for e in of_kind(events, *KEY_KINDS)]


def _wait_entry(probe: Probe, expected: str) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_S
    while probe.entry_text() != expected:
        assert time.monotonic() < deadline, probe.entry_text()
        time.sleep(POLL_S)


# ── engine: held reporting and the Q10 (a) rule table ─────────────────────


@pytest.mark.qa("QA-INP-033")
def test_every_action_result_reports_held_and_still_held_text(
    engine: AutomationEngine, probe: Probe
) -> None:
    a = probe.center("Drag Target")
    b = probe.center("Click Target")
    zoom = probe.center("Zoom Target")
    probe.park()
    engine.pointer_hold("press", a)
    assert engine.keyboard_hold("press", "shift").held == LEFT_AND_SHIFT

    calls: dict[str, Callable[[], Any]] = {
        "pointer_click": lambda: engine.pointer_click(b, button="right"),
        "pointer_scroll": lambda: engine.pointer_scroll(b, delta=1),
        "pointer_drag": lambda: engine.pointer_drag(b, Point(x=b.x + 40, y=b.y), button="right"),
        "pointer_hold": lambda: engine.pointer_hold("move", Point(x=a.x + 20, y=a.y)),
        "keyboard_press": lambda: engine.keyboard_press("a"),
        "keyboard_type": lambda: engine.keyboard_type("a"),
        "keyboard_hold": lambda: engine.keyboard_hold("press", "shift"),
        "touch_tap": lambda: engine.touch_tap(zoom),
        "touch_swipe": lambda: engine.touch_swipe(zoom, Point(x=zoom.x + 40, y=zoom.y)),
        "touch_pinch": lambda: engine.touch_pinch(zoom, 100, 60),
        "element_act": lambda: engine.element_act(
            RefAddr(ref=probe.element("Click Target").ref), "click"
        ),
    }
    for tool, call in calls.items():
        result = call()
        assert result.tool == tool
        assert result.held == LEFT_AND_SHIFT, (tool, result)
        assert result.render().endswith("\nStill held: left, shift"), (tool, result.render())
        # The app agrees: left still down, shift still in the modifier state.
        probe.wait_status("hold_status:", lambda s: field(s, "held") == "left")
        probe.wait_status("raw_events:", lambda s: "shift" in field(s, "held_mods"))

    info = engine.session_info()
    assert info.held == LEFT_AND_SHIFT
    assert info.render().endswith("\nStill held: left, shift"), info.render()

    released = engine.pointer_hold("release", Point(x=a.x + 20, y=a.y))
    assert released.held == HeldInput(keys=["shift"])
    assert released.render().endswith("\nStill held: shift"), released.render()
    released = engine.keyboard_hold("release", "shift")
    assert released.held == HeldInput() and released.changed
    assert "Still held" not in released.render(), released.render()

    # Window tools neither press nor release input, so they carry no held field.
    focused = engine.window_focus(WindowSelector(id=probe.window_id))
    assert "held" not in type(focused).model_fields
    assert "Still held" not in focused.render()
    assert engine.session_info().held == HeldInput()
    probe.wait_status("hold_status:", lambda s: field(s, "held") == "none")
    probe.wait_status("raw_events:", lambda s: field(s, "held_mods") == "none")


@pytest.mark.qa("QA-INP-034")
def test_hold_survives_failing_calls(engine: AutomationEngine, probe: Probe) -> None:
    a = probe.center("Drag Target")
    probe.park()
    engine.pointer_hold("press", a)
    probe.wait_status("hold_status:", lambda s: field(s, "held") == "left")

    # A query that finds nothing ends as data, an unknown ref as an error.
    nothing = engine.element_query(Locator(app="nope"), wait=Wait(timeout_ms=0))
    assert nothing.wait is not None and not nothing.wait.satisfied
    assert nothing.elements == []
    with pytest.raises((ValueError, RuntimeError)):
        engine.pointer_click(ElementPoint(ref="e999.999"))

    # Neither silently released the hold.
    assert engine.session_info().held == HeldInput(buttons=["left"])
    assert _raw_count(probe, "br") == 0
    assert field(probe.status("hold_status:"), "held") == "left"
    assert engine.pointer_hold("release", a).held == HeldInput()


@pytest.mark.qa("QA-INP-035")
def test_gesture_touching_held_button_refused_with_named_hold(
    engine: AutomationEngine, probe: Probe
) -> None:
    a = probe.center("Drag Target")
    b = probe.center("Click Target")
    probe.park()
    engine.pointer_hold("press", a)
    probe.wait_status("hold_status:", lambda s: field(s, "held") == "left")
    mark = probe.mark()
    bp, br = _raw_count(probe, "bp"), _raw_count(probe, "br")

    with pytest.raises(HeldControlError) as click:
        engine.pointer_click(b, button="left")
    assert str(click.value) == _left_refusal("pointer_click")
    with pytest.raises(HeldControlError) as drag:
        engine.pointer_drag(a, b, button="left")
    assert str(drag.value) == _left_refusal("pointer_drag")

    time.sleep(0.5)
    assert probe.events_since(mark) == []
    assert (_raw_count(probe, "bp"), _raw_count(probe, "br")) == (bp, br)
    assert engine.pointer_hold("release", a).held == HeldInput()


@pytest.mark.qa("QA-INP-036")
def test_gesture_modifiers_touching_held_key_refused(
    engine: AutomationEngine, probe: Probe
) -> None:
    b = probe.center("Click Target")
    probe.park()
    engine.keyboard_hold("press", "ctrl")
    probe.wait_status("raw_events:", lambda s: field(s, "held_mods") == "ctrl")
    mark = probe.mark()

    refusals: dict[str, Callable[[], object]] = {
        "pointer_click": lambda: engine.pointer_click(b, modifiers="ctrl"),
        "keyboard_press": lambda: engine.keyboard_press("ctrl+c"),
        "pointer_drag": lambda: engine.pointer_drag(
            b, Point(x=b.x + 40, y=b.y), modifiers="ctrl+shift"
        ),
    }
    for tool, call in refusals.items():
        with pytest.raises(HeldControlError) as refused:
            call()
        assert str(refused.value) == _key_refusal(tool, "ctrl")
    time.sleep(0.5)
    assert probe.events_since(mark) == []

    # Keys that are not held compose with the held ctrl, as in 0.11 (ctrl+x).
    pressed = engine.keyboard_press("x")
    assert pressed.held == HeldInput(keys=["ctrl"])
    events = probe.wait_events(lambda evs: bool(of_kind(evs, "key_release")), since=mark)
    assert _key_events(events) == [("key_press", "x"), ("key_release", "x")], events
    assert of_kind(events, "key_press")[0]["mods"] == ["ctrl"]
    assert field(probe.status("raw_events:"), "held_mods") == "ctrl"
    assert engine.keyboard_hold("release", "ctrl").held == HeldInput()


@pytest.mark.qa("QA-INP-037")
def test_gesture_on_other_control_works_while_held(engine: AutomationEngine, probe: Probe) -> None:
    a = probe.center("Drag Target")
    b = probe.center("Click Target")
    mark = probe.park()
    engine.pointer_hold("press", a)
    time.sleep(HOLD_SETTLE_S)

    clicked = engine.pointer_click(b, button="right")
    assert clicked.warnings == ["moved while left was held"], clicked
    assert clicked.held == HeldInput(buttons=["left"])
    time.sleep(HOLD_SETTLE_S)
    scrolled = engine.pointer_scroll(b, delta=1)
    assert scrolled.warnings == ["moved while left was held"], scrolled
    # GTK discards the scroll under the Drag Target's implicit grab (that window does
    # not select scroll events), so the probe oracle covers the click and the motion.
    events = probe.wait_events(
        lambda evs: any(e["kind"] == "button_release" and e["button"] == "right" for e in evs),
        since=mark,
    )
    # The approach to B was a drag segment of the held left button, and the right
    # click happened with left still down.
    assert any(
        e["kind"] == "motion" and e["held"] == ["left"] and probe.to_global(e["x"], e["y"]) == b
        for e in events
    ), events
    right = [e for e in of_kind(events, *BUTTON_KINDS) if e["button"] == "right"]
    assert [(e["kind"], e["held"]) for e in right] == [
        ("button_press", ["left"]),
        ("button_release", ["left", "right"]),
    ], right
    assert not [e for e in of_kind(events, "button_release") if e["button"] == "left"], events

    # shift held: 'a' arrives as 'A'.
    engine.keyboard_hold("press", "shift")
    engine.keyboard_press("a")
    _wait_entry(probe, "A")
    # ctrl held too: typing 'c' is the copy shortcut, not text.
    engine.keyboard_hold("press", "ctrl")
    typed = engine.keyboard_type("c")
    assert typed.held == HeldInput(buttons=["left"], keys=["shift", "ctrl"])
    time.sleep(0.5)
    assert probe.entry_text() == "A"
    assert field(probe.status("hold_status:"), "held") == "left"

    engine.keyboard_hold("release", "ctrl")
    engine.keyboard_hold("release", "shift")
    assert engine.pointer_hold("release", a).held == HeldInput()


@pytest.mark.qa("QA-INP-038")
def test_typing_shift_while_shift_held_is_refused(engine: AutomationEngine, probe: Probe) -> None:
    engine.keyboard_hold("press", "shift")
    probe.wait_status("raw_events:", lambda s: field(s, "held_mods") == "shift")
    mark = probe.mark()
    for text in ("aA!", "A"):
        with pytest.raises(HeldControlError) as refused:
            engine.keyboard_type(text)
        assert str(refused.value) == _key_refusal("keyboard_type", "shift")
    time.sleep(0.5)
    assert probe.events_since(mark) == []

    # Text that needs no shift composes with the held one (0.11 behaviour).
    assert engine.keyboard_type("b").held == HeldInput(keys=["shift"])
    _wait_entry(probe, "B")
    assert engine.keyboard_press("b").held == HeldInput(keys=["shift"])
    _wait_entry(probe, "BB")
    events = probe.settle(since=mark)
    assert ("key_release", "Shift_L") not in _key_events(events), events
    assert field(probe.status("raw_events:"), "held_mods") == "shift"
    assert engine.keyboard_hold("release", "shift").held == HeldInput()


@pytest.mark.qa("QA-INP-039")
def test_unicode_paste_chord_touching_held_ctrl_refused(
    engine: AutomationEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # KWin has no virtual-keyboard protocol, so wtype fails and the paste route
    # (ctrl+v into a GTK entry) is chosen: it touches the held ctrl.
    probe = start_probe_session(engine, tmp_path / "paste", enable_clipboard=True)
    engine.clipboard_write("before-paste")
    engine.keyboard_hold("press", "ctrl")
    mark = probe.mark()
    with pytest.raises(HeldControlError) as refused:
        engine.keyboard_type("한글", method="unicode")
    assert str(refused.value) == _key_refusal("keyboard_type", "ctrl")
    assert engine.clipboard_read().text == "before-paste"
    assert engine.session_info().held == HeldInput(keys=["ctrl"])
    time.sleep(0.5)
    assert probe.events_since(mark) == []
    assert engine.session_stop().released == HeldInput(keys=["ctrl"])

    # A working wtype route presses no named key, so the held ctrl does not matter.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    wtype = fake_bin / "wtype"
    wtype.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wtype.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    probe = start_probe_session(engine, tmp_path / "wtype")
    engine.keyboard_hold("press", "ctrl")
    mark = probe.mark()
    typed = engine.keyboard_type("한글", method="unicode")
    assert typed.route == "wtype" and typed.held == HeldInput(keys=["ctrl"])
    time.sleep(0.5)
    assert probe.events_since(mark) == []
    assert engine.keyboard_hold("release", "ctrl").held == HeldInput()


# ── stdio server: exit, kill and validation ──────────────────────────────


class StdioServer:
    """The installed kwin-mcp over raw newline-delimited JSON-RPC, with its process."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self._stderr: IO[bytes] = tempfile.TemporaryFile()  # noqa: SIM115 - closed in close()
        self.process = subprocess.Popen(
            ["kwin-mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            env={**os.environ, **(env or {})},
        )
        self._next_id = 0
        self._request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "kwin-mcp-e2e-held", "version": "0"},
            },
        )
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _write(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()

    def send(self, method: str, params: dict[str, Any]) -> int:
        self._next_id += 1
        self._write({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        return self._next_id

    def wait_reply(self, request_id: int) -> dict[str, Any]:
        assert self.process.stdout is not None
        deadline = time.monotonic() + RPC_TIMEOUT_S
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"no reply to request {request_id}:\n{self.stderr_text()}"
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                continue
            line = self.process.stdout.readline()
            assert line, f"server closed stdout:\n{self.stderr_text()}"
            message = json.loads(line)
            if message.get("id") == request_id and "method" not in message:
                assert "error" not in message, message
                return message["result"]

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self.wait_reply(self.send(method, params))

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        assert not result.get("isError"), result
        return result

    def close_stdin(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()

    def wait_exit(self) -> int:
        return self.process.wait(timeout=SERVER_EXIT_TIMEOUT_S)

    def stderr_text(self) -> str:
        self._stderr.seek(0)
        return self._stderr.read().decode(errors="replace")

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=SERVER_EXIT_TIMEOUT_S)
        self._stderr.close()


class LiveProbe:
    """A probe the test owns on the live KWin: its log and client origin."""

    def __init__(self, live: LiveKWin, log_path: Path) -> None:
        self.log_path = log_path
        self.process = spawn_probe(
            dbus_address=live.dbus_address,
            wayland_display=live.wayland_display,
            log_path=log_path,
        )
        windows = live.wait_for_windows(
            lambda ws: any(w.pid == self.process.pid and w.client and w.active for w in ws)
        )
        client = next(w for w in windows if w.pid == self.process.pid).client
        assert client is not None
        self.origin = client.as_ints()[:2]
        # The probe's raw recorder is ready once GTK maps the window; give it a frame.
        time.sleep(0.5)

    def point(self, lx: int, ly: int) -> dict[str, int]:
        return {"x": self.origin[0] + lx, "y": self.origin[1] + ly}

    def events(self) -> list[dict[str, Any]]:
        return read_events(self.log_path)

    def wait(self, predicate: Callable[[list[dict[str, Any]]], bool]) -> list[dict[str, Any]]:
        deadline = time.monotonic() + STATE_TIMEOUT_S
        events = self.events()
        while not predicate(events):
            assert time.monotonic() < deadline, events[-30:]
            time.sleep(POLL_S)
            events = self.events()
        return events


def _connect(server: StdioServer, live: LiveKWin) -> None:
    result = server.call(
        "session_connect",
        {"dbus_address": live.dbus_address, "wayland_display": live.wayland_display},
    )
    assert result["structuredContent"]["input_backend"] == "eis", result


def _count(events: list[dict[str, Any]], kind: str, name: str) -> int:
    key = "button" if kind.startswith("button") else "key"
    return sum(1 for e in events if e["kind"] == kind and e.get(key) == name)


def _hold_ctrl_and_left(server: StdioServer, probe: LiveProbe, since: int) -> None:
    server.call("keyboard_hold", {"phase": "press", "keys": "ctrl"})
    held = server.call(
        "pointer_hold", {"phase": "press", "target": probe.point(300, 200), "button": "left"}
    )
    assert held["structuredContent"]["held"] == {"buttons": ["left"], "keys": ["ctrl"]}, held
    probe.wait(lambda evs: _count(evs[since:], "button_press", "left") == 1)


@pytest.fixture
def live_probe(tmp_path: Path) -> Iterator[tuple[LiveKWin, LiveProbe]]:
    with live_kwin() as live:
        probe = LiveProbe(live, tmp_path / "live-probe.jsonl")
        try:
            yield live, probe
        finally:
            stop_process(probe.process)


@pytest.mark.qa("QA-INP-044")
@pytest.mark.qa("QA-SES-020")
def test_server_exit_releases_everything(live_probe: tuple[LiveKWin, LiveProbe]) -> None:
    live, probe = live_probe
    for leg in ("eof", "sigterm"):
        server = StdioServer()
        try:
            _connect(server, live)
            before = probe.events()
            _hold_ctrl_and_left(server, probe, len(before))
            if leg == "eof":
                server.close_stdin()
                assert server.wait_exit() == 0, server.stderr_text()
            else:
                server.process.send_signal(signal.SIGTERM)
                assert server.wait_exit() == 128 + signal.SIGTERM, server.stderr_text()
            start = len(before)
            events = probe.wait(
                lambda evs, start=start: (
                    _count(evs[start:], "button_release", "left") == 1
                    and _count(evs[start:], "key_release", "Control_L") == 1
                )
            )[start:]
            # Exactly one release of each, and the app's modifier state is clean.
            assert _count(events, "key_release", "Control_L") == 1, events
            assert _count(events, "button_release", "left") == 1, events
            assert events[-1]["held_mods"] == "none", events[-5:]
            assert (
                "kwin-mcp: exit cleanup: serialized session_stop path "
                "(completed on the tool thread)" in server.stderr_text()
            ), server.stderr_text()
        finally:
            server.kill()


@pytest.mark.qa("QA-INP-047")
def test_unreleased_modifier_and_button_do_not_leak_into_fresh_session(
    live_probe: tuple[LiveKWin, LiveProbe],
) -> None:
    live, probe = live_probe
    server = StdioServer()
    try:
        _connect(server, live)
        server.call("keyboard_hold", {"phase": "press", "keys": "shift"})
        server.call(
            "pointer_hold", {"phase": "press", "target": probe.point(300, 200), "button": "left"}
        )
        probe.wait(lambda evs: _count(evs, "button_press", "left") == 1)
    finally:
        # SIGKILL: no cleanup runs; only KWin's EIS disconnect can release the input.
        server.kill()

    # KWin 6.7.5 crashes (SIGSEGV) when this client dies holding input (6.3.6 does
    # not); the compositor's exit ends the live session, so nothing is left to check.
    # Only that crash is the known limit: any other KWin exit fails the test.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and live.process.poll() is None:
        time.sleep(POLL_S)
    exit_code = live.process.poll()
    if exit_code is not None:
        # The session script exits with kwin_wayland's status: 128 + signal number.
        assert exit_code == 128 + signal.SIGSEGV, exit_code
        require_or_skip("kwin-survives-client-kill", CRASH_LIMIT, os.environ)

    fresh = StdioServer()
    try:
        _connect(fresh, live)
        mark = len(probe.events())
        pressed = fresh.call("keyboard_press", {"keys": "a"})
        assert pressed["structuredContent"]["held"] == {"buttons": [], "keys": []}, pressed
        events = probe.wait(lambda evs: bool(of_kind(evs[mark:], "key_press")))
        (key,) = [e for e in of_kind(events[mark:], "key_press") if e["key"] in {"a", "A"}]
        left_released = _count(events, "button_release", "left") == 1
        if key["key"] != "a" or "shift" in key["mods"] or not left_released:
            require_or_skip("eis-disconnect-release", LEAK_LIMIT, os.environ)
        assert key["mods"] == [], key
        assert fresh.call("session_stop", {})["structuredContent"]["stopped"] is True
    finally:
        fresh.kill()


@pytest.mark.qa("QA-INP-049")
def test_shutdown_mid_drag_releases_button(live_probe: tuple[LiveKWin, LiveProbe]) -> None:
    live, probe = live_probe
    server = StdioServer()
    try:
        _connect(server, live)
        server.send(
            "tools/call",
            {
                "name": "pointer_drag",
                "arguments": {
                    "from": probe.point(100, 300),
                    "to": probe.point(400, 300),
                    "waypoints": [{**probe.point(250, 200), "dwell_ms": 3000}],
                },
            },
        )
        probe.wait(lambda evs: _count(evs, "button_press", "left") == 1)
        time.sleep(1.0)
        server.process.send_signal(signal.SIGTERM)
        code = server.wait_exit()
        # The drag's finally released the button (or KWin did on disconnect).
        deadline = time.monotonic() + STATE_TIMEOUT_S
        while _count(probe.events(), "button_release", "left") != 1:
            assert time.monotonic() < deadline, (code, server.stderr_text(), probe.events()[-5:])
            time.sleep(POLL_S)
        events = probe.events()
        assert of_kind(events, *BUTTON_KINDS)[-1]["kind"] == "button_release", events[-5:]
    finally:
        server.kill()


@pytest.mark.anyio
async def test_invalid_key_and_button_are_tool_errors_without_killing_server() -> None:
    invalid_key = "invalid-key-sentinel"
    async with running_mcp_server() as client:
        await client.call_structured("session_start", {"screen_width": 1280, "screen_height": 800})
        try:
            for tool, args in (
                ("keyboard_press", {"keys": invalid_key}),
                ("keyboard_hold", {"phase": "press", "keys": invalid_key}),
                ("keyboard_hold", {"phase": "release", "keys": invalid_key}),
            ):
                lines = await client.call_tool_error(tool, args, lines=None)
                assert f"Unknown key: {invalid_key}" in "\n".join(lines), (tool, lines)
            lines = await client.call_tool_error(
                "pointer_hold",
                {"phase": "press", "target": {"x": 640, "y": 400}, "button": "sideways-button"},
                lines=None,
            )
            assert "button" in "\n".join(lines).lower(), lines
            assert await client.session.send_ping() is not None
            names = {tool.name for tool in await client.list_tools()}
            assert {"keyboard_press", "keyboard_hold", "pointer_hold", "session_stop"} <= names
            pressed, text = await client.call_structured("keyboard_press", {"keys": "escape"})
            assert text == "Pressed: escape" and pressed["held"] == {"buttons": [], "keys": []}
        finally:
            stopped, _ = await client.call_structured("session_stop")
            assert stopped["stopped"] is True


@pytest.mark.anyio
async def test_invalid_input_arguments_are_tool_errors_before_any_input(tmp_path: Path) -> None:
    log_path = tmp_path / "probe-events.jsonl"
    async with running_mcp_server() as client:
        await client.call_structured("session_start", {"screen_width": 1280, "screen_height": 800})
        try:
            await client.call_structured(
                "app_launch",
                {
                    "command": "python3 /app/tests/e2e/interaction_probe.py",
                    "env": probe_env(log_path),
                },
            )
            found, _ = await client.call_structured(
                "element_query",
                {
                    "locator": {"app": "interaction_probe.py", "query": "Keyboard Target"},
                    "wait": {"timeout_ms": 15_000},
                },
            )
            assert found["wait"]["satisfied"], found
            point = {"x": 640, "y": 400}
            to = {"x": 740, "y": 400}
            await anyio.sleep(1.0)
            before = read_events(log_path)

            cases: list[tuple[str, dict[str, Any], str]] = [
                ("touch_swipe", {"from": point, "to": to, "fingers": 0}, "fingers"),
                ("touch_swipe", {"from": point, "to": to, "fingers": -1}, "fingers"),
                ("pointer_drag", {"from": point, "to": to, "waypoints": [{"x": 1}]}, "waypoints"),
                ("pointer_drag", {"from": point, "to": to, "waypoints": [[1, 2]]}, "waypoints"),
                (
                    "pointer_drag",
                    {"from": point, "to": to, "waypoints": [{"x": 1, "y": 2, "dwell_ms": "a"}]},
                    "dwell_ms",
                ),
                ("pointer_click", {"target": point, "modifiers": "hyper"}, "Unknown modifier"),
                ("pointer_click", {"target": point, "modifiers": "ctrl+"}, "Unknown modifier"),
                (
                    "pointer_drag",
                    {"from": point, "to": to, "modifiers": "hyper"},
                    "Unknown modifier",
                ),
                ("keyboard_type", {"text": "héllo€"}, 'Retry with method="unicode".'),
                ("keyboard_type", {"text": "a\x00b", "method": "unicode"}, "NUL"),
            ]
            for tool, args, fragment in cases:
                lines = await client.call_tool_error(tool, args, lines=None)
                assert fragment in "\n".join(lines), (tool, args, lines)
            # Give any event that did reach the probe time to show.
            await anyio.sleep(0.5)
            assert read_events(log_path) == before
        finally:
            stopped, _ = await client.call_structured("session_stop")
            assert stopped["stopped"] is True


@pytest.mark.anyio
async def test_session_stop_releases_libei_without_broken_pipe_warning() -> None:
    async with running_mcp_server() as client:
        info, _ = await client.call_structured(
            "session_start", {"screen_width": 1280, "screen_height": 800}
        )
        assert info["input_backend"] == "eis", info
        stopped, _ = await client.call_structured("session_stop")
        assert stopped["stopped"] is True
        assert "failed to send message" not in client.stderr_text(), client.stderr_text()


@pytest.fixture
def anyio_backend() -> str:
    """The installed MCP client runs on asyncio."""
    return "asyncio"
