"""Fixtures for the unit tree: an AutomationEngine wired to in-process fakes.

Nothing here starts KWin, a D-Bus daemon or the AT-SPI worker. The engine is
attached to a ``LiveSession`` pointing at a non-existent bus, so any code path
that escapes the fakes fails to connect instead of touching a real desktop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import kwin_mcp.input
from kwin_mcp.core import AutomationEngine
from kwin_mcp.input import InputBackend
from kwin_mcp.session import LiveSession

from ._fakes import (
    FAKE_DBUS_ADDRESS,
    FAKE_WAYLAND_SOCKET,
    FakeAtspiWorker,
    FakeEISClient,
    FakeElicitation,
    FakeEngine,
    FakeKWinScriptRunner,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def fake_eis(monkeypatch: pytest.MonkeyPatch) -> FakeEISClient:
    """Make every ``InputBackend`` created in this test talk to one recording fake."""
    client = FakeEISClient()

    def _connect(_dbus_address: str) -> FakeEISClient:
        return client

    monkeypatch.setattr(kwin_mcp.input, "EISClient", _connect)
    # The xdotool mirror only runs for the visual X11 path; keep it off here.
    monkeypatch.delenv("KWIN_MCP_X11_SCREENSHOT", raising=False)
    return client


@pytest.fixture
def fake_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_eis: FakeEISClient
) -> Iterator[FakeEngine]:
    """An engine with a fake live session, the real InputBackend over ``fake_eis``,
    and fake KWin-script and AT-SPI backends."""
    engine = AutomationEngine()
    engine._session = LiveSession(
        dbus_address=FAKE_DBUS_ADDRESS,
        wayland_socket=FAKE_WAYLAND_SOCKET,
        screenshot_dir=tmp_path,
    )
    engine._input = InputBackend(FAKE_DBUS_ADDRESS)
    kwin = FakeKWinScriptRunner()
    atspi = FakeAtspiWorker()
    monkeypatch.setattr(engine, "_run_kwin_query", kwin)
    monkeypatch.setattr(engine, "_run_atspi", atspi)
    try:
        yield FakeEngine(engine=engine, eis=fake_eis, kwin=kwin, atspi=atspi)
    finally:
        # Detach without session_stop(): its teardown would reach for the
        # (non-existent) bus to restore accessibility and sweep processes.
        engine._input = None
        engine._session = None


@pytest.fixture
def fake_elicitation() -> FakeElicitation:
    """A scripted consent user; queue answers with ``fake_elicitation.queue(...)``."""
    return FakeElicitation()
