"""Shared fixtures for kwin-mcp end-to-end tests.

These tests need a working KWin Wayland installation and are meant to run inside
the container built from docker/e2e.Dockerfile (see docker/README.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from kwin_mcp.core import AutomationEngine

if TYPE_CHECKING:
    from collections.abc import Iterator

SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800


@pytest.fixture
def screen_size() -> tuple[int, int]:
    """Resolution of the virtual session started by `kcalc_session`."""
    return SCREEN_WIDTH, SCREEN_HEIGHT


@pytest.fixture
def engine() -> Iterator[AutomationEngine]:
    """Provide an AutomationEngine and guarantee session teardown."""
    automation = AutomationEngine()
    try:
        yield automation
    finally:
        automation.session_stop()


@pytest.fixture
def kcalc_session(engine: AutomationEngine) -> AutomationEngine:
    """Virtual session running kcalc, ready for observation."""
    output = engine.session_start(
        app_command="kcalc",
        screen_width=SCREEN_WIDTH,
        screen_height=SCREEN_HEIGHT,
        keep_screenshots=True,
    )
    assert "Session started" in output, output
    assert "App launched: kcalc" in output, output
    # The image ships kwin-common, so KWin must expose its EIS interface.
    assert "Input backend: KWin EIS" in output, output

    # Block until the app is on the accessibility bus, so tests observe a fully
    # started app instead of racing its window creation.
    found = engine.wait_for_element(query="", app_name="kcalc", timeout_ms=15000)
    assert "Found 0 elements" not in found, found[:500]
    return engine
