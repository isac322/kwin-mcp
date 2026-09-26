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
import importlib.util
import sys
from types import ModuleType
from typing import Callable

import pytest

import kwin_mcp.input as input_module


def _exec_input_module() -> ModuleType:
    """Re-execute input.py as a fresh module under a throwaway name.

    ``importlib.reload`` would rebind module-level names (``MouseButton``,
    ``_BTN_CODES``) in place while modules imported earlier (``core``) keep
    referencing the old objects, leaking divergent classes into later tests.
    """
    module_name = "kwin_mcp._test_fresh_input"
    spec = importlib.util.spec_from_file_location(module_name, input_module.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


@pytest.fixture
def fresh_input_module() -> Callable[[], ModuleType]:
    """Return a loader that re-executes input.py isolated from the imported module."""
    return _exec_input_module


def test_import_does_not_load_libei(
    fresh_input_module: Callable[[], ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Importing the module must not require libei at all."""

    def _no_libei(*args: object, **kwargs: object) -> ctypes.CDLL:
        raise OSError("libei not available (simulated)")

    monkeypatch.setattr(ctypes, "CDLL", _no_libei)
    # Re-execute the module with CDLL broken: eager loading would raise
    # OSError here, lazy loading must complete without touching libei.
    module = fresh_input_module()

    assert module._libei is None


def test_get_libei_loads_lazily_and_caches(
    fresh_input_module: Callable[[], ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """_get_libei() loads on first call and returns the same handle after."""
    module = fresh_input_module()
    sentinel = ctypes.CDLL  # any stable object; never actually used as a library
    calls: list[int] = []

    def _fake_load() -> ctypes.CDLL:
        calls.append(1)
        return sentinel

    monkeypatch.setattr(module, "_load_libei", _fake_load)

    assert module._libei is None  # nothing loaded at import time
    assert module._get_libei() is sentinel
    assert module._get_libei() is sentinel
    assert len(calls) == 1


def test_get_libei_missing_library_is_optional_backend_error(
    fresh_input_module: Callable[[], ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing libei surfaces as RuntimeError, not OSError.

    ``AutomationEngine.session_start``/``session_connect`` treat only
    RuntimeError from ``InputBackend(...)`` as "no input backend", so the
    raw ``ctypes.CDLL`` OSError would escape the tool and leave the started
    KWin session running.
    """

    def _no_libei(*args: object, **kwargs: object) -> ctypes.CDLL:
        raise OSError("libei.so.1: cannot open shared object file")

    monkeypatch.setattr(ctypes, "CDLL", _no_libei)
    module = fresh_input_module()

    with pytest.raises(RuntimeError, match="libei1"):
        module._get_libei()
    # The failed load is not cached: a later call retries.
    assert module._libei is None
