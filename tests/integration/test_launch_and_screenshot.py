"""Observable verification that launching an app actually draws to the session."""

from __future__ import annotations

import re
import shutil

import pytest

from tests.integration._helpers import call_text, running_kwin

pytestmark = pytest.mark.skipif(shutil.which("kcalc") is None, reason="kcalc not installed")

_SIZE_RE = re.compile(r"Screenshot saved: (?P<path>\S+\.png) \((?P<kb>[0-9.]+) KB\)")


def _screenshot_size_bytes(tool_output: str) -> int:
    """Parse the '(12.3 KB)' tail from a ``screenshot`` tool result."""
    m = _SIZE_RE.search(tool_output)
    assert m, f"Unexpected screenshot output: {tool_output}"
    return int(float(m.group("kb")) * 1024)


async def test_kcalc_appears_in_accessibility_tree() -> None:
    """After ``launch_app('kcalc')`` the compositor exposes kcalc via AT-SPI2."""
    async with running_kwin() as client:
        launch = await call_text(client, "launch_app", {"command": "kcalc"})
        assert "App launched: kcalc" in launch, launch

        wait = await call_text(
            client,
            "wait_for_element",
            {
                "query": "",
                "app_name": "kcalc",
                "timeout_ms": 25000,
                "poll_interval_ms": 500,
            },
        )
        assert "Found" in wait, f"kcalc never appeared via AT-SPI: {wait}"
        first = wait.splitlines()[0] if wait else ""
        assert "Found 0 elements" not in first, f"kcalc AT-SPI tree is empty:\n{wait}"

        windows = await call_text(client, "list_windows", {})
    lowered = windows.lower()
    assert "kcalc" in lowered or "calculator" in lowered, (
        f"list_windows did not return kcalc:\n{windows}"
    )


@pytest.mark.xfail(
    reason=(
        "Platform limit: KWin --virtual rejects EglBackend initialization and "
        "falls back to QPainter scene; ScreenShot2.CaptureWorkspace returns "
        "ScreenShot2.Error.Cancelled in this configuration. spectacle fallback "
        "also fails because xdg-desktop-portal is disabled to prevent KWin "
        "segfaults on Fedora/Ubuntu/openSUSE. Tracked in issue #22."
    ),
    strict=False,
)
async def test_screenshot_after_kcalc_larger_than_empty_session() -> None:
    """Screenshot with kcalc open is meaningfully larger than an empty one.

    PNG compresses a near-solid black frame very efficiently, so a real app
    window always produces a bigger file — the strongest distro-agnostic
    signal that ``launch_app`` actually drew pixels into the compositor.
    """
    async with running_kwin() as client:
        empty = await call_text(client, "screenshot", {})
        empty_bytes = _screenshot_size_bytes(empty)

        await call_text(client, "launch_app", {"command": "kcalc"})
        await call_text(
            client,
            "wait_for_element",
            {
                "query": "",
                "app_name": "kcalc",
                "timeout_ms": 25000,
                "poll_interval_ms": 500,
            },
        )

        with_app = await call_text(client, "screenshot", {})
        with_app_bytes = _screenshot_size_bytes(with_app)

    assert with_app_bytes > empty_bytes * 1.5, (
        f"Screenshot with kcalc should be >1.5x empty session size: "
        f"empty={empty_bytes}B, with_app={with_app_bytes}B"
    )
