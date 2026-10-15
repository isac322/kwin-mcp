"""End-to-end coverage for ``wayland_list``.

The oracle is ``wayland-info`` itself, run by the test in the session environment
the engine uses (same compositor socket), so the tool's raw lines, its
case-sensitive substring filter and its parsed ``globals[]`` are checked against
an independent run, not against the tool's own output.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from typing import TYPE_CHECKING

import pytest
from _asserts import assert_render_roundtrip
from conftest import SCREEN_HEIGHT, SCREEN_WIDTH

from kwin_mcp.core import AutomationEngine
from kwin_mcp.models import WaylandGlobal, WaylandGlobals

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# wayland-info prints one line per global, e.g.
# "interface: 'wl_compositor',                            version:  6, name:  1".
_GLOBAL_LINE = re.compile(r"^interface: '([^']+)',\s+version:\s+(\d+),\s+name:\s+(\d+)$")
# The one per-client value in wayland-info output (KWin 6.7.5 color management).
_CLIENT_IMAGE_DESCRIPTION_ID = re.compile(r"(image description id: )\d+$")
WAYLAND_INFO_HINT = (
    "wayland-info not found. Install wayland-utils "
    "(e.g. 'sudo pacman -S wayland-utils' or 'sudo apt install wayland-utils')."
)
WAYLAND_INFO_TIMEOUT_S = 10.0


@pytest.fixture(scope="module")
def session() -> Iterator[AutomationEngine]:
    """One virtual session shared by the read-only wayland_list calls."""
    engine = AutomationEngine()
    try:
        info = engine.session_start(screen_width=SCREEN_WIDTH, screen_height=SCREEN_HEIGHT)
        assert info.kind == "virtual", info
        yield engine
    finally:
        engine.session_stop()


def _raw_lines(engine: AutomationEngine) -> list[str]:
    """``wayland-info`` run directly by the test against the session's compositor."""
    result = subprocess.run(
        ["wayland-info"],
        env=engine._session_env(),
        capture_output=True,
        timeout=WAYLAND_INFO_TIMEOUT_S,
        check=True,
    )
    return result.stdout.decode(errors="replace").splitlines()


def _client_ids_masked(line: str) -> str:
    return _CLIENT_IMAGE_DESCRIPTION_ID.sub(r"\1N", line)


def _globals_of(lines: list[str]) -> list[WaylandGlobal]:
    parsed: list[WaylandGlobal] = []
    for line in lines:
        match = _GLOBAL_LINE.match(line.strip())
        if match is not None:
            parsed.append(
                WaylandGlobal(
                    interface=match.group(1),
                    version=int(match.group(2)),
                    name=int(match.group(3)),
                )
            )
    return parsed


def _path_with(tmp_path: Path, script: str | None) -> str:
    """A PATH holding only ``tmp_path``, with a ``wayland-info`` shim when ``script`` is set."""
    if script is not None:
        shim = tmp_path / "wayland-info"
        shim.write_text(script, encoding="utf-8")
        shim.chmod(0o755)
    return str(tmp_path)


@pytest.mark.qa("QA-ESC-010")
def test_globals_parsed_interface_version_name(session: AutomationEngine) -> None:
    result = session.wayland_list()

    raw = _raw_lines(session)
    assert result.filter == ""
    # Two wayland-info runs are two clients: the color-management image description
    # id newer KWin hands each client differs between them. Only that id is masked;
    # every other line, global or detail, must match the direct run exactly.
    assert [_client_ids_masked(line) for line in result.lines] == [
        _client_ids_masked(line) for line in raw
    ]
    expected_globals = _globals_of(raw)
    assert result.globals == expected_globals
    interfaces = {item.interface for item in result.globals}
    assert {"wl_compositor", "wl_seat", "wl_output"} <= interfaces, interfaces
    assert all(item.version >= 1 and item.name >= 1 for item in result.globals), result.globals
    assert_render_roundtrip(WaylandGlobals, result.model_dump(mode="json"), "\n".join(result.lines))


@pytest.mark.qa("QA-ESC-011")
def test_filter_raw_line_substring_case_sensitive(session: AutomationEngine) -> None:
    raw = _raw_lines(session)

    seat = session.wayland_list(filter="wl_seat")
    expected_seat = [line for line in raw if "wl_seat" in line]
    assert expected_seat, raw
    assert seat.filter == "wl_seat"
    assert seat.lines == expected_seat
    assert seat.globals == _globals_of(expected_seat)
    assert {item.interface for item in seat.globals} == {"wl_seat"}, seat.globals
    assert seat.render() == "\n".join(expected_seat)

    # Case-sensitive, and the filter is a literal substring, never a regex.
    for literal in ("WL_SEAT", "zzz", ".*"):
        assert [line for line in raw if literal in line] == [], literal
        result = session.wayland_list(filter=literal)
        assert result.lines == [], (literal, result.lines[:5])
        assert result.globals == [], literal
        assert result.render() == f"No protocols matching '{literal}' found."


def test_wayland_list_filter_narrows_globals_to_wl_seat(session: AutomationEngine) -> None:
    all_protocols = session.wayland_list()
    seat_protocols = session.wayland_list(filter="wl_seat")

    assert len(all_protocols.globals) > 1, all_protocols.lines[:20]
    assert 0 < len(seat_protocols.globals) < len(all_protocols.globals), seat_protocols.lines
    assert {item.interface for item in seat_protocols.globals} == {"wl_seat"}
    assert set(seat_protocols.lines) <= set(all_protocols.lines)


def test_wayland_list_reports_when_filter_matches_nothing(session: AutomationEngine) -> None:
    protocol = "definitely-not-a-wayland-protocol"

    result = session.wayland_list(filter=protocol)

    assert result == WaylandGlobals(filter=protocol, lines=[], globals=[])
    assert result.render() == f"No protocols matching '{protocol}' found."


def test_wayland_list_reports_command_errors(
    session: AutomationEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(
        "PATH",
        _path_with(tmp_path, "#!/bin/sh\nprintf 'synthetic wayland-info failure\\n' >&2\nexit 7\n"),
    )

    with pytest.raises(RuntimeError, match=r"^wayland-info failed: synthetic wayland-info failure"):
        session.wayland_list()


@pytest.mark.qa("QA-ESC-012")
def test_missing_wayland_info_hint(
    session: AutomationEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "missing"
    missing.mkdir()
    monkeypatch.setenv("PATH", _path_with(missing, None))
    with pytest.raises(RuntimeError) as missing_info:
        session.wayland_list()
    assert str(missing_info.value) == WAYLAND_INFO_HINT

    failing = tmp_path / "failing"
    failing.mkdir()
    monkeypatch.setenv(
        "PATH", _path_with(failing, "#!/bin/sh\nprintf 'shim stderr line\\n' >&2\nexit 1\n")
    )
    with pytest.raises(RuntimeError) as failed_info:
        session.wayland_list()
    assert str(failed_info.value) == "wayland-info failed: shim stderr line\n"

    # exec: the shim itself becomes the sleeper, so the timeout kill closes stdout.
    hanging = tmp_path / "hanging"
    hanging.mkdir()
    sleep = shutil.which("sleep", path=os.defpath + ":/usr/bin:/bin")
    assert sleep is not None
    monkeypatch.setenv("PATH", _path_with(hanging, f"#!/bin/sh\nexec {sleep} 60\n"))
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as timeout_info:
        session.wayland_list()
    elapsed = time.monotonic() - started
    assert timeout_info.value.timeout == WAYLAND_INFO_TIMEOUT_S
    assert "timed out after 10" in str(timeout_info.value), str(timeout_info.value)
    assert WAYLAND_INFO_TIMEOUT_S <= elapsed < 3 * WAYLAND_INFO_TIMEOUT_S, elapsed

    # The session is unaffected by the failed helper runs.
    monkeypatch.undo()
    assert session.wayland_list(filter="wl_compositor").globals
