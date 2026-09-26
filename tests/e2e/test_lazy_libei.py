"""Lazy libei loading and the missing-library input backend path.

``kwin_mcp.input`` used to load libei eagerly at import time
(``_libei = _load_libei()`` at module level), so importing the module, and with
it the MCP server and CLI, failed on hosts without ``libei.so.1`` even though no
EIS input path was touched. Debian and Ubuntu ``kwin-wayland`` depend only on
``libeis1``, so such hosts are common. The library is now loaded on first use via
``_get_libei()``, and a failed load surfaces as the RuntimeError that
``AutomationEngine`` already treats as "no input backend".
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import kwin_mcp.input as input_module

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from kwin_mcp.core import AutomationEngine

MISSING_LIBEI = "libei.so.1: cannot open shared object file: No such file or directory"
KWIN_EXIT_TIMEOUT_SECONDS = 5.0


def _exec_input_module() -> ModuleType:
    """Execute input.py as a fresh module under a throwaway name.

    ``importlib.reload`` would rebind module-level names (``MouseButton``,
    ``_BTN_CODES``) in place while modules imported earlier (``core``) keep the
    old objects, so later in-process tests would look up a stale ``MouseButton``
    in a rebuilt ``_BTN_CODES`` and fail with KeyError.
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


def _break_cdll(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_libei(*args: object, **kwargs: object) -> ctypes.CDLL:
        raise OSError(MISSING_LIBEI)

    monkeypatch.setattr(ctypes, "CDLL", _no_libei)


def _live_kwin_pids() -> set[int]:
    """kwin_wayland pids that still run; zombies left by the reaperless PID 1 do not count."""
    pids: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip()
            stat = (entry / "stat").read_text()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if comm == "kwin_wayland" and stat.rpartition(")")[2].split()[0] not in ("Z", "X"):
            pids.add(int(entry.name))
    return pids


def _socket_fds() -> set[tuple[int, str]]:
    """Open socket descriptors of this process as (fd, inode link) pairs."""
    sockets: set[tuple[int, str]] = set()
    for entry in Path("/proc/self/fd").iterdir():
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.startswith("socket:"):
            sockets.add((int(entry.name), target))
    return sockets


def test_import_does_not_load_libei(monkeypatch: pytest.MonkeyPatch) -> None:
    """Executing the module must not open libei at all."""
    _break_cdll(monkeypatch)

    # Eager loading raises OSError here; lazy loading never touches libei.
    module = _exec_input_module()

    assert module._libei is None


def test_get_libei_loads_once_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_libei() loads on the first call and reuses the handle afterwards."""
    module = _exec_input_module()
    sentinel = object()  # any stable object; never used as a library
    calls: list[int] = []

    def _fake_load() -> object:
        calls.append(1)
        return sentinel

    monkeypatch.setattr(module, "_load_libei", _fake_load)

    assert module._libei is None
    assert module._get_libei() is sentinel
    assert module._get_libei() is sentinel
    assert len(calls) == 1


def test_missing_libei_raises_runtime_error_with_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed load is the optional-backend RuntimeError naming libei1, and is not cached.

    Once the library can be loaded, the next call succeeds without a restart.
    """
    _break_cdll(monkeypatch)
    module = _exec_input_module()

    with pytest.raises(RuntimeError, match="libei1") as excinfo:
        module._get_libei()

    assert isinstance(excinfo.value.__cause__, OSError)
    assert module._libei is None

    sentinel = object()  # stands in for the handle of a now-installed libei
    monkeypatch.setattr(module, "_load_libei", lambda: sentinel)

    assert module._get_libei() is sentinel
    assert module._libei is sentinel


def test_session_start_without_libei_reports_no_input_backend(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """session_start completes without libei and leaves nothing behind after session_stop.

    Before the fix the load OSError escaped session_start after KWin had
    started: the tool failed while the compositor kept running. Resolving libei
    only after connectToEIS also leaked the EIS socket fd KWin handed over.
    """

    def _missing_libei() -> ctypes.CDLL:
        raise OSError(MISSING_LIBEI)

    # monkeypatch restores any handle a previous test loaded.
    monkeypatch.setattr(input_module, "_libei", None)
    monkeypatch.setattr(input_module, "_load_libei", _missing_libei)
    baseline = _live_kwin_pids()
    baseline_sockets = _socket_fds()

    output = start_session()

    assert output.startswith("Session started."), output
    status = output.splitlines()[-1]
    assert status.startswith("No input backend available ("), output
    assert MISSING_LIBEI in status, output
    assert "libei1" in status, output
    assert _live_kwin_pids() - baseline, "session_start reported success without a compositor"

    with pytest.raises(RuntimeError, match="No input backend"):
        engine.mouse_click(10, 10)

    assert engine.session_stop() == "Session stopped."
    deadline = time.monotonic() + KWIN_EXIT_TIMEOUT_SECONDS
    while _live_kwin_pids() - baseline and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _live_kwin_pids() - baseline, "KWin outlived session_stop"
    leaked = _socket_fds() - baseline_sockets
    assert not leaked, f"socket fds outlived session_stop: {sorted(leaked)}"
