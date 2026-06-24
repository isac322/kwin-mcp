"""Observable verification that ``keyboard_type`` delivers key events.

Uses kcalc as the target: launching kate is more realistic but it shows a
welcome page by default and doesn't auto-focus its editor, making the test
fragile across kate versions and locales.

kcalc accepts digit keys from the keyboard — pressing ``8`` types ``8`` into
its display, just like clicking the ``8`` button. After typing, ``Ctrl+C``
copies the display value to the clipboard, which we read back to verify.

Checking the accessibility tree alone would be a vacuous test because kcalc's
digit button labels ``0``..``9`` are always in the tree regardless of any
keyboard input.
"""

from __future__ import annotations

import asyncio
import os
import shutil

import pytest

from tests.integration._helpers import call_text, coords_of_any, running_kwin

pytestmark = [
    pytest.mark.skipif(shutil.which("kcalc") is None, reason="kcalc not installed"),
    pytest.mark.skipif(
        shutil.which("wl-copy") is None or shutil.which("wl-paste") is None,
        reason="wl-clipboard not installed (needed to read kcalc's display via Ctrl+C)",
    ),
]

# Distinctive digit sequence typed via the keyboard after seeding the display
# with a mouse click on the ``1`` button. Expected final display: ``18675309``.
KEYSTROKES = "8675309"
EXPECTED_DISPLAY = "1" + KEYSTROKES


@pytest.fixture(autouse=True)
def _pinned_locale() -> None:
    os.environ["LANG"] = "C.UTF-8"
    os.environ["LC_ALL"] = "C.UTF-8"


@pytest.mark.xfail(
    reason=(
        "Platform interaction limit: EIS pointer click fails to grant keyboard "
        "focus to kcalc in KWin --virtual (verified via AT-SPI probe in Round "
        "19c and timing bumps in Round 19e). Suspect EIS focus guard, "
        "coordinate mismatch, or keymap issue. Requires structural input "
        "refactor (fake_input/KWin scripting)."
    ),
    strict=False,
)
async def test_keyboard_type_reaches_kcalc_display() -> None:
    """Type digits on the keyboard; Ctrl+C the display → clipboard has the digits.

    AT-SPI's ``focus_window`` does not translate to a Wayland keyboard focus
    change — the compositor decides which client receives keystrokes based on
    the last pointer/click activity. We click a kcalc digit button first
    (which both Wayland-focuses the window and seeds the display) and then
    rely on keystrokes for the rest.
    """
    async with running_kwin() as client:
        await call_text(client, "launch_app", {"command": "kcalc"})
        await call_text(
            client,
            "wait_for_element",
            {
                "query": "=",
                "app_name": "kcalc",
                "timeout_ms": 25000,
                "poll_interval_ms": 500,
            },
        )

        # Click "1" to focus kcalc at the compositor level and seed display=1.
        seed_x, seed_y = await coords_of_any(client, ["1"], "kcalc")
        await call_text(client, "mouse_click", {"x": seed_x, "y": seed_y})
        await asyncio.sleep(0.2)

        await call_text(client, "keyboard_type", {"text": KEYSTROKES})
        await asyncio.sleep(1.0)

        # Diagnostic: query the AT-SPI tree for the expected display value
        # BEFORE Ctrl+C. If found, keystrokes reached the focused kcalc
        # surface (so failure is in clipboard plumbing). If absent,
        # keystrokes never landed (focus / routing bug).
        pre_ctrl_c_tree = await call_text(
            client,
            "find_ui_elements",
            {"query": EXPECTED_DISPLAY, "app_name": "kcalc"},
        )

        await call_text(client, "keyboard_key", {"key": "ctrl+c"})
        await asyncio.sleep(1.0)
        clipboard = await call_text(client, "clipboard_get", {})

        await call_text(client, "screenshot", {})

    assert clipboard.strip() == EXPECTED_DISPLAY, (
        f"After click(1) + keyboard_type({KEYSTROKES!r}), kcalc display should "
        f"be {EXPECTED_DISPLAY!r} (via Ctrl+C), got {clipboard!r}\n"
        f"Pre-Ctrl+C AT-SPI search for {EXPECTED_DISPLAY!r}:\n{pre_ctrl_c_tree}"
    )
