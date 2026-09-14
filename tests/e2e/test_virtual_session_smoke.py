"""Basic end-to-end smoke test: launch an app in a virtual KWin session.

Covers the minimum contract every other automation flow builds on: a virtual
session starts, an app launched inside it becomes visible to AT-SPI2, and its
widgets can be queried.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from PIL import Image

if TYPE_CHECKING:
    from kwin_mcp.core import AutomationEngine

MIN_SCREENSHOT_BYTES = 10_000
SCREENSHOT_SERVICE = "org.kde.KWin.ScreenShot2"
SCREENSHOT_OPT_IN = "KWIN_MCP_E2E_SCREENSHOT"
BASE_INDICATOR = '[label] "2"'
KEY_SEND_ATTEMPTS = 5


def test_launched_app_is_visible_to_accessibility(kcalc_session: AutomationEngine) -> None:
    windows = kcalc_session.list_windows()
    assert "kcalc" in windows.lower(), windows


def test_widgets_of_the_launched_app_are_queryable(kcalc_session: AutomationEngine) -> None:
    # A calculator always exposes named buttons; finding one proves the AT-SPI2
    # tree is traversable, not merely that the app registered on the bus.
    elements = kcalc_session.find_ui_elements(query="", app_name="kcalc")
    # Truncated on purpose: the full tree is a few hundred lines of CI noise.
    assert '[button] "Equals"' in elements, elements[:500]


def test_keyboard_input_reaches_the_app(kcalc_session: AutomationEngine) -> None:
    # kcalc shows base indicators next to the value only once something has been
    # entered, and they stay put no matter how many digits follow — unlike the
    # value itself, which makes this oracle safe to re-check after a retry.
    def value_is_displayed() -> bool:
        return BASE_INDICATOR in kcalc_session.find_ui_elements(query="", app_name="kcalc")

    assert not value_is_displayed(), "kcalc already shows a value before any input"

    # Keyboard focus is not necessarily settled the moment kcalc reaches the
    # accessibility bus, and a keystroke sent too early is dropped, so retry.
    for _ in range(KEY_SEND_ATTEMPTS):
        kcalc_session.keyboard_type(text="7")
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
def test_screenshot_captures_the_session(
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
    assert SCREENSHOT_SERVICE in bus_names, bus_names

    output = kcalc_session.screenshot()
    assert "Screenshot saved" in output, output

    path = Path(output.removeprefix("Screenshot saved: ").rsplit(" (", 1)[0])
    assert path.stat().st_size > MIN_SCREENSHOT_BYTES, output

    with Image.open(path) as image:
        assert image.size == screen_size
