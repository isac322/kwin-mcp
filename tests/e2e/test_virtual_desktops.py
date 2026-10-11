"""End-to-end coverage for virtual_desktops and window_set_desktop.

A virtual KWin session starts with one desktop, so each test adds two through
KWin's own VirtualDesktopManager D-Bus interface, which also serves as an
independent witness for the current desktop.

KWin moves a window's transients along with it, and a modal dialog's main
window too; the relationship tests read those relationships back from KWin
before relying on them, so they cannot pass without the relationship in place.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.results import VirtualDesktop

_MANAGER = ("org.kde.KWin", "/VirtualDesktopManager")
_MANAGER_INTERFACE = "org.kde.KWin.VirtualDesktopManager"
_WAIT_SECONDS = 15.0
_TRANSIENT_PROBE = Path(__file__).with_name("transient_probe.py")

# Test-only readback of what Window::setDesktops follows: transientFor and modal.
_RELATIONS_SCRIPT = """
var out = [];
var windows = workspace.windowList();
for (var i = 0; i < windows.length; i++) {
    var w = windows[i];
    var desktops = [];
    for (var k = 0; k < w.desktops.length; k++) {
        desktops.push(String(w.desktops[k].id));
    }
    out.push({
        id: String(w.internalId), app: String(w.resourceClass), caption: w.caption,
        transientFor: w.transientFor ? String(w.transientFor.internalId) : null,
        modal: w.modal, desktops: desktops
    });
}
callDBus("{sink}", "{path}", "{sink}", "Report", JSON.stringify(out));
"""
_RUN_SCRIPT = "import sys, kwin_mcp.geometry as g; print(g.run_kwin_script(sys.stdin.read()))"


def _add_two_desktops(engine: AutomationEngine) -> list[VirtualDesktop]:
    # dbus_call raises when the call fails.
    for position, name in ((1, "Two"), (2, "Three")):
        engine.dbus_call(
            *_MANAGER,
            _MANAGER_INTERFACE,
            "createDesktop",
            [f"uint32:{position}", f"string:{name}"],
        )
    data = engine.virtual_desktops_data()
    assert len(data.desktops) == 3, data
    return data.desktops


def _current_from_dbus(engine: AutomationEngine) -> str:
    return engine.dbus_call(
        *_MANAGER,
        "org.freedesktop.DBus.Properties",
        "Get",
        [f"string:{_MANAGER_INTERFACE}", "string:current"],
    )


def _kcalc_ids(engine: AutomationEngine, count: int) -> list[str]:
    deadline = time.monotonic() + _WAIT_SECONDS
    while True:
        windows = engine.window_geometry_data(app_name="kcalc").windows
        if len(windows) >= count or time.monotonic() > deadline:
            assert len(windows) >= count, windows
            return [w.id for w in windows]
        time.sleep(0.25)


def _relations(engine: AutomationEngine) -> dict[str, dict[str, Any]]:
    """Every KWin window by id, with its transientFor, modal flag and desktops."""
    output = subprocess.run(
        [sys.executable, "-c", _RUN_SCRIPT],
        input=_RELATIONS_SCRIPT,
        env=engine._session_env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    return {window["id"]: window for window in json.loads(output)}


def _wait_for_transient(
    engine: AutomationEngine, caption: str, modal: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Wait for a window titled ``caption`` that is transient for another, and return both."""
    deadline = time.monotonic() + _WAIT_SECONDS
    while True:
        windows = _relations(engine)
        for child in windows.values():
            if caption in child["caption"] and child["transientFor"] in windows:
                parent = windows[child["transientFor"]]
                # The precondition is read from KWin, not assumed from the toolkit.
                assert child["modal"] is modal, child
                return parent, child
        if time.monotonic() > deadline:
            pytest.fail(f"no {caption!r} transient within {_WAIT_SECONDS}s: {windows}")
        time.sleep(0.25)


def _run_script(engine: AutomationEngine, tmp_path: Path, source: str) -> None:
    script = tmp_path / "test-script.js"
    script.write_text(source, encoding="utf-8")
    output = engine.dbus_call(
        "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "loadScript", [f"string:{script}"]
    )
    assert output.lstrip("-").isdigit(), output
    engine.dbus_call("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "start")


def test_virtual_desktops_lists_stable_ids_names_and_one_current(
    kcalc_session: AutomationEngine,
) -> None:
    desktops = _add_two_desktops(kcalc_session)

    ids = [d.id for d in desktops]
    assert len(set(ids)) == 3 and all(ids), desktops
    assert [d.name for d in desktops][1:] == ["Two", "Three"], desktops
    assert [d.current for d in desktops].count(True) == 1, desktops
    current = next(d for d in desktops if d.current)
    assert current.id in _current_from_dbus(kcalc_session)

    # Ids are stable: listing again reports the same desktops.
    again = kcalc_session.virtual_desktops_data().desktops
    assert [d.id for d in again] == ids


def test_window_set_desktop_moves_exactly_one_window_without_switching(
    kcalc_session: AutomationEngine,
) -> None:
    desktops = _add_two_desktops(kcalc_session)
    current = next(d for d in desktops if d.current)
    target = next(d for d in desktops if not d.current)

    # Two windows of the same app with the same caption: only the id may select one.
    kcalc_session.launch_app(command="kcalc")
    moved_id, other_id = _kcalc_ids(kcalc_session, 2)[:2]

    data = kcalc_session.window_set_desktop_data(window_id=moved_id, desktop_id=target.id)

    assert data.observation_error is None, data
    assert data.applied is True, data
    assert data.before == [current.id], data
    assert data.before_on_all_desktops is False, data
    assert data.observed == [target.id], data
    assert data.current_desktop_id_before == current.id, data
    assert data.current_desktop_id_after == current.id, data
    assert current.id in _current_from_dbus(kcalc_session)
    assert [d.current for d in kcalc_session.virtual_desktops_data().desktops] == [
        d.current for d in desktops
    ]

    # The other kcalc window did not move: putting it on the current desktop
    # (where it already is) reports it there before and after.
    stay = kcalc_session.window_set_desktop_data(window_id=other_id, desktop_id=current.id)
    assert stay.before == [current.id], stay
    assert stay.applied is True, stay

    # Moving back restores the original assignment.
    back = kcalc_session.window_set_desktop_data(window_id=moved_id, desktop_id=current.id)
    assert back.applied is True, back
    assert back.before == [target.id], back
    assert back.observed == [current.id], back


def test_unknown_window_or_desktop_changes_nothing(kcalc_session: AutomationEngine) -> None:
    desktops = _add_two_desktops(kcalc_session)
    current = next(d for d in desktops if d.current)
    target = next(d for d in desktops if not d.current)
    (window_id,) = _kcalc_ids(kcalc_session, 1)[:1]

    with pytest.raises(RuntimeError, match="No window with id"):
        kcalc_session.window_set_desktop_data(
            window_id="{00000000-0000-0000-0000-000000000000}", desktop_id=target.id
        )

    with pytest.raises(RuntimeError, match="No virtual desktop with id"):
        kcalc_session.window_set_desktop_data(
            window_id=window_id, desktop_id="00000000-0000-0000-0000-000000000000"
        )

    # Neither call moved the window.
    check = kcalc_session.window_set_desktop_data(window_id=window_id, desktop_id=current.id)
    assert check.before == [current.id], check


def test_window_on_all_desktops_ends_on_exactly_the_target(
    kcalc_session: AutomationEngine, tmp_path: Path
) -> None:
    desktops = _add_two_desktops(kcalc_session)
    target = next(d for d in desktops if not d.current)
    (window_id,) = _kcalc_ids(kcalc_session, 1)[:1]
    _run_script(
        kcalc_session,
        tmp_path,
        f"""
        var windows = workspace.windowList();
        for (var i = 0; i < windows.length; i++) {{
            if (String(windows[i].internalId) === {json.dumps(window_id)}) {{
                windows[i].onAllDesktops = true;
            }}
        }}
        """,
    )

    data = kcalc_session.window_set_desktop_data(window_id=window_id, desktop_id=target.id)

    assert data.before == [], data
    assert data.before_on_all_desktops is True, data
    assert data.applied is True, data
    assert data.observed == [target.id], data


def _assert_refused_without_changes(
    engine: AutomationEngine, window_id: str, desktop_id: str, related_id: str
) -> None:
    """Request a move that must be refused; nothing it involves may change."""
    involved = (window_id, related_id)
    # Captured immediately before the call, not assumed from setup.
    before = _relations(engine)
    memberships = {i: before[i]["desktops"] for i in involved}
    for i in involved:
        # A window already on the target could not show a refused move.
        assert memberships[i] != [desktop_id], before[i]
    current = [d.id for d in engine.virtual_desktops_data().desktops if d.current]
    current_dbus = _current_from_dbus(engine)

    with pytest.raises(RuntimeError, match="related window") as refused:
        engine.window_set_desktop_data(window_id=window_id, desktop_id=desktop_id)

    assert related_id in str(refused.value)
    after = _relations(engine)
    for i in involved:
        assert after[i]["desktops"] == memberships[i], (memberships[i], after[i])
    assert [d.id for d in engine.virtual_desktops_data().desktops if d.current] == current
    assert _current_from_dbus(engine) == current_dbus


def test_parent_with_a_transient_child_is_refused_and_neither_moves(
    kcalc_session: AutomationEngine,
) -> None:
    desktops = _add_two_desktops(kcalc_session)
    target = next(d for d in desktops if not d.current)
    kcalc_session.launch_app(command=f"python3 {_TRANSIENT_PROBE}")
    parent, child = _wait_for_transient(kcalc_session, "Transient Probe Dialog", modal=False)

    _assert_refused_without_changes(kcalc_session, parent["id"], target.id, child["id"])


def test_transient_child_moves_alone_then_its_parent_may_follow(
    kcalc_session: AutomationEngine,
) -> None:
    desktops = _add_two_desktops(kcalc_session)
    current = next(d for d in desktops if d.current)
    target = next(d for d in desktops if not d.current)
    kcalc_session.launch_app(command=f"python3 {_TRANSIENT_PROBE}")
    parent, child = _wait_for_transient(kcalc_session, "Transient Probe Dialog", modal=False)

    # A non-modal transient has no transients of its own: KWin moves only it.
    moved = kcalc_session.window_set_desktop_data(window_id=child["id"], desktop_id=target.id)
    assert moved.applied is True, moved
    assert _relations(kcalc_session)[parent["id"]]["desktops"] == [current.id]

    # The child is already exactly on the target, so moving the parent moves nothing else.
    followed = kcalc_session.window_set_desktop_data(window_id=parent["id"], desktop_id=target.id)
    assert followed.applied is True, followed
    after = _relations(kcalc_session)
    assert after[parent["id"]]["desktops"] == [target.id], after[parent["id"]]
    assert after[child["id"]]["desktops"] == [target.id], after[child["id"]]


def test_modal_dialog_is_refused_because_its_main_window_would_move(
    kcalc_session: AutomationEngine,
) -> None:
    desktops = _add_two_desktops(kcalc_session)
    target = next(d for d in desktops if not d.current)
    # KWrite's Open File dialog is a modal transient of its main window.
    kcalc_session.launch_app(command="kwrite")
    deadline = time.monotonic() + _WAIT_SECONDS
    while not (kwrite := kcalc_session.window_geometry_data(app_name="kwrite").windows):
        assert time.monotonic() < deadline, "kwrite never mapped a window"
        time.sleep(0.25)
    kcalc_session.focus_window(window_id=kwrite[0].id)
    kcalc_session.keyboard_key(key="ctrl+o")
    main, dialog = _wait_for_transient(kcalc_session, "Open File", modal=True)
    assert main["id"] == kwrite[0].id, (main, dialog)

    _assert_refused_without_changes(kcalc_session, dialog["id"], target.id, main["id"])
