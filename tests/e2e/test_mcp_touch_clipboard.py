"""Installed-package MCP stdio coverage for touch and clipboard tools."""

from __future__ import annotations

import ast
import re
import shlex
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol

import anyio
import pytest
from _asserts import element_count
from mcp_harness import running_mcp_server

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800
POLL_INTERVAL_SECONDS = 0.1
INPUT_TIMEOUT_SECONDS = 4.0
CLIPBOARD_DISABLED_HINT = (
    "Clipboard not enabled. Pass enable_clipboard=True to session_start, "
    "or use session_connect (clipboard is always enabled for live sessions)."
)
_RECT = r"\((-?\d+), (-?\d+), (\d+)x(\d+)\)"
_SCROLLBAR = re.compile(
    rf'\[scroll bar] "[^"]*" @ screen {_RECT} value=(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)'
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class McpClient(Protocol):
    async def call_text(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> str: ...

    async def call_result(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any: ...


@asynccontextmanager
async def _virtual_session(
    *,
    app_command: str = "",
    enable_clipboard: bool = False,
) -> AsyncIterator[McpClient]:
    async with running_mcp_server() as client:
        try:
            output = await client.call_text(
                "session_start",
                {
                    "app_command": app_command,
                    "screen_width": SCREEN_WIDTH,
                    "screen_height": SCREEN_HEIGHT,
                    "enable_clipboard": enable_clipboard,
                    "isolate_home": True,
                },
            )
            assert "Session started" in output, output
            assert "Input backend: KWin EIS" in output, output
            yield client
        finally:
            await client.call_result("session_stop")


def _rect(output: str, pattern: str) -> tuple[int, int, int, int]:
    matches = re.findall(pattern, output)
    assert len(matches) == 1, output[:500]
    x, y, width, height = matches[0]
    return int(x), int(y), int(width), int(height)


async def _wait_for_app(client: McpClient, app_name: str, query: str = "") -> str:
    output = await client.call_text(
        "wait_for_element",
        {
            "query": query,
            "app_name": app_name,
            "timeout_ms": 15000,
            "poll_interval_ms": 200,
        },
    )
    assert element_count(output) > 0, output[:500]
    return output


async def _global_element_center(
    client: McpClient,
    app_name: str,
    role: str,
    name: str,
) -> tuple[int, int]:
    elements = await client.call_text(
        "find_ui_elements",
        {"query": name, "app_name": app_name},
    )
    assert element_count(elements) > 0, elements[:500]
    # find_ui_elements already reports global screen coordinates.
    x, y, width, height = _rect(
        elements,
        rf'\[{re.escape(role)}\] "{re.escape(name)}" @ screen {_RECT}',
    )
    return x + width // 2, y + height // 2


async def _wait_for_binary(client: McpClient, expected: str) -> None:
    deadline = time.monotonic() + INPUT_TIMEOUT_SECONDS
    expected_label = re.compile(rf'^- \[label\] "{re.escape(expected)}"(?: |$)')
    output = ""
    while time.monotonic() < deadline:
        output = await client.call_text(
            "find_ui_elements", {"query": expected, "app_name": "kcalc"}
        )
        if any(expected_label.match(line) for line in output.splitlines()):
            return
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    assert any(expected_label.match(line) for line in output.splitlines()), output[:500]


async def _focused_text(client: McpClient, app_name: str = "kwrite") -> str:
    output = await client.call_text(
        "find_ui_elements",
        {"query": "text", "app_name": app_name, "states": ["focused"]},
    )
    if output.startswith("No elements found"):
        return ""
    assert element_count(output) > 0, output[:500]
    matches = re.findall(
        rf'^- \[text] "[^"]*" @ (?:screen {_RECT}|unavailable \([^)]+\))'
        rf"(?: text=(.*?))?(?: \[actions:.*])?$",
        output,
        re.MULTILINE,
    )
    assert len(matches) == 1, output[:500]
    text_repr = matches[0][-1]
    return ast.literal_eval(text_repr) if text_repr else ""


async def _wait_for_text(client: McpClient, expected: str) -> None:
    deadline = time.monotonic() + INPUT_TIMEOUT_SECONDS
    actual = ""
    while time.monotonic() < deadline:
        actual = await _focused_text(client)
        if actual == expected:
            return
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    assert actual == expected


async def _text_area(
    client: McpClient,
    app_name: str,
    name: str,
) -> tuple[int, int, int, int]:
    elements = await client.call_text(
        "find_ui_elements",
        {"query": name, "app_name": app_name},
    )
    return _rect(elements, rf'\[text\] "{re.escape(name)}" @ screen {_RECT}')


async def _scroll_position(client: McpClient, app_name: str = "kwrite") -> float:
    elements = await client.call_text(
        "find_ui_elements",
        {"query": "", "app_name": app_name},
    )
    candidates: list[float] = []
    for match in _SCROLLBAR.finditer(elements):
        _, _, width, height, value, maximum = match.groups()
        width_int, height_int = int(width), int(height)
        if float(maximum) > 0 and width_int > 0 and height_int > width_int:
            candidates.append(float(value))
    assert candidates, f"no vertical scrollbar reported a usable value: {elements[:500]}"
    return max(candidates)


async def _wait_for_scroll(
    client: McpClient,
    predicate: Callable[[float], bool],
    message: str,
) -> float:
    deadline = time.monotonic() + INPUT_TIMEOUT_SECONDS
    while True:
        position = await _scroll_position(client)
        if predicate(position):
            return position
        if time.monotonic() >= deadline:
            raise AssertionError(f"{message}: scrollbar value={position}")
        await anyio.sleep(POLL_INTERVAL_SECONDS)


async def _reset_scroll(client: McpClient) -> None:
    await client.call_text("keyboard_key", {"key": "ctrl+home"})
    await _wait_for_scroll(client, lambda value: value == 0.0, "editor did not return to the top")


async def _pinch_selection(
    client: McpClient,
    *,
    center_x: int,
    center_y: int,
    start_distance: int,
    end_distance: int,
) -> str:
    # Collapse any selection left by the previous pinch before measuring what
    # Ctrl+C does with no selection under the current KWrite configuration.
    await client.call_text("keyboard_key", {"key": "right"})
    sentinel = f"no-selection-{start_distance}-{end_distance}"
    assert await client.call_text("clipboard_set", {"text": sentinel}) == (
        f"Clipboard set: {sentinel!r}"
    )
    await client.call_text("keyboard_key", {"key": "ctrl+c"})
    await anyio.sleep(0.3)
    no_selection_clipboard = await client.call_text("clipboard_get")

    output = await client.call_text(
        "touch_pinch",
        {
            "center_x": center_x,
            "center_y": center_y,
            "start_distance": start_distance,
            "end_distance": end_distance,
            "duration_ms": 400,
        },
    )
    direction = "in" if end_distance < start_distance else "out"
    assert output == (
        f"Pinch {direction} at ({center_x}, {center_y}): {start_distance}→{end_distance}px"
    )
    await anyio.sleep(1.2)
    await client.call_text("keyboard_key", {"key": "ctrl+c"})
    await anyio.sleep(0.8)

    selection = await client.call_text("clipboard_get")
    assert selection != no_selection_clipboard, (
        f"pinch {direction} produced no application selection"
    )
    assert len(selection) >= 8, repr(selection)
    return selection


async def test_clipboard_disabled_guards_return_without_hanging() -> None:
    async with running_mcp_server() as client:
        session_running = False
        try:
            output = await client.call_text(
                "session_start",
                {
                    "screen_width": SCREEN_WIDTH,
                    "screen_height": SCREEN_HEIGHT,
                    "isolate_home": True,
                },
            )
            session_running = True
            assert "Session started" in output, output

            with anyio.fail_after(2):
                get_output = await client.call_text("clipboard_get")
            assert get_output == CLIPBOARD_DISABLED_HINT

            with anyio.fail_after(2):
                set_output = await client.call_text("clipboard_set", {"text": "disabled"})
            assert set_output == CLIPBOARD_DISABLED_HINT
        finally:
            if session_running:
                await client.call_result("session_stop")


async def test_clipboard_roundtrip_and_paste_cross_stdio(tmp_path: Path) -> None:
    document = tmp_path / "kwin-mcp-mcp-clipboard.txt"
    async with _virtual_session(
        app_command=f"kwrite {shlex.quote(str(document))}",
        enable_clipboard=True,
    ) as client:
        await _wait_for_app(client, "kwrite", document.name)
        await client.call_text("focus_window", {"app_name": "kwrite"})
        payload = "Clipboard MCP QA\n한글\t42!"

        assert await client.call_text("clipboard_set", {"text": payload}) == (
            f"Clipboard set: {payload!r}"
        )
        assert await client.call_text("clipboard_get") == payload

        await client.call_text("keyboard_key", {"key": "ctrl+v"})
        await _wait_for_text(client, payload)


async def test_touch_wrappers_change_gui_state_and_report_kwin_limits(tmp_path: Path) -> None:
    document = tmp_path / "kwin-mcp-mcp-touch.txt"
    document.write_text(
        "\n".join(f"line {index:03d} ---------------------------" for index in range(200))
    )

    async with _virtual_session(app_command="kcalc", enable_clipboard=True) as client:
        await _wait_for_app(client, "kcalc", "Seven")
        seven = await _global_element_center(client, "kcalc", "button", "Seven")
        tap_output = await client.call_text(
            "touch_tap",
            {"x": seven[0], "y": seven[1], "hold_ms": 650},
        )
        assert tap_output == f"Touch tap at {seven} held 650ms"
        await _wait_for_binary(client, "111")

        launch_output = await client.call_text(
            "launch_app",
            {"command": f"kwrite {shlex.quote(str(document))}"},
        )
        assert "App launched: kwrite" in launch_output, launch_output
        await _wait_for_app(client, "kwrite", document.name)
        await client.call_text("focus_window", {"app_name": "kwrite"})
        await anyio.sleep(1.0)
        text_x, text_y, text_width, text_height = await _text_area(
            client,
            "kwrite",
            document.name,
        )
        center_x = text_x + text_width // 2
        center_y = text_y + text_height // 2
        from_y = text_y + (text_height * 3) // 4
        to_y = text_y + text_height // 4

        assert await _scroll_position(client) == 0.0
        swipe_output = await client.call_text(
            "touch_swipe",
            {
                "from_x": center_x,
                "from_y": from_y,
                "to_x": center_x,
                "to_y": to_y,
                "duration_ms": 350,
            },
        )
        assert swipe_output == (
            f"Touch swipe from ({center_x}, {from_y}) to ({center_x}, {to_y}) in 350ms"
        )
        await _wait_for_scroll(
            client,
            lambda value: value > 0.0,
            "single-finger swipe did not scroll",
        )

        # The image has no pinch-zoom application. KWrite exposes delivery through
        # text selection, so run both directions and read each selection from the clipboard.
        await _pinch_selection(
            client,
            center_x=center_x,
            center_y=center_y,
            start_distance=80,
            end_distance=320,
        )
        await _pinch_selection(
            client,
            center_x=center_x,
            center_y=center_y,
            start_distance=320,
            end_distance=80,
        )

        # KWin passes the two-finger stream to the app in this setup.
        await _reset_scroll(client)
        output = await client.call_text(
            "touch_multi_swipe",
            {
                "from_x": center_x,
                "from_y": from_y,
                "to_x": center_x,
                "to_y": to_y,
                "fingers": 2,
                "duration_ms": 400,
            },
        )
        assert output == (
            f"2-finger swipe from ({center_x}, {from_y}) to ({center_x}, {to_y}) in 400ms"
        )
        await _wait_for_scroll(
            client,
            lambda value: value > 0.0,
            "2-finger application swipe did not scroll",
        )

        # KWin consumes three-, four-, and five-finger swipes as compositor gestures. The
        # wrapper still reports each injected gesture, while KWrite's scroll value stays put.
        for fingers in (3, 4, 5):
            await _reset_scroll(client)
            output = await client.call_text(
                "touch_multi_swipe",
                {
                    "from_x": center_x,
                    "from_y": from_y,
                    "to_x": center_x,
                    "to_y": to_y,
                    "fingers": fingers,
                    "duration_ms": 400,
                },
            )
            assert output == (
                f"{fingers}-finger swipe from ({center_x}, {from_y}) "
                f"to ({center_x}, {to_y}) in 400ms"
            )
            await anyio.sleep(1.0)
            assert await _scroll_position(client) == 0.0, (
                f"KWin-reserved {fingers}-finger gesture unexpectedly reached KWrite"
            )
