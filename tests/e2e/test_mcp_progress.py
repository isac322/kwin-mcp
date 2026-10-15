"""Progress notifications over stdio: only with a progressToken, strictly increasing.

Long-running tools stream ``notifications/progress`` (S §4 Progress): session
startup phases and the KWin readiness heartbeat, the element_query wait
heartbeat, hold slices, drag segments, typing, touch steps and capture frames.
Without a progressToken the server sends none, and nothing follows a response.

Message texts and step counts are asserted exactly as the engine reports them.
Timing follows the suite rule: lower bound = stated value minus 10 %, upper
bound <= 3x; heartbeat spacing is read from the server-side progress values
(elapsed time) where the value encodes it.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import stat
import time
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from atspi_oracle import AtspiOracle
from mcp_harness import (
    MAX_LINE_BYTES,
    RAW_CALL_TIMEOUT_SECONDS,
    ProgressLog,
    raw_mcp_client,
    running_mcp_server,
)
from session_harness import live_kwin
from visual_harness import nested_visual_kwin

if TYPE_CHECKING:
    from pathlib import Path

    from mcp_harness import McpTestClient, RawJsonRpcClient
    from visual_harness import NestedVisualKWin

pytestmark = pytest.mark.anyio

Event = tuple[float, float | None, str | None]

SCREEN = {"screen_width": 1280, "screen_height": 800}
MISSING_ELEMENT = "kwin-mcp-e2e-element-that-never-exists"
WAIT_TIMEOUT_MS = 1500
FRAME_DELAYS = [0, 100]
PROGRESS_METHOD = "notifications/progress"
PROBE_COMMAND = "python3 /app/tests/e2e/interaction_probe.py"
PROBE_APP = "interaction_probe.py"
PROBE_ENV = {
    "GDK_BACKEND": "wayland",
    "GTK_MODULES": "gail:atk-bridge",
    "NO_AT_BRIDGE": "0",
    "XDG_SESSION_TYPE": "wayland",
}
APP_WAIT_MS = 15_000
# Hold slices (input._HOLD_SLICE_S).
HOLD_SLICE_S = 0.5
# element_query's wait heartbeat (core._ATSPI_HEARTBEAT_S), in ms.
WAIT_HEARTBEAT_MS = 250.0
# The KWin readiness read loop reports at least every 0.5 s; its progress value
# is 1 + elapsed / 60 s (session._STARTUP_READ_TIMEOUT).
KWIN_HEARTBEAT_S = 0.5
KWIN_STARTUP_READ_TIMEOUT_S = 60.0
KWIN_READY_DELAY_S = 3
SESSION_START_PHASES = [
    "Starting KWin",
    "KWin ready",
    "Wayland socket ready",
    "Input backend ready",
]
KWIN_HEARTBEAT = re.compile(r"^Waiting for KWin \((\d+)s\)$")


@pytest.fixture
def anyio_backend() -> str:
    """The installed MCP client runs on asyncio."""
    return "asyncio"


def _visual_env(visual: NestedVisualKWin) -> dict[str, str]:
    return {"DISPLAY": visual.x_display, "KWIN_MCP_X11_SCREENSHOT": "1"}


def _assert_strictly_increasing(events: list[Event]) -> None:
    """Every value exceeds the previous one, has a total, and never passes it."""
    assert events, events
    values = [value for value, _, _ in events]
    assert values[0] > 0, events
    assert all(a < b for a, b in itertools.pairwise(values)), events
    for value, total, _ in events:
        assert total is not None, events
        assert value <= total, events


def _assert_spacing(values: list[float], interval: float, events: object) -> None:
    """Consecutive ``values`` are ``interval`` apart: at least -10 %, at most 3x."""
    for a, b in itertools.pairwise(values):
        assert interval * 0.9 <= b - a <= interval * 3, events


async def _connect(client: McpTestClient, visual: NestedVisualKWin) -> None:
    info, _ = await client.call_structured(
        "session_connect",
        {"dbus_address": visual.dbus_address, "wayland_display": visual.wayland_display},
    )
    assert info["kind"] == "live", info


async def _start_session(client: McpTestClient) -> dict[str, Any]:
    info, _ = await client.call_structured("session_start", SCREEN)
    assert info["input_backend"] == "eis", info
    return info


# ── Raw JSON-RPC transcript ──────────────────────────────────────────────


async def _request_logged(
    client: RawJsonRpcClient, method: str, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Send one request; return its result and every notification read before it.

    ``RawJsonRpcClient.request`` drops notifications, so this reads the stream
    itself: notifications are the transcript under test here.
    """
    client._next_id += 1
    request_id = client._next_id
    await client._send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": client._with_meta(params),
        }
    )
    notifications: list[dict[str, Any]] = []
    with anyio.fail_after(RAW_CALL_TIMEOUT_SECONDS):
        while True:
            message = json.loads(await client._stdout.receive_until(b"\n", MAX_LINE_BYTES))
            if "method" in message:
                if "id" in message:
                    await client._answer(message)
                else:
                    notifications.append(message)
                continue
            assert message.get("id") == request_id, message
            assert "error" not in message, message
            return message["result"], notifications


async def _call_logged(
    client: RawJsonRpcClient, name: str, arguments: dict[str, Any], *, token: str | None
) -> tuple[dict[str, Any], list[Event]]:
    """Call a tool (with ``token`` as progressToken, or none); return result and progress."""
    params: dict[str, Any] = {"name": name, "arguments": arguments}
    if token is not None:
        params["_meta"] = {"progressToken": token}
    result, notifications = await _request_logged(client, "tools/call", params)
    text = "\n".join(c["text"] for c in result["content"] if c["type"] == "text")
    assert not result.get("isError"), text
    events: list[Event] = []
    for note in notifications:
        if note["method"] != PROGRESS_METHOD:
            continue
        progress = note["params"]
        assert progress["progressToken"] == token, note
        events.append((progress["progress"], progress.get("total"), progress.get("message")))
    return result, events


async def _call_both(
    client: RawJsonRpcClient, name: str, arguments: dict[str, Any], token: str
) -> list[Event]:
    """Without a token: no progress at all; with one: strictly increasing progress."""
    _, silent = await _call_logged(client, name, arguments, token=None)
    assert silent == [], (name, silent)
    _, events = await _call_logged(client, name, arguments, token=token)
    _assert_strictly_increasing(events)
    return events


# ── Ported 0.11 coverage ─────────────────────────────────────────────────


async def test_long_running_tools_report_increasing_progress_only_when_requested() -> None:
    """session_start and element_query(wait) stream progress; without a token nothing changes."""
    wait_arguments = {
        "locator": {"query": MISSING_ELEMENT},
        "wait": {"timeout_ms": WAIT_TIMEOUT_MS},
    }
    async with running_mcp_server() as client:
        start_progress = ProgressLog()
        started, _ = await client.call_structured(
            "session_start", SCREEN, progress_callback=start_progress
        )
        await start_progress.settle()
        assert len(start_progress.events) >= 4, start_progress.events
        _assert_strictly_increasing(start_progress.events)
        with_message = [message for _, _, message in start_progress.events if message]
        assert len(with_message) >= 4, start_progress.events

        wait_progress = ProgressLog()
        waited, _ = await client.call_structured(
            "element_query", wait_arguments, progress_callback=wait_progress
        )
        assert waited["wait"]["satisfied"] is False, waited
        assert waited["wait"]["timeout_ms"] == WAIT_TIMEOUT_MS, waited
        await wait_progress.settle()
        assert len(wait_progress.events) >= 3, wait_progress.events
        _assert_strictly_increasing(wait_progress.events)

        # No progressToken: the same calls succeed with the same results.
        unreported, _ = await client.call_structured("element_query", wait_arguments)
        assert unreported["elements"] == waited["elements"] == [], unreported
        assert unreported["wait"]["satisfied"] is False, unreported
        assert unreported["wait"]["timeout_ms"] == WAIT_TIMEOUT_MS, unreported

        await client.call_structured("session_stop")
        restarted, _ = await client.call_structured("session_start", SCREEN)
        per_session = {"wayland_socket", "dbus_address", "artifacts_dir"}
        assert {k: v for k, v in restarted.items() if k not in per_session} == {
            k: v for k, v in started.items() if k not in per_session
        }, (started, restarted)


@pytest.mark.visual
async def test_action_then_frame_burst_reports_strictly_increasing_progress() -> None:
    """The gesture's step count and the frame burst restart at 1; emitted progress must not."""
    with nested_visual_kwin() as visual:
        async with running_mcp_server(env=_visual_env(visual)) as client:
            try:
                await _connect(client, visual)
                swipe_progress = ProgressLog()
                swiped, _ = await client.call_structured(
                    "touch_swipe",
                    {
                        "from": {"x": 200, "y": 600},
                        "to": {"x": 600, "y": 600},
                        "duration_ms": 300,
                        "capture": {"delays_ms": FRAME_DELAYS},
                    },
                    progress_callback=swipe_progress,
                )
                assert len(swiped["frames"]) == len(FRAME_DELAYS), swiped
                await swipe_progress.settle()
                events = swipe_progress.events
                assert len(events) > len(FRAME_DELAYS), events
                _assert_strictly_increasing(events)
                # The action's counter and the burst each start at 1; the burst's raw
                # values are offset, so the last two events are its frames.
                assert [message for _, _, message in events[-2:]] == [
                    f"Frame {index}/{len(FRAME_DELAYS)} at {delay}ms"
                    for index, delay in enumerate(FRAME_DELAYS, 1)
                ], events
                progress, total, _ = events[-1]
                assert progress == total, events
                await client.call_structured("session_stop")
            finally:
                (visual.artifact_dir / "mcp-server.stderr.log").write_text(
                    client.stderr_text(), encoding="utf-8"
                )


# ── QA list ──────────────────────────────────────────────────────────────


@pytest.mark.qa("QA-MCP-006")
@pytest.mark.visual
@pytest.mark.timeout(300)
async def test_progress_only_with_token_and_monotonic() -> None:
    token = "qa-mcp-006"
    async with raw_mcp_client({}) as client:
        _, silent = await _call_logged(client, "session_start", SCREEN, token=None)
        assert silent == [], silent
        await _call_logged(client, "session_stop", {}, token=None)
        _, started = await _call_logged(client, "session_start", SCREEN, token=token)
        _assert_strictly_increasing(started)
        assert started[-1][0] == started[-1][1], started

        # element_query without wait never reports, token or not.
        for query_token in (None, token):
            _, read_now = await _call_logged(client, "element_query", {}, token=query_token)
            assert read_now == [], read_now
        wait_args = {"locator": {"query": MISSING_ELEMENT}, "wait": {"timeout_ms": 600}}
        waited = await _call_both(client, "element_query", wait_args, token)
        assert {message for _, _, message in waited} == {
            f"Waiting for query='{MISSING_ELEMENT}'"
        }, waited

        drag = {
            "from": {"x": 200, "y": 200},
            "to": {"x": 500, "y": 200},
            "waypoints": [{"x": 300, "y": 300}],
        }
        dragged = await _call_both(client, "pointer_drag", drag, token)
        assert [m for _, _, m in dragged] == ["Drag segment 1/2", "Drag segment 2/2"], dragged
        typed = await _call_both(client, "keyboard_type", {"text": "abc"}, token)
        assert typed == [(3, 3, "Typed 3/3 characters")], typed
        swipe = {"from": {"x": 200, "y": 500}, "to": {"x": 600, "y": 500}}
        swiped = await _call_both(client, "touch_swipe", swipe, token)
        assert {m for _, _, m in swiped} == {"Swiping"}, swiped
        pinch = {"center": {"x": 640, "y": 400}, "start_distance": 300, "end_distance": 100}
        pinched = await _call_both(client, "touch_pinch", pinch, token)
        assert {m for _, _, m in pinched} == {"Pinching"}, pinched
        tap = {"target": {"x": 640, "y": 400}, "hold_ms": 1000}
        tapped = await _call_both(client, "touch_tap", tap, token)
        assert {m for _, _, m in tapped} == {"Holding touch"}, tapped
        await _call_logged(client, "session_stop", {}, token=None)

    # A hold followed by a frame burst needs a compositor that answers ScreenShot2.
    with nested_visual_kwin() as visual:
        async with raw_mcp_client({}, env=_visual_env(visual)) as client:
            connect = {
                "dbus_address": visual.dbus_address,
                "wayland_display": visual.wayland_display,
            }
            _, silent = await _call_logged(client, "session_connect", connect, token=None)
            assert silent == [], silent
            await _call_logged(client, "session_stop", {}, token=None)
            _, connected = await _call_logged(client, "session_connect", connect, token=token)
            _assert_strictly_increasing(connected)
            assert connected[-1][0] == connected[-1][1], connected

            click = {
                "target": {"x": 640, "y": 400},
                "hold_ms": 1500,
                "capture": {"delays_ms": [0, 500]},
            }
            clicked = await _call_both(client, "pointer_click", click, token)
            messages = [message for _, _, message in clicked]
            assert messages[-2:] == ["Frame 1/2 at 0ms", "Frame 2/2 at 500ms"], clicked
            holds = messages[:-2]
            assert len(holds) >= 3, clicked
            assert set(holds) == {"Holding left button"}, clicked
            assert clicked[-1][0] == clicked[-1][1], clicked
            await _call_logged(client, "session_stop", {}, token=None)


@pytest.mark.qa("QA-MCP-007")
@pytest.mark.slow
@pytest.mark.timeout(600)
async def test_no_progress_after_response() -> None:
    token = "qa-mcp-007"
    hold_s = 2.0
    async with raw_mcp_client({}) as client:
        await _call_logged(client, "session_start", SCREEN, token=None)
        click = {"target": {"x": 640, "y": 400}, "hold_ms": int(hold_s * 1000)}
        _, events = await _call_logged(client, "pointer_click", click, token=token)
        _assert_strictly_increasing(events)
        assert {message for _, _, message in events} == {"Holding left button"}, events
        assert len(events) >= 4, events
        _assert_spacing([0.0, *[value for value, _, _ in events[:-1]]], HOLD_SLICE_S, events)
        assert events[-1][0] == events[-1][1] == hold_s, events

        # Longer than a hold slice: any straggler would be read before the pong.
        await anyio.sleep(1.0)
        _, after = await _request_logged(client, "ping", {})
        assert [note for note in after if note["method"] == PROGRESS_METHOD] == [], after


@pytest.mark.qa("QA-SES-005")
async def test_session_start_progress_four_steps() -> None:
    async with running_mcp_server() as client:
        log = ProgressLog()
        await client.call_structured("session_start", {}, progress_callback=log)
        await log.settle()
        events = log.events
        _assert_strictly_increasing(events)
        phases = [
            (value, total, message)
            for value, total, message in events
            if message is None or KWIN_HEARTBEAT.match(message) is None
        ]
        assert phases == [
            (float(step), 4.0, message) for step, message in enumerate(SESSION_START_PHASES, 1)
        ], events
        # Heartbeats only between "Starting KWin" and "KWin ready".
        for value, total, message in events:
            if message is not None and KWIN_HEARTBEAT.match(message):
                assert 1 < value < 2, events
                assert total == 4, events
        assert events[-1] == (4, 4, "Input backend ready"), events


@pytest.mark.qa("QA-SES-006")
@pytest.mark.slow
@pytest.mark.timeout(600)
async def test_kwin_ready_heartbeat(tmp_path: Path) -> None:
    real_kwin = shutil.which("kwin_wayland")
    assert real_kwin is not None
    wrapper = tmp_path / "kwin_wayland"
    wrapper.write_text(f'#!/bin/sh\nsleep {KWIN_READY_DELAY_S}\nexec {real_kwin} "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env = {"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}

    async with running_mcp_server(env=env) as client:
        log = ProgressLog()
        started_at = time.monotonic()
        info, _ = await client.call_structured("session_start", {}, progress_callback=log)
        elapsed = time.monotonic() - started_at
        await log.settle()
        assert info["kind"] == "virtual", info
        assert elapsed >= KWIN_READY_DELAY_S * 0.9, elapsed

        events = log.events
        _assert_strictly_increasing(events)
        messages = [message for _, _, message in events]
        assert "KWin ready" in messages, events
        ready = messages.index("KWin ready")
        heartbeats = [
            (value, int(match.group(1)))
            for value, _, message in events[:ready]
            if message is not None and (match := KWIN_HEARTBEAT.match(message)) is not None
        ]
        # The read loop wakes at least every 0.5 s for the >= 3 s delay.
        assert len(heartbeats) >= int(KWIN_READY_DELAY_S / KWIN_HEARTBEAT_S * 0.9), events
        assert max(seconds for _, seconds in heartbeats) >= KWIN_READY_DELAY_S - 1, events
        waited_s = [(value - 1) * KWIN_STARTUP_READ_TIMEOUT_S for value, _ in heartbeats]
        for a, b in itertools.pairwise(waited_s):
            assert b - a <= KWIN_HEARTBEAT_S * 3, events
        assert events[-1] == (4, 4, "Input backend ready"), events


@pytest.mark.qa("QA-SES-014")
async def test_session_connect_progress_three_steps() -> None:
    with live_kwin() as live:
        async with running_mcp_server() as client:
            log = ProgressLog()
            info, _ = await client.call_structured(
                "session_connect",
                {"dbus_address": live.dbus_address, "wayland_display": live.wayland_display},
                progress_callback=log,
            )
            await log.settle()
            assert info["kind"] == "live", info
            assert log.events == [
                (1, 3, "KWin reachable on D-Bus"),
                (2, 3, "Wayland display reachable"),
                (3, 3, "Input backend ready"),
            ], log.events
            await client.call_structured("session_stop")


@pytest.mark.qa("QA-ELEM-020")
async def test_element_query_heartbeat_only_with_wait() -> None:
    timeout_ms = 1000
    async with running_mcp_server() as client:
        await _start_session(client)
        read_now = ProgressLog()
        await client.call_structured("element_query", {}, progress_callback=read_now)
        await read_now.settle()
        assert read_now.events == [], read_now.events

        log = ProgressLog()
        result, _ = await client.call_structured(
            "element_query",
            {"locator": {"query": "zz"}, "wait": {"timeout_ms": timeout_ms}},
            progress_callback=log,
        )
        await log.settle()
        assert result["wait"]["satisfied"] is False, result
        events = log.events
        _assert_strictly_increasing(events)
        assert {message for _, _, message in events} == {"Waiting for query='zz'"}, events
        assert {total for _, total, _ in events} == {timeout_ms}, events
        # Values are elapsed ms; the last may be clamped to the timeout.
        values = [value for value, _, _ in events if value < timeout_ms]
        assert len(values) >= int(timeout_ms / WAIT_HEARTBEAT_MS * 0.9) - 1, events
        _assert_spacing(values, WAIT_HEARTBEAT_MS, events)
        assert values[0] <= WAIT_HEARTBEAT_MS * 3, events


def _assert_hold_slices(events: list[Event], hold_s: float, message: str, minimum: int) -> None:
    _assert_strictly_increasing(events)
    assert {text for _, _, text in events} == {message}, events
    assert len(events) >= minimum, events
    assert {total for _, total, _ in events} == {hold_s}, events
    assert events[-1][0] == hold_s, events
    # Every slice but the last is a full 0.5 s; the values are elapsed seconds.
    _assert_spacing([0.0, *[value for value, _, _ in events[:-1]]], HOLD_SLICE_S, events)
    assert events[-1][0] - (events[-2][0] if len(events) > 1 else 0.0) <= HOLD_SLICE_S * 3


@pytest.mark.qa("QA-INP-005")
async def test_hold_progress_holding_left_button() -> None:
    async with running_mcp_server() as client:
        await _start_session(client)
        click_log = ProgressLog()
        await client.call_structured(
            "pointer_click",
            {"target": {"x": 640, "y": 400}, "hold_ms": 2000},
            progress_callback=click_log,
        )
        await click_log.settle()
        _assert_hold_slices(click_log.events, 2.0, "Holding left button", 4)

        tap_log = ProgressLog()
        await client.call_structured(
            "touch_tap",
            {"target": {"x": 640, "y": 400}, "hold_ms": 1500},
            progress_callback=tap_log,
        )
        await tap_log.settle()
        _assert_hold_slices(tap_log.events, 1.5, "Holding touch", 3)


@pytest.mark.qa("QA-INP-025")
async def test_drag_progress_per_segment() -> None:
    async with running_mcp_server() as client:
        await _start_session(client)
        log = ProgressLog()
        await client.call_structured(
            "pointer_drag",
            {
                "from": {"x": 200, "y": 200},
                "to": {"x": 800, "y": 500},
                "waypoints": [
                    {"x": 350, "y": 250},
                    {"x": 500, "y": 300},
                    {"x": 650, "y": 400},
                ],
            },
            progress_callback=log,
        )
        await log.settle()
        assert log.events == [(index, 4, f"Drag segment {index}/4") for index in range(1, 5)], (
            log.events
        )


async def _focus_probe_entry(client: McpTestClient) -> None:
    await client.call_structured("app_launch", {"command": PROBE_COMMAND, "env": PROBE_ENV})
    found, _ = await client.call_structured(
        "element_query",
        {
            "locator": {"app": PROBE_APP, "query": "Keyboard Target"},
            "wait": {"timeout_ms": APP_WAIT_MS},
        },
    )
    assert found["wait"]["satisfied"], found
    targets = [el for el in found["elements"] if el["name"] == "Keyboard Target"]
    assert len(targets) == 1, found
    await client.call_structured("pointer_click", {"target": {"ref": targets[0]["ref"]}})


def _wait_entry_text(oracle: AtspiOracle, expected: str) -> None:
    def has_text(nodes: list[Any]) -> bool:
        return any(n.name == "Keyboard Target" and n.text == expected for n in nodes)

    nodes = oracle.wait_for(has_text, app=PROBE_APP, timeout_s=15.0)
    assert has_text(nodes)


@pytest.mark.qa("QA-INP-058")
@pytest.mark.timeout(400)
async def test_keyboard_type_progress_every_half_second() -> None:
    text = ("the quick brown fox jumps over the lazy dog " * 3)[:120]
    assert len(text) == 120
    async with running_mcp_server() as client:
        info = await _start_session(client)
        oracle = AtspiOracle(info["dbus_address"])
        await _focus_probe_entry(client)

        log = ProgressLog()
        typed, _ = await client.call_structured(
            "keyboard_type", {"text": text}, progress_callback=log
        )
        await log.settle()
        assert typed["chars"] == 120, typed
        events = log.events
        _assert_strictly_increasing(events)
        assert len(events) >= 6, events
        pattern = re.compile(r"^Typed (\d+)/120 characters$")
        for value, total, message in events:
            match = pattern.match(message or "")
            assert match is not None, events
            assert int(match.group(1)) == value, events
            assert total == 120, events
        assert events[-1] == (120, 120, "Typed 120/120 characters"), events
        _wait_entry_text(oracle, text)

        # Large text: no length cap; the echo collapses to a count. It is appended.
        large = ("abcdefghij" * 200)[:2000]
        large_log = ProgressLog()
        typed, rendered = await client.call_structured(
            "keyboard_type", {"text": large}, progress_callback=large_log
        )
        await large_log.settle()
        assert typed["chars"] == 2000, typed
        assert typed["echo"] == "<2000 characters>", typed
        assert "<2000 characters>" in rendered, rendered
        _assert_strictly_increasing(large_log.events)
        assert large_log.events[-1] == (2000, 2000, "Typed 2000/2000 characters")
        _wait_entry_text(oracle, text + large)


@pytest.mark.qa("QA-INP-070")
async def test_touch_progress_texts() -> None:
    async with running_mcp_server() as client:
        await _start_session(client)
        cases: list[tuple[str, dict[str, Any], int, str, int]] = [
            (
                "touch_swipe",
                {"from": {"x": 300, "y": 400}, "to": {"x": 900, "y": 400}, "fingers": 1},
                30,
                "Swiping",
                300,
            ),
            (
                "touch_swipe",
                {"from": {"x": 300, "y": 400}, "to": {"x": 900, "y": 400}, "fingers": 3},
                30,
                "3-finger swipe",
                300,
            ),
            (
                "touch_pinch",
                {"center": {"x": 640, "y": 400}, "start_distance": 400, "end_distance": 100},
                50,
                "Pinching",
                500,
            ),
        ]
        for name, arguments, steps, message, duration_ms in cases:
            log = ProgressLog()
            started = time.monotonic()
            await client.call_structured(name, arguments, progress_callback=log)
            elapsed_ms = (time.monotonic() - started) * 1000
            await log.settle()
            # One report every 10 % of the steps (10 ms each), the last at the total.
            every = steps // 10
            assert log.events == [
                (float(done), float(steps), message) for done in range(every, steps + 1, every)
            ], (name, log.events)
            assert elapsed_ms >= duration_ms * 0.9, (name, elapsed_ms)
