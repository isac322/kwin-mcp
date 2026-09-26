"""Backward-compat integration tests for dbus_call MCP tool.

The dbus_call refactor (Task 9) replaces the dbus-send subprocess shim
with an in-process dbus-python call. These tests exercise the dual-shape
``args`` contract (legacy ``"type:value"`` strings + typed JSON dicts +
mixed lists) against a real virtual KWin session and verify the public
output format is preserved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from kwin_mcp.core import AutomationEngine

pytestmark = pytest.mark.kwin


def test_legacy_get_id_no_args(engine: AutomationEngine) -> None:
    out = engine.dbus_call(
        service="org.freedesktop.DBus",
        path="/org/freedesktop/DBus",
        interface="org.freedesktop.DBus",
        method="GetId",
    )
    assert out
    assert not out.startswith("D-Bus error:")
    assert not out.startswith("Argument error:")
    assert len(out) >= 32


def test_typed_get_id_empty_array(engine: AutomationEngine) -> None:
    out = engine.dbus_call(
        service="org.freedesktop.DBus",
        path="/org/freedesktop/DBus",
        interface="org.freedesktop.DBus",
        method="GetId",
        args=[],
    )
    assert out
    assert not out.startswith("D-Bus error:")


def test_kwin_show_desktop_legacy_bool(engine: AutomationEngine) -> None:
    out = engine.dbus_call(
        service="org.kde.KWin",
        path="/KWin",
        interface="org.kde.KWin",
        method="showDesktop",
        args=["boolean:false"],
    )
    assert out == ""


def test_kwin_show_desktop_typed_bool(engine: AutomationEngine) -> None:
    out = engine.dbus_call(
        service="org.kde.KWin",
        path="/KWin",
        interface="org.kde.KWin",
        method="showDesktop",
        args=[{"type": "boolean", "value": False}],
    )
    assert out == ""


def test_mixed_legacy_and_typed_args(engine: AutomationEngine) -> None:
    out_a = engine.dbus_call(
        service="org.kde.KWin",
        path="/KWin",
        interface="org.kde.KWin",
        method="showDesktop",
        args=["boolean:true"],
    )
    out_b = engine.dbus_call(
        service="org.kde.KWin",
        path="/KWin",
        interface="org.kde.KWin",
        method="showDesktop",
        args=[{"type": "boolean", "value": True}],
    )
    assert out_a == ""
    assert out_b == ""


def test_unknown_service_returns_dbus_error(engine: AutomationEngine) -> None:
    out = engine.dbus_call(
        service="org.example.DefinitelyDoesNotExist",
        path="/x",
        interface="org.example.X",
        method="Foo",
    )
    assert out.startswith("D-Bus error:")
