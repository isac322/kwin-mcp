"""Shared fixtures for kwin-mcp end-to-end tests.

These tests need a working KWin Wayland installation and are meant to run inside
the container built from docker/e2e.Dockerfile (see docker/README.md).

Constants:
    ``SCREEN_WIDTH``, ``SCREEN_HEIGHT``: the size of every session the fixtures start.
    ``APP_WAIT_TIMEOUT_MS``: how long ``wait_for_app`` waits for an app's elements.

Fixtures:
    ``screen_size() -> tuple[int, int]``: ``(SCREEN_WIDTH, SCREEN_HEIGHT)``.
    ``engine() -> AutomationEngine``: a fresh engine; ``session_stop()`` on teardown.
    ``start_session(*, enable_clipboard=False, home="host", retention=NO_RETENTION)
    -> SessionInfo``: ``engine.session_start`` at the fixed test resolution.
    ``launch(command, env=None) -> AppLaunchResult``: ``engine.app_launch``.
    ``wait_for_app(app_name) -> ElementList``: waits (``element_query`` with
    ``Locator(app=app_name)`` and ``Wait(timeout_ms=APP_WAIT_TIMEOUT_MS)``) until the
    app has AT-SPI2 elements; asserts the wait was satisfied and the list is non-empty.
    ``kcalc_session() -> AutomationEngine``: a virtual session whose input backend is
    EIS, with kcalc launched and queryable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

import pytest
from _asserts import element_count

from kwin_mcp.core import AutomationEngine
from kwin_mcp.models import Locator, Wait

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import Unpack

    from kwin_mcp.models import AppLaunchResult, ElementList, HomeMode, Retention, SessionInfo

SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800
APP_WAIT_TIMEOUT_MS = 15000


class _SessionStartKwargs(TypedDict, total=False):
    """Optional ``AutomationEngine.session_start`` keywords forwarded by ``start_session``."""

    enable_clipboard: bool
    home: HomeMode
    retention: Retention


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
def start_session(engine: AutomationEngine) -> Callable[..., SessionInfo]:
    """Start a virtual session at the fixed test resolution."""

    def _start(**kwargs: Unpack[_SessionStartKwargs]) -> SessionInfo:
        return engine.session_start(
            screen_width=SCREEN_WIDTH, screen_height=SCREEN_HEIGHT, **kwargs
        )

    return _start


@pytest.fixture
def launch(engine: AutomationEngine) -> Callable[..., AppLaunchResult]:
    """Launch a program in the running session."""

    def _launch(command: str, env: dict[str, str] | None = None) -> AppLaunchResult:
        return engine.app_launch(command, env=env)

    return _launch


@pytest.fixture
def wait_for_app(engine: AutomationEngine) -> Callable[[str], ElementList]:
    """Block until an app is queryable over AT-SPI2, then return its elements."""

    def _wait(app_name: str) -> ElementList:
        found = engine.element_query(
            locator=Locator(app=app_name), wait=Wait(timeout_ms=APP_WAIT_TIMEOUT_MS)
        )
        assert found.wait is not None, found.render()[:500]
        assert found.wait.satisfied, found.render()[:500]
        element_count(found)
        return found

    return _wait


@pytest.fixture
def kcalc_session(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
) -> AutomationEngine:
    """Virtual session running kcalc, ready for observation."""
    info = start_session()
    assert info.kind == "virtual", info
    # The image ships kwin-common, so KWin must expose its EIS interface.
    assert info.input_backend == "eis", info
    launched = launch("kcalc")
    assert launched.command == "kcalc", launched
    assert launched.pid > 0, launched

    # Observe a fully started app instead of racing its window creation.
    wait_for_app("kcalc")
    return engine
