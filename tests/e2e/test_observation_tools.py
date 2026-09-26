"""End-to-end contracts for the non-input observation tools."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

import pytest
from _asserts import element_count

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from kwin_mcp.core import AutomationEngine

_TREE_ELEMENT = re.compile(r"^\s*- \[([^]]+)]", re.MULTILINE)
_TREE_APPLICATION = re.compile(r'^- \[application] "([^"]+)"', re.MULTILINE)
_WINDOW_APP = re.compile(r"^- (.+?) \(\d+ windows\)$")
_ACTIVE_WINDOW_MARKER = re.compile(r"\[active(?:, focused)?]$")
_FOCUSED_WINDOW_MARKER = re.compile(r"\[(?:active, )?focused]$")
_GLOBAL_INTERFACE = re.compile(r"^\s*interface: '([^']+)'", re.MULTILINE)
_LAUNCHED_PID = re.compile(r"\(PID=(\d+)\)")
LOG_TIMEOUT_SECONDS = 2.0
WINDOW_STATE_TIMEOUT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.1
LOG_LINES = ("observation-line-one", "observation-line-two", "observation-line-three")
ENV_NAME = "KWIN_MCP_OBSERVATION_ENV"
ENV_VALUE = "kwin-mcp-env-value"


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


def _pid_from_launch(output: str) -> int:
    match = _LAUNCHED_PID.search(output)
    assert match is not None, output[:500]
    return int(match.group(1))


def _wait_for_log(engine: AutomationEngine, pid: int, expected: str) -> str:
    deadline = time.monotonic() + LOG_TIMEOUT_SECONDS
    output = "(no log output yet)"
    while time.monotonic() < deadline:
        output = engine.read_app_log(pid=pid, last_n_lines=0)
        if output == expected:
            break
        time.sleep(POLL_INTERVAL_SECONDS)
    return output


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


def test_accessibility_tree_max_depth_limits_traversal(
    kcalc_session: AutomationEngine,
) -> None:
    root_only = kcalc_session.accessibility_tree(app_name="kcalc", max_depth=0)
    one_level = kcalc_session.accessibility_tree(app_name="kcalc", max_depth=1)

    assert _TREE_ELEMENT.findall(root_only) == ["application"], root_only[:500]
    assert len(_TREE_ELEMENT.findall(one_level)) > 1, one_level[:500]
    assert "  - [frame]" in one_level, one_level[:500]
    assert not any(line.startswith("    - [") for line in one_level.splitlines()), one_level[:500]


def test_accessibility_tree_without_app_filter_reports_all_launched_apps(
    kcalc_session: AutomationEngine,
    wait_for_app: Callable[[str], str],
) -> None:
    _launch_kwrite(kcalc_session, wait_for_app)

    tree = kcalc_session.accessibility_tree()
    app_names = {name.lower() for name in _TREE_APPLICATION.findall(tree)}
    assert any("kcalc" in name for name in app_names), tree[:500]
    assert any("kwrite" in name for name in app_names), tree[:500]


def test_accessibility_tree_reports_nonexistent_app(
    kcalc_session: AutomationEngine,
) -> None:
    output = kcalc_session.accessibility_tree(app_name="definitely-not-an-accessible-app")

    assert output == "(no accessible applications found)"


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


def test_find_ui_elements_reports_nonexistent_query(
    kcalc_session: AutomationEngine,
) -> None:
    query = "definitely-not-a-real-kcalc-element"
    output = kcalc_session.find_ui_elements(query=query, app_name="kcalc")

    assert output == f"No elements found matching query='{query}'"


def test_find_ui_elements_requires_every_requested_state(
    kcalc_session: AutomationEngine,
) -> None:
    kcalc_session.wait_for_element(
        query="",
        app_name="kcalc",
        timeout_ms=1000,
        poll_interval_ms=50,
        expected_states=["focused", "focusable"],
    )
    focused = kcalc_session.find_ui_elements(query="", app_name="kcalc", states=["focused"])
    focusable = kcalc_session.find_ui_elements(query="", app_name="kcalc", states=["focusable"])
    focused_and_focusable = kcalc_session.find_ui_elements(
        query="", app_name="kcalc", states=["focused", "focusable"]
    )

    combined_count = element_count(focused_and_focusable)
    assert 0 < combined_count <= element_count(focused), focused_and_focusable[:500]
    assert combined_count <= element_count(focusable), focused_and_focusable[:500]
    assert "matching states=['focused', 'focusable']" in focused_and_focusable


def test_wait_for_element_succeeds_when_expected_states_are_present(
    kcalc_session: AutomationEngine,
) -> None:
    output = kcalc_session.wait_for_element(
        query="",
        app_name="kcalc",
        timeout_ms=1000,
        poll_interval_ms=50,
        expected_states=["focused", "focusable"],
    )

    assert element_count(output) > 0, output[:500]
    assert "matching states=['focused', 'focusable']" in output


def test_list_windows_reports_two_launched_apps(
    kcalc_session: AutomationEngine,
    wait_for_app: Callable[[str], str],
) -> None:
    _launch_kwrite(kcalc_session, wait_for_app)

    windows = kcalc_session.list_windows()
    app_names = set(_window_blocks(windows))
    assert any("kcalc" in name for name in app_names), windows[:500]
    assert any("kwrite" in name for name in app_names), windows[:500]


def test_list_windows_marks_the_active_window(
    kcalc_session: AutomationEngine,
) -> None:
    deadline = time.monotonic() + WINDOW_STATE_TIMEOUT_SECONDS
    windows = kcalc_session.list_windows()
    kcalc_lines: list[str] = []
    while time.monotonic() < deadline:
        kcalc_lines = [
            line
            for app_name, lines in _window_blocks(windows).items()
            if "kcalc" in app_name
            for line in lines
        ]
        if any(_ACTIVE_WINDOW_MARKER.search(line) for line in kcalc_lines):
            break
        time.sleep(POLL_INTERVAL_SECONDS)
        windows = kcalc_session.list_windows()

    assert any(_ACTIVE_WINDOW_MARKER.search(line) for line in kcalc_lines), (
        f"kcalc did not become active within {WINDOW_STATE_TIMEOUT_SECONDS} seconds:\n"
        f"{windows[:500]}"
    )


def test_list_windows_marks_a_focused_top_level_window(
    kcalc_session: AutomationEngine,
    wait_for_app: Callable[[str], str],
    tmp_path: Path,
) -> None:
    probe = tmp_path / "focused_window_probe.py"
    probe.write_text(
        "import gi\n"
        "gi.require_version('Gtk', '3.0')\n"
        "from gi.repository import GLib, Gtk\n"
        "GLib.set_application_name('focused_window_probe.py')\n"
        "window = Gtk.Window(title='Focused Window Probe')\n"
        "window.set_can_focus(True)\n"
        "window.set_focus_on_map(True)\n"
        "window.set_default_size(320, 160)\n"
        "window.connect('destroy', Gtk.main_quit)\n"
        "window.show_all()\n"
        "def focus_window():\n"
        "    window.present()\n"
        "    window.grab_focus()\n"
        "    return False\n"
        "GLib.timeout_add(100, focus_window)\n"
        "Gtk.main()\n",
        encoding="utf-8",
    )
    kcalc_session.launch_app(command=f"/usr/bin/python3 {probe}")
    wait_for_app("focused_window_probe.py")

    deadline = time.monotonic() + WINDOW_STATE_TIMEOUT_SECONDS
    windows = kcalc_session.list_windows()
    probe_lines: list[str] = []
    while time.monotonic() < deadline:
        probe_lines = [
            line
            for app_name, lines in _window_blocks(windows).items()
            if "focused_window_probe.py" in app_name
            for line in lines
        ]
        if any(_FOCUSED_WINDOW_MARKER.search(line) for line in probe_lines):
            break
        time.sleep(POLL_INTERVAL_SECONDS)
        windows = kcalc_session.list_windows()

    assert any(_FOCUSED_WINDOW_MARKER.search(line) for line in probe_lines), (
        "focused_window_probe.py did not become focused within "
        f"{WINDOW_STATE_TIMEOUT_SECONDS} seconds:\n{windows[:500]}"
    )


def test_list_windows_reports_when_no_accessible_apps_exist(
    engine: AutomationEngine,
    start_session: Callable[..., str],
) -> None:
    start_output = start_session()
    assert "Session started" in start_output, start_output[:500]

    assert engine.list_windows() == "(no accessible applications found)"


def test_read_app_log_returns_all_lines_and_requested_tail(
    kcalc_session: AutomationEngine,
) -> None:
    expected = "\n".join(LOG_LINES) + "\n"
    command = "printf '" + "\\n".join(LOG_LINES) + "\\n'"
    pid = _pid_from_launch(kcalc_session.launch_app(command=command))

    all_lines = _wait_for_log(kcalc_session, pid, expected)
    assert all_lines == expected, all_lines[:500]
    assert kcalc_session.read_app_log(pid=pid, last_n_lines=2) == "\n".join(LOG_LINES[-2:])


def test_read_app_log_rejects_an_unknown_pid(
    kcalc_session: AutomationEngine,
) -> None:
    with pytest.raises(ValueError) as exc_info:
        kcalc_session.read_app_log(pid=-1)

    message = str(exc_info.value)
    assert message.startswith("No app with PID -1. Available PIDs: ["), message
    assert message.endswith("]"), message


def test_read_app_log_reports_empty_process_output(
    kcalc_session: AutomationEngine,
) -> None:
    pid = _pid_from_launch(kcalc_session.launch_app(command="true"))

    assert kcalc_session.read_app_log(pid=pid, last_n_lines=0) == "(no log output yet)"


def test_launch_app_passes_environment_to_the_process(
    kcalc_session: AutomationEngine,
) -> None:
    command = f"sh -c 'printf %s \"${ENV_NAME}\"'"
    launch_output = kcalc_session.launch_app(
        command=command,
        env={ENV_NAME: ENV_VALUE},
    )
    pid = _pid_from_launch(launch_output)

    assert f"App launched: {command}" in launch_output, launch_output[:500]
    assert _wait_for_log(kcalc_session, pid, ENV_VALUE) == ENV_VALUE


def test_launch_app_rejects_a_missing_command(
    kcalc_session: AutomationEngine,
) -> None:
    command = "/definitely/not/a/kwin-mcp-app"

    with pytest.raises(FileNotFoundError) as exc_info:
        kcalc_session.launch_app(command=command)

    assert exc_info.value.filename == command


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


def test_wayland_info_reports_when_filter_matches_nothing(
    kcalc_session: AutomationEngine,
) -> None:
    protocol = "definitely-not-a-wayland-protocol"

    assert (
        kcalc_session.wayland_info(filter_protocol=protocol)
        == f"No protocols matching '{protocol}' found."
    )


def test_wayland_info_reports_command_errors(
    kcalc_session: AutomationEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    command = tmp_path / "wayland-info"
    command.write_text(
        "#!/bin/sh\nprintf 'synthetic wayland-info failure\\n' >&2\nexit 7\n",
        encoding="utf-8",
    )
    command.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert kcalc_session.wayland_info() == ("wayland-info failed: synthetic wayland-info failure\n")
