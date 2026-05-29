from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from kwin_mcp.session import Session, SessionConfig

if TYPE_CHECKING:
    from collections.abc import Iterator

    from kwin_mcp.session import SessionInfo

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)


@pytest.fixture(scope="session")
def virtual_session() -> Iterator[SessionInfo]:
    session = Session()
    info = session.start(SessionConfig(socket_name="wayland-test"))
    try:
        yield info
    finally:
        logger.info("Stopping virtual KWin session")
        session.stop()
