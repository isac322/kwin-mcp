from __future__ import annotations

import dbus
import dbus.bus
import pytest


@pytest.mark.kwin
def test_virtual_session_reachable(virtual_session) -> None:
    assert virtual_session.dbus_address

    bus = dbus.bus.BusConnection(virtual_session.dbus_address)
    kwin_obj = bus.get_object("org.kde.KWin", "/org/kde/KWin")
    dbus.Interface(kwin_obj, "org.freedesktop.DBus.Peer").Ping()
