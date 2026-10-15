"""Byte-exact goldens for ``render()`` of every result model in ``kwin_mcp.models``.

The MCP text content and the ``kwin-mcp-cli`` output of every tool are
``result.render()``. The tree, window, desktop, action, session, close-consent
and frame lines below are literals of the 0.11 (and PR #87/#103) texts, extended
only where S adds data to the text (refs, window ids, kinds, frames' image line).
Any drift in the human-readable output fails here without a KWin session.
"""

import asyncio
import re
from typing import Any

import pytest
from mcp.client.client import Client
from mcp.types import CallToolResult, TextContent

from kwin_mcp.core import AutomationEngine
from kwin_mcp.models import (
    ActionResult,
    AppEntry,
    AppLaunchResult,
    AppLogResult,
    ClipboardText,
    ClipboardWriteResult,
    DbusReply,
    Desktop,
    DesktopList,
    ElementActResult,
    ElementList,
    ElementWaitResult,
    Frame,
    HeldInput,
    ImageMapping,
    KeptPaths,
    KeyboardHoldResult,
    KeyboardPressResult,
    KeyboardTypeResult,
    Locator,
    PartialRect,
    PlaceApplied,
    PlaceBefore,
    Point,
    PointerClickResult,
    PointerDragResult,
    PointerHoldResult,
    PointerMoveResult,
    PointerScrollResult,
    Rect,
    Retention,
    ScreenSize,
    SessionInfo,
    SessionInfoResult,
    SessionStopResult,
    TouchPinchResult,
    TouchSwipeResult,
    TouchTapResult,
    TypeRoute,
    UIElement,
    WaylandGlobal,
    WaylandGlobals,
    Window,
    WindowCloseResult,
    WindowFocusResult,
    WindowList,
    WindowPlaceResult,
    WindowSelector,
    text_echo,
)
from kwin_mcp.server import build_server

# ── Elements ─────────────────────────────────────────────────────────────


def _element(**overrides: Any) -> UIElement:
    fields: dict[str, Any] = {
        "ref": "e1.6",
        "role": "push button",
        "name": "7",
        "description": "",
        "states": ["enabled", "visible"],
        "depth": 3,
        "app": "kcalc",
        "window_id": "{w1}",
        "child_count": 0,
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
        ref="e1.1",
        depth=0,
        role="application",
        name="kcalc",
        states=[],
        actions=[],
        window_id=None,
        child_count=1,
        **_unmapped("not-a-window"),
    ),
    _element(
        ref="e1.2",
        depth=1,
        role="frame",
        name='KCalc — "x"',
        states=["active", "enabled"],
        actions=[],
        rect=Rect(x=100, y=50, width=400, height=500),
    ),
    _element(
        ref="e1.3",
        depth=2,
        name='quote "q" \\ é',
        states=[],
        actions=[],
        window_id=None,
        **_unmapped("ambiguous"),
    ),
    _element(
        ref="e1.4",
        depth=2,
        role="scroll bar",
        value=12.5,
        value_max=100.0,
        actions=[],
        rect=Rect(x=-5, y=0, width=30, height=40),
    ),
    _element(
        ref="e1.5",
        depth=3,
        role="text",
        text="line1\nline2 'x' aaaaa…",
        actions=["SetFocus"],
        **_unmapped("no-extents"),
    ),
    # A value without a maximum is not rendered.
    _element(ref="e1.6", depth=3, value=3.0, value_max=None),
    _element(
        ref="e1.7",
        depth=4,
        actions=["Press", "SetFocus"],
        window_id=None,
        **_unmapped("windows-changed"),
    ),
]

# The 0.11 tree lines, each followed by its ref and by the app/window tags
# where those change from the previous line.
TREE_LINES = (
    '- [application] "kcalc" @ unavailable (not-a-window) [ref=e1.1, children=1]\n'
    '  - [frame] "KCalc — "x"" (active, enabled) @ screen (100, 50, 400x500)'
    " [ref=e1.2, window={w1}]\n"
    '    - [push button] "quote "q" \\ é" @ unavailable (ambiguous) [ref=e1.3, window=none]\n'
    '    - [scroll bar] "7" (enabled, visible) @ screen (-5, 0, 30x40) value=12.5/100'
    " [ref=e1.4, window={w1}]\n"
    '      - [text] "7" (enabled, visible) @ unavailable (no-extents)'
    " text=\"line1\\nline2 'x' aaaaa…\" [actions: SetFocus] [ref=e1.5]\n"
    '      - [push button] "7" (enabled, visible) @ screen (10, 20, 30x40) [actions: Press]'
    " [ref=e1.6]\n"
    '        - [push button] "7" (enabled, visible) @ unavailable (windows-changed)'
    " [actions: Press, SetFocus] [ref=e1.7, window=none]"
)


@pytest.mark.qa("QA-MCP-003")
def test_empty_locator_renders_the_indented_tree_with_states() -> None:
    expected = "# Accessibility Tree (7 elements)\n\n" + TREE_LINES

    assert ElementList(elements=ELEMENTS).render() == expected
    # Empty values select everything too, so they keep the tree header.
    assert ElementList(locator=Locator(query="", states=[]), elements=ELEMENTS).render() == (
        expected
    )


@pytest.mark.qa("QA-MCP-003")
def test_empty_tree_reports_no_applications() -> None:
    assert ElementList(elements=[]).render() == "(no accessible applications found)"


@pytest.mark.qa("QA-MCP-003")
@pytest.mark.parametrize(
    ("locator", "expected_header"),
    [
        pytest.param(
            Locator(query="Equals", states=["enabled", "visible"]),
            "Found 7 elements matching query='Equals', states=['enabled', 'visible']:",
            id="query-and-states",
        ),
        pytest.param(
            Locator(app="kcalc", query="7", role="push button"),
            "Found 7 elements matching query='7', role='push button', app='kcalc':",
            id="every-field",
        ),
    ],
)
def test_locator_renders_found_header_over_the_tree_body(
    locator: Locator, expected_header: str
) -> None:
    result = ElementList(locator=locator, elements=ELEMENTS)

    assert result.render() == expected_header + "\n\n" + TREE_LINES


@pytest.mark.qa("QA-MCP-003")
@pytest.mark.parametrize(
    ("locator", "expected"),
    [
        pytest.param(Locator(query="7"), "No elements found matching query='7'", id="query"),
        pytest.param(
            Locator(states=["focused"]),
            "No elements found matching states=['focused']",
            id="states",
        ),
        pytest.param(
            Locator(query="x", states=[]),
            "No elements found matching query='x'",
            id="empty-states-hidden",
        ),
        pytest.param(
            Locator(query="it's", states=["a", "b"]),
            "No elements found matching query='it's', states=['a', 'b']",
            id="query-and-states",
        ),
        pytest.param(
            Locator(app="kcalc", role="push button"),
            "No elements found matching role='push button', app='kcalc'",
            id="role-and-app",
        ),
    ],
)
def test_locator_reports_no_matches(locator: Locator, expected: str) -> None:
    assert ElementList(locator=locator, elements=[]).render() == expected


@pytest.mark.qa("QA-MCP-003")
def test_from_worker_maps_unmapped_elements_without_coordinates() -> None:
    worker_element = {
        "ref": "e2.1",
        "role": "push button",
        "name": "7",
        "description": "",
        "states": [],
        "app": "kcalc",
        "window_id": None,
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
    mapped_element = {
        **worker_element,
        "ref": "e2.2",
        "window_id": "{w}",
        "mapped": True,
        "x": 3,
        "y": 4,
        "width": 5,
        "height": 6,
    }

    unmapped = UIElement.from_worker(worker_element)
    mapped = UIElement.from_worker(mapped_element)

    assert unmapped.rect is None
    assert unmapped.unavailable == "unmapped"
    assert unmapped.window_id is None
    assert mapped.rect == Rect(x=3, y=4, width=5, height=6)
    assert mapped.unavailable is None
    assert mapped.window_id == "{w}"
    result = ElementList(locator=Locator(query="7"), elements=[unmapped, mapped])
    assert result.render() == (
        "Found 2 elements matching query='7':\n\n"
        "  - [push button] \"7\" @ unavailable (unmapped) [ref=e2.1, app='kcalc']\n"
        '  - [push button] "7" @ screen (3, 4, 5x6) [ref=e2.2, window={w}]'
    )


def _top_level(**overrides: Any) -> UIElement:
    fields: dict[str, Any] = {"depth": 1, "role": "frame", "actions": [], "states": []}
    fields.update(overrides)
    return _element(**fields)


def _application(**overrides: Any) -> UIElement:
    fields: dict[str, Any] = {
        "depth": 0,
        "role": "application",
        "actions": [],
        "states": [],
        "window_id": None,
        **_unmapped("not-a-window"),
    }
    fields.update(overrides)
    return _element(**fields)


@pytest.mark.qa("QA-MCP-003")
def test_application_level_tree_keeps_placeholders_states_and_child_counts() -> None:
    # The max_depth=1 listing: child_count is the AT-SPI child count and may
    # exceed the listed top-levels; empty names get the placeholders.
    elements = [
        _application(ref="e3.1", name="kcalc", app="kcalc", child_count=2),
        _top_level(
            ref="e3.2",
            name="KCalc",
            states=["active", "focused"],
            window_id="{a}",
            rect=Rect(x=0, y=0, width=400, height=500),
        ),
        _application(ref="e3.3", name="", app="", child_count=1),
        _top_level(ref="e3.4", name="", app="", window_id=None, **_unmapped("no-extents")),
        _application(ref="e3.5", name="kwrite", app="kwrite", child_count=1),
        _top_level(
            ref="e3.6",
            name="Untitled — KWrite",
            app="kwrite",
            states=["focused"],
            window_id="{b}",
            rect=Rect(x=10, y=20, width=800, height=600),
        ),
    ]

    assert ElementList(elements=elements).render() == (
        "# Accessibility Tree (6 elements)\n\n"
        '- [application] "kcalc" @ unavailable (not-a-window) [ref=e3.1, children=2]\n'
        '  - [frame] "KCalc" (active, focused) @ screen (0, 0, 400x500) [ref=e3.2, window={a}]\n'
        '- [application] "(unnamed)" @ unavailable (not-a-window) [ref=e3.3, children=1]\n'
        '  - [frame] "(untitled)" @ unavailable (no-extents) [ref=e3.4]\n'
        '- [application] "kwrite" @ unavailable (not-a-window) [ref=e3.5, children=1]\n'
        '  - [frame] "Untitled — KWrite" (focused) @ screen (10, 20, 800x600)'
        " [ref=e3.6, window={b}]"
    )


# ── Windows ──────────────────────────────────────────────────────────────

WINDOWS = [
    Window(
        id="{3f2c-a1}",
        app_id="org.kde.kcalc",
        caption='KCalc — "x"',
        kind="normal",
        active=True,
        frame=Rect(x=-5, y=0, width=400, height=500),
        client=Rect(x=0, y=30, width=390, height=465),
        moveable=True,
        resizeable=True,
        closeable=True,
        desktops=["d1"],
        on_all_desktops=False,
    ),
    Window(
        id="{b}",
        app_id="org.kde.kwrite",
        caption="Untitled — KWrite",
        kind="dialog",
        active=False,
        frame=Rect(x=10, y=20, width=800, height=600),
        client=Rect(x=10, y=50, width=800, height=570),
        moveable=False,
        resizeable=False,
        closeable=True,
        desktops=[],
        on_all_desktops=True,
    ),
]

KCALC_WINDOW = (
    '- org.kde.kcalc "KCalc — "x"" [active]\n'
    "    id:     {3f2c-a1}\n"
    "    frame:  (-5, 0, 400x500)\n"
    "    client: (0, 30, 390x465)\n"
    "    kind:   normal\n"
    "    moveable: True, resizeable: True, closeable: True\n"
    "    desktops: ['d1']"
)
KWRITE_WINDOW = (
    '- org.kde.kwrite "Untitled — KWrite"\n'
    "    id:     {b}\n"
    "    frame:  (10, 20, 800x600)\n"
    "    client: (10, 50, 800x570)\n"
    "    kind:   dialog\n"
    "    moveable: False, resizeable: False, closeable: True\n"
    "    desktops: [] (all desktops)"
)


@pytest.mark.qa("QA-MCP-003")
def test_window_list_marks_only_the_active_window() -> None:
    expected = "Windows (2):\n" + KCALC_WINDOW + "\n" + KWRITE_WINDOW

    assert WindowList(windows=WINDOWS).render() == expected


@pytest.mark.qa("QA-MCP-003")
@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        pytest.param(None, "No windows found.", id="unfiltered"),
        pytest.param(
            WindowSelector(app_id="kcalc"), "No windows found for 'kcalc'.", id="app-filter"
        ),
        pytest.param(
            WindowSelector(id="{nope'}"), 'No window with id "{nope\'}".', id="window-id-repr"
        ),
        pytest.param(
            WindowSelector(id="{x}", app_id="kcalc"),
            "No window with id '{x}'.",
            id="window-id-wins-over-app",
        ),
        pytest.param(WindowSelector(active=True), "No active window.", id="active-only"),
        pytest.param(WindowSelector(caption="Untitled"), "No windows found.", id="caption"),
    ],
)
def test_window_list_reports_no_windows(selector: WindowSelector | None, expected: str) -> None:
    assert WindowList(filter=selector).render() == expected


@pytest.mark.qa("QA-MCP-003")
def test_active_window_filter_keeps_the_active_window_header() -> None:
    result = WindowList(filter=WindowSelector(active=True), windows=[WINDOWS[0]])

    assert result.render() == "Active window:\n" + KCALC_WINDOW
    # Any other filter uses the list header.
    other = WindowList(filter=WindowSelector(active=True, kind=["normal"]), windows=[WINDOWS[0]])
    assert other.render() == "Windows (1):\n" + KCALC_WINDOW


@pytest.mark.qa("QA-MCP-003")
def test_desktop_list_marks_the_current_desktop() -> None:
    result = DesktopList(
        desktops=[
            Desktop(id="d-1", name="Desktop 1", current=True),
            Desktop(id="d-2", name='Work "2"', current=False),
        ],
        current_desktop_id="d-1",
    )

    assert result.render() == (
        'Virtual desktops (2):\n- "Desktop 1" [current]\n    id: d-1\n- "Work "2""\n    id: d-2'
    )
    assert DesktopList(current_desktop_id=None).render() == "No virtual desktops found."


# ── Frames ───────────────────────────────────────────────────────────────

COORDINATE_SPACE = (
    "Coordinate space: logical; origin (0, 0); size 1280x800; backend ScreenShot2; "
    "coverage full; topology observed stable before/after capture"
)


def _frame(**overrides: Any) -> Frame:
    fields: dict[str, Any] = {
        "delay_ms": None,
        "path": "/tmp/kwin-mcp-screenshots-1/screenshot_20261012_120000.png",
        "uri": "kwin://screenshots/screenshot_20261012_120000.png",
        "width": 1280,
        "height": 800,
        "size_kb": 12.3,
        "mapping": ImageMapping(scale=1.0, origin=Point(x=0, y=0)),
        "coverage": "full",
        "coordinate_space": COORDINATE_SPACE,
    }
    fields.update(overrides)
    return Frame(**fields)


@pytest.mark.qa("QA-MCP-003")
def test_frame_renders_path_size_coordinate_space_and_image_line() -> None:
    assert _frame().render() == (
        "Screenshot saved: /tmp/kwin-mcp-screenshots-1/screenshot_20261012_120000.png"
        " (12.3 KB)\n" + COORDINATE_SPACE + "\nImage: 1280x800 px; scale 1; resource"
        " kwin://screenshots/screenshot_20261012_120000.png"
    )


@pytest.mark.qa("QA-MCP-003")
def test_downscaled_and_unproven_frames() -> None:
    downscaled = _frame(
        width=640, height=400, mapping=ImageMapping(scale=0.5, origin=Point(x=0, y=0))
    )
    unproven = _frame(
        size_kb=7.0,
        mapping=None,
        coordinate_space="Coordinate space: unavailable (topology changed); backend spectacle",
    )

    assert downscaled.render().endswith(
        "\nImage: 640x400 px; scale 0.5; resource kwin://screenshots/screenshot_20261012_120000.png"
    )
    assert unproven.render() == (
        "Screenshot saved: /tmp/kwin-mcp-screenshots-1/screenshot_20261012_120000.png (7.0 KB)\n"
        "Coordinate space: unavailable (topology changed); backend spectacle\n"
        "Image: 1280x800 px; scale unavailable; resource"
        " kwin://screenshots/screenshot_20261012_120000.png"
    )


# ── Actions ──────────────────────────────────────────────────────────────

LEFT_AND_SHIFT = HeldInput(buttons=["left"], keys=["shift"])


@pytest.mark.qa("QA-MCP-003")
def test_action_result_frames_warnings_and_still_held_come_last() -> None:
    frames = [
        _frame(delay_ms=0, path="/tmp/s/frame_0.png", uri="kwin://screenshots/frame_0.png"),
        _frame(
            delay_ms=50,
            path="/tmp/s/frame_50.png",
            uri="kwin://screenshots/frame_50.png",
            size_kb=9.8,
        ),
    ]
    result = PointerClickResult(
        at=Point(x=10, y=20),
        button="right",
        count=1,
        frames=frames,
        held=LEFT_AND_SHIFT,
        warnings=["moved while left was held"],
    )

    assert result.render() == "\n".join(
        [
            "Clicked right at (10, 20)",
            "Captured 2 frames:",
            "  0ms: /tmp/s/frame_0.png (12.3 KB)",
            "    " + COORDINATE_SPACE,
            "    Image: 1280x800 px; scale 1; resource kwin://screenshots/frame_0.png",
            "  50ms: /tmp/s/frame_50.png (9.8 KB)",
            "    " + COORDINATE_SPACE,
            "    Image: 1280x800 px; scale 1; resource kwin://screenshots/frame_50.png",
            "Warning: moved while left was held",
            "Still held: left, shift",
        ]
    )


@pytest.mark.qa("QA-MCP-003")
def test_no_still_held_line_when_nothing_is_held() -> None:
    result = PointerHoldResult(
        at=Point(x=8, y=8), phase="release", button="left", changed=True, held=HeldInput()
    )

    assert result.render() == "Button left released at (8, 8)"


@pytest.mark.qa("QA-MCP-003")
@pytest.mark.parametrize(
    "echoed",
    [pytest.param("Ab\tc\n", id="short"), pytest.param("x" * 200, id="at-limit")],
)
def test_text_echo_is_repr_up_to_the_limit(echoed: str) -> None:
    assert text_echo(echoed) == repr(echoed)


@pytest.mark.qa("QA-MCP-003")
def test_text_echo_over_the_limit_is_length_only() -> None:
    assert text_echo("y" * 201) == "<201 characters>"


@pytest.mark.qa("QA-MCP-003")
def test_type_route_values() -> None:
    assert TypeRoute(kind="wtype").render() == "wtype"
    assert TypeRoute(kind="paste", chord="ctrl+shift+v").render() == "paste:ctrl+shift+v"


# ── Sessions ─────────────────────────────────────────────────────────────


def _session(**overrides: Any) -> SessionInfo:
    fields: dict[str, Any] = {
        "kind": "virtual",
        "wayland_socket": "wayland-mcp-1",
        "dbus_address": "unix:path=/tmp/dbus-1",
        "screen": ScreenSize(width=1920, height=1080),
        "home_dir": None,
        "retention": Retention(),
        "artifacts_dir": "/tmp/kwin-mcp-screenshots-1",
        "input_backend": "eis",
        "clipboard": False,
        "accessibility": "on",
        "warnings": [],
    }
    fields.update(overrides)
    return SessionInfo(**fields)


def _live(accessibility: str, warnings: list[str]) -> SessionInfo:
    return _session(
        kind="live",
        wayland_socket="wayland-0",
        dbus_address="unix:path=/run/user/1000/bus",
        screen=ScreenSize(width=2560, height=1440),
        clipboard=True,
        accessibility=accessibility,
        warnings=warnings,
    )


LIVE_HEAD = (
    "Connected to live KWin session. D-Bus: unix:path=/run/user/1000/bus, Wayland: wayland-0\n"
    "Screen: 2560x1440\n"
    "Artifacts: /tmp/kwin-mcp-screenshots-1\n"
    "Retention: artifacts=false, home=false\n"
    "Clipboard: enabled\n"
    "Input backend: KWin EIS\n"
)


# ── The golden table ─────────────────────────────────────────────────────

GOLDENS = [
    # Sessions: the virtual start text and the four live accessibility states.
    pytest.param(
        _session(),
        "Session started. Wayland socket: wayland-mcp-1\n"
        "D-Bus: unix:path=/tmp/dbus-1\n"
        "Screen: 1920x1080\n"
        "Artifacts: /tmp/kwin-mcp-screenshots-1\n"
        "Retention: artifacts=false, home=false\n"
        "Clipboard: disabled\n"
        "Input backend: KWin EIS\n"
        "Accessibility: on (org.a11y.Status.IsEnabled)",
        id="session-virtual",
    ),
    pytest.param(
        _session(
            screen=ScreenSize(width=1280, height=800),
            home_dir="/tmp/kwin-mcp-home-1",
            retention=Retention(artifacts=True, home=True),
            input_backend=None,
            clipboard=True,
            warnings=["AT-SPI bus activation failed", "No input backend available (EIS refused)"],
        ),
        "Session started. Wayland socket: wayland-mcp-1\n"
        "D-Bus: unix:path=/tmp/dbus-1\n"
        "Isolated home: /tmp/kwin-mcp-home-1\n"
        "Screen: 1280x800\n"
        "Artifacts: /tmp/kwin-mcp-screenshots-1\n"
        "Retention: artifacts=true, home=true\n"
        "Clipboard: enabled\n"
        "Input backend: none\n"
        "Accessibility: on (org.a11y.Status.IsEnabled)\n"
        "Warning: AT-SPI bus activation failed\n"
        "Warning: No input backend available (EIS refused)",
        id="session-virtual-isolated-no-eis",
    ),
    pytest.param(
        _live("on", []),
        LIVE_HEAD + "Accessibility: on (org.a11y.Status.IsEnabled)",
        id="session-live-a11y-on",
    ),
    pytest.param(
        _live(
            "switched_on",
            [
                "switched on for this connection; "
                "an app that reads it only at startup must be restarted"
            ],
        ),
        LIVE_HEAD + "Accessibility: switched on for this connection "
        "(org.a11y.Status.IsEnabled); session_stop switches it back off. "
        "Firefox follows it at once; an app that reads it only at startup must be restarted.\n"
        "Warning: switched on for this connection; an app that reads it only at startup "
        "must be restarted",
        id="session-live-a11y-switched-on",
    ),
    pytest.param(
        _live("off", ["Reconnect with enable_accessibility=true"]),
        LIVE_HEAD + "Accessibility: off (org.a11y.Status.IsEnabled is false), so apps that "
        "check it, such as Firefox, expose no accessibility tree. Reconnect with "
        "enable_accessibility=true to switch it on until session_stop.\n"
        "Warning: Reconnect with enable_accessibility=true",
        id="session-live-a11y-off",
    ),
    pytest.param(
        _live("unavailable", ["org.a11y.Status unavailable (bus gone)"]),
        LIVE_HEAD + "Accessibility: org.a11y.Status unavailable\n"
        "Warning: org.a11y.Status unavailable (bus gone)",
        id="session-live-a11y-unavailable",
    ),
    pytest.param(
        SessionStopResult(stopped=False),
        "No session running.",
        id="session-stop-idempotent",
    ),
    pytest.param(
        SessionStopResult(
            stopped=True,
            kind="virtual",
            kept=KeptPaths(
                artifacts_dir="/tmp/kwin-mcp-screenshots-1", home_dir="/tmp/kwin-mcp-home-1"
            ),
            released=LEFT_AND_SHIFT,
        ),
        "Session stopped.\n"
        "Artifacts kept: /tmp/kwin-mcp-screenshots-1\n"
        "Isolated home kept: /tmp/kwin-mcp-home-1\n"
        "Released held input: left, shift",
        id="session-stop-virtual",
    ),
    pytest.param(
        SessionStopResult(
            stopped=True,
            kind="live",
            warnings=[
                "EIS disconnected: held input is no longer tracked; KWin may keep it pressed"
            ],
        ),
        "Disconnected from live session.\n"
        "Warning: EIS disconnected: held input is no longer tracked; KWin may keep it pressed",
        id="session-stop-live",
    ),
    pytest.param(SessionInfoResult(session=None), "No session running.", id="session-info-none"),
    pytest.param(
        SessionInfoResult(
            session=_session(),
            apps=[
                AppEntry(
                    pid=4242,
                    command="kate --new",
                    running=True,
                    exit_code=None,
                    log_path="/tmp/kwin-mcp-screenshots-1/app_kate_1.log",
                ),
                AppEntry(
                    pid=4243,
                    command="kcalc",
                    running=False,
                    exit_code=0,
                    log_path="/tmp/kwin-mcp-screenshots-1/app_kcalc_2.log",
                ),
            ],
            held=HeldInput(buttons=["left"]),
            warnings=[
                "KWin replaced the EIS keyboard device: held shift stays pressed until "
                "released, but new keys no longer see it as a modifier"
            ],
        ),
        "Session: virtual. Wayland socket: wayland-mcp-1\n"
        "D-Bus: unix:path=/tmp/dbus-1\n"
        "Screen: 1920x1080\n"
        "Artifacts: /tmp/kwin-mcp-screenshots-1\n"
        "Retention: artifacts=false, home=false\n"
        "Clipboard: disabled\n"
        "Input backend: KWin EIS\n"
        "Accessibility: on (org.a11y.Status.IsEnabled)\n"
        "Apps (2):\n"
        "- PID 4242: kate --new (running); log: /tmp/kwin-mcp-screenshots-1/app_kate_1.log\n"
        "- PID 4243: kcalc (exited 0); log: /tmp/kwin-mcp-screenshots-1/app_kcalc_2.log\n"
        "Warning: KWin replaced the EIS keyboard device: held shift stays pressed until "
        "released, but new keys no longer see it as a modifier\n"
        "Still held: left",
        id="session-info",
    ),
    # Apps.
    pytest.param(
        AppLaunchResult(
            pid=4242,
            command="kate --new",
            log_path="/tmp/kwin-mcp-screenshots-1/app_kate_1.log",
            log_uri="kwin://apps/4242/log",
        ),
        "App launched: kate --new (PID=4242)\n"
        "App log: /tmp/kwin-mcp-screenshots-1/app_kate_1.log\n"
        "Log resource: kwin://apps/4242/log",
        id="app-launch",
    ),
    pytest.param(
        AppLogResult(pid=4242, log_path="/tmp/a/app_kate_1.log", lines=[], truncated=False),
        "App log of PID 4242: /tmp/a/app_kate_1.log\n(no log output yet)",
        id="app-log-empty",
    ),
    pytest.param(
        AppLogResult(pid=4242, log_path="/tmp/a/app_kate_1.log", lines=["b", "c"], truncated=True),
        "App log of PID 4242: /tmp/a/app_kate_1.log (last 2 lines)\nb\nc",
        id="app-log-tail",
    ),
    # Frames and elements.
    pytest.param(
        _frame(),
        "Screenshot saved: /tmp/kwin-mcp-screenshots-1/screenshot_20261012_120000.png"
        " (12.3 KB)\n" + COORDINATE_SPACE + "\nImage: 1280x800 px; scale 1; resource"
        " kwin://screenshots/screenshot_20261012_120000.png",
        id="frame",
    ),
    pytest.param(
        ElementList(elements=ELEMENTS),
        "# Accessibility Tree (7 elements)\n\n" + TREE_LINES,
        id="element-list-tree",
    ),
    pytest.param(
        ElementList(
            locator=Locator(query="OK"),
            elements=[],
            wait=ElementWaitResult(
                until="present", timeout_ms=200, satisfied=False, elapsed_ms=230
            ),
        ),
        "NOT SATISFIED: Timeout after 200 ms (until=present, elapsed 230 ms)\n"
        "No elements found matching query='OK'",
        id="element-list-wait-timeout",
    ),
    pytest.param(
        ElementList(
            locator=Locator(query="7", states=["focused"]),
            elements=[_element(depth=3, states=["focused"])],
            wait=ElementWaitResult(until="absent", timeout_ms=0, satisfied=False, elapsed_ms=40),
        ),
        "NOT SATISFIED: Timeout after 0 ms (until=absent, elapsed 40 ms)\n"
        "Found 1 elements matching query='7', states=['focused']:\n\n"
        '      - [push button] "7" (focused) @ screen (10, 20, 30x40) [actions: Press]'
        " [ref=e1.6, app='kcalc', window={w1}]",
        id="element-list-wait-absent-timeout",
    ),
    pytest.param(
        ElementList(
            locator=Locator(query="7"),
            elements=[_element(depth=1, name="7", role="frame", actions=[])],
            wait=ElementWaitResult(
                until="present", timeout_ms=5000, satisfied=True, elapsed_ms=120
            ),
        ),
        "Found 1 elements matching query='7':\n\n"
        '  - [frame] "7" (enabled, visible) @ screen (10, 20, 30x40)'
        " [ref=e1.6, app='kcalc', window={w1}]\n"
        "Wait satisfied (until=present) after 120 ms",
        id="element-list-wait-satisfied",
    ),
    # Actions.
    pytest.param(
        PointerClickResult(
            at=Point(x=10, y=20), button="left", count=2, modifiers="ctrl+shift", hold_ms=500
        ),
        "Clicked left at (10, 20) (double) with ctrl+shift held 500ms",
        id="pointer-click-double",
    ),
    pytest.param(
        PointerClickResult(at=Point(x=1, y=2), button="middle", count=3, held=LEFT_AND_SHIFT),
        "Clicked middle at (1, 2) (triple)\nStill held: left, shift",
        id="pointer-click-triple-held",
    ),
    pytest.param(PointerMoveResult(at=Point(x=3, y=4)), "Mouse moved to (3, 4)", id="pointer-move"),
    pytest.param(
        PointerScrollResult(
            at=Point(x=5, y=6), delta=3, axis="vertical", unit="smooth_step", steps=3
        ),
        "Scrolled vertical (smooth) by 3 at (5, 6) in 3 steps",
        id="pointer-scroll-smooth",
    ),
    pytest.param(
        PointerScrollResult(
            at=Point(x=5, y=6), delta=-2, axis="horizontal", unit="detent", steps=1
        ),
        "Scrolled horizontal (discrete) by -2 at (5, 6)",
        id="pointer-scroll-detent",
    ),
    pytest.param(
        PointerDragResult(
            start=Point(x=1, y=2),
            at=Point(x=30, y=40),
            button="left",
            modifiers="ctrl",
            waypoints=2,
        ),
        "Dragged from (1, 2) to (30, 40) with ctrl via 2 waypoints",
        id="pointer-drag",
    ),
    pytest.param(
        PointerDragResult(
            start=Point(x=1, y=2), at=Point(x=30, y=40), button="right", dwell_ms=300
        ),
        "Dragged from (1, 2) to (30, 40) with the right button, dwelling 300ms before the release",
        id="pointer-drag-right-dwell",
    ),
    pytest.param(
        PointerHoldResult(
            at=Point(x=5, y=5),
            phase="press",
            button="left",
            changed=True,
            held=HeldInput(buttons=["left"]),
        ),
        "Button left pressed at (5, 5)\nStill held: left",
        id="pointer-hold-press",
    ),
    pytest.param(
        PointerHoldResult(
            at=Point(x=6, y=6),
            phase="press",
            button="left",
            changed=False,
            held=HeldInput(buttons=["left"]),
        ),
        "Button left already held; moved to (6, 6) without a press\nStill held: left",
        id="pointer-hold-press-idempotent",
    ),
    pytest.param(
        PointerHoldResult(
            at=Point(x=7, y=7),
            phase="move",
            button="left",
            changed=False,
            held=HeldInput(buttons=["left"]),
        ),
        "Button left moved to (7, 7) while held\nStill held: left",
        id="pointer-hold-move",
    ),
    pytest.param(
        PointerHoldResult(at=Point(x=8, y=8), phase="release", button="middle", changed=False),
        "Button middle released at (8, 8) (it was not held by kwin-mcp)",
        id="pointer-hold-untracked-release",
    ),
    pytest.param(
        KeyboardPressResult(keys="ctrl+shift+t"), "Pressed: ctrl+shift+t", id="keyboard-press"
    ),
    pytest.param(
        KeyboardTypeResult(method="keys", route="keys", chars=5, echo=text_echo("Ab\tc\n")),
        "Typed: 'Ab\\tc\\n'\nRoute: keys; 5 characters",
        id="keyboard-type-keys",
    ),
    pytest.param(
        KeyboardTypeResult(
            method="unicode",
            route="paste:ctrl+shift+v",
            chars=201,
            echo=text_echo("한" * 201),
        ),
        "Typed unicode: <201 characters>\nRoute: paste:ctrl+shift+v; 201 characters",
        id="keyboard-type-unicode-long",
    ),
    pytest.param(
        KeyboardHoldResult(
            phase="press", keys="ctrl+shift", changed=True, held=HeldInput(keys=["ctrl", "shift"])
        ),
        "Key down: ctrl+shift\nStill held: ctrl, shift",
        id="keyboard-hold-press",
    ),
    pytest.param(
        KeyboardHoldResult(
            phase="press", keys="shift", changed=False, held=HeldInput(keys=["shift"])
        ),
        "Key down: shift (already held; no event sent)\nStill held: shift",
        id="keyboard-hold-press-idempotent",
    ),
    pytest.param(
        KeyboardHoldResult(phase="release", keys="alt", changed=False),
        "Key up: alt (it was not held by kwin-mcp)",
        id="keyboard-hold-untracked-release",
    ),
    pytest.param(
        TouchTapResult(at=Point(x=1, y=2), hold_ms=600),
        "Touch tap at (1, 2) held 600ms",
        id="touch-tap",
    ),
    pytest.param(
        TouchSwipeResult(start=Point(x=0, y=0), at=Point(x=100, y=0), fingers=1, duration_ms=300),
        "Touch swipe from (0, 0) to (100, 0) in 300ms",
        id="touch-swipe-one-finger",
    ),
    pytest.param(
        TouchSwipeResult(start=Point(x=0, y=0), at=Point(x=100, y=0), fingers=3, duration_ms=300),
        "3-finger swipe from (0, 0) to (100, 0) in 300ms",
        id="touch-swipe-three-fingers",
    ),
    pytest.param(
        TouchPinchResult(
            at=Point(x=50, y=50), start_distance=200, end_distance=100, duration_ms=500
        ),
        "Pinch in at (50, 50): 200→100px in 500ms",
        id="touch-pinch",
    ),
    pytest.param(
        ElementActResult(
            element=_element(),
            action="Press",
            dispatch="confirmed",
        ),
        'Invoked AT-SPI action: Press on [push button] "7" in kcalc\n'
        'Element: - [push button] "7" (enabled, visible) @ screen (10, 20, 30x40)'
        " [actions: Press] [ref=e1.6, app='kcalc', window={w1}]",
        id="element-act-confirmed",
    ),
    pytest.param(
        ElementActResult(
            element=_element(),
            action="Press",
            dispatch="sent_unconfirmed",
            dispatch_error="Timeout was reached",
        ),
        "Sent AT-SPI action 'Press' to [push button] \"7\" in kcalc, but the application did "
        "not confirm (Timeout was reached); the action may have run — check the UI before "
        "retrying.\n"
        'Element: - [push button] "7" (enabled, visible) @ screen (10, 20, 30x40)'
        " [actions: Press] [ref=e1.6, app='kcalc', window={w1}]",
        id="element-act-sent-unconfirmed",
    ),
    # Windows and desktops.
    pytest.param(
        WindowList(windows=WINDOWS),
        "Windows (2):\n" + KCALC_WINDOW + "\n" + KWRITE_WINDOW,
        id="window-list",
    ),
    pytest.param(
        DesktopList(
            desktops=[Desktop(id="d-1", name="Desktop 1", current=True)], current_desktop_id="d-1"
        ),
        'Virtual desktops (1):\n- "Desktop 1" [current]\n    id: d-1',
        id="desktop-list",
    ),
    pytest.param(
        WindowFocusResult(window_id="{3f2c-a1}", window=WINDOWS[0]),
        'Focused: org.kde.kcalc KCalc — "x"\n' + KCALC_WINDOW,
        id="window-focus",
    ),
    pytest.param(
        WindowFocusResult(window_id="{gone}", window=None),
        "Focused: {gone}\nobserved: window no longer exists (it closed or its client exited)",
        id="window-focus-vanished",
    ),
    pytest.param(
        WindowFocusResult(
            window_id="{3f2c-a1}", window=None, observation_error="KWin query timed out after 30s"
        ),
        "Focused: {3f2c-a1}\n"
        "observation failed: KWin query timed out after 30s (the window may already have moved)",
        id="window-focus-readback-failed",
    ),
]

PLACE_BEFORE = PlaceBefore(
    app_id="org.kde.kcalc",
    caption='KCalc — "x"',
    frame=Rect(x=0, y=0, width=400, height=500),
    desktops=["d1"],
    on_all_desktops=False,
    moveable=True,
    resizeable=True,
)

GOLDENS += [
    pytest.param(
        WindowPlaceResult(
            window_id="{3f2c-a1}",
            requested_frame=PartialRect(x=-5, width=300),
            window=WINDOWS[0],
            before=PLACE_BEFORE,
            applied=PlaceApplied(frame=False),
            pending=["width"],
        ),
        'Geometry update requested: org.kde.kcalc "KCalc — "x"" ({3f2c-a1})\n'
        "before:    (0, 0, 400x500)\n"
        "requested: x=-5, y=None, width=300, height=None\n"
        "observed:  (-5, 0, 400x500)\n"
        "not yet applied: width (the client may still be applying the change; verify with "
        "window_list)\n"
        "moveable: True, resizeable: True\n" + KCALC_WINDOW,
        id="window-place-frame-pending",
    ),
    pytest.param(
        WindowPlaceResult(
            window_id="{3f2c-a1}",
            requested_desktop="d1",
            window=WINDOWS[0],
            before=PLACE_BEFORE.model_copy(update={"desktops": [], "on_all_desktops": True}),
            applied=PlaceApplied(desktop=True),
            current_desktop_before="d1",
            current_desktop_after="d1",
        ),
        "Window desktop set: {3f2c-a1} -> d1\n"
        "before:   [] (all desktops)\n"
        "observed: ['d1']\n"
        "current desktop: d1 -> d1\n"
        "moveable: True, resizeable: True\n" + KCALC_WINDOW,
        id="window-place-desktop",
    ),
    pytest.param(
        WindowPlaceResult(
            window_id="{3f2c-a1}",
            requested_frame=PartialRect(x=5),
            window=None,
            before=PLACE_BEFORE,
            applied=PlaceApplied(frame=False),
        ),
        'Geometry update requested: org.kde.kcalc "KCalc — "x"" ({3f2c-a1})\n'
        "before:    (0, 0, 400x500)\n"
        "requested: x=5, y=None, width=None, height=None\n"
        "observed: window no longer exists (it closed or its client exited)\n"
        "moveable: True, resizeable: True",
        id="window-place-vanished",
    ),
    pytest.param(
        WindowPlaceResult(
            window_id="{3f2c-a1}",
            requested_frame=PartialRect(y=7),
            requested_desktop="d2",
            window=None,
            observation_error="KWin query timed out after 30s",
            before=PLACE_BEFORE,
            applied=PlaceApplied(frame=False, desktop=False),
            current_desktop_before="d1",
            warnings=["frame capture failed after the action: no ScreenShot2"],
        ),
        "Window desktop not confirmed: {3f2c-a1} -> d2\n"
        "before:   ['d1']\n"
        "current desktop: d1 -> None\n"
        'Geometry update requested: org.kde.kcalc "KCalc — "x"" ({3f2c-a1})\n'
        "before:    (0, 0, 400x500)\n"
        "requested: x=None, y=7, width=None, height=None\n"
        "observation failed: KWin query timed out after 30s (the window may already have moved)\n"
        "moveable: True, resizeable: True\n"
        "Warning: frame capture failed after the action: no ScreenShot2",
        id="window-place-readback-failed",
    ),
    # window_close: every outcome text verbatim.
    pytest.param(
        WindowCloseResult(
            window_id="{3f2c-a1}",
            app_id="org.kde.kcalc",
            caption="KCalc",
            requested=True,
            consent="not_required",
        ),
        'Close requested: org.kde.kcalc "KCalc" ({3f2c-a1}). The app may keep the window open, '
        "for example to ask about unsaved changes; check window_list.",
        id="window-close-requested",
    ),
    pytest.param(
        WindowCloseResult(window_id="{nope}", requested=False, refusal="no_window"),
        "No window with id '{nope}'.",
        id="window-close-no-window",
    ),
    pytest.param(
        WindowCloseResult(
            window_id="{panel}",
            app_id="plasmashell",
            caption="Panel",
            requested=False,
            consent="not_required",
            refusal="not_closeable",
        ),
        'Window plasmashell "Panel" ({panel}) cannot be closed.',
        id="window-close-not-closeable",
    ),
    pytest.param(
        WindowCloseResult(
            window_id="{k}", requested=False, consent="not_confirmed", refusal="no_consent"
        ),
        "window_close not performed: the user did not confirm closing window {k}.",
        id="window-close-not-confirmed",
    ),
    pytest.param(
        WindowCloseResult(
            window_id="{k}", requested=False, consent="declined", refusal="no_consent"
        ),
        "window_close not performed: the user declined closing window {k}. "
        "Do not retry unless the user asks.",
        id="window-close-declined",
    ),
    pytest.param(
        WindowCloseResult(
            window_id="{k}", requested=False, consent="cancelled", refusal="no_consent"
        ),
        "window_close not performed: the confirmation for window {k} was dismissed or never "
        "shown (a client with no one to answer cancels it automatically). If the user asked "
        "to close this window, close it through the app's own UI instead.",
        id="window-close-cancelled",
    ),
    pytest.param(
        WindowCloseResult(
            window_id="{k}", requested=False, consent="unsupported", refusal="no_consent"
        ),
        "window_close is disabled in live sessions: it could discard unsaved work on the real "
        "desktop. Close the window through the app's own UI instead.",
        id="window-close-unsupported",
    ),
    # Clipboard and escape hatches.
    pytest.param(ClipboardText(text="héllo\n"), "héllo\n", id="clipboard-read"),
    pytest.param(
        ClipboardWriteResult(length=5, echo=text_echo("hello")),
        "Clipboard set: 'hello'",
        id="clipboard-write",
    ),
    pytest.param(
        ClipboardWriteResult(length=201, echo=text_echo("z" * 201)),
        "Clipboard set: <201 characters>",
        id="clipboard-write-long",
    ),
    pytest.param(
        DbusReply(reply="{a1b2}", signature="s"),
        "{a1b2}\n(reply signature 's')",
        id="dbus-string",
    ),
    pytest.param(DbusReply(reply=None, signature=""), "(no reply)", id="dbus-void"),
    pytest.param(
        DbusReply(reply=True, signature="b"), "true\n(reply signature 'b')", id="dbus-bool"
    ),
    pytest.param(
        DbusReply(reply=1.5, signature="d"), "1.5\n(reply signature 'd')", id="dbus-double"
    ),
    pytest.param(
        DbusReply(reply=[1, "x"], signature="is"),
        "[1, \"x\"]\n(reply signature 'is')",
        id="dbus-multi-value",
    ),
    pytest.param(
        DbusReply(reply={"a": [1, 2]}, signature="a{sv}"),
        "{\"a\": [1, 2]}\n(reply signature 'a{sv}')",
        id="dbus-container",
    ),
    pytest.param(
        WaylandGlobals(
            lines=["interface: 'wl_seat', version: 9, name: 3"],
            globals=[WaylandGlobal(interface="wl_seat", version=9, name=3)],
        ),
        "interface: 'wl_seat', version: 9, name: 3",
        id="wayland-all",
    ),
    pytest.param(
        WaylandGlobals(filter="WL_SEAT", lines=[]),
        "No protocols matching 'WL_SEAT' found.",
        id="wayland-no-match",
    ),
]


@pytest.mark.qa("QA-MCP-003")
@pytest.mark.parametrize(("result", "expected"), GOLDENS)
@pytest.mark.qa("QA-MCP-002")
def test_render_goldens_per_model(result: Any, expected: str) -> None:
    assert result.render() == expected


# A pydantic repr such as ``Point(x=1, y=2)`` or ``Rect(x=...`` must never reach the text.
_PYDANTIC_REPR = re.compile(r"\b[A-Z][A-Za-z]+\(\w+=")


@pytest.mark.qa("QA-MCP-003")
@pytest.mark.parametrize(("result", "expected"), GOLDENS)
def test_render_contains_no_pydantic_repr(result: Any, expected: str) -> None:
    del expected

    assert _PYDANTIC_REPR.search(result.render()) is None


@pytest.mark.qa("QA-MCP-003")
@pytest.mark.parametrize(("result", "expected"), GOLDENS)
def test_render_is_reproducible_from_structured_content(result: Any, expected: str) -> None:
    # The server sends model_dump(mode="json") as structuredContent; a client
    # that re-validates it must be able to rebuild the same text.
    rebuilt = type(result).model_validate(result.model_dump(mode="json"))

    assert rebuilt.render() == expected


@pytest.mark.qa("QA-MCP-003")
def test_action_result_base_summary_names_the_tool() -> None:
    assert ActionResult(tool="custom", at=Point(x=1, y=2)).render() == "custom at (1, 2)"


@pytest.mark.qa("QA-MCP-016")
def test_engine_exception_is_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected engine failure is a tool error carrying its message, not a crash.

    The first line keeps 0.11's ``Error executing tool <name>: <msg>`` and the
    result has no structured content; the server keeps serving the next call.
    """
    engine = AutomationEngine()

    def explode() -> SessionInfoResult:
        msg = "unexpected engine failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(engine, "session_info", explode)
    server = build_server(engine=engine)

    async def run() -> tuple[CallToolResult, CallToolResult]:
        async with Client(server, mode="legacy") as client:
            failed = await client.call_tool("session_info", {})
            after = await client.call_tool("session_stop", {})
        return failed, after

    failed, after = asyncio.run(run())

    assert failed.is_error
    assert failed.structured_content is None
    block = failed.content[0]
    assert isinstance(block, TextContent)
    assert block.text.splitlines()[0] == (
        "Error executing tool session_info: unexpected engine failure"
    )
    assert not after.is_error, after.content
