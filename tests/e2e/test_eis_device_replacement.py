"""Input keeps reaching apps after KWin replaces its EIS devices (issue #76).

KWin removes the EIS keyboard of every client and adds a new one whenever the
keyboard layouts are reconfigured, and does the same with the absolute
pointer/touch device whenever an output changes. kwin-mcp used to read libei
events only during the handshake, so it kept sending to the removed devices:
KWin discarded every key, click and touch while the tools reported success.
These tests trigger each replacement on a real KWin and observe the result in
the probe app over AT-SPI, with a working-input control before each trigger.
When KWin drops the EIS connection altogether, input tools must fail instead
of reporting input that never arrived. Races and orderings KWin cannot be made
to produce on demand are covered by the scripted-libei units in
``test_eis_handshake.py``.

The triggers (``KeyboardLayoutTrigger``, ``change_output``, ``disconnect_eis``) are
importable: ``test_eis_held_revalidation.py`` and the held-input tests reuse them.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import dbus
import dbus.bus
import pytest
from dbus.lowlevel import SignalMessage
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
from probe_session import POLL_S, Probe, field, start_probe_session
from visual_harness import _apply_output_scale

from kwin_mcp.models import HeldInput

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from kwin_mcp.core import AutomationEngine

LAYOUT_SIGNAL_TIMEOUT_SECONDS = 5.0
# An output change KWin applies through KScreen, like a user changing scale.
OUTPUT_SCALE = 1.25
# Input tools must report a dropped EIS connection without stalling.
DISCONNECT_FAILURE_SECONDS = 1.0
EIS_CLOSED = "KWin closed the EIS connection; input is unavailable"
EIS_DISCONNECTED_WARNING = (
    "EIS disconnected: held input is no longer tracked; KWin may keep it pressed"
)


class KeyboardLayoutTrigger:
    """Make KWin reconfigure its keyboard layouts, as a layout settings change does.

    KWin 6.4+ watches kxkbrc through KConfigWatcher and KWin 6.3 listens for
    org.kde.keyboard reloadConfig, so both signals are sent. A synchronous call
    to KWin on the same connection follows them: messages on one connection
    arrive in order, so its reply means KWin has handled the signals and queued
    the EIS keyboard replacement. KWin announces every reconfigure with the
    public org.kde.KeyboardLayouts.layoutListChanged signal, which proves the
    trigger took effect on this KWin version.
    """

    def __init__(self, dbus_address: str) -> None:
        self._bus = dbus.bus.BusConnection(dbus_address, mainloop=DBusGMainLoop())
        self._announced = 0
        self._bus.add_signal_receiver(
            self._on_layout_list_changed,
            signal_name="layoutListChanged",
            dbus_interface="org.kde.KeyboardLayouts",
            path="/Layouts",
        )
        # The match rule must be in place before the first trigger.
        self._barrier()

    def close(self) -> None:
        self._bus.close()

    def reconfigure(self) -> None:
        expected = self._announced + 1
        notify = SignalMessage("/kxkbrc", "org.kde.kconfig.notify", "ConfigChanged")
        changed_keys = dbus.Array([dbus.ByteArray(b"LayoutList")], signature="ay")
        notify.append(dbus.Dictionary({dbus.String("Layout"): changed_keys}, signature="saay"))
        self._bus.send_message(notify)
        self._bus.send_message(SignalMessage("/Layouts", "org.kde.keyboard", "reloadConfig"))
        self._barrier()

        context = GLib.MainContext.default()
        deadline = time.monotonic() + LAYOUT_SIGNAL_TIMEOUT_SECONDS
        while self._announced < expected and time.monotonic() < deadline:
            if not context.iteration(False):
                time.sleep(POLL_S)
        assert self._announced >= expected, "KWin did not reconfigure its keyboard layouts"

    def _barrier(self) -> None:
        kwin = self._bus.get_object("org.kde.KWin", "/org/kde/KWin")
        dbus.Interface(kwin, "org.freedesktop.DBus.Introspectable").Introspect(timeout=5)

    def _on_layout_list_changed(self, *_args: object) -> None:
        self._announced += 1


def change_output(engine: AutomationEngine, log_dir: Path) -> None:
    """Rescale every output through KScreen: KWin replaces the absolute pointer device."""
    info = engine.session_info().session
    assert info is not None
    _apply_output_scale(
        dbus_address=info.dbus_address,
        wayland_display=info.wayland_socket,
        scale=OUTPUT_SCALE,
        log_path=log_dir / "kscreen-doctor.log",
        isolated_home={},
    )


def disconnect_eis(engine: AutomationEngine) -> None:
    """Close kwin-mcp's EIS connection from KWin's side (RemoteDesktop.disconnect)."""
    info = engine.session_info().session
    assert info is not None
    assert engine._input is not None
    cookie = engine._input._client._cookie
    bus = dbus.bus.BusConnection(info.dbus_address)
    try:
        remote_desktop = dbus.Interface(
            bus.get_object("org.kde.KWin", "/org/kde/KWin/EIS/RemoteDesktop"),
            "org.kde.KWin.EIS.RemoteDesktop",
        )
        remote_desktop.disconnect(dbus.Int32(cookie))
    finally:
        bus.close()


def wait_for_entry_text(probe: Probe, expected: str) -> None:
    deadline = time.monotonic() + 5.0
    actual = probe.entry_text()
    while actual != expected and time.monotonic() < deadline:
        time.sleep(POLL_S)
        actual = probe.entry_text()
    assert actual == expected, f"Keyboard Target text is {actual!r}, expected {expected!r}"


@pytest.fixture
def probe(engine: AutomationEngine, tmp_path: Path) -> Probe:
    return start_probe_session(engine, tmp_path)


def _wait_change(probe: Probe, prefix: str, previous: str) -> str:
    return probe.wait_status(prefix, lambda s: s != previous)


def test_keyboard_input_survives_layout_reconfigures(
    engine: AutomationEngine, probe: Probe
) -> None:
    engine.keyboard_type("before")
    wait_for_entry_text(probe, "before")

    trigger = KeyboardLayoutTrigger(probe.dbus_address)
    try:
        trigger.reconfigure()
        engine.keyboard_type(" after")
        wait_for_entry_text(probe, "before after")

        trigger.reconfigure()
        engine.keyboard_type(" again")
        wait_for_entry_text(probe, "before after again")
    finally:
        trigger.close()

    stopped = engine.session_stop()
    assert stopped.stopped and stopped.released == HeldInput()


def test_touch_first_then_click_survive_output_change(
    engine: AutomationEngine, probe: Probe, tmp_path: Path
) -> None:
    click_status = probe.status("click_status:")
    engine.pointer_click(probe.center("Click Target"))
    control = _wait_change(probe, "click_status:", click_status)
    assert "button=left count=1" in control, control

    change_output(engine, tmp_path)

    # Touch goes first: it resolves the replaced absolute device before any
    # pointer call could have picked up the new one.
    drag_status = probe.status("drag_status:")
    engine.touch_tap(probe.center("Drag Target"))
    touched = _wait_change(probe, "drag_status:", drag_status)
    assert field(touched, "source") == "touch", touched

    click_status = probe.status("click_status:")
    engine.pointer_click(probe.center("Click Target"), button="right")
    clicked = _wait_change(probe, "click_status:", click_status)
    assert field(clicked, "button") == "right", clicked


@pytest.mark.qa("QA-INP-045")
def test_input_tools_error_after_eis_disconnect(engine: AutomationEngine, probe: Probe) -> None:
    engine.keyboard_type("before")
    wait_for_entry_text(probe, "before")
    target = probe.center("Click Target")

    disconnect_eis(engine)

    # The next revalidation (here session_info) reports the loss eagerly.
    deadline = time.monotonic() + 5.0
    info = engine.session_info()
    while EIS_DISCONNECTED_WARNING not in info.warnings:
        assert time.monotonic() < deadline, info.render()
        time.sleep(POLL_S)
        info = engine.session_info()
    assert info.held == HeldInput()
    assert info.session is not None and info.session.input_backend is None, info.render()

    tools: dict[str, Callable[[], object]] = {
        "keyboard_type": lambda: engine.keyboard_type("x"),
        "pointer_click": lambda: engine.pointer_click(target),
        "touch_tap": lambda: engine.touch_tap(target),
    }
    for name, call in tools.items():
        started = time.monotonic()
        with pytest.raises(RuntimeError, match=EIS_CLOSED):
            call()
        elapsed = time.monotonic() - started
        assert elapsed < DISCONNECT_FAILURE_SECONDS, (name, elapsed)
    wait_for_entry_text(probe, "before")

    stopped = engine.session_stop()
    assert stopped.stopped and stopped.released == HeldInput()
