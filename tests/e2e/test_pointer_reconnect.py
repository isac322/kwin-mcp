"""Clicks must reach a window that appeared beneath a parked pointer (issue #66).

KWin sends no ``wl_pointer.motion`` when a surface is mapped or moved under a
stationary cursor, and it drops an absolute motion to the cursor's current
position. A new window placed so its button lies under the parked cursor
therefore keeps the stale surface-local position from when it mapped, and a
click that only re-sent the same position was dispatched by GTK at that stale
spot. The persistent ``animation_clicks`` counter is the oracle: the API's
success string never proved delivery.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from session_harness import live_kwin

from kwin_mcp import geometry

if TYPE_CHECKING:
    import pytest
    from session_harness import LiveKWin

    from kwin_mcp.core import AutomationEngine

PROBE_COMMAND = "python3 /app/tests/e2e/counted_gui_probe.py"
PROBE_SELECTOR = "counted_gui_probe.py"
PROBE_ENV = {
    "GDK_BACKEND": "wayland",
    "GTK_MODULES": "gail:atk-bridge",
    "NO_AT_BRIDGE": "0",
    "XDG_SESSION_TYPE": "wayland",
}
# Well away from KWin's centred initial placement, so the surface-local point
# recorded when the window maps under the parked cursor misses the button.
FRAME_ORIGIN = (40, 100)
STATE_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.1
_RECT = r"\((-?\d+), (-?\d+), (\d+)x(\d+)\)"
_BUTTON = re.compile(rf'\[button\] "Animation Target" .*?@ screen {_RECT}')
_CLICKS = re.compile(r'"animation_clicks: (\d+)"')
_PLACE_SCRIPT = """
var windows = workspace.windowList();
var placed = 0;
for (var i = 0; i < windows.length; i++) {
    var w = windows[i];
    if (w.caption.indexOf("GUI Probe") !== -1) {
        var g = w.frameGeometry;
        w.frameGeometry = {x: FRAME_X, y: FRAME_Y, width: g.width, height: g.height};
        placed++;
    }
}
callDBus("{sink}", "{path}", "{sink}", "Report", "" + placed);
""".replace("FRAME_X", str(FRAME_ORIGIN[0])).replace("FRAME_Y", str(FRAME_ORIGIN[1]))

Rect = tuple[int, int, int, int]


def _connect(engine: AutomationEngine, live: LiveKWin) -> None:
    output = engine.session_connect(
        dbus_address=live.dbus_address,
        wayland_display=live.wayland_display,
    )
    assert "Input backend: KWin EIS" in output, output


def _probe_windows() -> list[geometry.KWinWindow]:
    return [
        window
        for window in geometry.collect_windows(timeout=10)
        if window["normal"] and PROBE_SELECTOR in window["app"].lower()
    ]


def _client_rect() -> Rect:
    windows = _probe_windows()
    assert len(windows) == 1, windows
    x, y, width, height = windows[0]["client"]
    return x, y, width, height


def _button_rect(engine: AutomationEngine) -> Rect | None:
    matches = _BUTTON.findall(engine.accessibility_tree(app_name=PROBE_SELECTOR))
    if len(matches) != 1:
        return None
    x, y, width, height = (int(value) for value in matches[0])
    return x, y, width, height


def _clicks(engine: AutomationEngine) -> int:
    tree = engine.accessibility_tree(app_name=PROBE_SELECTOR)
    matches = _CLICKS.findall(tree)
    assert len(matches) == 1, tree[:1500]
    return int(matches[0])


def _await_clicks(engine: AutomationEngine, expected: int) -> int:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    clicks = _clicks(engine)
    while clicks < expected and time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        clicks = _clicks(engine)
    return clicks


def _launch_probe(engine: AutomationEngine) -> Rect:
    """Launch a fresh probe and return its client rectangle where it mapped."""
    launched = engine.launch_app(PROBE_COMMAND, env=PROBE_ENV)
    assert launched.startswith(f"App launched: {PROBE_COMMAND}"), launched
    waited = engine.wait_for_element("Animation Target", app_name=PROBE_SELECTOR, timeout_ms=15_000)
    assert '[button] "Animation Target"' in waited, waited[:1500]
    return _client_rect()


def _place_probe(engine: AutomationEngine) -> Rect:
    """Move the probe to FRAME_ORIGIN and return the settled button rectangle."""
    assert geometry.run_kwin_script(_PLACE_SCRIPT, timeout=10) == "1"
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    previous: Rect | None = None
    while time.monotonic() < deadline:
        current = _button_rect(engine)
        if current is not None and current == previous:
            return current
        previous = current
        time.sleep(POLL_INTERVAL_SECONDS * 3)
    raise AssertionError(f"button position never settled: {previous}")


def _center(rect: Rect) -> tuple[int, int]:
    x, y, width, height = rect
    return x + width // 2, y + height // 2


def _await_probe_gone() -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS * 3
    while _probe_windows():
        assert time.monotonic() < deadline, "previous probe window never closed"
        time.sleep(POLL_INTERVAL_SECONDS)


def test_first_click_after_reconnect_reaches_new_window_under_parked_pointer(
    engine: AutomationEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with live_kwin() as live:
        # geometry.run_kwin_script/query talk to the session bus in-process.
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", live.dbus_address)

        # Healthy control: the pointer starts elsewhere, so the move is real.
        _connect(engine, live)
        _launch_probe(engine)
        target = _center(_place_probe(engine))
        assert _clicks(engine) == 0
        engine.mouse_click(*target)
        assert _await_clicks(engine, 1) == 1

        # Disconnect without restarting KWin: the cursor stays parked on target
        # and the engine-launched probe is terminated.
        engine.session_stop()
        _await_probe_gone()

        _connect(engine, live)
        map_x, map_y, _, _ = _launch_probe(engine)
        button = _place_probe(engine)
        assert _center(button) == target, (button, target)

        # Guard against a vacuous pass: the surface-local point the new window
        # was entered at must lie outside the button once the window is placed.
        client_x, client_y, _, _ = _client_rect()
        stale_x = target[0] - map_x + client_x
        stale_y = target[1] - map_y + client_y
        bx, by, bw, bh = button
        assert not (bx <= stale_x < bx + bw and by <= stale_y < by + bh), (
            f"stale entry point ({stale_x}, {stale_y}) is inside the button {button}"
        )

        assert _clicks(engine) == 0
        assert engine.mouse_click(*target) == f"Clicked left at {target}"
        assert _await_clicks(engine, 1) == 1

        # A second click at the same spot on the now-existing window still lands.
        engine.mouse_click(*target)
        assert _await_clicks(engine, 2) == 2
