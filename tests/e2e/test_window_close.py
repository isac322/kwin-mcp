"""End-to-end coverage for window ids, active_window and window_close.

window_close addresses one window by its KWin id, so the tests use two windows
of the same app and check that only the targeted one goes away. Window ids come
from the caller and reach a KWin script, so an id that would run as script if
it were spliced into the source must stay data.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

import pytest
from session_harness import live_kwin

if TYPE_CHECKING:
    from collections.abc import Callable

    from kwin_mcp.core import AutomationEngine

WINDOW_ID = re.compile(r"^    id:\s+(\S+)$", re.MULTILINE)
ACTIVE_HEADER = re.compile(r'^- (\S+) ".*" \[active\]$', re.MULTILINE)
STATE_TIMEOUT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.2
# Closes the active window if the id is ever pasted into the script source
# instead of being passed as a string literal.
INJECTED_ID = '"; workspace.activeWindow.closeWindow(); var _x="'


def _ids(engine: AutomationEngine, app_name: str) -> list[str]:
    return WINDOW_ID.findall(engine.window_geometry(app_name=app_name))


def _await_ids(
    engine: AutomationEngine, app_name: str, predicate: Callable[[list[str]], bool]
) -> list[str]:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    ids = _ids(engine, app_name)
    while not predicate(ids):
        assert time.monotonic() < deadline, engine.window_geometry(app_name=app_name)[:1000]
        time.sleep(POLL_INTERVAL_SECONDS)
        ids = _ids(engine, app_name)
    return ids


def _active_id(engine: AutomationEngine) -> str:
    active = engine.active_window()
    ids = WINDOW_ID.findall(active)
    assert len(ids) == 1, active[:500]
    return ids[0]


def _await_active_app(engine: AutomationEngine, app_name: str) -> str:
    """Wait until active_window reports app_name; return its id."""
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    active = engine.active_window()
    while app_name not in active.lower():
        assert time.monotonic() < deadline, active[:500]
        time.sleep(POLL_INTERVAL_SECONDS)
        active = engine.active_window()
    return _active_id(engine)


def _two_kcalc_windows(engine: AutomationEngine) -> list[str]:
    engine.launch_app("kcalc")
    return _await_ids(engine, "kcalc", lambda ids: len(ids) == 2)


def test_window_close_closes_only_the_targeted_window(kcalc_session: AutomationEngine) -> None:
    target, survivor = _two_kcalc_windows(kcalc_session)

    closed = kcalc_session.window_close(window_id=target)
    assert closed.startswith("Close requested:"), closed
    assert target in closed, closed

    assert _await_ids(kcalc_session, "kcalc", lambda ids: target not in ids) == [survivor]
    # The survivor must still be there once the close has fully settled.
    time.sleep(1)
    assert _ids(kcalc_session, "kcalc") == [survivor]
    assert kcalc_session.window_geometry(window_id=target) == f"No window with id {target!r}."


def test_window_close_reports_an_unknown_id(kcalc_session: AutomationEngine) -> None:
    before = _ids(kcalc_session, "kcalc")
    assert kcalc_session.window_close(window_id="{no-such-window}") == (
        "No window with id '{no-such-window}'."
    )
    time.sleep(1)
    assert _ids(kcalc_session, "kcalc") == before


@pytest.mark.parametrize("tool", ["window_close", "window_geometry"])
def test_window_id_with_a_quote_is_data_not_script(
    kcalc_session: AutomationEngine, tool: str
) -> None:
    ids = _two_kcalc_windows(kcalc_session)
    # The injected script would close the active window, so make sure there
    # is one: otherwise a missing close would prove nothing.
    assert kcalc_session.focus_window(app_name="kcalc").startswith("Focused:")
    active = _await_active_app(kcalc_session, "kcalc")
    assert active in ids

    call = getattr(kcalc_session, tool)
    reply = call(window_id=INJECTED_ID)

    time.sleep(2)
    assert sorted(_ids(kcalc_session, "kcalc")) == sorted(ids), reply
    assert _active_id(kcalc_session) == active, reply
    assert reply == f"No window with id {INJECTED_ID!r}."


def test_active_window_follows_focus_window(
    kcalc_session: AutomationEngine, wait_for_app: Callable[[str], str]
) -> None:
    kcalc_session.launch_app(command="kwrite")
    wait_for_app("kwrite")
    [kcalc_id] = _ids(kcalc_session, "kcalc")
    [kwrite_id] = _ids(kcalc_session, "kwrite")

    for app_name, expected in (("kwrite", kwrite_id), ("kcalc", kcalc_id), ("kwrite", kwrite_id)):
        assert kcalc_session.focus_window(app_name=app_name).startswith("Focused:")
        assert _await_active_app(kcalc_session, app_name) == expected
        # window_geometry marks the same window as active, and only that one.
        report = kcalc_session.window_geometry()
        marked = ACTIVE_HEADER.findall(report)
        assert len(marked) == 1 and app_name in marked[0].lower(), report[:1000]
        assert kcalc_session.window_geometry(window_id=expected).count("[active]") == 1


def test_window_close_is_refused_in_live_sessions(
    engine: AutomationEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    with live_kwin() as live:
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", live.dbus_address)
        engine.session_connect(
            dbus_address=live.dbus_address, wayland_display=live.wayland_display
        )
        try:
            engine.launch_app("kcalc")
            [window_id] = _await_ids(engine, "kcalc", lambda ids: len(ids) == 1)

            refused = engine.window_close(window_id=window_id)
            assert refused.startswith("window_close is disabled in live sessions"), refused

            # Observation and activation stay available, as with focus_window.
            assert engine.focus_window(app_name="kcalc").startswith("Focused:")
            assert _await_active_app(engine, "kcalc") == window_id
            time.sleep(1)
            assert _ids(engine, "kcalc") == [window_id]
        finally:
            engine.session_stop()
