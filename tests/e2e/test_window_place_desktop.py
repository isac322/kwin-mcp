"""End-to-end coverage for window_place's desktop part (PR #103 window_set_desktop).

A virtual KWin session starts with one desktop, so each test adds two with a
test-owned KWin script. Window memberships, transient/modal relationships and
the current desktop are read back through ``kwin_oracle``, never through the
tool under test.
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from kwin_oracle import KWinOracle, OracleWindow
from window_zoo import MAIN_CAPTION, ZooProcess

from kwin_mcp.models import WindowSelector

if TYPE_CHECKING:
    from collections.abc import Callable

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import AppLaunchResult, ElementList, SessionInfo

UNKNOWN_ID = "{00000000-0000-0000-0000-000000000000}"
STATE_TIMEOUT_S = 15.0
POLL_S = 0.25
TRANSIENT_PROBE = Path(__file__).with_name("transient_probe.py")


def _info(engine: AutomationEngine) -> SessionInfo:
    session = engine.session_info().session
    assert session is not None
    return session


def _add_two_desktops(oracle: KWinOracle) -> tuple[str, str, str]:
    """Add "Two" and "Three" after the first desktop; return the three ids, first current."""
    oracle.run(
        "workspace.createDesktop(1, 'Two');\nworkspace.createDesktop(2, 'Three');\nreport(1);"
    )
    desktops = oracle.desktops()
    assert [d.name for d in desktops][1:] == ["Two", "Three"], desktops
    assert oracle.current_desktop().id == desktops[0].id
    first, second, third = (d.id for d in desktops)
    return first, second, third


def _kcalc_windows(oracle: KWinOracle, count: int) -> list[OracleWindow]:
    windows = oracle.wait_for_windows(
        lambda ws: len([w for w in ws if "kcalc" in w.resource_class.lower()]) == count
    )
    return [w for w in windows if "kcalc" in w.resource_class.lower()]


def _window(oracle: KWinOracle, window_id: str) -> OracleWindow:
    window = oracle.window(window_id)
    assert window is not None, window_id
    return window


def _assert_refused_without_changes(
    engine: AutomationEngine, oracle: KWinOracle, window_id: str, desktop_id: str, related_id: str
) -> None:
    """Request a move that must be refused; nothing it involves may change."""
    involved = (window_id, related_id)
    # Captured immediately before the call, not assumed from setup.
    memberships = {i: _window(oracle, i).desktops for i in involved}
    for i in involved:
        # A window already on the target could not show a refused move.
        assert memberships[i] != (desktop_id,), memberships
    current = oracle.current_desktop().id
    active = oracle.active_window()

    with pytest.raises(RuntimeError, match="related window") as refused:
        engine.window_place(WindowSelector(id=window_id), desktop=desktop_id)

    message = str(refused.value)
    assert message.startswith(f"Refused: KWin would also move 1 related window(s) of {window_id!r}")
    assert related_id in message, message
    assert message.endswith("nothing was changed."), message
    for i in involved:
        assert _window(oracle, i).desktops == memberships[i], i
    assert oracle.current_desktop().id == current
    assert oracle.active_window() == active


@pytest.fixture
def oracle(kcalc_session: AutomationEngine) -> KWinOracle:
    return KWinOracle(_info(kcalc_session).dbus_address)


@pytest.mark.qa("QA-WIN-023")
def test_moves_exactly_one_window_without_switching(
    kcalc_session: AutomationEngine, oracle: KWinOracle, launch: Callable[..., AppLaunchResult]
) -> None:
    first, _, third = _add_two_desktops(oracle)
    # Two windows of the same app with the same caption: only the id may select one.
    launch("kcalc")
    moved, other = _kcalc_windows(oracle, 2)
    active = oracle.active_window()

    result = kcalc_session.window_place(WindowSelector(id=moved.id), desktop=third)

    assert result.observation_error is None, result
    assert result.applied.desktop is True, result
    assert result.applied.frame is None, result
    assert result.before.desktops == [first], result
    assert result.before.on_all_desktops is False, result
    assert result.window is not None and result.window.desktops == [third], result
    assert result.current_desktop_before == first, result
    assert result.current_desktop_after == first, result
    assert _window(oracle, moved.id).desktops == (third,)
    assert _window(oracle, other.id).desktops == (first,)
    assert oracle.current_desktop().id == first
    # The moved window is not activated.
    assert oracle.active_window() == active
    assert result.render().splitlines()[:4] == [
        f"Window desktop set: {moved.id} -> {third}",
        f"before:   ['{first}']",
        f"observed: ['{third}']",
        f"current desktop: {first} -> {first}",
    ]

    # The other kcalc window did not move: putting it on the current desktop
    # (where it already is) reports it there before and after.
    stay = kcalc_session.window_place(WindowSelector(id=other.id), desktop=first)
    assert stay.before.desktops == [first], stay
    assert stay.applied.desktop is True, stay

    # Moving back restores the original assignment.
    back = kcalc_session.window_place(WindowSelector(id=moved.id), desktop=first)
    assert back.applied.desktop is True, back
    assert back.before.desktops == [third], back
    assert back.window is not None and back.window.desktops == [first], back
    assert _window(oracle, moved.id).desktops == (first,)


@pytest.mark.qa("QA-WIN-026")
def test_unknown_window_or_desktop_changes_nothing(
    kcalc_session: AutomationEngine, oracle: KWinOracle
) -> None:
    first, second, third = _add_two_desktops(oracle)
    [kcalc] = _kcalc_windows(oracle, 1)

    with pytest.raises(ValueError, match=r"^No window with id '\{0{8}-"):
        kcalc_session.window_place(WindowSelector(id=UNKNOWN_ID), desktop=second)

    # Ids only: pager positions, names and the empty string never match.
    known = f"Use an id from desktop_list: '{first}', '{second}', '{third}'."
    for desktop in ("1", "2", "", "Two", UNKNOWN_ID.strip("{}")):
        with pytest.raises(RuntimeError) as error:
            kcalc_session.window_place(WindowSelector(id=kcalc.id), desktop=desktop)
        assert str(error.value) == (
            f"No virtual desktop with id {desktop!r} (unknown desktop id); "
            f"nothing was changed.\n{known}"
        )

    # The desktop argument is a string id: no positional number arm exists.
    annotation = kcalc_session.window_place.__annotations__["desktop"]
    assert annotation == str | None, annotation

    # Neither call moved the window.
    assert _window(oracle, kcalc.id).desktops == (first,)
    assert oracle.current_desktop().id == first


@pytest.mark.qa("QA-WIN-024")
def test_on_all_desktops_ends_on_exactly_the_target(
    kcalc_session: AutomationEngine, oracle: KWinOracle
) -> None:
    _, second, _ = _add_two_desktops(oracle)
    [kcalc] = _kcalc_windows(oracle, 1)
    oracle.run(
        f"var w = findWindow({json.dumps(kcalc.id)});\nw.onAllDesktops = true;\nreport(true);"
    )
    assert _window(oracle, kcalc.id).on_all_desktops

    result = kcalc_session.window_place(WindowSelector(id=kcalc.id), desktop=second)

    assert result.before.desktops == [], result
    assert result.before.on_all_desktops is True, result
    assert result.applied.desktop is True, result
    assert result.window is not None and result.window.desktops == [second], result
    assert result.window.on_all_desktops is False, result
    assert "before:   [] (all desktops)" in result.render().splitlines()
    after = _window(oracle, kcalc.id)
    assert after.desktops == (second,) and not after.on_all_desktops, after


def _wait_for_transient(
    oracle: KWinOracle, predicate: Callable[[OracleWindow], bool]
) -> tuple[OracleWindow, OracleWindow]:
    """Wait for a window matching ``predicate`` that is transient for another; return both."""
    deadline = time.monotonic() + STATE_TIMEOUT_S
    while True:
        windows = {w.id: w for w in oracle.windows()}
        for child in windows.values():
            if predicate(child) and child.transient_for in windows:
                return windows[child.transient_for], child
        assert time.monotonic() < deadline, list(windows.values())
        time.sleep(POLL_S)


@pytest.mark.qa("QA-WIN-027")
def test_transient_parent_child_modal_refusal(
    kcalc_session: AutomationEngine,
    oracle: KWinOracle,
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
) -> None:
    first, second, third = _add_two_desktops(oracle)
    info = _info(kcalc_session)
    with ZooProcess(wayland_display=info.wayland_socket, dbus_address=info.dbus_address) as zoo:
        zoo.expect(f"window: {MAIN_CAPTION}")
        zoo.command("utility", "window: Zoo Utility")
        # The relationship is read from KWin, not assumed from the toolkit.
        parent, child = _wait_for_transient(
            oracle, lambda w: w.caption == "Zoo Utility" and not w.modal
        )
        assert parent.caption == MAIN_CAPTION, parent

        # A parent with a transient child: KWin would carry the child along.
        _assert_refused_without_changes(kcalc_session, oracle, parent.id, second, child.id)

        # A non-modal transient has no transients of its own: KWin moves only it.
        moved = kcalc_session.window_place(WindowSelector(id=child.id), desktop=second)
        assert moved.applied.desktop is True, moved
        assert _window(oracle, parent.id).desktops == (first,)
        assert _window(oracle, child.id).desktops == (second,)

        # The child is already exactly on the target, so the parent may follow.
        followed = kcalc_session.window_place(WindowSelector(id=parent.id), desktop=second)
        assert followed.applied.desktop is True, followed
        assert _window(oracle, parent.id).desktops == (second,)
        assert _window(oracle, child.id).desktops == (second,)

    # A modal dialog: moving it would move its main window too. KWrite's Open
    # File dialog is a modal transient of its main window.
    launch("kwrite")
    wait_for_app("kwrite")
    windows = oracle.wait_for_windows(lambda ws: any("kwrite" in w.resource_class for w in ws))
    kwrite = next(w for w in windows if "kwrite" in w.resource_class)
    kcalc_session.window_focus(WindowSelector(id=kwrite.id))
    kcalc_session.keyboard_press("ctrl+o")
    main, dialog = _wait_for_transient(oracle, lambda w: "Open File" in w.caption and w.modal)
    assert main.id == kwrite.id, (main, dialog)
    _assert_refused_without_changes(kcalc_session, oracle, dialog.id, third, main.id)


def _set_probe_desktops(oracle: KWinOracle, assignments: dict[str, str]) -> None:
    """Assign ancestors first, then split descendants using KWin's native setters."""
    oracle.run(
        f"""
        var assignments = {json.dumps(list(assignments.items()))};
        for (var a = 0; a < assignments.length; a++) {{
            var w = findWindow(assignments[a][0]);
            if (!w) throw new Error("probe window vanished");
            for (var d = 0; d < workspace.desktops.length; d++) {{
                if (String(workspace.desktops[d].id) === assignments[a][1]) {{
                    w.desktops = [workspace.desktops[d]];
                    break;
                }}
            }}
        }}
        report(true);
        """
    )
    oracle.wait_for_windows(
        lambda windows: all(
            any(w.id == window_id and w.desktops == (desktop_id,) for w in windows)
            for window_id, desktop_id in assignments.items()
        )
    )


def _desktop_current_from_dbus(engine: AutomationEngine) -> str:
    reply = engine.dbus_call(
        "org.kde.KWin",
        "/VirtualDesktopManager",
        "org.freedesktop.DBus.Properties",
        "Get",
        ["string:org.kde.KWin.VirtualDesktopManager", "string:current"],
    )
    assert isinstance(reply.reply, str), reply
    return reply.reply


def _focus_sentinel_snapshot(
    engine: AutomationEngine, oracle: KWinOracle
) -> dict[str, OracleWindow]:
    """Focus KCalc so the tested probe must not gain focus when its desktop is set."""
    [sentinel] = engine.window_list(filter=WindowSelector(app_id="kcalc")).windows
    focused = engine.window_focus(WindowSelector(id=sentinel.id))
    assert focused.window is not None and focused.window.active, focused
    windows = oracle.wait_for_windows(
        lambda windows: any(w.id == sentinel.id and w.active for w in windows)
    )
    return {w.id: w for w in windows}


def _assert_probe_desktop_state(
    engine: AutomationEngine,
    oracle: KWinOracle,
    before: dict[str, OracleWindow],
    changed: dict[str, str],
    current: str,
) -> None:
    """Witness every membership, survivor, and active/current window independently."""
    after = {w.id: w for w in oracle.windows()}
    assert set(after) == set(before), (before, after)
    for window_id, window in before.items():
        expected = (changed[window_id],) if window_id in changed else window.desktops
        assert after[window_id].desktops == expected, (window_id, before, after)
    assert [w.id for w in after.values() if w.active] == [
        w.id for w in before.values() if w.active
    ], (before, after)
    desktops = engine.desktop_list()
    assert desktops.current_desktop_id == current, desktops
    assert [d.id for d in desktops.desktops if d.current] == [current], desktops
    assert oracle.current_desktop().id == current
    assert _desktop_current_from_dbus(engine) == current


def test_parent_already_on_target_is_noop_with_divergent_transient_child(
    kcalc_session: AutomationEngine, oracle: KWinOracle
) -> None:
    current, other, _ = _add_two_desktops(oracle)
    kcalc_session.app_launch(shlex.join([sys.executable, str(TRANSIENT_PROBE)]))
    parent, child = _wait_for_transient(
        oracle, lambda w: w.caption == "Transient Probe Dialog" and not w.modal
    )
    assignments = {parent.id: current, child.id: other}
    _set_probe_desktops(oracle, assignments)
    before = _focus_sentinel_snapshot(kcalc_session, oracle)
    assert before[parent.id].transient_for is None, before[parent.id]
    assert before[child.id].transient_for == parent.id, before[child.id]
    assert not before[child.id].modal, before[child.id]
    for window_id, desktop in assignments.items():
        assert before[window_id].desktops == (desktop,), before[window_id]
    assert _desktop_current_from_dbus(kcalc_session) == current

    result = kcalc_session.window_place(WindowSelector(id=parent.id), desktop=current)

    assert result.applied.desktop is True, result
    assert result.observation_error is None, result
    assert result.before.desktops == [current], result
    assert result.before.on_all_desktops is False, result
    assert result.window is not None and result.window.desktops == [current], result
    assert result.current_desktop_before == result.current_desktop_after == current, result
    _assert_probe_desktop_state(kcalc_session, oracle, before, {}, current)


def test_parent_moves_only_itself_when_targeted_child_stops_nested_propagation(
    kcalc_session: AutomationEngine, oracle: KWinOracle
) -> None:
    current, target, _ = _add_two_desktops(oracle)
    kcalc_session.app_launch(shlex.join([sys.executable, str(TRANSIENT_PROBE), "--nested"]))
    parent, child = _wait_for_transient(
        oracle, lambda w: w.caption == "Transient Probe Dialog" and not w.modal
    )
    middle, grandchild = _wait_for_transient(
        oracle, lambda w: w.caption == "Transient Probe Grandchild" and not w.modal
    )
    assert middle.id == child.id, (child, middle, grandchild)
    assignments = {parent.id: current, child.id: target, grandchild.id: current}
    _set_probe_desktops(oracle, assignments)
    before = _focus_sentinel_snapshot(kcalc_session, oracle)
    assert before[parent.id].transient_for is None, before[parent.id]
    assert before[child.id].transient_for == parent.id, before[child.id]
    assert before[grandchild.id].transient_for == child.id, before[grandchild.id]
    assert all(not before[w.id].modal for w in (child, grandchild)), before
    for window_id, desktop in assignments.items():
        assert before[window_id].desktops == (desktop,), before[window_id]
    assert _desktop_current_from_dbus(kcalc_session) == current

    result = kcalc_session.window_place(WindowSelector(id=parent.id), desktop=target)

    assert result.applied.desktop is True, result
    assert result.observation_error is None, result
    assert result.before.desktops == [current], result
    assert result.before.on_all_desktops is False, result
    assert result.window is not None and result.window.desktops == [target], result
    assert result.current_desktop_before == result.current_desktop_after == current, result
    _assert_probe_desktop_state(kcalc_session, oracle, before, {parent.id: target}, current)


def test_parent_move_refuses_both_nested_transients_that_would_change(
    kcalc_session: AutomationEngine, oracle: KWinOracle
) -> None:
    current, target, third = _add_two_desktops(oracle)
    kcalc_session.app_launch(shlex.join([sys.executable, str(TRANSIENT_PROBE), "--nested"]))
    parent, child = _wait_for_transient(
        oracle, lambda w: w.caption == "Transient Probe Dialog" and not w.modal
    )
    middle, grandchild = _wait_for_transient(
        oracle, lambda w: w.caption == "Transient Probe Grandchild" and not w.modal
    )
    assert middle.id == child.id, (child, middle, grandchild)
    assignments = {parent.id: current, child.id: current, grandchild.id: third}
    _set_probe_desktops(oracle, assignments)
    before = _focus_sentinel_snapshot(kcalc_session, oracle)
    assert before[parent.id].transient_for is None, before[parent.id]
    assert before[child.id].transient_for == parent.id, before[child.id]
    assert before[grandchild.id].transient_for == child.id, before[grandchild.id]
    assert all(not before[w.id].modal for w in (child, grandchild)), before
    for window_id, desktop in assignments.items():
        assert before[window_id].desktops == (desktop,), before[window_id]
        assert desktop != target
    assert _desktop_current_from_dbus(kcalc_session) == current

    with pytest.raises(RuntimeError, match="related window") as refused:
        kcalc_session.window_place(WindowSelector(id=parent.id), desktop=target)

    assert child.id in str(refused.value), refused.value
    assert grandchild.id in str(refused.value), refused.value
    _assert_probe_desktop_state(kcalc_session, oracle, before, {}, current)


def test_modal_dialog_is_refused_because_its_main_window_would_move(
    kcalc_session: AutomationEngine, oracle: KWinOracle
) -> None:
    _, target, _ = _add_two_desktops(oracle)
    kcalc_session.app_launch("kwrite")
    deadline = time.monotonic() + STATE_TIMEOUT_S
    while not (kwrite := kcalc_session.window_list(filter=WindowSelector(app_id="kwrite")).windows):
        assert time.monotonic() < deadline, "kwrite never mapped a window"
        time.sleep(POLL_S)
    kcalc_session.window_focus(WindowSelector(id=kwrite[0].id))
    kcalc_session.keyboard_press(keys="ctrl+o")
    main, dialog = _wait_for_transient(oracle, lambda w: "Open File" in w.caption and w.modal)
    assert main.id == kwrite[0].id, (main, dialog)
    _assert_refused_without_changes(kcalc_session, oracle, dialog.id, target, main.id)
