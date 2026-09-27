"""Byte-exact contracts for the text formatters in ``kwin_mcp.results``.

The MCP TextContent and ``kwin-mcp-cli`` output of ``window_geometry``,
``find_ui_elements``, ``accessibility_tree``, and ``list_windows`` are rendered
from the typed result models. Every expected string below is a literal captured
from the pre-model formatting code, so any drift in the human-readable output
fails here without needing a KWin session.
"""

from __future__ import annotations

from typing import Any

import pytest

from kwin_mcp.results import (
    AccessibilityTreeResult,
    AccessibleApplication,
    AccessibleWindow,
    FindUIElementsResult,
    ListWindowsResult,
    Rect,
    UIElement,
    WindowGeometry,
    WindowGeometryResult,
    format_find,
    format_list_windows,
    format_tree,
    format_window_geometry,
)


def _element(**overrides: Any) -> UIElement:
    fields: dict[str, Any] = {
        "role": "push button",
        "name": "7",
        "description": "",
        "states": ["enabled", "visible"],
        "depth": 3,
        "rect": Rect(x=10, y=20, width=30, height=40),
        "unavailable": None,
        "actions": ["Press"],
        "text": "",
        "value": None,
        "value_max": None,
    }
    fields.update(overrides)
    return UIElement(**fields)


def _unmapped(reason: str) -> dict[str, Any]:
    return {"rect": None, "unavailable": reason}


ELEMENTS: list[UIElement] = [
    _element(
        depth=0,
        role="application",
        name="kcalc",
        states=[],
        actions=[],
        **_unmapped("not-a-window"),
    ),
    _element(
        depth=1,
        role="frame",
        name='KCalc — "x"',
        states=["active", "enabled"],
        actions=[],
        rect=Rect(x=100, y=50, width=400, height=500),
    ),
    _element(depth=2, name='quote "q" \\ é', states=[], actions=[], **_unmapped("ambiguous")),
    _element(
        depth=2,
        role="scroll bar",
        value=12.5,
        value_max=100.0,
        actions=[],
        rect=Rect(x=-5, y=0, width=30, height=40),
    ),
    _element(
        depth=3,
        role="text",
        text="line1\nline2 'x' aaaaa…",
        actions=["SetFocus"],
        **_unmapped("no-extents"),
    ),
    # A value without a maximum is not rendered.
    _element(depth=3, value=3.0, value_max=None),
    _element(depth=4, actions=["Press", "SetFocus"], **_unmapped("windows-changed")),
]

FIND_ELEMENT_LINES = (
    '- [application] "kcalc" @ unavailable (not-a-window)\n'
    '- [frame] "KCalc — "x"" @ screen (100, 50, 400x500)\n'
    '- [push button] "quote "q" \\ é" @ unavailable (ambiguous)\n'
    '- [scroll bar] "7" @ screen (-5, 0, 30x40) value=12.5/100\n'
    '- [text] "7" @ unavailable (no-extents) text="line1\\nline2 \'x\' aaaaa…"'
    " [actions: SetFocus]\n"
    '- [push button] "7" @ screen (10, 20, 30x40) [actions: Press]\n'
    '- [push button] "7" @ unavailable (windows-changed) [actions: Press, SetFocus]'
)


@pytest.mark.parametrize(
    ("query", "states", "expected"),
    [
        pytest.param(
            "", None, "Found 7 elements matching (all):\n\n" + FIND_ELEMENT_LINES, id="all"
        ),
        pytest.param(
            "Equals",
            ["enabled", "visible"],
            "Found 7 elements matching query='Equals', states=['enabled', 'visible']:\n\n"
            + FIND_ELEMENT_LINES,
            id="query-and-states",
        ),
    ],
)
def test_format_find_renders_every_element_line(
    query: str, states: list[str] | None, expected: str
) -> None:
    result = FindUIElementsResult(query=query, states=states, elements=ELEMENTS)

    assert format_find(result) == expected


@pytest.mark.parametrize(
    ("query", "states", "expected"),
    [
        pytest.param("", None, "No elements found matching (all)", id="all"),
        pytest.param("7", None, "No elements found matching query='7'", id="query"),
        pytest.param("", ["focused"], "No elements found matching states=['focused']", id="states"),
        pytest.param("x", [], "No elements found matching query='x'", id="empty-states-hidden"),
        pytest.param(
            "it's",
            ["a", "b"],
            "No elements found matching query='it's', states=['a', 'b']",
            id="query-and-states",
        ),
    ],
)
def test_format_find_reports_no_matches(
    query: str, states: list[str] | None, expected: str
) -> None:
    result = FindUIElementsResult(query=query, states=states, elements=[])

    assert format_find(result) == expected


def test_format_tree_indents_by_depth_and_shows_states() -> None:
    expected = (
        "# Accessibility Tree (7 elements)\n\n"
        '- [application] "kcalc" @ unavailable (not-a-window)\n'
        '  - [frame] "KCalc — "x"" (active, enabled) @ screen (100, 50, 400x500)\n'
        '    - [push button] "quote "q" \\ é" @ unavailable (ambiguous)\n'
        '    - [scroll bar] "7" (enabled, visible) @ screen (-5, 0, 30x40) value=12.5/100\n'
        '      - [text] "7" (enabled, visible) @ unavailable (no-extents)'
        " text=\"line1\\nline2 'x' aaaaa…\" [actions: SetFocus]\n"
        '      - [push button] "7" (enabled, visible) @ screen (10, 20, 30x40) [actions: Press]\n'
        '        - [push button] "7" (enabled, visible) @ unavailable (windows-changed)'
        " [actions: Press, SetFocus]"
    )

    assert format_tree(AccessibilityTreeResult(elements=ELEMENTS)) == expected


def test_format_tree_reports_no_applications() -> None:
    assert format_tree(AccessibilityTreeResult(elements=[])) == (
        "(no accessible applications found)"
    )


def test_from_worker_maps_unmapped_elements_without_coordinates() -> None:
    worker_element = {
        "role": "push button",
        "name": "7",
        "description": "",
        "states": [],
        "x": 0,
        "y": 0,
        "width": 0,
        "height": 0,
        "actions": [],
        "text": "",
        "value": None,
        "value_max": None,
        "children_count": 0,
        "depth": 1,
        "has_extents": False,
        "mapped": False,
        "unavailable": "",
    }
    mapped_element = {**worker_element, "mapped": True, "x": 3, "y": 4, "width": 5, "height": 6}

    unmapped = UIElement.from_worker(worker_element)
    mapped = UIElement.from_worker(mapped_element)

    assert unmapped.rect is None
    assert unmapped.unavailable == "unmapped"
    assert mapped.rect == Rect(x=3, y=4, width=5, height=6)
    assert mapped.unavailable is None
    result = FindUIElementsResult(query="7", states=None, elements=[unmapped, mapped])
    assert format_find(result) == (
        "Found 2 elements matching query='7':\n\n"
        '- [push button] "7" @ unavailable (unmapped)\n'
        '- [push button] "7" @ screen (3, 4, 5x6)'
    )


WINDOWS = [
    WindowGeometry(
        id="{3f2c-a1}",
        app="org.kde.kcalc",
        caption='KCalc — "x"',
        active=True,
        frame=Rect(x=-5, y=0, width=400, height=500),
        client=Rect(x=0, y=30, width=390, height=465),
    ),
    WindowGeometry(
        id="{b}",
        app="org.kde.kwrite",
        caption="Untitled — KWrite",
        active=False,
        frame=Rect(x=10, y=20, width=800, height=600),
        client=Rect(x=10, y=50, width=800, height=570),
    ),
]


def test_format_window_geometry_marks_only_the_active_window() -> None:
    expected = (
        "Windows (2):\n"
        '- org.kde.kcalc "KCalc — "x"" [active]\n'
        "    id:     {3f2c-a1}\n"
        "    frame:  (-5, 0, 400x500)\n"
        "    client: (0, 30, 390x465)\n"
        '- org.kde.kwrite "Untitled — KWrite"\n'
        "    id:     {b}\n"
        "    frame:  (10, 20, 800x600)\n"
        "    client: (10, 50, 800x570)"
    )

    assert format_window_geometry(WindowGeometryResult(windows=WINDOWS), "", "") == expected


@pytest.mark.parametrize(
    ("app_name", "window_id", "expected"),
    [
        pytest.param("", "", "No windows found.", id="unfiltered"),
        pytest.param("kcalc", "", "No windows found for 'kcalc'.", id="app-filter"),
        pytest.param("", "{nope'}", 'No window with id "{nope\'}".', id="window-id-repr"),
        pytest.param("kcalc", "{x}", "No window with id '{x}'.", id="window-id-wins-over-app"),
    ],
)
def test_format_window_geometry_reports_no_windows(
    app_name: str, window_id: str, expected: str
) -> None:
    assert format_window_geometry(WindowGeometryResult(), app_name, window_id) == expected


def test_format_window_geometry_reports_kwin_failure_before_filters() -> None:
    result = WindowGeometryResult(error="KWin query timed out after 30s")

    assert format_window_geometry(result, "kcalc", "{x}") == (
        "Window geometry unavailable: KWin query timed out after 30s"
    )


def test_format_list_windows_renders_state_markers_and_child_counts() -> None:
    result = ListWindowsResult(
        applications=[
            # window_count is the AT-SPI child count and may exceed the listed windows.
            AccessibleApplication(
                name="kcalc",
                window_count=2,
                windows=[AccessibleWindow(title="KCalc", active=True, focused=True)],
            ),
            AccessibleApplication(
                name="(unnamed)",
                window_count=1,
                windows=[AccessibleWindow(title="(untitled)", active=False, focused=False)],
            ),
            AccessibleApplication(
                name="kwrite",
                window_count=1,
                windows=[AccessibleWindow(title="Untitled — KWrite", active=False, focused=True)],
            ),
        ]
    )

    assert format_list_windows(result) == (
        "Applications (3):\n"
        "- kcalc (2 windows)\n"
        '    - "KCalc" [active, focused]\n'
        "- (unnamed) (1 windows)\n"
        '    - "(untitled)"\n'
        "- kwrite (1 windows)\n"
        '    - "Untitled — KWrite" [focused]'
    )


def test_format_list_windows_reports_no_applications() -> None:
    assert format_list_windows(ListWindowsResult(applications=[])) == (
        "(no accessible applications found)"
    )
