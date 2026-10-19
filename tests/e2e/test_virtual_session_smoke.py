"""Basic end-to-end smoke test: launch an app in a virtual KWin session.

Covers the minimum contract every other automation flow builds on: a virtual
session starts, an app launched inside it becomes visible to AT-SPI2, its
widgets can be queried, and keystrokes injected over EIS reach it.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from _asserts import CoordinateSpace, coordinate_spaces, frame_png
from _suite_policy import VIRTUAL_SCREENSHOT_SKIP_PREFIX
from PIL import Image, ImageChops
from visual_harness import requires_native_capture, spectacle_oracle

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
    reason=f"{VIRTUAL_SCREENSHOT_SKIP_PREFIX}: exercise real virtual ScreenShot2 PNG capture",
)
def test_screen_capture_exact_virtual_backend(
    kcalc_session: AutomationEngine,
    screen_size: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISPLAY", "")
    monkeypatch.setenv("KWIN_MCP_X11_SCREENSHOT", "0")
    bus_names = kcalc_session.dbus_call(
        service="org.freedesktop.DBus",
        path="/",
        interface="org.freedesktop.DBus",
        method="ListNames",
    )
    # A registered service is necessary, but the PNG and geometry checks below
    # also require the provider to answer with a real full-resolution image.
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

    if requires_native_capture():
        assert coordinate_spaces(frame.render()) == [
            CoordinateSpace((0, 0), screen_size, "screenshot2", "full")
        ], frame.render()
        session = kcalc_session.session_info().session
        assert session is not None
        artifact_root = Path(os.environ["KWIN_MCP_ARTIFACT_DIR"])
        evidence = artifact_root / f"exact-virtual-{session.wayland_socket}"
        evidence.mkdir(parents=True, exist_ok=True)
        evidence.chmod(0o755)
        product_png = evidence / "product.png"
        shutil.copy2(path, product_png)
        oracle_png = spectacle_oracle(
            session.dbus_address, session.wayland_socket, evidence / "spectacle.png"
        )
        # This fixture is one scale-1 output: both independent captures share
        # the same pixel grid. Fractional-scale tests must not use this comparison.
        with Image.open(product_png) as product, Image.open(oracle_png) as oracle:
            assert product.size == oracle.size == screen_size, (product.size, oracle.size)
            difference = ImageChops.difference(product.convert("RGB"), oracle.convert("RGB"))
        red, green, blue = difference.split()
        changed = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        different_pixels = sum(changed.histogram()[1:])
        bbox = difference.getbbox()
        (evidence / "rgb-comparison.json").write_text(
            json.dumps(
                {
                    "size": list(screen_size),
                    "compared_pixels": screen_size[0] * screen_size[1],
                    "different_pixels": different_pixels,
                    "difference_bbox": bbox,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if different_pixels:
            difference.save(evidence / "rgb-difference.png")
        assert different_pixels == 0 and bbox is None, (
            f"product ScreenShot2 and independent Spectacle RGB differ: "
            f"{different_pixels} pixels, bbox={bbox}; evidence: {evidence}"
        )
