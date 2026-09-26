"""End-to-end coverage for window activation, scrolling and dragging.

These flows all need a window's global position, which find_ui_elements now
reports directly in screen coordinates.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from _asserts import element_count

if TYPE_CHECKING:
    from collections.abc import Callable

    from kwin_mcp.core import AutomationEngine

LONG_DOCUMENT = "/tmp/kwin-mcp-scroll.txt"
_SCROLLBAR = re.compile(r"\[scroll bar\][^\n]*?value=(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)")
PROBE_COMMAND = "python3 /app/tests/e2e/interaction_probe.py"
PROBE_SELECTOR = "interaction_probe.py"
PROBE_ENV = {
    "GDK_BACKEND": "wayland",
    "GTK_MODULES": "gail:atk-bridge",
    "NO_AT_BRIDGE": "0",
    "XDG_SESSION_TYPE": "wayland",
}
STATE_TIMEOUT_SECONDS = 5.0
_RECT = r"\((-?\d+), (-?\d+), (\d+)x(\d+)\)"


def _windows_of(engine: AutomationEngine, app: str) -> list[str]:
    """Window lines belonging to one application section of list_windows()."""
    lines: list[str] = []
    inside = False
    for line in engine.list_windows().splitlines():
        if line.startswith("- "):
            inside = line.startswith(f"- {app} ")
            continue
        if inside and line.strip().startswith("- "):
            lines.append(line.strip())
    return lines


def _text_area(engine: AutomationEngine, app: str, name: str) -> tuple[int, int, int, int]:
    elements = engine.find_ui_elements(query=name, app_name=app)
    match = re.search(rf'\[text\] "{name}" @ screen \((\d+), (\d+), (\d+)x(\d+)\)', elements)
    assert match is not None, elements[:500]
    x, y, width, height = match.groups()
    return int(x), int(y), int(width), int(height)


def _scroll_position(engine: AutomationEngine, app: str) -> float:
    """Vertical scrollbar position, which AT-SPI exposes through Value."""
    for line in engine.find_ui_elements(query="", app_name=app).splitlines():
        match = _SCROLLBAR.search(line)
        if match and float(match.group(2)) > 0 and "0x0" not in line:
            return float(match.group(1))
    raise AssertionError("no scrollbar reported a usable value")


def _swipe_span(engine: AutomationEngine) -> tuple[int, int, int]:
    """Column and start/end rows for a swipe kept inside the text widget.

    Fixed pixel offsets around the centre can fall outside a short widget, and
    a touch that starts off the editor scrolls nothing.
    """
    tx, ty, tw, th = _text_area(engine, "kwrite", "kwin-mcp-scroll.txt")
    return tx + tw // 2, ty + (th * 3) // 4, ty + th // 4


def _probe_rect(engine: AutomationEngine, name: str) -> tuple[int, int, int, int]:
    """Global rectangle of a probe widget, once KWin has matched its window."""
    pattern = re.compile(rf'^- \[[^]]+] "{re.escape(name)}" @ screen {_RECT}', re.MULTILINE)
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    elements = ""
    while time.monotonic() < deadline:
        elements = engine.find_ui_elements(query=name, app_name=PROBE_SELECTOR)
        match = pattern.search(elements)
        if match is not None:
            x, y, width, height = (int(value) for value in match.groups())
            return x, y, width, height
        time.sleep(0.2)
    raise AssertionError(elements[:500])


def _probe_field(status: str, name: str) -> str:
    match = re.search(rf"(?:^| ){re.escape(name)}=([^ ]+)", status)
    assert match is not None, status
    return match.group(1)


def _wait_for_drag_release(engine: AutomationEngine) -> str:
    """The probe's drag status once a touch sequence has ended."""
    pattern = re.compile(r'^- \[label] "(drag_status:[^"]*)"', re.MULTILINE)
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    status = ""
    while time.monotonic() < deadline:
        match = pattern.search(
            engine.find_ui_elements(query="drag_status:", app_name=PROBE_SELECTOR)
        )
        status = match.group(1) if match else ""
        if status and _probe_field(status, "release") != "none":
            return status
        time.sleep(0.1)
    raise AssertionError(f"touch sequence never ended on the probe: {status}")


def _open_editor(engine: AutomationEngine, wait_for_app: Callable[[str], str]) -> None:
    with open(LONG_DOCUMENT, "w") as handle:
        handle.write("\n".join(f"line {i:03d} ---------------------------" for i in range(200)))
    engine.launch_app(command=f"kwrite {LONG_DOCUMENT}")
    wait_for_app("kwrite")
    # Placement and the first paint settle shortly after the app reaches the bus.
    time.sleep(2)


def _scroll_target(engine: AutomationEngine) -> tuple[int, int]:
    tx, ty, tw, th = _text_area(engine, "kwrite", "kwin-mcp-scroll.txt")
    return tx + tw // 2, ty + th // 2


def test_focus_window_activates_the_requested_window(
    kcalc_session: AutomationEngine, wait_for_app: Callable[[str], str]
) -> None:
    _open_editor(kcalc_session, wait_for_app)
    assert "Focused" in kcalc_session.focus_window(app_name="kcalc")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any("[active]" in line for line in _windows_of(kcalc_session, "kcalc")):
            return
        time.sleep(0.25)
    raise AssertionError(f"kcalc never became active: {kcalc_session.list_windows()[:500]}")


def test_mouse_scroll_moves_the_scrollbar(
    kcalc_session: AutomationEngine, wait_for_app: Callable[[str], str]
) -> None:
    _open_editor(kcalc_session, wait_for_app)
    cx, cy = _scroll_target(kcalc_session)
    assert _scroll_position(kcalc_session, "kwrite") == 0.0

    kcalc_session.mouse_scroll(x=cx, y=cy, delta=10)
    time.sleep(1)
    smooth = _scroll_position(kcalc_session, "kwrite")
    assert smooth > 0.0, "smooth scrolling did not move the view"

    # Discrete scrolling counts wheel detents, which libei transmits in 120ths.
    kcalc_session.mouse_scroll(x=cx, y=cy, delta=5, discrete=True)
    time.sleep(1)
    assert _scroll_position(kcalc_session, "kwrite") > smooth, "discrete scrolling did nothing"


def test_touch_swipe_scrolls_the_editor(
    kcalc_session: AutomationEngine, wait_for_app: Callable[[str], str]
) -> None:
    _open_editor(kcalc_session, wait_for_app)
    # Touch lands by position, so make sure kcalc is not stacked on the target.
    kcalc_session.focus_window(app_name="kwrite")
    time.sleep(1)
    cx, from_y, to_y = _swipe_span(kcalc_session)
    assert _scroll_position(kcalc_session, "kwrite") == 0.0
    kcalc_session.touch_swipe(from_x=cx, from_y=from_y, to_x=cx, to_y=to_y, duration_ms=300)
    time.sleep(1.5)
    assert _scroll_position(kcalc_session, "kwrite") > 0.0, "touch swipe did not scroll"


def test_mouse_drag_selects_text(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
) -> None:
    # Reading a selection back needs the clipboard, which is opt-in per session.
    start_session(enable_clipboard=True)
    _open_editor(engine, wait_for_app)

    tx, ty, _, _ = _text_area(engine, "kwrite", "kwin-mcp-scroll.txt")
    line_y = ty + 12
    engine.mouse_drag(from_x=tx + 60, from_y=line_y, to_x=tx + 170, to_y=line_y)
    time.sleep(0.8)
    engine.keyboard_key(key="ctrl+c")
    time.sleep(0.8)

    selection = engine.clipboard_get()
    # A near-no-op drag would still copy a character or two, so require a span.
    assert len(selection) >= 8, repr(selection)
    assert selection in "line 000 ---------------------------", repr(selection)


def test_touch_multi_swipe_delivers_both_fingers_to_the_app(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
) -> None:
    """Both touch sequences of a two-finger swipe reach the application.

    KWrite cannot show this: its QScroller only flicks on exactly one
    touchscreen point, and once Qt groups the second finger with the first
    (QTBUG-125197) a two-finger swipe no longer scrolls it. The GTK probe
    counts the touch sequences it receives instead. See docker/README.md.
    """
    output = start_session(PROBE_COMMAND, env=PROBE_ENV)
    assert "Input backend: KWin EIS" in output, output
    wait_for_app(PROBE_SELECTOR)
    x, y, width, height = _probe_rect(engine, "Drag Target")
    from_x, from_y = x + width // 4, y + (height * 3) // 4
    to_x, to_y = x + width * 3 // 4, y + height // 4
    dx, dy = to_x - from_x, from_y - to_y

    engine.touch_multi_swipe(
        from_x=from_x, from_y=from_y, to_x=to_x, to_y=to_y, fingers=2, duration_ms=400
    )
    status = _wait_for_drag_release(engine)

    assert _probe_field(status, "source") == "touch", status
    assert _probe_field(status, "release") == "touch", status
    assert int(_probe_field(status, "fingers")) == 2, status
    assert int(_probe_field(status, "motions")) >= 20, status
    # bounds are (min_x,min_y,max_x,max_y) over both fingers in widget pixels.
    # The spans prove the requested displacement arrived in both axes: the
    # vertical span is the swipe's dy plus the fingers' 20px spacing, so a
    # stream that loses Y motion cannot produce it.
    bounds = [int(v) for v in _probe_field(status, "bounds").strip("()").split(",")]
    assert bounds[2] - bounds[0] >= dx * 3 // 4, status
    assert bounds[3] - bounds[1] >= (dy + 20) * 3 // 4, status


def test_touch_pinch_delivers_multitouch_to_the_app(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
) -> None:
    """KWrite has no pinch-zoom, so this pins delivery, not zoom semantics.

    The editor treats the two touch points as a selection drag, which is
    readable through the clipboard; a pinch that never reached the app would
    leave the selection empty.
    """
    start_session(enable_clipboard=True)
    _open_editor(engine, wait_for_app)
    tx, ty, tw, th = _text_area(engine, "kwrite", "kwin-mcp-scroll.txt")
    cx, cy = tx + tw // 2, ty + th // 2
    engine.clipboard_set(text="no-selection-yet")
    engine.touch_pinch(
        center_x=cx, center_y=cy, start_distance=80, end_distance=320, duration_ms=400
    )
    time.sleep(1.2)
    engine.keyboard_key(key="ctrl+c")
    time.sleep(0.8)

    selection = engine.clipboard_get()
    assert selection != "no-selection-yet", "pinch produced no selection: touches never landed"
    assert len(selection) >= 8, repr(selection)


def test_find_ui_elements_reports_scrollbar_values(
    kcalc_session: AutomationEngine, wait_for_app: Callable[[str], str]
) -> None:
    """The Value interface is what makes scroll position assertable at all."""
    _open_editor(kcalc_session, wait_for_app)
    elements = kcalc_session.find_ui_elements(query="", app_name="kwrite")
    assert element_count(elements) > 0, elements[:300]
    assert _SCROLLBAR.search(elements) is not None, "no scrollbar reported a value"
