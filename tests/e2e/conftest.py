"""Shared fixtures for kwin-mcp end-to-end tests.

These tests need a working KWin Wayland installation and are meant to run inside
the container built from docker/e2e.Dockerfile (see docker/README.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from _asserts import element_count

from kwin_mcp.core import AutomationEngine

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800


@pytest.fixture
def screen_size() -> tuple[int, int]:
    """Resolution of the virtual sessions started by the session fixtures."""
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
def start_session(engine: AutomationEngine) -> Callable[..., str]:
    """Start a virtual session at the fixed test resolution."""

    def _start(app_command: str = "", **kwargs: object) -> str:
        return engine.session_start(
            app_command=app_command,
            screen_width=SCREEN_WIDTH,
            screen_height=SCREEN_HEIGHT,
            **kwargs,  # type: ignore[arg-type]
        )

    return _start


@pytest.fixture
def wait_for_app(engine: AutomationEngine) -> Callable[[str], str]:
    """Block until an app is queryable over AT-SPI2, then return its elements."""

    def _wait(app_name: str) -> str:
        found = engine.wait_for_element(query="", app_name=app_name, timeout_ms=15000)
        assert element_count(found) > 0, found[:500]
        return found

    return _wait


@pytest.fixture
def kcalc_session(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
) -> AutomationEngine:
    """Virtual session running kcalc, ready for observation."""
    output = start_session("kcalc", keep_screenshots=True)
    assert "Session started" in output, output
    assert "App launched: kcalc" in output, output
    # The image ships kwin-common, so KWin must expose its EIS interface.
    assert "Input backend: KWin EIS" in output, output

    # Observe a fully started app instead of racing its window creation.
    wait_for_app("kcalc")
    return engine
