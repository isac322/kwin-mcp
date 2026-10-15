"""Installed-package MCP stdio coverage for the pointer, keyboard and touch tools.

The 0.11 suite drove the fourteen pointer/keyboard tools through KCalc and
KWrite over stdio. The same scenario now runs on the 1.0 tools: pointer_hold,
pointer_click (count, hold_ms, modifiers, right button), pointer_move,
pointer_drag (waypoints), pointer_scroll (smooth steps and detents),
keyboard_type (keys and unicode), keyboard_press, keyboard_hold and touch_tap.
Every effect is read back by the test itself: the accessibility tree through
``atspi_oracle`` and the clipboard through ``wl-paste`` on the session socket.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from typing import TYPE_CHECKING, Any, cast

import anyio
import pytest
from atspi_oracle import AtspiOracle
from conftest import APP_WAIT_TIMEOUT_MS
from kwin_oracle import KWinOracle
from mcp_harness import running_mcp_server

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from atspi_oracle import AtspiNode
    from kwin_oracle import OracleWindow
    from mcp_harness import McpTestClient

    from kwin_mcp.models import (
        ElementList,
        KeyboardHoldResult,
        KeyboardTypeResult,
        PointerHoldResult,
        SessionInfo,
        UIElement,
    )

POLL_INTERVAL_SECONDS = 0.1
INPUT_TIMEOUT_SECONDS = 5.0
WL_PASTE = shutil.which("wl-paste")


@pytest.fixture
def anyio_backend() -> str:
    """The MCP stdio client is exercised on asyncio."""
    return "asyncio"


def _binary(expected: str) -> re.Pattern[str]:
    # KCalc 26.08 groups binary digits in fours from the right; older releases do not.
    return re.compile(" ?".join(re.escape(digit) for digit in expected))


def _wait_for_binary(atspi: AtspiOracle, expected: str) -> None:
    pattern = _binary(expected)
    atspi.wait_for(
        lambda ns: any(
            n.role == "label" and n.name is not None and pattern.fullmatch(n.name) for n in ns
        ),
        app="kcalc",
        timeout_s=INPUT_TIMEOUT_SECONDS,
    )


def _focused_text(nodes: list[AtspiNode]) -> str | None:
    focused = [n for n in nodes if n.role == "text" and "focused" in n.states]
    return focused[0].text if len(focused) == 1 else None


def _showing(nodes: list[AtspiNode], prefix: str) -> bool:
    return any(
        n.role == "menu item"
        and n.name is not None
        and n.name.startswith(prefix)
        and "showing" in n.states
        for n in nodes
    )


def _wait_for_menu(atspi: AtspiOracle, prefix: str) -> list[AtspiNode]:
    """Wait until a showing KWrite menu item's name starts with ``prefix``."""
    deadline = time.monotonic() + INPUT_TIMEOUT_SECONDS
    nodes = atspi.walk("kwrite")
    while not _showing(nodes, prefix):
        if time.monotonic() >= deadline:
            shown = [n.name for n in nodes if n.role == "menu item" and "showing" in n.states]
            msg = f"no showing menu item {prefix!r}; showing menu items: {shown}"
            raise AssertionError(msg)
        time.sleep(POLL_INTERVAL_SECONDS)
        nodes = atspi.walk("kwrite")
    return nodes


def _context_menu_at(window: OracleWindow, x: int, y: int) -> bool:
    """A KWrite popup whose top-left corner is the click point (Qt context menus open there).

    Newer KWin cascades windows to fractional logical positions (e.g. 342.67), so the
    popup origin can sit up to one pixel from the integer click point.
    """
    return (
        "kwrite" in window.resource_class
        and window.kind == "popup"
        and window.frame is not None
        and abs(window.frame.x - x) < 1
        and abs(window.frame.y - y) < 1
    )


async def _clipboard(socket: str, predicate: Callable[[str], bool]) -> str:
    """The session clipboard as ``wl-paste`` reads it, once ``predicate`` holds."""
    assert WL_PASTE is not None
    env = {**os.environ, "WAYLAND_DISPLAY": socket}
    deadline = time.monotonic() + INPUT_TIMEOUT_SECONDS
    text = ""
    while time.monotonic() < deadline:
        pasted = subprocess.run(
            [WL_PASTE, "--no-newline"], env=env, capture_output=True, timeout=5, check=False
        )
        text = pasted.stdout.decode(errors="replace")
        if pasted.returncode == 0 and predicate(text):
            return text
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"clipboard never reached expected value: {text!r}")


async def _element(client: McpTestClient, locator: dict[str, Any]) -> UIElement:
    """The one element named exactly ``locator["query"]``, waiting until it is present."""
    found = cast(
        "ElementList",
        await client.call_model(
            "element_query", {"locator": locator, "wait": {"timeout_ms": APP_WAIT_TIMEOUT_MS}}
        ),
    )
    assert found.wait is not None and found.wait.satisfied, found.render()
    # ``query`` is a substring match; the old exact-name lookups compare whole names.
    named = [e for e in found.elements if e.name == locator["query"]]
    assert len(named) == 1, found.render()
    return named[0]


def _kcalc_key(name: str) -> dict[str, Any]:
    # Hidden keys of other KCalc modes describe themselves with digit words too.
    return {"app": "kcalc", "query": name, "role": "button", "states": ["showing"]}


def _center(element: UIElement) -> dict[str, int]:
    assert element.rect is not None, element
    rect = element.rect
    return {"x": rect.x + rect.width // 2, "y": rect.y + rect.height // 2}


def _vertical_scroll(nodes: list[AtspiNode]) -> float:
    candidates = [
        n.value
        for n in nodes
        if n.role == "scroll bar"
        and n.value is not None
        and n.value_max is not None
        and n.value_max > 0
        and n.window is not None
        and n.window[3] > n.window[2] > 0
    ]
    assert candidates, [(n.role, n.name, n.window, n.value) for n in nodes if n.role]
    return max(cast("list[float]", candidates))


def _wait_for_scroll(atspi: AtspiOracle, *, previous: float, increasing: bool) -> float:
    def moved(nodes: list[AtspiNode]) -> bool:
        position = _vertical_scroll(nodes)
        return position > previous if increasing else position < previous

    return _vertical_scroll(atspi.wait_for(moved, app="kwrite", timeout_s=INPUT_TIMEOUT_SECONDS))


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
            info = cast(
                "SessionInfo",
                await client.call_model(
                    "session_start",
                    {
                        "screen_width": 1280,
                        "screen_height": 800,
                        "enable_clipboard": True,
                        "home": "isolated",
                    },
                ),
            )
            assert info.input_backend == "eis", info
            atspi = AtspiOracle(info.dbus_address)
            oracle = KWinOracle(info.dbus_address)
            await client.call_model("app_launch", {"command": "kcalc"})

            # pointer_hold: press, observe the pressed key, release.
            seven = await _element(client, _kcalc_key("Seven"))
            held = cast(
                "PointerHoldResult",
                await client.call_model(
                    "pointer_hold", {"phase": "press", "target": {"ref": seven.ref}}
                ),
            )
            assert held.held.buttons == ["left"], held.render()
            atspi.wait_for(
                lambda ns: any(n.name == "Seven" and "pressed" in n.states for n in ns),
                app="kcalc",
                timeout_s=INPUT_TIMEOUT_SECONDS,
            )
            released = cast(
                "PointerHoldResult",
                await client.call_model(
                    "pointer_hold", {"phase": "release", "target": {"ref": seven.ref}}
                ),
            )
            assert released.held.buttons == [], released.render()
            _wait_for_binary(atspi, "111")

            # pointer_click: a double click with a held last press enters two zeros.
            zero = await _element(client, _kcalc_key("Zero"))
            await client.call_model(
                "pointer_click", {"target": _center(zero), "count": 2, "hold_ms": 30}
            )
            _wait_for_binary(atspi, "1010111100")

            # touch_tap: one tap on a key enters one digit.
            one = await _element(client, _kcalc_key("One"))
            await client.call_model("touch_tap", {"target": {"ref": one.ref}})
            _wait_for_binary(atspi, "1101101011001")

            await client.call_model(
                "app_launch", {"command": f"kwrite {shlex.quote(str(editor_path))}"}
            )
            await _element(client, {"app": "kwrite", "query": editor_path.name, "role": "text"})
            await client.call_model(
                "window_focus", {"window": {"app_id": "kwrite", "caption": editor_path.name}}
            )

            # keyboard_type (keys and unicode), keyboard_press and keyboard_hold.
            await client.call_model("keyboard_type", {"text": "alpha beta gamma"})
            await client.call_model("keyboard_press", {"keys": "Return"})
            typed = cast(
                "KeyboardTypeResult",
                await client.call_model("keyboard_type", {"text": "한글", "method": "unicode"}),
            )
            assert typed.route, typed.render()
            shift = cast(
                "KeyboardHoldResult",
                await client.call_model("keyboard_hold", {"phase": "press", "keys": "shift"}),
            )
            assert shift.held.keys, shift.render()
            await client.call_model("keyboard_press", {"keys": "a"})
            unshift = cast(
                "KeyboardHoldResult",
                await client.call_model("keyboard_hold", {"phase": "release", "keys": "shift"}),
            )
            assert unshift.held.keys == [], unshift.render()
            expected_text = "alpha beta gamma\n한글A"
            atspi.wait_for(
                lambda ns: _focused_text(ns) == expected_text,
                app="kwrite",
                timeout_s=INPUT_TIMEOUT_SECONDS,
            )

            # pointer_click opens the File menu; pointer_move hovers over Edit.
            file_menu = await _element(
                client, {"app": "kwrite", "query": "File", "role": "menu item"}
            )
            edit_menu = await _element(
                client, {"app": "kwrite", "query": "Edit", "role": "menu item"}
            )
            await client.call_model("pointer_click", {"target": _center(file_menu)})
            nodes = _wait_for_menu(atspi, "Save As")
            # KWrite 25+ keeps Select All in Edit > Selection; Undo is a top Edit item.
            assert not _showing(nodes, "Undo"), [n.name for n in nodes]
            await client.call_model("pointer_move", {"target": _center(edit_menu)})
            _wait_for_menu(atspi, "Undo")
            await client.call_model("keyboard_press", {"keys": "Escape"})

            text = await _element(
                client, {"app": "kwrite", "query": editor_path.name, "role": "text"}
            )
            assert text.rect is not None, text
            text_x, text_y, text_width = text.rect.x, text.rect.y, text.rect.width
            first_line_y = text_y + 12
            first_word_x = text_x + 20

            # A double click selects a word.
            await client.call_model(
                "pointer_click", {"target": {"x": first_word_x, "y": first_line_y}, "count": 2}
            )
            await client.call_model("keyboard_press", {"keys": "ctrl+c"})
            selected = await _clipboard(info.wayland_socket, lambda t: t == "alpha")
            assert selected == "alpha"

            # A shift+click extends the selection from the caret.
            await client.call_model("keyboard_press", {"keys": "ctrl+Home"})
            await client.call_model(
                "pointer_click",
                {
                    "target": {"x": text_x + min(text_width - 20, 155), "y": first_line_y},
                    "modifiers": "shift",
                },
            )
            await client.call_model("keyboard_press", {"keys": "ctrl+c"})
            shifted = await _clipboard(info.wayland_socket, lambda t: "alpha beta" in t)
            assert shifted in expected_text

            # A right click opens the context menu.
            await client.call_model(
                "pointer_click",
                {"target": {"x": first_word_x, "y": first_line_y}, "button": "right"},
            )
            # KWrite's context menu is not exposed over AT-SPI; KWin shows it as a
            # popup opened at the click point.
            oracle.wait_for_windows(
                lambda ws: any(_context_menu_at(w, first_word_x, first_line_y) for w in ws)
            )
            await client.call_model("keyboard_press", {"keys": "Escape"})
            oracle.wait_for_windows(lambda ws: not any(w.kind == "popup" for w in ws))

            # pointer_drag through waypoints selects text.
            drag_end_x = text_x + min(text_width - 20, 150)
            await client.call_model(
                "pointer_drag",
                {
                    "from": {"x": text_x + 3, "y": first_line_y},
                    "to": {"x": drag_end_x, "y": first_line_y},
                    "waypoints": [
                        {"x": text_x + 55, "y": first_line_y, "dwell_ms": 20},
                        {"x": text_x + 105, "y": first_line_y, "dwell_ms": 20},
                    ],
                },
            )
            await client.call_model("clipboard_write", {"text": "drag-selection-pending"})
            await client.call_model("keyboard_press", {"keys": "ctrl+c"})
            dragged = await _clipboard(
                info.wayland_socket, lambda t: len(t) >= 8 and t in "alpha beta gamma"
            )
            assert dragged in "alpha beta gamma"

            await client.call_model("keyboard_press", {"keys": "ctrl+s"})
            await client.call_model("keyboard_press", {"keys": "ctrl+w"})
            await anyio.sleep(0.5)

            # pointer_scroll: smooth steps and detents, both directions.
            await client.call_model(
                "app_launch", {"command": f"kwrite {shlex.quote(str(long_path))}"}
            )
            scroll_text = await _element(
                client, {"app": "kwrite", "query": long_path.name, "role": "text"}
            )
            await client.call_model(
                "window_focus", {"window": {"app_id": "kwrite", "caption": long_path.name}}
            )
            target = {"ref": scroll_text.ref}
            start = _vertical_scroll(atspi.walk("kwrite"))

            await client.call_model("pointer_scroll", {"target": target, "delta": 10, "steps": 4})
            down = _wait_for_scroll(atspi, previous=start, increasing=True)
            await client.call_model("pointer_scroll", {"target": target, "delta": -5, "steps": 2})
            up = _wait_for_scroll(atspi, previous=down, increasing=False)
            await client.call_model(
                "pointer_scroll", {"target": target, "delta": 8, "unit": "detent"}
            )
            detent_down = _wait_for_scroll(atspi, previous=up, increasing=True)
            await client.call_model(
                "pointer_scroll", {"target": target, "delta": -4, "unit": "detent"}
            )
            _wait_for_scroll(atspi, previous=detent_down, increasing=False)
        finally:
            await client.call_result("session_stop")
