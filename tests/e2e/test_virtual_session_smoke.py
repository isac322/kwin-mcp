"""Basic end-to-end smoke test: launch an app in a virtual KWin session.

Covers the minimum contract every other automation flow builds on: a virtual
session starts, an app launched inside it becomes visible to AT-SPI2, its
widgets can be queried, and keystrokes injected over EIS reach it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from _asserts import frame_png
from PIL import Image

from kwin_mcp.models import Locator

if TYPE_CHECKING:
    from kwin_mcp.core import AutomationEngine

MIN_SCREENSHOT_BYTES = 10_000
SCREENSHOT_SERVICE = "org.kde.KWin.ScreenShot2"
SCREENSHOT_OPT_IN = "KWIN_MCP_E2E_SCREENSHOT"
KEY_SEND_ATTEMPTS = 5


def _kcalc_elements(engine: AutomationEngine) -> list[tuple[str, str]]:
    found = engine.element_query(locator=Locator(app="kcalc"))
    return [(element.role, element.name) for element in found.elements]


def test_launched_app_is_visible_to_accessibility(kcalc_session: AutomationEngine) -> None:
    # The top level of the accessibility tree lists one entry per application.
    top_level = kcalc_session.element_query(max_depth=1)
    apps = {element.app.lower() for element in top_level.elements}
    assert "kcalc" in apps, top_level.render()[:500]
    assert "kcalc" in top_level.render().lower(), top_level.render()[:500]


def test_widgets_of_the_launched_app_are_queryable(kcalc_session: AutomationEngine) -> None:
    # A calculator always exposes named buttons; finding one proves the AT-SPI2
    # tree is traversable, not merely that the app registered on the bus.
    found = kcalc_session.element_query(locator=Locator(app="kcalc"))
    equals = [e for e in found.elements if (e.role, e.name) == ("button", "Equals")]
    # Truncated on purpose: the full tree is a few hundred lines of CI noise.
    assert len(equals) == 1, found.render()[:500]
    assert equals[0].app == "kcalc", equals[0]
    assert '[button] "Equals"' in found.render(), found.render()[:500]


def test_keyboard_input_reaches_the_app(kcalc_session: AutomationEngine) -> None:
    # kcalc shows base indicators next to the value only once something has been
    # entered, and they stay put no matter how many digits follow — unlike the
    # value itself, which makes this oracle safe to re-check after a retry.
    def value_is_displayed() -> bool:
        return ("label", "2") in _kcalc_elements(kcalc_session)

    assert not value_is_displayed(), "kcalc already shows a value before any input"

    # Keyboard focus is not necessarily settled the moment kcalc reaches the
    # accessibility bus, and a keystroke sent too early is dropped, so retry.
    for _ in range(KEY_SEND_ATTEMPTS):
        typed = kcalc_session.keyboard_type(text="7")
        assert typed.method == "keys", typed
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if value_is_displayed():
                return
            time.sleep(0.2)
    pytest.fail("keystroke injected over EIS never reached kcalc")


@pytest.mark.skipif(
    os.environ.get(SCREENSHOT_OPT_IN) != "1",
    reason=(
        f"set {SCREENSHOT_OPT_IN}=1 to run: KWin's virtual backend never answers"
        " ScreenShot2 in a headless container (no reply, zero bytes), so capture"
        " can only be exercised against a GPU-backed session"
    ),
)
def test_screen_capture_exact_virtual_backend(
    kcalc_session: AutomationEngine, screen_size: tuple[int, int]
) -> None:
    bus_names = kcalc_session.dbus_call(
        service="org.freedesktop.DBus",
        path="/",
        interface="org.freedesktop.DBus",
        method="ListNames",
    )
    # KWin registers ScreenShot2 only when the screenshot effect loads, and that
    # effect needs OpenGL compositing — i.e. a DRM render node in the container.
    assert isinstance(bus_names.reply, list), bus_names
    assert SCREENSHOT_SERVICE in bus_names.reply, bus_names

    frame = kcalc_session.screen_capture(max_edge=0)
    path = frame_png(frame)
    assert Path(frame.path) == path
    assert path.stat().st_size > MIN_SCREENSHOT_BYTES, frame.render()
    # One output at scale 1: the logical workspace is the configured size at (0, 0).
    assert (frame.width, frame.height) == screen_size, frame
    assert frame.coverage == "full", frame
    assert frame.mapping is not None, frame
    assert frame.mapping.scale == 1, frame
    assert (frame.mapping.origin.x, frame.mapping.origin.y) == (0, 0), frame
    with Image.open(path) as image:
        assert image.size == screen_size
