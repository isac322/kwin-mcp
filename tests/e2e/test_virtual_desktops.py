"""End-to-end coverage for virtual_desktops and window_set_desktop.

A virtual KWin session starts with one desktop, so each test adds two through
KWin's own VirtualDesktopManager D-Bus interface, which also serves as an
independent witness for the current desktop.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.results import VirtualDesktop

_MANAGER = ("org.kde.KWin", "/VirtualDesktopManager")
_MANAGER_INTERFACE = "org.kde.KWin.VirtualDesktopManager"
_WAIT_SECONDS = 15.0


def _add_two_desktops(engine: AutomationEngine) -> list[VirtualDesktop]:
    for position, name in ((1, "Two"), (2, "Three")):
        output = engine.dbus_call(
            *_MANAGER,
            _MANAGER_INTERFACE,
            "createDesktop",
            [f"uint32:{position}", f"string:{name}"],
        )
        assert not output.startswith(("D-Bus call failed:", "D-Bus error:")), output
    data = engine.virtual_desktops_data()
    assert data.error is None, data.error
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

    assert data.error is None, data
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

    missing_window = kcalc_session.window_set_desktop_data(
        window_id="{00000000-0000-0000-0000-000000000000}", desktop_id=target.id
    )
    assert missing_window.applied is False
    assert missing_window.error is not None and "No window with id" in missing_window.error

    missing_desktop = kcalc_session.window_set_desktop_data(
        window_id=window_id, desktop_id="00000000-0000-0000-0000-000000000000"
    )
    assert missing_desktop.applied is False
    assert missing_desktop.error is not None
    assert "No virtual desktop with id" in missing_desktop.error

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
