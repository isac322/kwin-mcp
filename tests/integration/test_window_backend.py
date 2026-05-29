"""End-to-end tests for the KWin scripting window backend (Tasks 20-23).

Exercises ``engine.window_list`` / ``active_window`` / ``window_geometry`` /
``window_activate`` / ``window_close`` against a real virtual KWin session
running ``kcalc``, plus the live-session mutation gate added in Task 23.
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from kwin_mcp.session import LiveSession

if TYPE_CHECKING:
    from kwin_mcp.core import AutomationEngine

pytestmark = pytest.mark.kwin

_LIVE_GATE_ERROR = (
    "Error: window mutation not supported in live session (v1 safety). Use virtual session."
)


def _kcalc_windows(engine: AutomationEngine) -> list[dict]:
    """Return entries from ``window_list()`` whose app_id matches ``"kcalc"``."""
    data = json.loads(engine.window_list())
    if not isinstance(data, list):
        return []
    return [w for w in data if "kcalc" in str(w.get("app_id", w.get("appId", ""))).lower()]


def _wait_for_kcalc(
    engine: AutomationEngine,
    *,
    count: int = 1,
    timeout: float = 5.0,
) -> list[dict]:
    """Poll ``window_list()`` until at least ``count`` kcalc windows appear."""
    deadline = time.monotonic() + timeout
    wins: list[dict] = []
    while time.monotonic() < deadline:
        wins = _kcalc_windows(engine)
        if len(wins) >= count:
            return wins
        time.sleep(0.1)
    pytest.fail(f"expected >={count} kcalc window(s) within {timeout}s, got {len(wins)}")


def _cleanup_kcalc(engine: AutomationEngine, *, timeout: float = 3.0) -> None:
    """Close every kcalc window and block until ``window_list()`` no longer reports any.

    Without the wait, ``window_close`` returns OK before KWin has finished
    removing the window from ``windowList()``, which causes a stale id to
    leak into the next test and break ``window_geometry`` lookups.
    """
    for win in _kcalc_windows(engine):
        with contextlib.suppress(Exception):
            engine.window_close(str(win["id"]))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _kcalc_windows(engine):
            return
        time.sleep(0.1)


def test_window_list_includes_kcalc(engine: AutomationEngine) -> None:
    """Launching kcalc surfaces a window with ``app_id == 'kcalc'`` in ``window_list()``."""
    engine.launch_app("kcalc")
    try:
        wins = _wait_for_kcalc(engine)
        entry = wins[0]
        assert "id" in entry, f"window entry missing 'id': {entry!r}"
        assert "app_id" in entry, f"window entry missing 'app_id': {entry!r}"
        assert "kcalc" in str(entry["app_id"]).lower(), (
            f"app_id={entry['app_id']!r} does not contain 'kcalc'"
        )
    finally:
        _cleanup_kcalc(engine)


def test_active_window_after_launch(engine: AutomationEngine) -> None:
    """``active_window()`` reports a kcalc window after kcalc launches."""
    engine.launch_app("kcalc")
    try:
        wins = _wait_for_kcalc(engine)
        kcalc_ids = {str(w["id"]) for w in wins}

        deadline = time.monotonic() + 2.0
        last_raw = ""
        matched = False
        while time.monotonic() < deadline:
            last_raw = engine.active_window()
            parsed = json.loads(last_raw)
            if isinstance(parsed, dict) and str(parsed.get("id")) in kcalc_ids:
                matched = True
                break
            time.sleep(0.1)
        assert matched, f"active_window never matched a kcalc id; last raw={last_raw!r}"
    finally:
        _cleanup_kcalc(engine)


def test_window_geometry_returns_dimensions(engine: AutomationEngine) -> None:
    """``window_geometry()`` returns int x/y/width/height for a real kcalc window."""
    engine.launch_app("kcalc")
    try:
        wins = _wait_for_kcalc(engine)
        wid = str(wins[0]["id"])
        geom = json.loads(engine.window_geometry(wid))
        for key in ("x", "y", "width", "height"):
            assert key in geom, f"geometry missing '{key}': {geom!r}"
            assert isinstance(geom[key], int), (
                f"geometry['{key}']={geom[key]!r} is {type(geom[key]).__name__}, not int"
            )
    finally:
        _cleanup_kcalc(engine)


def test_window_activate_changes_active(engine: AutomationEngine) -> None:
    """``window_activate(window_id)`` makes ``active_window()`` report that id."""
    engine.launch_app("kcalc")
    engine.launch_app("kcalc")
    try:
        wins = _wait_for_kcalc(engine, count=1, timeout=5.0)
        first_id = str(wins[0]["id"])

        result = engine.window_activate(first_id)
        assert result == "OK", f"window_activate failed: {result!r}"

        deadline = time.monotonic() + 0.5
        last_raw = ""
        matched = False
        while time.monotonic() < deadline:
            last_raw = engine.active_window()
            parsed = json.loads(last_raw)
            if isinstance(parsed, dict) and str(parsed.get("id")) == first_id:
                matched = True
                break
            time.sleep(0.1)
        assert matched, f"active_window did not become {first_id}; last raw={last_raw!r}"
    finally:
        _cleanup_kcalc(engine)


def test_window_close_removes_window(engine: AutomationEngine) -> None:
    """``window_close(window_id)`` removes the window from ``window_list()`` within 2s."""
    engine.launch_app("kcalc")
    wins = _wait_for_kcalc(engine)
    wid = str(wins[0]["id"])

    result = engine.window_close(wid)
    assert result == "OK", f"window_close failed: {result!r}"

    deadline = time.monotonic() + 2.0
    gone = False
    while time.monotonic() < deadline:
        ids = {str(w["id"]) for w in _kcalc_windows(engine)}
        if wid not in ids:
            gone = True
            break
        time.sleep(0.1)
    # Clean up any siblings before asserting so a failure leaves the session tidy.
    _cleanup_kcalc(engine)
    assert gone, f"window {wid} still present 2s after window_close"


def test_live_mode_blocks_mutating_ops(engine: AutomationEngine) -> None:
    """Mutating ops return the live-gate error; observation ops are not gated."""
    original_session = engine._session
    engine._session = MagicMock(spec=LiveSession)
    try:
        assert engine.window_activate("12345") == _LIVE_GATE_ERROR
        assert engine.window_close("12345") == _LIVE_GATE_ERROR

        # Observation ops must NOT short-circuit on the live gate. They may
        # raise (or return a different "Error: ..." string) once the mock's
        # fake D-Bus address propagates into KWinScriptingBackend — either
        # outcome proves the gate did not fire.
        observers = (
            ("window_list", lambda: engine.window_list()),
            ("active_window", lambda: engine.active_window()),
            ("window_geometry", lambda: engine.window_geometry("12345")),
        )
        for name, call in observers:
            try:
                out = call()
            except Exception:
                continue
            assert out != _LIVE_GATE_ERROR, f"{name} unexpectedly hit live gate: {out!r}"
    finally:
        engine._session = original_session


def test_invalid_window_id_returns_error(engine: AutomationEngine) -> None:
    """Closing a non-existent window id returns an ``"Error: ..."`` string, not ``"OK"``."""
    result = engine.window_close("99999999")
    assert result.startswith("Error:"), f"expected 'Error:' prefix, got {result!r}"
    assert result != "OK"
