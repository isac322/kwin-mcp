from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from kwin_mcp.core import AutomationEngine

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(scope="session")
def engine() -> Iterator[AutomationEngine]:
    eng = AutomationEngine()
    eng.session_start(screen_width=1920, screen_height=1080)
    try:
        yield eng
    finally:
        eng.session_stop()
