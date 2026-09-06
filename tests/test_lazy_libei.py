"""Tests for lazy libei loading.

``kwin_mcp.input`` used to load libei eagerly at import time
(``_libei = _load_libei()`` at module level), so importing the module — and
with it the whole package: MCP server, CLI — failed on systems without
``libei.so.1`` installed, even though no EIS input path was touched there.
The library is now loaded lazily on first use via ``_get_libei()``.
"""

from __future__ import annotations

import ctypes
import importlib

import pytest

import kwin_mcp.input as input_module


@pytest.fixture(autouse=True)
def _fresh_module_state():
    """Re-execute the module around each test for clean module-level state."""
    importlib.reload(input_module)
    yield
    importlib.reload(input_module)


def test_import_does_not_load_libei(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing the module must not require libei at all."""

    def _no_libei(*args: object, **kwargs: object) -> ctypes.CDLL:
        raise OSError("libei not available (simulated)")

    monkeypatch.setattr(ctypes, "CDLL", _no_libei)
    # Re-execute the module with CDLL broken: eager loading would raise
    # OSError here, lazy loading must complete without touching libei.
    importlib.reload(input_module)

    assert input_module._libei is None


def test_get_libei_loads_lazily_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_libei() loads on first call and returns the same handle after."""
    sentinel = ctypes.CDLL  # any stable object; never actually used as a library
    calls: list[int] = []

    def _fake_load() -> ctypes.CDLL:
        calls.append(1)
        return sentinel

    monkeypatch.setattr(input_module, "_load_libei", _fake_load)

    assert input_module._libei is None  # nothing loaded at import time
    assert input_module._get_libei() is sentinel
    assert input_module._get_libei() is sentinel
    assert len(calls) == 1
