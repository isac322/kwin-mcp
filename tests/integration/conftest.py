"""Pytest configuration for kwin-mcp integration tests.

MCP client + KWin session lifecycle live in ``_helpers`` as async context
managers (see the docstring there for rationale). This file only handles
collection-level skip logic for missing system prerequisites.
"""

from __future__ import annotations

import os
import shutil

import pytest

REQUIRED_BINARIES = ("kwin_wayland", "dbus-run-session", "gdbus")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip the suite when required binaries or override env are missing."""
    if os.environ.get("KWIN_MCP_SKIP_INTEGRATION"):
        skip = pytest.mark.skip(reason="KWIN_MCP_SKIP_INTEGRATION=1")
        for item in items:
            item.add_marker(skip)
        return

    missing = [b for b in REQUIRED_BINARIES if shutil.which(b) is None]
    if missing:
        skip = pytest.mark.skip(reason=f"Missing required binaries: {', '.join(missing)}")
        for item in items:
            item.add_marker(skip)
