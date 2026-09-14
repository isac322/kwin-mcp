"""Installed-package MCP stdio coverage for pointer and keyboard tools."""

from __future__ import annotations

import ast
import re
import shlex
import time
from typing import TYPE_CHECKING

import anyio
import pytest
from mcp_harness import running_mcp_server

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mcp_harness import McpTestClient

_RECT = r"\((-?\d+), (-?\d+), (\d+)x(\d+)\)"
_SCROLLBAR = re.compile(
    rf'\[scroll bar] "[^"]*" @ {_RECT} value=(-?\d+(?:\.\d+)?)/(-?\d+(?:\.\d+)?)'
)
POLL_INTERVAL_SECONDS = 0.1
INPUT_TIMEOUT_SECONDS = 5.0


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _single_rect(output: str, pattern: str) -> tuple[int, int, int, int]:
    matches = re.findall(pattern, output)
    assert len(matches) == 1, output[:1000]
    return tuple(int(value) for value in matches[0])  # type: ignore[return-value]


async def _global_element_rect(
    client: McpTestClient, app_name: str, role: str, name: str
) -> tuple[int, int, int, int]:
    elements = await client.call_text("find_ui_elements", {"query": name, "app_name": app_name})
    local_x, local_y, width, height = _single_rect(
        elements,
        rf'\[{re.escape(role)}] "{re.escape(name)}" @ {_RECT}',
    )
    geometry = await client.call_text("window_geometry", {"app_name": app_name})
    client_x, client_y, _, _ = _single_rect(geometry, rf"client:\s+{_RECT}")
    return client_x + local_x, client_y + local_y, width, height


async def _global_element_center(
    client: McpTestClient, app_name: str, role: str, name: str
) -> tuple[int, int]:
    x, y, width, height = await _global_element_rect(client, app_name, role, name)
    return x + width // 2, y + height // 2


async def _wait_for_binary(client: McpTestClient, expected: str) -> None:
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
    assert any(expected_label.match(line) for line in output.splitlines()), output[:1000]


def _focused_text(output: str) -> str:
    matches = re.findall(
        rf'^- \[text] "[^"]*" @ {_RECT}(?: text=(.*?))?(?: \[actions:.*])?$',
        output,
        re.MULTILINE,
    )
    assert len(matches) == 1, output[:1000]
    text_repr = matches[0][-1]
    return ast.literal_eval(text_repr) if text_repr else ""


async def _wait_for_focused_text(client: McpTestClient, expected: str) -> None:
    deadline = time.monotonic() + INPUT_TIMEOUT_SECONDS
    actual = ""
    output = ""
    while time.monotonic() < deadline:
        output = await client.call_text(
            "find_ui_elements",
            {"query": "text", "app_name": "kwrite", "states": ["focused"]},
        )
        if not output.startswith("No elements found"):
            actual = _focused_text(output)
            if actual == expected:
                return
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    assert actual == expected, output[:1000]


async def _wait_for_clipboard(client: McpTestClient, predicate: Callable[[str], bool]) -> str:
    deadline = time.monotonic() + INPUT_TIMEOUT_SECONDS
    text = ""
    while time.monotonic() < deadline:
        text = await client.call_text("clipboard_get")
        if predicate(text):
            return text
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"clipboard never reached expected value: {text!r}")


def _scroll_position(output: str) -> float:
    candidates: list[float] = []
    for match in _SCROLLBAR.finditer(output):
        _, _, width, height, value, maximum = match.groups()
        width_int, height_int = int(width), int(height)
        if float(maximum) <= 0 or width_int == 0 or height_int == 0:
            continue
        if height_int > width_int:
            candidates.append(float(value))
    assert candidates, output[:1500]
    return max(candidates)


async def _wait_for_scroll_change(
    client: McpTestClient,
    *,
    previous: float,
    increasing: bool,
) -> float:
    deadline = time.monotonic() + INPUT_TIMEOUT_SECONDS
    position = previous
    output = ""
    while time.monotonic() < deadline:
        output = await client.call_text("find_ui_elements", {"query": "", "app_name": "kwrite"})
        position = _scroll_position(output)
        if (position > previous) if increasing else (position < previous):
            return position
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    direction = "increase" if increasing else "decrease"
    raise AssertionError(
        f"scrollbar did not {direction} from {previous}: {position}\n{output[:1500]}"
    )


@pytest.mark.anyio
async def test_pointer_and_keyboard_tools_cross_installed_mcp_stdio(tmp_path: Path) -> None:
    editor_path = tmp_path / "kwin-mcp-pointer-keyboard.txt"
    editor_path.write_text("", encoding="utf-8")
    long_path = tmp_path / "kwin-mcp-scroll.txt"
    long_path.write_text(
        "\n".join(f"line {index:03d} " + "-" * 320 for index in range(180)),
        encoding="utf-8",
    )

    async with running_mcp_server() as client:
        try:
            start = await client.call_text(
                "session_start",
                {
                    "app_command": "kcalc",
                    "screen_width": 1280,
                    "screen_height": 800,
                    "enable_clipboard": True,
                    "isolate_home": True,
                },
            )
            assert "Input backend: KWin EIS" in start, start
            await client.call_text(
                "wait_for_element",
                {"query": "Seven", "app_name": "kcalc", "timeout_ms": 5000},
            )

            seven = await _global_element_center(client, "kcalc", "button", "Seven")
            await client.call_text(
                "mouse_button_down", {"x": seven[0], "y": seven[1], "button": "left"}
            )
            pressed_tree = await client.call_text("accessibility_tree", {"app_name": "kcalc"})
            assert re.search(r'\[button] "Seven" \([^)]*\bpressed\b', pressed_tree), pressed_tree[
                :1000
            ]
            await client.call_text(
                "mouse_button_up", {"x": seven[0], "y": seven[1], "button": "left"}
            )
            await _wait_for_binary(client, "111")

            zero = await _global_element_center(client, "kcalc", "button", "Zero")
            await client.call_text(
                "mouse_click",
                {
                    "x": zero[0],
                    "y": zero[1],
                    "double": True,
                    "hold_ms": 30,
                },
            )
            await _wait_for_binary(client, "1010111100")

            await client.call_text(
                "launch_app", {"command": f"kwrite {shlex.quote(str(editor_path))}"}
            )
            await client.call_text(
                "wait_for_element",
                {"query": editor_path.name, "app_name": "kwrite", "timeout_ms": 5000},
            )
            await client.call_text("focus_window", {"app_name": "kwrite"})

            await client.call_text("keyboard_type", {"text": "alpha beta gamma"})
            await client.call_text("keyboard_key", {"key": "Return"})
            await client.call_text("keyboard_type_unicode", {"text": "한글"})
            await client.call_text("keyboard_key_down", {"key": "shift"})
            await client.call_text("keyboard_key", {"key": "a"})
            await client.call_text("keyboard_key_up", {"key": "shift"})
            expected_text = "alpha beta gamma\n한글A"
            await _wait_for_focused_text(client, expected_text)

            file_menu = await _global_element_center(client, "kwrite", "menu item", "File")
            edit_menu = await _global_element_center(client, "kwrite", "menu item", "Edit")
            await client.call_text("mouse_click", {"x": file_menu[0], "y": file_menu[1]})
            file_menu_item = await client.call_text(
                "wait_for_element",
                {
                    "query": "Save As",
                    "app_name": "kwrite",
                    "expected_states": ["showing"],
                    "timeout_ms": 3000,
                },
            )
            assert '[menu item] "Save As…"' in file_menu_item, file_menu_item
            select_all_before_hover = await client.call_text(
                "find_ui_elements",
                {
                    "query": "Select All",
                    "app_name": "kwrite",
                    "states": ["showing"],
                },
            )
            assert select_all_before_hover.startswith("No elements found"), select_all_before_hover

            await client.call_text("mouse_move", {"x": edit_menu[0], "y": edit_menu[1]})
            select_all_after_hover = await client.call_text(
                "wait_for_element",
                {
                    "query": "Select All",
                    "app_name": "kwrite",
                    "expected_states": ["showing"],
                    "timeout_ms": 3000,
                },
            )
            assert "Select All" in select_all_after_hover, select_all_after_hover
            await client.call_text("keyboard_key", {"key": "Escape"})

            text_x, text_y, text_width, _ = await _global_element_rect(
                client, "kwrite", "text", editor_path.name
            )
            first_line_y = text_y + 12
            first_word_x = text_x + 20
            await client.call_text(
                "mouse_click",
                {"x": first_word_x, "y": first_line_y, "double": True},
            )
            await client.call_text("keyboard_key", {"key": "ctrl+c"})
            selected_word = await _wait_for_clipboard(client, lambda text: text == "alpha")
            assert selected_word == "alpha"

            await client.call_text("keyboard_key", {"key": "ctrl+home"})
            await client.call_text(
                "mouse_click",
                {
                    "x": text_x + min(text_width - 20, 155),
                    "y": first_line_y,
                    "modifiers": ["shift"],
                },
            )
            await client.call_text("keyboard_key", {"key": "ctrl+c"})
            shifted_selection = await _wait_for_clipboard(client, lambda text: "alpha beta" in text)
            assert shifted_selection in expected_text

            await client.call_text(
                "mouse_click",
                {"x": first_word_x, "y": first_line_y, "button": "right"},
            )
            context_menu = await client.call_text(
                "wait_for_element",
                {
                    "query": "Select All",
                    "app_name": "kwrite",
                    "expected_states": ["showing"],
                    "timeout_ms": 3000,
                },
            )
            assert "Select All" in context_menu, context_menu
            await client.call_text("keyboard_key", {"key": "Escape"})

            drag_end_x = text_x + min(text_width - 20, 150)
            await client.call_text(
                "mouse_drag",
                {
                    "from_x": text_x + 3,
                    "from_y": first_line_y,
                    "to_x": drag_end_x,
                    "to_y": first_line_y,
                    "waypoints": [
                        [text_x + 55, first_line_y, 20],
                        [text_x + 105, first_line_y, 20],
                    ],
                },
            )
            await client.call_text("clipboard_set", {"text": "drag-selection-pending"})
            await client.call_text("keyboard_key", {"key": "ctrl+c"})
            dragged_selection = await _wait_for_clipboard(
                client,
                lambda text: len(text) >= 8 and text in "alpha beta gamma",
            )
            assert dragged_selection in "alpha beta gamma"

            await client.call_text("keyboard_key", {"key": "ctrl+s"})
            await client.call_text("keyboard_key", {"key": "ctrl+w"})
            await anyio.sleep(0.5)

            await client.call_text(
                "launch_app", {"command": f"kwrite {shlex.quote(str(long_path))}"}
            )
            await client.call_text(
                "wait_for_element",
                {"query": long_path.name, "app_name": "kwrite", "timeout_ms": 5000},
            )
            await client.call_text("focus_window", {"app_name": "kwrite"})
            scroll_x, scroll_y, scroll_width, scroll_height = await _global_element_rect(
                client, "kwrite", "text", long_path.name
            )
            scroll_target = (
                scroll_x + scroll_width // 2,
                scroll_y + scroll_height // 2,
            )
            initial_elements = await client.call_text(
                "find_ui_elements", {"query": "", "app_name": "kwrite"}
            )
            vertical_start = _scroll_position(initial_elements)

            await client.call_text(
                "mouse_scroll",
                {
                    "x": scroll_target[0],
                    "y": scroll_target[1],
                    "delta": 10,
                    "steps": 4,
                },
            )
            vertical_down = await _wait_for_scroll_change(
                client,
                previous=vertical_start,
                increasing=True,
            )
            await client.call_text(
                "mouse_scroll",
                {
                    "x": scroll_target[0],
                    "y": scroll_target[1],
                    "delta": -5,
                    "steps": 2,
                },
            )
            vertical_up = await _wait_for_scroll_change(
                client,
                previous=vertical_down,
                increasing=False,
            )

            # Horizontal semantics are exercised by the dedicated deterministic GUI probe test.
            await client.call_text(
                "mouse_scroll",
                {
                    "x": scroll_target[0],
                    "y": scroll_target[1],
                    "delta": 8,
                    "discrete": True,
                },
            )
            vertical_discrete_down = await _wait_for_scroll_change(
                client,
                previous=vertical_up,
                increasing=True,
            )
            await client.call_text(
                "mouse_scroll",
                {
                    "x": scroll_target[0],
                    "y": scroll_target[1],
                    "delta": -4,
                    "discrete": True,
                },
            )
            await _wait_for_scroll_change(
                client,
                previous=vertical_discrete_down,
                increasing=False,
            )
        finally:
            await client.call_result("session_stop")
