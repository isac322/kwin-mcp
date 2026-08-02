"""End-to-end contracts for pointer, keyboard, touch, and clipboard injection."""

from __future__ import annotations

import ast
import re
import time
from typing import TYPE_CHECKING

from _asserts import element_count

if TYPE_CHECKING:
    from collections.abc import Callable

    from kwin_mcp.core import AutomationEngine

_RECT = r"\((-?\d+), (-?\d+), (\d+)x(\d+)\)"
_DYNAMIC_LABEL = re.compile(
    r'^\s*- \[label] "([^"]*)" \(enabled, sensitive, read-only\) @ \(0, 0, 0x0\)$'
)
POLL_INTERVAL_SECONDS = 0.1
INPUT_TIMEOUT_SECONDS = 3.0


def _rect(output: str, pattern: str) -> tuple[int, int, int, int]:
    matches = re.findall(pattern, output)
    assert len(matches) == 1, output[:500]
    return tuple(int(value) for value in matches[0])  # type: ignore[return-value]


def _global_element_center(
    engine: AutomationEngine, app_name: str, role: str, name: str
) -> tuple[int, int]:
    elements = engine.find_ui_elements(query=name, app_name=app_name)
    assert element_count(elements) > 0, elements[:500]
    local_x, local_y, width, height = _rect(
        elements,
        rf'\[{re.escape(role)}\] "{re.escape(name)}" @ {_RECT}',
    )

    geometry = engine.window_geometry(app_name=app_name)
    client_x, client_y, _, _ = _rect(geometry, rf"client:\s+{_RECT}")
    return client_x + local_x + width // 2, client_y + local_y + height // 2


def _binary_value(tree: str) -> str | None:
    lines = tree.splitlines()
    for index, line in enumerate(lines):
        if index == 0 or not re.search(r'\[label] "2" \(enabled, sensitive, read-only\)', line):
            continue
        match = _DYNAMIC_LABEL.match(lines[index - 1])
        assert match is not None, tree[:500]
        return match.group(1)
    return None


def _wait_for_binary(engine: AutomationEngine, expected: str) -> None:
    deadline = time.monotonic() + INPUT_TIMEOUT_SECONDS
    actual: str | None = None
    tree = ""
    while time.monotonic() < deadline:
        tree = engine.accessibility_tree(app_name="kcalc")
        actual = _binary_value(tree)
        if actual == expected:
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    assert actual == expected, tree[:500]


def _focused_text(engine: AutomationEngine, app_name: str = "kwrite") -> str:
    output = engine.find_ui_elements(
        query="text",
        app_name=app_name,
        states=["focused"],
    )
    assert element_count(output) > 0, output[:500]
    matches = re.findall(
        rf'^- \[text] "[^"]*" @ {_RECT}(?: text=(.*?))?(?: \[actions:.*])?$',
        output,
        re.MULTILINE,
    )
    assert len(matches) == 1, output[:500]
    text_repr = matches[0][-1]
    return ast.literal_eval(text_repr) if text_repr else ""


def _wait_for_text(engine: AutomationEngine, expected: str) -> None:
    deadline = time.monotonic() + INPUT_TIMEOUT_SECONDS
    actual = ""
    while time.monotonic() < deadline:
        actual = _focused_text(engine)
        if actual == expected:
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    assert actual == expected


def _start_kwrite(
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
    filename: str,
    *,
    enable_clipboard: bool = False,
) -> None:
    output = start_session(
        f"kwrite /tmp/{filename}",
        enable_clipboard=enable_clipboard,
    )
    assert "Input backend: KWin EIS" in output, output
    wait_for_app("kwrite")


def test_mouse_click_hits_seven_then_zero(kcalc_session: AutomationEngine) -> None:
    seven = _global_element_center(kcalc_session, "kcalc", "button", "Seven")
    zero = _global_element_center(kcalc_session, "kcalc", "button", "Zero")

    kcalc_session.mouse_click(*seven)
    _wait_for_binary(kcalc_session, "111")

    kcalc_session.mouse_click(*zero)
    _wait_for_binary(kcalc_session, "1000110")


def test_mouse_button_down_and_up_activate_seven(kcalc_session: AutomationEngine) -> None:
    seven = _global_element_center(kcalc_session, "kcalc", "button", "Seven")

    kcalc_session.mouse_button_down(*seven)
    pressed_tree = kcalc_session.accessibility_tree(app_name="kcalc")
    assert re.search(r'\[button] "Seven" \([^)]*\bpressed\b', pressed_tree), pressed_tree[:500]

    kcalc_session.mouse_button_up(*seven)
    _wait_for_binary(kcalc_session, "111")


def test_mouse_move_then_click_keeps_the_pointer_on_seven(
    kcalc_session: AutomationEngine,
) -> None:
    seven = _global_element_center(kcalc_session, "kcalc", "button", "Seven")

    # KCalc exposes no hover state, so raw motion has no independent AT-SPI oracle.
    assert kcalc_session.mouse_move(*seven) == f"Mouse moved to {seven}"
    kcalc_session.mouse_click(*seven)
    _wait_for_binary(kcalc_session, "111")


def test_keyboard_key_types_then_clears_text(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
) -> None:
    _start_kwrite(start_session, wait_for_app, "kwin-mcp-key.txt")

    engine.keyboard_key("7")
    _wait_for_text(engine, "7")

    engine.keyboard_key("ctrl+a")
    engine.keyboard_key("Delete")
    _wait_for_text(engine, "")


def test_keyboard_key_down_and_up_control_shift(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
) -> None:
    _start_kwrite(start_session, wait_for_app, "kwin-mcp-key-pair.txt")

    engine.keyboard_key_down("shift")
    engine.keyboard_key("a")
    engine.keyboard_key_up("shift")
    engine.keyboard_key("a")

    _wait_for_text(engine, "Aa")


def test_keyboard_type_unicode_reaches_kwrite(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
) -> None:
    _start_kwrite(
        start_session,
        wait_for_app,
        "kwin-mcp-unicode.txt",
        enable_clipboard=True,
    )

    output = engine.keyboard_type_unicode("한글")
    assert output == "Typed unicode: '한글'", output
    _wait_for_text(engine, "한글")

    engine.keyboard_key("ctrl+a")
    engine.keyboard_key("ctrl+c")
    assert engine.clipboard_get() == "한글"


def test_clipboard_roundtrip_and_paste_reach_kwrite(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
) -> None:
    _start_kwrite(
        start_session,
        wait_for_app,
        "kwin-mcp-clipboard.txt",
        enable_clipboard=True,
    )
    payload = "Clipboard QA\n한글\t42!"

    assert engine.clipboard_set(payload) == f"Clipboard set: {payload!r}"
    assert engine.clipboard_get() == payload

    engine.keyboard_key("ctrl+v")
    _wait_for_text(engine, payload)


def test_touch_tap_hits_seven(kcalc_session: AutomationEngine) -> None:
    seven = _global_element_center(kcalc_session, "kcalc", "button", "Seven")

    kcalc_session.touch_tap(*seven)
    _wait_for_binary(kcalc_session, "111")
