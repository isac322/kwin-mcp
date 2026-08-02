"""End-to-end contracts for the non-input observation tools."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from _asserts import element_count

if TYPE_CHECKING:
    from collections.abc import Callable

    from kwin_mcp.core import AutomationEngine

_TREE_ELEMENT = re.compile(r"^\s*- \[([^]]+)]", re.MULTILINE)
_WINDOW_APP = re.compile(r"^- (.+?) \(\d+ windows\)$")
_GLOBAL_INTERFACE = re.compile(r"^\s*interface: '([^']+)'", re.MULTILINE)
_LAUNCHED_PID = re.compile(r"\(PID=(\d+)\)")
LOG_TIMEOUT_SECONDS = 2.0
POLL_INTERVAL_SECONDS = 0.1
LOG_MESSAGE = "kwin-mcp-observation-log"


def _window_blocks(output: str) -> dict[str, list[str]]:
    blocks: dict[str, list[str]] = {}
    current_app = ""
    for line in output.splitlines():
        match = _WINDOW_APP.match(line)
        if match is not None:
            current_app = match.group(1).lower()
            blocks[current_app] = []
        elif current_app and line.startswith("    - "):
            blocks[current_app].append(line.strip())
    return blocks


def _launch_kwrite(engine: AutomationEngine, wait_for_app: Callable[[str], str]) -> str:
    output = engine.launch_app(command="kwrite")
    assert "App launched: kwrite" in output, output[:500]
    wait_for_app("kwrite")
    return output


def test_accessibility_tree_role_filter_narrows_rendered_elements(
    kcalc_session: AutomationEngine,
) -> None:
    unfiltered = kcalc_session.accessibility_tree(app_name="kcalc")
    buttons_only = kcalc_session.accessibility_tree(app_name="kcalc", role="button")

    all_roles = _TREE_ELEMENT.findall(unfiltered)
    filtered_roles = _TREE_ELEMENT.findall(buttons_only)
    assert 0 < len(filtered_roles) < len(all_roles), buttons_only[:500]
    assert set(filtered_roles) == {"button"}, buttons_only[:500]


def test_find_ui_elements_targeted_query_returns_equals_button(
    kcalc_session: AutomationEngine,
) -> None:
    all_elements = kcalc_session.find_ui_elements(query="", app_name="kcalc")
    equals_elements = kcalc_session.find_ui_elements(query="Equals", app_name="kcalc")

    assert 0 < element_count(equals_elements) < element_count(all_elements), equals_elements[:500]
    element_lines = [line for line in equals_elements.splitlines() if line.startswith("- [")]
    assert any(re.match(r'^- \[[^]]+] "Equals"(?: |$)', line) for line in element_lines), (
        equals_elements[:500]
    )


def test_find_ui_elements_focused_state_is_strict_subset(
    kcalc_session: AutomationEngine,
) -> None:
    all_elements = kcalc_session.find_ui_elements(query="", app_name="kcalc")
    focused_elements = kcalc_session.find_ui_elements(
        query="", app_name="kcalc", states=["focused"]
    )

    assert 0 < element_count(focused_elements) < element_count(all_elements), focused_elements[:500]


def test_list_windows_reports_two_launched_apps(
    kcalc_session: AutomationEngine,
    wait_for_app: Callable[[str], str],
) -> None:
    _launch_kwrite(kcalc_session, wait_for_app)

    windows = kcalc_session.list_windows()
    app_names = set(_window_blocks(windows))
    assert any("kcalc" in name for name in app_names), windows[:500]
    assert any("kwrite" in name for name in app_names), windows[:500]


def test_read_app_log_returns_launched_process_output(
    kcalc_session: AutomationEngine,
) -> None:
    launch_output = kcalc_session.launch_app(command=f"printf {LOG_MESSAGE}")
    pid_match = _LAUNCHED_PID.search(launch_output)
    assert pid_match is not None, launch_output[:500]
    pid = int(pid_match.group(1))

    deadline = time.monotonic() + LOG_TIMEOUT_SECONDS
    log_output = "(no log output yet)"
    while time.monotonic() < deadline:
        log_output = kcalc_session.read_app_log(pid=pid)
        if log_output != "(no log output yet)":
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    assert log_output == LOG_MESSAGE, log_output[:500]


def test_wayland_info_filter_narrows_globals_to_wl_seat(
    kcalc_session: AutomationEngine,
) -> None:
    all_protocols = kcalc_session.wayland_info()
    seat_protocols = kcalc_session.wayland_info(filter_protocol="wl_seat")

    all_globals = _GLOBAL_INTERFACE.findall(all_protocols)
    seat_globals = _GLOBAL_INTERFACE.findall(seat_protocols)
    assert len(all_globals) > 1, all_protocols[:500]
    assert 0 < len(seat_globals) < len(all_globals), seat_protocols[:500]
    assert set(seat_globals) == {"wl_seat"}, seat_protocols[:500]
