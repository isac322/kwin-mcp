"""End-to-end coverage for the window_geometry tool.

It reports window frame and client rectangles in global screen coordinates —
the same space find_ui_elements and accessibility_tree now report — so its
output is pinned here rather than only exercised indirectly through the
input tests.
"""

from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING

import kwin_mcp.core as core

if TYPE_CHECKING:
    import pytest

    from kwin_mcp.core import AutomationEngine

_RECT = r"\((\d+), (\d+), (\d+)x(\d+)\)"
KCALC_SIZE = (640, 480)


def _engine_with_timed_out_kwin_query(monkeypatch: pytest.MonkeyPatch) -> AutomationEngine:
    engine = core.AutomationEngine()
    monkeypatch.setattr(engine, "_get_session", lambda: object())
    monkeypatch.setattr(engine, "_session_env", lambda: {})

    def time_out(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="kwin_mcp.geometry", timeout=30)

    monkeypatch.setattr(core.subprocess, "run", time_out)
    return engine


def _rect(output: str, kind: str) -> tuple[int, int, int, int]:
    match = re.search(rf"{kind}:\s+{_RECT}", output)
    assert match is not None, output[:500]
    x, y, width, height = match.groups()
    return int(x), int(y), int(width), int(height)


def test_client_geometry_is_centred_on_the_screen(
    kcalc_session: AutomationEngine, screen_size: tuple[int, int]
) -> None:
    output = kcalc_session.window_geometry(app_name="kcalc")
    assert "kcalc" in output, output[:500]

    _, _, client_w, client_h = _rect(output, "client")
    client_x, client_y, _, _ = _rect(output, "client")
    assert (client_w, client_h) == KCALC_SIZE, output[:500]

    # KWin centres the window horizontally; vertically it leaves room for the
    # panel strip, so only the horizontal centring is pinned exactly.
    screen_width, screen_height = screen_size
    assert client_x == (screen_width - client_w) // 2, output[:500]
    assert 0 < client_y < screen_height - client_h, output[:500]


def test_frame_encloses_the_client_area(kcalc_session: AutomationEngine) -> None:
    output = kcalc_session.window_geometry(app_name="kcalc")
    frame_x, frame_y, frame_w, frame_h = _rect(output, "frame")
    client_x, client_y, client_w, client_h = _rect(output, "client")

    # The frame carries the decoration, so it must enclose the client area.
    assert frame_x <= client_x and frame_y <= client_y, output[:500]
    assert frame_x + frame_w >= client_x + client_w, output[:500]
    assert frame_y + frame_h >= client_y + client_h, output[:500]
    assert (frame_w, frame_h) != (client_w, client_h), output[:500]


def test_accessibility_rectangles_are_global_screen_coordinates(
    kcalc_session: AutomationEngine,
) -> None:
    """AT-SPI2 rectangles are translated to screen coordinates by kwin-mcp."""
    elements = kcalc_session.find_ui_elements(query="", app_name="kcalc")
    frame = re.search(rf'\[frame\] "" @ screen {_RECT}', elements)
    assert frame is not None, elements[:500]
    screen_x, screen_y, screen_w, screen_h = (int(value) for value in frame.groups())

    # The window's top-level rectangle lands exactly on the client origin KWin
    # reports, with the same size.
    client_x, client_y, client_w, client_h = _rect(
        kcalc_session.window_geometry(app_name="kcalc"), "client"
    )
    assert (screen_x, screen_y) == (client_x, client_y), elements[:500]
    assert (screen_w, screen_h) == (client_w, client_h), elements[:500]


def test_unknown_app_name_reports_no_windows(kcalc_session: AutomationEngine) -> None:
    output = kcalc_session.window_geometry(app_name="no-such-application")
    assert output == "No windows found for 'no-such-application'.", output[:500]


def test_focus_window_reports_kwin_query_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine_with_timed_out_kwin_query(monkeypatch)

    assert engine.focus_window(app_name="kcalc") == (
        "Failed to focus 'kcalc': KWin query timed out after 30s"
    )


def test_window_geometry_reports_kwin_query_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine_with_timed_out_kwin_query(monkeypatch)

    assert (
        engine.window_geometry(app_name="kcalc")
        == "Window geometry unavailable: KWin query timed out after 30s"
    )
