"""End-to-end coverage for the window_geometry tool.

It is the only tool reporting global screen coordinates, and every conversion
from an accessibility rectangle to a click depends on it, so its output is
pinned here rather than only exercised indirectly through the input tests.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kwin_mcp.core import AutomationEngine

_RECT = r"\((\d+), (\d+), (\d+)x(\d+)\)"
KCALC_SIZE = (640, 480)


def _rect(output: str, kind: str) -> tuple[int, int, int, int]:
    match = re.search(rf"{kind}:\s+{_RECT}", output)
    assert match is not None, output[:500]
    return tuple(int(value) for value in match.groups())  # type: ignore[return-value]


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


def test_accessibility_rectangles_are_relative_to_the_client_origin(
    kcalc_session: AutomationEngine,
) -> None:
    """The reason the tool exists: AT-SPI2 rectangles need this offset."""
    elements = kcalc_session.find_ui_elements(query="", app_name="kcalc")
    frame = re.search(rf'\[frame\] "" @ {_RECT}', elements)
    assert frame is not None, elements[:500]
    local_x, local_y, local_w, local_h = (int(value) for value in frame.groups())

    # AT-SPI2 anchors the window at the origin because a Wayland client cannot
    # know better, while the size still matches what KWin reports.
    assert (local_x, local_y) == (0, 0), elements[:500]
    _, _, client_w, client_h = _rect(kcalc_session.window_geometry(app_name="kcalc"), "client")
    assert (local_w, local_h) == (client_w, client_h), elements[:500]


def test_unknown_app_name_reports_no_windows(kcalc_session: AutomationEngine) -> None:
    output = kcalc_session.window_geometry(app_name="no-such-application")
    assert output == "No windows found for 'no-such-application'.", output[:500]
