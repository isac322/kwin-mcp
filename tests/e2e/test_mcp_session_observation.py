"""Installed-package MCP stdio coverage for session and observation tools."""

from __future__ import annotations

import os
import re

import anyio
import pytest
from mcp_harness import running_mcp_server

SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800
ENV_NAME = "KWIN_MCP_E2E_ENV"
ENV_VALUE = "installed-mcp-session-env"
ENV_COMMAND = f'sh -c \'printf "%s" "${ENV_NAME}"\''
PID_PATTERN = re.compile(r"\(PID=(\d+)\)")
TREE_ROLE_PATTERN = re.compile(r"^\s*- \[([^]]+)]", re.MULTILINE)
WINDOW_HEADER_PATTERN = re.compile(r"^- (.+?) \(\d+ windows\)$")
CLIENT_GEOMETRY_PATTERN = re.compile(r"client: \((\d+), (\d+), (\d+)x(\d+)\)")
WINDOW_ID_PATTERN = re.compile(r"^    id:\s+(\S+)$", re.MULTILINE)
WAIT_TIMEOUT_MS = 300


@pytest.fixture
def anyio_backend() -> str:
    """The MCP stdio client is exercised on asyncio."""
    return "asyncio"


def _result_text(result: object) -> str:
    """Join textual MCP result blocks without depending on source-tree types."""
    content = getattr(result, "content", [])
    return "\n".join(str(getattr(block, "text", "")) for block in content)


def _window_blocks(output: str) -> dict[str, list[str]]:
    """Group list_windows entries by their accessible application name."""
    blocks: dict[str, list[str]] = {}
    current_app = ""
    for line in output.splitlines():
        match = WINDOW_HEADER_PATTERN.match(line)
        if match is not None:
            current_app = match.group(1).lower()
            blocks[current_app] = []
        elif current_app and line.startswith("    - "):
            blocks[current_app].append(line.strip())
    return blocks


@pytest.mark.anyio
async def test_installed_server_observes_virtual_session_and_apps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise session, process, AT-SPI, window, D-Bus, and Wayland tools over stdio."""
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    async with running_mcp_server() as client:
        session_running = False
        try:
            connect_output = await client.call_text("session_connect")
            assert connect_output == (
                "No D-Bus address available. Provide dbus_address parameter "
                "or ensure $DBUS_SESSION_BUS_ADDRESS is set."
            ), connect_output
            assert await client.session.send_ping() is not None

            start_output = await client.call_text(
                "session_start",
                {
                    "app_command": ENV_COMMAND,
                    "screen_width": SCREEN_WIDTH,
                    "screen_height": SCREEN_HEIGHT,
                    "env": {ENV_NAME: ENV_VALUE},
                },
            )
            session_running = True
            assert "Session started." in start_output, start_output
            assert "Wayland socket: wayland-mcp-" in start_output, start_output
            assert f"App launched: {ENV_COMMAND}" in start_output, start_output

            pid_match = PID_PATTERN.search(start_output)
            assert pid_match is not None, start_output
            env_pid = int(pid_match.group(1))

            log_deadline = anyio.current_time() + 5
            env_log = "(no log output yet)"
            while anyio.current_time() < log_deadline:
                env_log = await client.call_text(
                    "read_app_log", {"pid": env_pid, "last_n_lines": 10}
                )
                if env_log == ENV_VALUE:
                    break
                await anyio.sleep(0.1)
            assert env_log == ENV_VALUE, env_log

            bus_names = await client.call_text(
                "dbus_call",
                {
                    "service": "org.freedesktop.DBus",
                    "path": "/",
                    "interface": "org.freedesktop.DBus",
                    "method": "ListNames",
                },
            )
            assert "org.kde.KWin" in bus_names, bus_names[:500]

            kwin_owner = await client.call_text(
                "dbus_call",
                {
                    "service": "org.freedesktop.DBus",
                    "path": "/",
                    "interface": "org.freedesktop.DBus",
                    "method": "GetNameOwner",
                    "args": ["string:org.kde.KWin"],
                },
            )
            assert re.fullmatch(r":\d+\.\d+", kwin_owner), kwin_owner

            kcalc_launch = await client.call_text("launch_app", {"command": "kcalc"})
            assert "App launched: kcalc" in kcalc_launch, kcalc_launch
            waited = await client.call_text(
                "wait_for_element",
                {"query": "Equals", "app_name": "kcalc", "timeout_ms": 15_000},
            )
            assert '[button] "Equals"' in waited, waited[:500]

            tree = await client.call_text(
                "accessibility_tree",
                {"app_name": "kcalc", "max_depth": 15, "role": "button"},
            )
            roles = TREE_ROLE_PATTERN.findall(tree)
            assert roles, tree[:500]
            assert set(roles) == {"button"}, tree[:500]
            assert '[button] "Equals"' in tree, tree[:500]

            equals_elements = await client.call_text(
                "find_ui_elements", {"query": "Equals", "app_name": "kcalc"}
            )
            element_lines = [
                line for line in equals_elements.splitlines() if line.startswith("- [")
            ]
            assert element_lines, equals_elements[:500]
            assert any(re.match(r'^- \[[^]]+] "Equals"(?: |$)', line) for line in element_lines), (
                equals_elements[:500]
            )

            timeout_started = anyio.current_time()
            timeout_result = await client.call_result(
                "wait_for_element",
                {
                    "query": "definitely-not-a-real-kcalc-element",
                    "app_name": "kcalc",
                    "timeout_ms": WAIT_TIMEOUT_MS,
                    "poll_interval_ms": 50,
                },
            )
            timeout_elapsed = anyio.current_time() - timeout_started
            assert not timeout_result.isError, _result_text(timeout_result)
            assert (
                _result_text(timeout_result)
                == f"Timeout after {WAIT_TIMEOUT_MS}ms: no elements matching "
                "query='definitely-not-a-real-kcalc-element'"
            )
            assert timeout_elapsed >= WAIT_TIMEOUT_MS / 1000
            assert timeout_elapsed < 5, timeout_elapsed

            kwrite_launch = await client.call_text("launch_app", {"command": "kwrite"})
            assert "App launched: kwrite" in kwrite_launch, kwrite_launch
            kwrite_elements = await client.call_text(
                "wait_for_element",
                {"query": "", "app_name": "kwrite", "timeout_ms": 15_000},
            )
            assert "Found " in kwrite_elements, kwrite_elements[:500]

            windows = await client.call_text("list_windows")
            blocks = _window_blocks(windows)
            assert any("kcalc" in app for app in blocks), windows[:500]
            assert any("kwrite" in app for app in blocks), windows[:500]

            focus_output = await client.call_text("focus_window", {"app_name": "kcalc"})
            assert focus_output.startswith("Focused:"), focus_output
            focus_deadline = anyio.current_time() + 5
            while anyio.current_time() < focus_deadline:
                windows = await client.call_text("list_windows")
                blocks = _window_blocks(windows)
                kcalc_lines = [
                    line for app, lines in blocks.items() if "kcalc" in app for line in lines
                ]
                if any("[active" in line for line in kcalc_lines):
                    break
                await anyio.sleep(0.25)
            else:
                pytest.fail(f"kcalc never became active: {windows[:500]}")

            geometry = await client.call_text("window_geometry", {"app_name": "kcalc"})
            geometry_match = CLIENT_GEOMETRY_PATTERN.search(geometry)
            assert geometry_match is not None, geometry[:500]
            x, y, width, height = (int(value) for value in geometry_match.groups())
            assert 0 <= x < SCREEN_WIDTH, geometry
            assert 0 <= y < SCREEN_HEIGHT, geometry
            assert width > 0 and x + width <= SCREEN_WIDTH, geometry
            assert height > 0 and y + height <= SCREEN_HEIGHT, geometry

            active = await client.call_text("active_window")
            assert active.startswith("Active window:"), active
            assert "kcalc" in active.lower(), active
            assert "[active]" in active, active

            kwrite_geometry = await client.call_text("window_geometry", {"app_name": "kwrite"})
            kwrite_ids = WINDOW_ID_PATTERN.findall(kwrite_geometry)
            assert len(kwrite_ids) == 1, kwrite_geometry
            close_output = await client.call_text("window_close", {"window_id": kwrite_ids[0]})
            assert close_output.startswith("Close requested:"), close_output
            close_deadline = anyio.current_time() + 10
            while anyio.current_time() < close_deadline:
                remaining = await client.call_text("window_geometry", {"window_id": kwrite_ids[0]})
                if remaining == f"No window with id {kwrite_ids[0]!r}.":
                    break
                await anyio.sleep(0.25)
            else:
                pytest.fail(f"kwrite window was not closed: {remaining[:500]}")
            kcalc_geometry = await client.call_text("window_geometry", {"app_name": "kcalc"})
            assert len(WINDOW_ID_PATTERN.findall(kcalc_geometry)) == 1, kcalc_geometry

            seat_protocols = await client.call_text("wayland_info", {"filter_protocol": "wl_seat"})
            assert "interface: 'wl_seat'" in seat_protocols, seat_protocols[:500]
            assert "interface: 'wl_compositor'" not in seat_protocols, seat_protocols[:500]

            stop_output = await client.call_text("session_stop")
            session_running = False
            assert stop_output == "Session stopped.", stop_output
        finally:
            if session_running:
                await client.call_text("session_stop")


@pytest.mark.skipif(
    os.environ.get("KWIN_MCP_E2E_SCREENSHOT") == "1",
    reason="headless screenshot-error behavior contradicts the explicitly enabled success path",
)
@pytest.mark.anyio
async def test_virtual_screenshot_error_crosses_stdio_without_killing_server() -> None:
    """The headless virtual backend screenshot failure remains an MCP tool error."""
    async with running_mcp_server() as client:
        session_running = False
        try:
            start_output = await client.call_text(
                "session_start",
                {"screen_width": SCREEN_WIDTH, "screen_height": SCREEN_HEIGHT},
            )
            session_running = True
            assert "Session started." in start_output, start_output

            screenshot_result = await client.call_result("screenshot")
            screenshot_error = _result_text(screenshot_result)
            assert screenshot_result.isError, screenshot_error
            assert "Screenshot capture failed" in screenshot_error, screenshot_error
            assert "ScreenShot2" in screenshot_error, screenshot_error
            assert "Spectacle" in screenshot_error, screenshot_error

            bus_names = await client.call_text(
                "dbus_call",
                {
                    "service": "org.freedesktop.DBus",
                    "path": "/",
                    "interface": "org.freedesktop.DBus",
                    "method": "ListNames",
                },
            )
            assert "org.kde.KWin" in bus_names, bus_names[:500]

            stop_output = await client.call_text("session_stop")
            session_running = False
            assert stop_output == "Session stopped.", stop_output
        finally:
            if session_running:
                await client.call_text("session_stop")
