"""End-to-end coverage for window ids, the active window and window_close in virtual sessions.

A virtual session needs no consent (``consent: "not_required"``); live-session
consent flows are covered by ``test_window_close_consent.py``. Which windows
exist and which one is active is read through ``kwin_oracle``.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

import pytest
from kwin_oracle import KWinOracle, OracleWindow
from window_zoo import MAIN_CAPTION, ZooProcess

from kwin_mcp.models import PartialRect, WindowSelector

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import AppLaunchResult, ElementList, SessionInfo

STATE_TIMEOUT_S = 10.0
POLL_S = 0.2
# Closes the active window if the id is ever pasted into the script source
# instead of being passed as a string literal.
INJECTED_ID = '"; workspace.activeWindow.closeWindow(); var _x="'


def _info(engine: AutomationEngine) -> SessionInfo:
    session = engine.session_info().session
    assert session is not None
    return session


def _of(windows: list[OracleWindow], pid: int) -> list[OracleWindow]:
    return [w for w in windows if w.pid == pid]


def _kcalc_ids(oracle: KWinOracle) -> list[str]:
    return [w.id for w in oracle.windows() if "kcalc" in w.resource_class.lower()]


def _await_kcalc_ids(oracle: KWinOracle, predicate: Callable[[list[str]], bool]) -> list[str]:
    deadline = time.monotonic() + STATE_TIMEOUT_S
    ids = _kcalc_ids(oracle)
    while not predicate(ids):
        assert time.monotonic() < deadline, oracle.windows()
        time.sleep(POLL_S)
        ids = _kcalc_ids(oracle)
    return ids


def _wait_active(oracle: KWinOracle, window_id: str) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_S
    active = oracle.active_window()
    while active is None or active.id != window_id:
        assert time.monotonic() < deadline, (window_id, active)
        time.sleep(POLL_S)
        active = oracle.active_window()


def _active_id(oracle: KWinOracle) -> str:
    active = oracle.active_window()
    assert active is not None
    return active.id


def _two_kcalc_windows(oracle: KWinOracle, launch: Callable[..., AppLaunchResult]) -> list[str]:
    launch("kcalc")
    return _await_kcalc_ids(oracle, lambda ids: len(ids) == 2)


def _open(oracle: KWinOracle, zoo: ZooProcess, command: str, title: str) -> OracleWindow:
    before = {w.id for w in _of(oracle.windows(), zoo.pid)}
    zoo.command(command, f"window: {title}")
    windows = oracle.wait_for_windows(lambda ws: any(w.id not in before for w in _of(ws, zoo.pid)))
    added = [w for w in _of(windows, zoo.pid) if w.id not in before]
    assert len(added) == 1, added
    return added[0]


@pytest.fixture
def oracle(kcalc_session: AutomationEngine) -> KWinOracle:
    return KWinOracle(_info(kcalc_session).dbus_address)


@pytest.fixture
def zoo(kcalc_session: AutomationEngine, oracle: KWinOracle) -> Iterator[ZooProcess]:
    info = _info(kcalc_session)
    with ZooProcess(wayland_display=info.wayland_socket, dbus_address=info.dbus_address) as app:
        app.layer_shell()
        app.expect(f"window: {MAIN_CAPTION}")
        oracle.wait_for_windows(lambda ws: len(_of(ws, app.pid)) == 1)
        yield app


def test_window_focus_targets_exact_window_id(
    kcalc_session: AutomationEngine, oracle: KWinOracle, launch: Callable[..., AppLaunchResult]
) -> None:
    first, second = _two_kcalc_windows(oracle, launch)

    for expected in (first, second, first):
        result = kcalc_session.window_focus(WindowSelector(id=expected))
        assert result.window_id == expected, result
        assert result.render().startswith("Focused: "), result.render()
        _wait_active(oracle, expected)
        listed = kcalc_session.window_list(WindowSelector(id=expected))
        assert [w.active for w in listed.windows] == [True], listed
        assert listed.render().count("[active]") == 1, listed.render()


@pytest.mark.qa("QA-WIN-031")
def test_virtual_close_consent_not_required_closes_only_target(
    kcalc_session: AutomationEngine, oracle: KWinOracle, launch: Callable[..., AppLaunchResult]
) -> None:
    target, survivor = _two_kcalc_windows(oracle, launch)
    expected = oracle.window(target)
    assert expected is not None

    closed = kcalc_session.window_close(WindowSelector(id=target))

    assert closed.model_dump(exclude={"frames", "warnings"}) == {
        "window_id": target,
        "app_id": expected.resource_class,
        "caption": expected.caption,
        "requested": True,
        "consent": "not_required",
        "refusal": None,
    }, closed
    assert closed.render() == (
        f'Close requested: {expected.resource_class} "{expected.caption}" ({target}). '
        "The app may keep the window open, for example to ask about unsaved changes; "
        "check window_list."
    )

    assert _await_kcalc_ids(oracle, lambda ids: target not in ids) == [survivor]
    # The survivor must still be there once the close has fully settled.
    time.sleep(1)
    assert _kcalc_ids(oracle) == [survivor]
    gone = kcalc_session.window_list(WindowSelector(id=target))
    assert gone.windows == [], gone
    assert gone.render() == f"No window with id {target!r}."


def test_window_close_reports_an_unknown_id(
    kcalc_session: AutomationEngine, oracle: KWinOracle
) -> None:
    before = _kcalc_ids(oracle)

    closed = kcalc_session.window_close(WindowSelector(id="{no-such-window}"))

    # An id that names no window is a refusal, not an error.
    assert (closed.window_id, closed.requested, closed.refusal) == (
        "{no-such-window}",
        False,
        "no_window",
    ), closed
    assert closed.render() == "No window with id '{no-such-window}'."
    time.sleep(1)
    assert _kcalc_ids(oracle) == before


@pytest.mark.parametrize("tool", ["window_close", "window_geometry"])
def test_window_id_with_a_quote_is_data_not_script(
    kcalc_session: AutomationEngine,
    oracle: KWinOracle,
    launch: Callable[..., AppLaunchResult],
    tool: str,
) -> None:
    ids = _two_kcalc_windows(oracle, launch)
    # The injected script would close the active window, so make sure there
    # is one: otherwise a missing close would prove nothing.
    kcalc_session.window_focus(WindowSelector(id=ids[0]))
    _wait_active(oracle, ids[0])

    missing = f"No window with id {INJECTED_ID!r}."
    if tool == "window_close":
        closed = kcalc_session.window_close(WindowSelector(id=INJECTED_ID))
        assert closed.refusal == "no_window", closed
        assert closed.render() == missing
    else:
        # window_geometry's id lookup is window_list's id filter in 1.0.
        listed = kcalc_session.window_list(WindowSelector(id=INJECTED_ID))
        assert listed.windows == [], listed
        assert listed.render() == missing

    time.sleep(2)
    assert sorted(_kcalc_ids(oracle)) == sorted(ids)
    assert _active_id(oracle) == ids[0]


def test_window_list_active_follows_window_focus(
    kcalc_session: AutomationEngine,
    oracle: KWinOracle,
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
) -> None:
    launch("kwrite")
    wait_for_app("kwrite")
    windows = oracle.wait_for_windows(lambda ws: any("kwrite" in w.resource_class for w in ws))
    [kcalc_id] = _kcalc_ids(oracle)
    [kwrite_id] = [w.id for w in windows if "kwrite" in w.resource_class]

    for app_id, expected in (("kwrite", kwrite_id), ("kcalc", kcalc_id), ("kwrite", kwrite_id)):
        result = kcalc_session.window_focus(WindowSelector(app_id=app_id))
        assert result.window_id == expected, result
        _wait_active(oracle, expected)
        active = kcalc_session.window_list(WindowSelector(active=True))
        assert [w.id for w in active.windows] == [expected], active
        assert active.render().startswith("Active window:\n- "), active.render()
        # The unfiltered list marks the same window as active, and only that one.
        report = kcalc_session.window_list()
        assert [w.id for w in report.windows if w.active] == [expected], report
        assert report.render().count("[active]") == 1, report.render()


@pytest.mark.qa("QA-WIN-032")
def test_dialog_close_by_id(
    kcalc_session: AutomationEngine, oracle: KWinOracle, zoo: ZooProcess
) -> None:
    [main] = _of(oracle.windows(), zoo.pid)
    dialog = _open(oracle, zoo, "dialog", "Zoo Dialog")
    assert oracle.activate(dialog.id)
    _wait_active(oracle, dialog.id)

    listed = kcalc_session.window_list(WindowSelector(active=True)).windows
    assert [w.id for w in listed] == [dialog.id], listed

    placed = kcalc_session.window_place(WindowSelector(id=dialog.id), frame=PartialRect(x=50, y=50))
    assert placed.window_id == dialog.id, placed
    deadline = time.monotonic() + STATE_TIMEOUT_S
    while True:
        moved = oracle.window(dialog.id)
        assert moved is not None and moved.frame is not None
        if moved.frame.as_ints()[:2] == (50, 50):
            break
        assert time.monotonic() < deadline, moved
        time.sleep(POLL_S)

    closed = kcalc_session.window_close(WindowSelector(active=True))
    assert (closed.window_id, closed.requested, closed.consent) == (
        dialog.id,
        True,
        "not_required",
    ), closed
    zoo.expect("closed: Zoo Dialog")
    oracle.wait_for_windows(lambda ws: dialog.id not in {w.id for w in ws})
    assert oracle.window(main.id) is not None


@pytest.mark.qa("QA-WIN-034")
def test_app_refuses_close(
    kcalc_session: AutomationEngine, oracle: KWinOracle, zoo: ZooProcess
) -> None:
    refuser = _open(oracle, zoo, "refuser", "Zoo Refuser")
    start = len(zoo.lines)

    closed = kcalc_session.window_close(WindowSelector(id=refuser.id))

    assert (closed.requested, closed.consent, closed.refusal) == (True, "not_required", None)
    assert "The app may keep the window open" in closed.render(), closed.render()
    zoo.expect("refused-close: Zoo Refuser", start=start)
    time.sleep(1)
    assert oracle.window(refuser.id) is not None


@pytest.mark.qa("QA-WIN-042")
def test_app_name_no_match_is_tool_error(
    kcalc_session: AutomationEngine, oracle: KWinOracle
) -> None:
    before = {w.id for w in oracle.windows()}

    # A name selector that matches nothing is an error, unlike an unknown id.
    with pytest.raises(
        ValueError,
        match=re.escape("No window matches {app_id='nope'}.\nList windows with window_list."),
    ):
        kcalc_session.window_close(WindowSelector(app_id="nope"))
    assert {w.id for w in oracle.windows()} == before
