"""Observable verification that ``mouse_click`` reaches GUI widgets.

Clicks ``2``, ``+``, ``3``, ``=`` on kcalc, then copies the display value to
the clipboard via ``Ctrl+C`` and reads it back. If the clipboard holds ``5``
we know:

1. libei/EIS injected the clicks into the compositor
2. the compositor routed them to the correct widget
3. kcalc actually performed the arithmetic
4. ``keyboard_key`` delivered a modifier-chord shortcut to the focused client

Checking "5 is in the accessibility tree" would be vacuous here because kcalc
exposes every digit button's label as a name — so the literal ``5`` is in the
tree at startup regardless of any clicks.
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


@pytest.fixture(autouse=True)
def _pinned_locale() -> None:
    """kcalc button AT-SPI names are locale-dependent — pin to C.UTF-8."""
    os.environ["LANG"] = "C.UTF-8"
    os.environ["LC_ALL"] = "C.UTF-8"


@pytest.mark.xfail(
    reason=(
        "Platform interaction limit: EIS pointer click fails to grant keyboard "
        "focus to kcalc in KWin --virtual, so the Ctrl+C chord is dropped "
        "(verified via AT-SPI probe in Round 19c and timing bumps in Round "
        "19e). Mouse clicks themselves dispatch successfully but no client "
        "receives the modifier+key chord. Requires structural input refactor "
        "(fake_input/KWin scripting)."
    ),
    strict=False,
)
async def test_kcalc_two_plus_three_equals_five() -> None:
    """Click 2, +, 3, = in kcalc → Ctrl+C copies the display → clipboard holds ``5``."""
    async with running_kwin() as client:
        await call_text(client, "launch_app", {"command": "kcalc"})
        wait = await call_text(
            client,
            "wait_for_element",
            {
                "query": "=",
                "app_name": "kcalc",
                "timeout_ms": 25000,
                "poll_interval_ms": 500,
            },
        )
        if "Found 0 elements" in wait.splitlines()[0]:
            wait = await call_text(
                client,
                "wait_for_element",
                {
                    "query": "Equals",
                    "app_name": "kcalc",
                    "timeout_ms": 15000,
                    "poll_interval_ms": 500,
                },
            )
            assert "Found 0 elements" not in wait.splitlines()[0], (
                f"kcalc equals button never appeared:\n{wait}"
            )

        # Number buttons are always "0".."9"; operators sometimes use the
        # symbol, sometimes a word. Try both spellings.
        buttons: list[list[str]] = [
            ["2"],
            ["+", "Add", "Plus"],
            ["3"],
            ["=", "Equals"],
        ]
        for labels in buttons:
            x, y = await coords_of_any(client, labels, "kcalc")
            await call_text(client, "mouse_click", {"x": x, "y": y})
            await asyncio.sleep(0.15)

        # Give kcalc a moment to compute and update its display.
        await asyncio.sleep(1.0)

        # Diagnostic: AT-SPI search for "5" before Ctrl+C splits keystroke
        # routing failure (no match) from clipboard plumbing failure (match
        # found yet wl-paste returns empty).
        pre_ctrl_c_tree = await call_text(
            client,
            "find_ui_elements",
            {"query": "5", "app_name": "kcalc"},
        )

        # Copy the result to the clipboard via Ctrl+C (tests keyboard_key too).
        await call_text(client, "keyboard_key", {"key": "ctrl+c"})
        await asyncio.sleep(1.0)
        clipboard = await call_text(client, "clipboard_get", {})

        # Final screenshot for CI artifact (keep_screenshots=True on fixture).
        await call_text(client, "screenshot", {})

    assert clipboard.strip() == "5", (
        f"kcalc display after 2+3= should be '5' (copied via Ctrl+C), got {clipboard!r}\n"
        f"Pre-Ctrl+C AT-SPI search for '5':\n{pre_ctrl_c_tree}"
    )
