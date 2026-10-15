"""End-to-end coverage for desktop_list: KWin's virtual desktops in pager order.

A virtual KWin session starts with one desktop, so each test adds two. The
expected state is read independently: through KWin's own
VirtualDesktopManager D-Bus interface (``dbus-send``) and ``kwin_oracle``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import TYPE_CHECKING

import pytest
from kwin_oracle import KWinOracle

if TYPE_CHECKING:
    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import SessionInfo

_MANAGER_PATH = "/VirtualDesktopManager"
_MANAGER_INTERFACE = "org.kde.KWin.VirtualDesktopManager"
_VARIANT_STRING = re.compile(r'variant\s+string\s+"([^"]*)"')


def _info(engine: AutomationEngine) -> SessionInfo:
    session = engine.session_info().session
    assert session is not None
    return session


def _dbus_send(dbus_address: str, *args: str) -> str:
    result = subprocess.run(
        ["dbus-send", "--session", "--print-reply", "--dest=org.kde.KWin", *args],
        env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": dbus_address},
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return result.stdout


def _add_two_desktops(dbus_address: str) -> None:
    for position, name in ((1, "Two"), (2, "Three")):
        _dbus_send(
            dbus_address,
            _MANAGER_PATH,
            f"{_MANAGER_INTERFACE}.createDesktop",
            f"uint32:{position}",
            f"string:{name}",
        )


def _current_from_dbus(dbus_address: str) -> str:
    reply = _dbus_send(
        dbus_address,
        _MANAGER_PATH,
        "org.freedesktop.DBus.Properties.Get",
        f"string:{_MANAGER_INTERFACE}",
        "string:current",
    )
    match = _VARIANT_STRING.search(reply)
    assert match is not None, reply
    return match.group(1)


def test_desktop_list_lists_stable_ids_names_and_one_current(
    kcalc_session: AutomationEngine,
) -> None:
    dbus_address = _info(kcalc_session).dbus_address
    _add_two_desktops(dbus_address)

    desktops = kcalc_session.desktop_list().desktops
    assert len(desktops) == 3, desktops
    ids = [d.id for d in desktops]
    assert len(set(ids)) == 3 and all(ids), desktops
    assert [d.name for d in desktops][1:] == ["Two", "Three"], desktops
    assert [d.current for d in desktops].count(True) == 1, desktops
    current = next(d for d in desktops if d.current)
    assert current.id == _current_from_dbus(dbus_address), desktops

    # Ids are stable: listing again reports the same desktops.
    again = kcalc_session.desktop_list().desktops
    assert [d.id for d in again] == ids


@pytest.mark.qa("QA-WIN-011")
def test_desktops_in_kwin_order_with_current(kcalc_session: AutomationEngine) -> None:
    dbus_address = _info(kcalc_session).dbus_address
    oracle = KWinOracle(dbus_address)
    _add_two_desktops(dbus_address)
    expected = oracle.desktops()
    assert len(expected) == 3, expected
    second = expected[1].id
    oracle.run(
        "var list = workspace.desktops;\n"
        "for (var i = 0; i < list.length; i++) {\n"
        f"    if (String(list[i].id) === {json.dumps(second)}) "
        "{ workspace.currentDesktop = list[i]; }\n"
        "}\n"
        "report(true);"
    )
    assert oracle.current_desktop().id == second

    listing = kcalc_session.desktop_list()
    assert [(d.id, d.name) for d in listing.desktops] == [(d.id, d.name) for d in expected]
    assert [d.current for d in listing.desktops] == [False, True, False], listing
    assert listing.current_desktop_id == second, listing
    assert _current_from_dbus(dbus_address) == second
    assert listing.render() == "\n".join(
        [
            "Virtual desktops (3):",
            *(
                f'- "{d.name}"{" [current]" if d.id == second else ""}\n    id: {d.id}'
                for d in expected
            ),
        ]
    )
