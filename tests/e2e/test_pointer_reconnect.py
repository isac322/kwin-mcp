"""Clicks must reach a window that appeared beneath a parked pointer (issue #66).

KWin sends no ``wl_pointer.motion`` when a surface is mapped or moved under a
stationary cursor, and it drops an absolute motion to the cursor's current
position. A new window placed so its button lies under the parked cursor
therefore keeps the stale surface-local position from when it mapped, and a
click that only re-sent the same position was dispatched by GTK at that stale
spot. The persistent ``animation_clicks`` counter is the oracle: the API's
success result never proved delivery.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from kwin_oracle import Rect as OracleRect
from session_harness import live_kwin

from kwin_mcp.models import Locator, Point, Wait

if TYPE_CHECKING:
    from kwin_oracle import KWinOracle, OracleWindow
    from session_harness import LiveKWin

    from kwin_mcp.core import AutomationEngine

PROBE_COMMAND = "python3 /app/tests/e2e/counted_gui_probe.py"
PROBE_APP = "counted_gui_probe.py"
PROBE_CAPTION = "GUI Probe"
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

Rect = tuple[int, int, int, int]


def _connect(engine: AutomationEngine, live: LiveKWin) -> None:
    info = engine.session_connect(
        dbus_address=live.dbus_address, wayland_display=live.wayland_display
    )
    assert info.kind == "live" and info.input_backend == "eis", info.render()


def _probe_windows(oracle: KWinOracle) -> list[OracleWindow]:
    return [w for w in oracle.windows() if w.normal and PROBE_CAPTION in w.caption]


def _probe_window(oracle: KWinOracle) -> OracleWindow:
    windows = _probe_windows(oracle)
    assert len(windows) == 1, windows
    return windows[0]


def _client_rect(oracle: KWinOracle) -> Rect:
    client = _probe_window(oracle).client
    assert client is not None
    return client.as_ints()


def _button_rect(engine: AutomationEngine) -> Rect | None:
    found = engine.element_query(Locator(app=PROBE_APP, query="Animation Target", role="button"))
    rects = [e.rect for e in found.elements if e.name == "Animation Target" and e.rect is not None]
    if len(rects) != 1:
        return None
    rect = rects[0]
    return rect.x, rect.y, rect.width, rect.height


def _clicks(engine: AutomationEngine) -> int:
    found = engine.element_query(Locator(app=PROBE_APP, query="animation_clicks:"))
    names = [e.name for e in found.elements if e.name.startswith("animation_clicks: ")]
    assert len(names) == 1, found.render()[:1500]
    return int(names[0].removeprefix("animation_clicks: "))


def _await_clicks(engine: AutomationEngine, expected: int) -> int:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    clicks = _clicks(engine)
    while clicks < expected and time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        clicks = _clicks(engine)
    return clicks


def _launch_probe(engine: AutomationEngine, oracle: KWinOracle) -> Rect:
    """Launch a fresh probe and return its client rectangle where it mapped."""
    launched = engine.app_launch(PROBE_COMMAND, env=PROBE_ENV)
    assert launched.command == PROBE_COMMAND, launched
    waited = engine.element_query(
        Locator(app=PROBE_APP, query="Animation Target", role="button"),
        wait=Wait(timeout_ms=15_000),
    )
    assert waited.wait is not None and waited.wait.satisfied, waited.render()[:1500]
    return _client_rect(oracle)


def _place_probe(engine: AutomationEngine, oracle: KWinOracle) -> Rect:
    """Move the probe to FRAME_ORIGIN and return the settled button rectangle."""
    window = _probe_window(oracle)
    assert window.frame is not None
    frame = OracleRect(
        float(FRAME_ORIGIN[0]), float(FRAME_ORIGIN[1]), window.frame.width, window.frame.height
    )
    assert oracle.set_frame(window.id, frame)
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    previous: Rect | None = None
    while time.monotonic() < deadline:
        current = _button_rect(engine)
        if current is not None and current == previous:
            return current
        previous = current
        time.sleep(POLL_INTERVAL_SECONDS * 3)
    raise AssertionError(f"button position never settled: {previous}")


def _center(rect: Rect) -> Point:
    x, y, width, height = rect
    return Point(x=x + width // 2, y=y + height // 2)


def _await_probe_gone(oracle: KWinOracle) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS * 3
    while _probe_windows(oracle):
        assert time.monotonic() < deadline, "previous probe window never closed"
        time.sleep(POLL_INTERVAL_SECONDS)


def test_first_click_after_reconnect_reaches_new_window_under_parked_pointer(
    engine: AutomationEngine,
) -> None:
    with live_kwin() as live:
        oracle = live.oracle()

        # Healthy control: the pointer starts elsewhere, so the move is real.
        _connect(engine, live)
        _launch_probe(engine, oracle)
        target = _center(_place_probe(engine, oracle))
        assert _clicks(engine) == 0
        engine.pointer_click(target)
        assert _await_clicks(engine, 1) == 1

        # Disconnect without restarting KWin: the cursor stays parked on target
        # and the engine-launched probe is terminated.
        stopped = engine.session_stop()
        assert stopped.stopped and stopped.kind == "live", stopped.render()
        _await_probe_gone(oracle)

        _connect(engine, live)
        map_x, map_y, _, _ = _launch_probe(engine, oracle)
        button = _place_probe(engine, oracle)
        assert _center(button) == target, (button, target)

        # Guard against a vacuous pass: the surface-local point the new window
        # was entered at must lie outside the button once the window is placed.
        client_x, client_y, _, _ = _client_rect(oracle)
        stale_x = target.x - map_x + client_x
        stale_y = target.y - map_y + client_y
        bx, by, bw, bh = button
        assert not (bx <= stale_x < bx + bw and by <= stale_y < by + bh), (
            f"stale entry point ({stale_x}, {stale_y}) is inside the button {button}"
        )

        assert _clicks(engine) == 0
        result = engine.pointer_click(target)
        assert result.render() == f"Clicked left at ({target.x}, {target.y})"
        assert _await_clicks(engine, 1) == 1

        # A second click at the same spot on the now-existing window still lands.
        engine.pointer_click(target)
        assert _await_clicks(engine, 2) == 2
