"""End-to-end contracts for pointer, keyboard, touch, and clipboard injection."""

from __future__ import annotations

import ast
import re
import time
from typing import TYPE_CHECKING

import pytest
from _asserts import element_count

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from kwin_mcp.core import AutomationEngine

_RECT = r"\((-?\d+), (-?\d+), (\d+)x(\d+)\)"
_DYNAMIC_LABEL = re.compile(
    r'^\s*- \[label] "([^"]*)" \(enabled, sensitive, read-only\) '
    r"@ (?:screen \(-?\d+, -?\d+, \d+x\d+\)|unavailable \([^)]+\))$"
)
POLL_INTERVAL_SECONDS = 0.1
INPUT_TIMEOUT_SECONDS = 3.0


def _rect(output: str, pattern: str) -> tuple[int, int, int, int]:
    matches = re.findall(pattern, output, re.MULTILINE)
    assert len(matches) == 1, output[:500]
    x, y, width, height = matches[0]
    return int(x), int(y), int(width), int(height)


def _global_element_center(
    engine: AutomationEngine, app_name: str, role: str | None, name: str
) -> tuple[int, int]:
    elements = engine.find_ui_elements(query=name, app_name=app_name)
    assert element_count(elements) > 0, elements[:500]
    role_pattern = re.escape(role) if role is not None else r"[^]]+"
    # find_ui_elements already reports global screen coordinates.
    x, y, width, height = _rect(
        elements,
        rf'^- \[{role_pattern}] "{re.escape(name)}" @ screen {_RECT}(?:\s|$)',
    )
    return x + width // 2, y + height // 2


def _has_exact_accessible_name(output: str, name: str) -> bool:
    return (
        re.search(
            rf'^- \[[^]]+] "{re.escape(name)}"(?: |$)',
            output,
            re.MULTILINE,
        )
        is not None
    )


def _wait_for_exact_accessible_name(
    engine: AutomationEngine,
    app_name: str,
    expected_name: str,
) -> None:
    deadline = time.monotonic() + INPUT_TIMEOUT_SECONDS
    elements = ""
    while time.monotonic() < deadline:
        elements = engine.find_ui_elements(query=expected_name, app_name=app_name)
        if _has_exact_accessible_name(elements, expected_name):
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"AT-SPI name {expected_name!r} never appeared:\n{elements[:1500]}")


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
        rf'^- \[text] "[^"]*" @ (?:screen {_RECT}|unavailable \([^)]+\))'
        rf"(?: text=(.*?))?(?: \[actions:.*])?$",
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


def test_mouse_move_enters_hover_target_in_exact_virtual_session(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
) -> None:
    # This compositor is a standalone kwin_wayland --virtual backend, not the nested
    # DISPLAY that xdotool mirrors for visual screenshots.
    app_name = "gui_probe.py"
    output = start_session(
        "python3 /app/tests/e2e/gui_probe.py",
        isolate_home=True,
    )
    assert "Session started. Wayland socket:" in output, output
    assert "Input backend: KWin EIS" in output, output
    wait_for_app(app_name)

    idle = engine.find_ui_elements(query="hover_status: idle", app_name=app_name)
    assert _has_exact_accessible_name(idle, "hover_status: idle"), idle[:500]
    hover_target = _global_element_center(engine, app_name, None, "Hover Target")

    assert engine.mouse_move(*hover_target) == f"Mouse moved to {hover_target}"
    _wait_for_exact_accessible_name(engine, app_name, "hover_status: entered")


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


@pytest.mark.parametrize(
    ("host_layout", "isolate_home"),
    [(None, False), ("ru,us", False), ("ru,us", True)],
    ids=["host-default", "host-ru-us", "host-ru-us-isolated-home"],
)
def test_keyboard_type_ignores_host_keyboard_layout(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    host_layout: str | None,
    isolate_home: bool,
) -> None:
    # keyboard_type sends evdev keycodes from a US QWERTY table, and KWin maps
    # them through its own keymap. A host whose first layout is not US, set in
    # kxkbrc or in XKB_DEFAULT_LAYOUT, must not turn "hello" into "руддщ".
    if host_layout is not None:
        host_config = tmp_path / "host-config"
        host_config.mkdir()
        (host_config / "kxkbrc").write_text(f"[Layout]\nLayoutList={host_layout}\nUse=true\n")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(host_config))
        monkeypatch.setenv("XKB_DEFAULT_LAYOUT", host_layout)

    output = start_session("kwrite /tmp/kwin-mcp-host-layout.txt", isolate_home=isolate_home)
    assert "Input backend: KWin EIS" in output, output
    wait_for_app("kwrite")

    engine.keyboard_type("hello")
    _wait_for_text(engine, "hello")


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
