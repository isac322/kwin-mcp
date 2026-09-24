"""Installed-package EIS cleanup and input-validation coverage."""

from __future__ import annotations

import ast
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import pytest
from mcp_harness import running_mcp_server
from visual_harness import nested_visual_kwin

if TYPE_CHECKING:
    from mcp_harness import McpTestClient

SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800
PROBE_COMMAND = "python3 /app/tests/e2e/interaction_probe.py"
PROBE_SELECTOR = "interaction_probe.py"
PROBE_TITLE = "Interaction Probe"
POLL_INTERVAL_SECONDS = 0.1
STATE_TIMEOUT_SECONDS = 5.0
_RECT = r"\((-?\d+), (-?\d+), (\d+)x(\d+)\)"
_KEYBOARD_TARGET_LINE = re.compile(
    r'^\s*- \[[^]]+\] "Keyboard Target"(?:\s|$)',
    re.MULTILINE,
)


@pytest.fixture
def anyio_backend() -> str:
    """The installed MCP client runs on asyncio."""
    return "asyncio"


def _result_text(result: object) -> str:
    content = getattr(result, "content", [])
    return "\n".join(str(getattr(block, "text", "")) for block in content)


def _single_rect(output: str, pattern: str) -> tuple[int, int, int, int]:
    matches = re.findall(pattern, output, re.MULTILINE)
    assert len(matches) == 1, output[:1500]
    x, y, width, height = matches[0]
    return int(x), int(y), int(width), int(height)


async def _global_element_center(client: McpTestClient, name: str) -> tuple[int, int]:
    elements = await client.call_text(
        "find_ui_elements",
        {"query": name, "app_name": PROBE_SELECTOR},
    )
    local_x, local_y, width, height = _single_rect(
        elements,
        rf'^- \[[^]]+] "{re.escape(name)}" @ {_RECT}(?:\s|$)',
    )
    geometry = await client.call_text("window_geometry", {"app_name": PROBE_SELECTOR})
    client_x, client_y, _, _ = _single_rect(geometry, rf"client:\s+{_RECT}")
    return client_x + local_x + width // 2, client_y + local_y + height // 2


def _status_name(tree: str, prefix: str) -> str:
    matches = re.findall(rf'\[label] "({re.escape(prefix)}[^"]*)"', tree)
    assert len(matches) == 1, tree[:2000]
    return matches[0]


async def _wait_for_click_status(client: McpTestClient, previous: str) -> str:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    tree = ""
    while time.monotonic() < deadline:
        tree = await client.call_text("accessibility_tree", {"app_name": PROBE_SELECTOR})
        current = _status_name(tree, "click_status:")
        if current != previous:
            return current
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"click status did not change from {previous!r}:\n{tree[:2000]}")


def _entry_text(tree: str) -> str:
    for line in tree.splitlines():
        if _KEYBOARD_TARGET_LINE.match(line) is None:
            continue
        match = re.search(r"\btext=(.+?)(?: \[actions:|$)", line)
        return ast.literal_eval(match.group(1)) if match is not None else ""
    raise AssertionError(f"Keyboard Target was absent from the accessibility tree:\n{tree[:2000]}")


async def _wait_for_entry_text(client: McpTestClient, expected: str) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    actual = ""
    tree = ""
    while time.monotonic() < deadline:
        tree = await client.call_text("accessibility_tree", {"app_name": PROBE_SELECTOR})
        actual = _entry_text(tree)
        if actual == expected:
            return
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    assert actual == expected, tree[:2000]


async def _connect(client: McpTestClient, dbus_address: str, wayland_display: str) -> None:
    output = await client.call_text(
        "session_connect",
        {
            "dbus_address": dbus_address,
            "wayland_display": wayland_display,
            "keep_screenshots": False,
        },
    )
    assert output.startswith("Connected to live KWin session."), output
    assert "Input backend: KWin EIS" in output, output


async def _launch_probe(client: McpTestClient) -> Path:
    output = await client.call_text(
        "launch_app",
        {
            "command": PROBE_COMMAND,
            "env": {
                "GDK_BACKEND": "wayland",
                "GTK_MODULES": "gail:atk-bridge",
                "NO_AT_BRIDGE": "0",
                "XDG_SESSION_TYPE": "wayland",
            },
        },
    )
    assert f"App launched: {PROBE_COMMAND}" in output, output
    log_match = re.search(r"^App log: (.+)$", output, re.MULTILINE)
    assert log_match is not None, output
    waited = await client.call_text(
        "wait_for_element",
        {"query": "Keyboard Target", "app_name": PROBE_SELECTOR, "timeout_ms": 15_000},
    )
    assert _KEYBOARD_TARGET_LINE.search(waited) is not None, waited[:1500]
    tree = await client.call_text("accessibility_tree", {"app_name": PROBE_SELECTOR})
    assert f'[frame] "{PROBE_TITLE}"' in tree, tree[:2000]
    return Path(log_match.group(1))


@pytest.mark.anyio
@pytest.mark.visual
async def test_unreleased_modifier_and_button_do_not_leak_into_fresh_session() -> None:
    """Disconnecting EIS clears held state before the next client connection."""
    with nested_visual_kwin() as visual:
        async with running_mcp_server(
            env={"DISPLAY": visual.x_display, "KWIN_MCP_X11_SCREENSHOT": "1"},
        ) as client:
            first_connected = False
            second_connected = False
            first_artifact_dir: Path | None = None
            second_artifact_dir: Path | None = None
            try:
                await _connect(client, visual.dbus_address, visual.wayland_display)
                first_connected = True
                first_log = await _launch_probe(client)
                first_artifact_dir = first_log.parent
                click_x, click_y = await _global_element_center(client, "Click Target")

                assert (
                    await client.call_text("keyboard_key_down", {"key": "shift"})
                    == "Key down: shift"
                )
                assert (
                    await client.call_text(
                        "mouse_button_down",
                        {"x": click_x, "y": click_y, "button": "left"},
                    )
                    == f"Button left pressed at ({click_x}, {click_y})"
                )

                stop_first = await client.call_text("session_stop")
                first_connected = False
                assert stop_first == "Disconnected from live session.", stop_first
                assert not first_artifact_dir.exists(), first_artifact_dir

                await _connect(client, visual.dbus_address, visual.wayland_display)
                second_connected = True
                second_log = await _launch_probe(client)
                second_artifact_dir = second_log.parent

                initial_tree = await client.call_text(
                    "accessibility_tree", {"app_name": PROBE_SELECTOR}
                )
                assert _entry_text(initial_tree) == "", initial_tree[:2000]
                initial_click = _status_name(initial_tree, "click_status:")

                typed = await client.call_text("keyboard_type", {"text": "lowercase-clean"})
                assert typed == "Typed: 'lowercase-clean'", typed
                await _wait_for_entry_text(client, "lowercase-clean")

                fresh_click_x, fresh_click_y = await _global_element_center(client, "Click Target")
                clicked = await client.call_text(
                    "mouse_click", {"x": fresh_click_x, "y": fresh_click_y}
                )
                assert clicked == (f"Clicked left at ({fresh_click_x}, {fresh_click_y})"), clicked
                fresh_click_status = await _wait_for_click_status(client, initial_click)
                match = re.fullmatch(
                    r"click_status: button=left count=1 modifiers=none hold_ms=(\d+)",
                    fresh_click_status,
                )
                assert match is not None, fresh_click_status
                (visual.artifact_dir / "input-cleanup-evidence.txt").write_text(
                    f"keyboard_text=lowercase-clean\n{fresh_click_status}\n",
                    encoding="utf-8",
                )
                assert int(match.group(1)) < 500, fresh_click_status

                stop_second = await client.call_text("session_stop")
                second_connected = False
                assert stop_second == "Disconnected from live session.", stop_second
                assert not second_artifact_dir.exists(), second_artifact_dir
            finally:
                (visual.artifact_dir / "input-cleanup-mcp-server.stderr.log").write_text(
                    client.stderr_text(), encoding="utf-8"
                )
                if second_connected or first_connected:
                    await client.call_result("session_stop")

            assert first_artifact_dir is not None and not first_artifact_dir.exists()
            assert second_artifact_dir is not None and not second_artifact_dir.exists()


@pytest.mark.anyio
@pytest.mark.visual
async def test_mouse_drag_releases_state_before_x11_mirror_error(tmp_path: Path) -> None:
    """A failed final X11 mirror must not leave the EIS drag state latched."""
    failing_xdotool = tmp_path / "xdotool"
    failing_xdotool.write_text(
        "#!/bin/sh\n"
        'marker="${0}.called"\n'
        'if [ -e "$marker" ]; then\n'
        '    echo "synthetic final mirror failure" >&2\n'
        "    exit 73\n"
        "fi\n"
        ': > "$marker"\n',
        encoding="utf-8",
    )
    failing_xdotool.chmod(0o755)

    with nested_visual_kwin() as visual:
        async with running_mcp_server(
            env={
                "DISPLAY": visual.x_display,
                "KWIN_MCP_X11_SCREENSHOT": "1",
                "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            },
        ) as client:
            connected = False
            artifact_dir: Path | None = None
            try:
                await _connect(client, visual.dbus_address, visual.wayland_display)
                connected = True
                probe_log = await _launch_probe(client)
                artifact_dir = probe_log.parent

                drag_x, drag_y = await _global_element_center(client, "Drag Target")
                drag_result = await client.call_result(
                    "mouse_drag",
                    {
                        "from_x": drag_x - 100,
                        "from_y": drag_y,
                        "to_x": drag_x + 100,
                        "to_y": drag_y,
                        "button": "left",
                        "modifiers": ["shift"],
                    },
                )
                drag_error = _result_text(drag_result)
                assert drag_result.isError is True, drag_error
                assert "xdotool failed to mirror the screenshot cursor" in drag_error, drag_error
                assert "(exit 73)" in drag_error, drag_error

                failing_xdotool.unlink()

                typed = await client.call_text("keyboard_type", {"text": "lowercase-clean"})
                assert typed == "Typed: 'lowercase-clean'", typed
                await _wait_for_entry_text(client, "lowercase-clean")

                tree = await client.call_text("accessibility_tree", {"app_name": PROBE_SELECTOR})
                initial_click = _status_name(tree, "click_status:")
                click_x, click_y = await _global_element_center(client, "Click Target")
                clicked = await client.call_text(
                    "mouse_click",
                    {"x": click_x, "y": click_y, "button": "right"},
                )
                assert clicked == f"Clicked right at ({click_x}, {click_y})", clicked
                click_status = await _wait_for_click_status(client, initial_click)
                match = re.fullmatch(
                    r"click_status: button=right count=1 modifiers=none hold_ms=(\d+)",
                    click_status,
                )
                assert match is not None, click_status
                assert int(match.group(1)) < 500, click_status

                stop = await client.call_text("session_stop")
                connected = False
                assert stop == "Disconnected from live session.", stop
                assert not artifact_dir.exists(), artifact_dir
            finally:
                (visual.artifact_dir / "drag-cleanup-mcp-server.stderr.log").write_text(
                    client.stderr_text(), encoding="utf-8"
                )
                if connected:
                    await client.call_result("session_stop")

            assert artifact_dir is not None and not artifact_dir.exists()


@pytest.mark.anyio
async def test_invalid_key_and_button_are_tool_errors_without_killing_server() -> None:
    """Bad input names fail explicitly while the stdio server remains usable."""
    invalid_key = "invalid-key-sentinel"
    invalid_button = "sideways-button"

    async with running_mcp_server() as client:
        session_running = False
        try:
            start = await client.call_text(
                "session_start",
                {"screen_width": SCREEN_WIDTH, "screen_height": SCREEN_HEIGHT},
            )
            session_running = True
            assert "Session started." in start, start
            assert "Input backend: KWin EIS" in start, start

            for tool_name in ("keyboard_key", "keyboard_key_down", "keyboard_key_up"):
                key_result = await client.call_result(tool_name, {"key": invalid_key})
                key_error = _result_text(key_result)
                assert key_result.isError is True, (tool_name, key_error)
                assert f"Unknown key: {invalid_key}" in key_error, (tool_name, key_error)

            button_result = await client.call_result(
                "mouse_button_down",
                {
                    "x": SCREEN_WIDTH // 2,
                    "y": SCREEN_HEIGHT // 2,
                    "button": invalid_button,
                },
            )
            button_error = _result_text(button_result)
            assert button_result.isError is True, button_error
            assert invalid_button in button_error, button_error
            assert "button" in button_error.lower(), button_error

            assert await client.session.send_ping() is not None
            tools = await client.session.list_tools()
            assert {
                "keyboard_key",
                "keyboard_key_down",
                "keyboard_key_up",
                "mouse_click",
                "session_stop",
            } <= {tool.name for tool in tools.tools}
            valid_key = await client.call_text("keyboard_key", {"key": "escape"})
            assert valid_key == "Pressed: escape", valid_key
        finally:
            if session_running:
                assert await client.call_text("session_stop") == "Session stopped."
