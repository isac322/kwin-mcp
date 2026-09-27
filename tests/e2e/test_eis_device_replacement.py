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
"""

from __future__ import annotations

import ast
import re
import time
from typing import TYPE_CHECKING

import dbus
import dbus.bus
import pytest
from dbus.lowlevel import SignalMessage
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
from visual_harness import _apply_output_scale

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.session import SessionInfo

PROBE_COMMAND = "python3 /app/tests/e2e/interaction_probe.py"
PROBE_SELECTOR = "interaction_probe.py"
PROBE_ENV = {
    "GDK_BACKEND": "wayland",
    "GTK_MODULES": "gail:atk-bridge",
    "NO_AT_BRIDGE": "0",
    "XDG_SESSION_TYPE": "wayland",
}
POLL_INTERVAL_SECONDS = 0.1
STATE_TIMEOUT_SECONDS = 5.0
LAYOUT_SIGNAL_TIMEOUT_SECONDS = 5.0
# An output change KWin applies through KScreen, like a user changing scale.
OUTPUT_SCALE = 1.25
# Input tools must report a dropped EIS connection without stalling.
DISCONNECT_FAILURE_SECONDS = 1.0
_RECT = r"\((-?\d+), (-?\d+), (\d+)x(\d+)\)"
_KEYBOARD_TARGET_LINE = re.compile(r'^\s*- \[[^]]+\] "Keyboard Target"(?:\s|$)')


@pytest.fixture
def probe_session(engine: AutomationEngine, start_session: Callable[..., str]) -> SessionInfo:
    """A virtual session running the interaction probe with EIS input."""
    output = start_session(PROBE_COMMAND, env=PROBE_ENV)
    assert "Input backend: KWin EIS" in output, output
    waited = engine.wait_for_element(
        query="Keyboard Target", app_name=PROBE_SELECTOR, timeout_ms=15_000
    )
    assert "Keyboard Target" in waited, waited[:1500]
    info = engine._get_session().info
    assert info is not None
    return info


def _tree(engine: AutomationEngine) -> str:
    return engine.accessibility_tree(app_name=PROBE_SELECTOR)


def _entry_text(tree: str) -> str:
    for line in tree.splitlines():
        if _KEYBOARD_TARGET_LINE.match(line) is None:
            continue
        match = re.search(r"\btext=(.+?)(?: \[actions:|$)", line)
        return ast.literal_eval(match.group(1)) if match is not None else ""
    raise AssertionError(f"Keyboard Target was absent from the accessibility tree:\n{tree[:2000]}")


def _wait_for_entry_text(engine: AutomationEngine, expected: str) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    actual = _entry_text(_tree(engine))
    while actual != expected and time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        actual = _entry_text(_tree(engine))
    assert actual == expected, f"Keyboard Target text is {actual!r}, expected {expected!r}"


def _status(tree: str, prefix: str) -> str:
    matches = re.findall(rf'\[label] "({re.escape(prefix)}[^"]*)"', tree)
    assert len(matches) == 1, tree[:2000]
    return matches[0]


def _wait_for_status_change(engine: AutomationEngine, prefix: str, previous: str) -> str:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    current = _status(_tree(engine), prefix)
    while current == previous and time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        current = _status(_tree(engine), prefix)
    assert current != previous, f"{prefix} stayed {previous!r}"
    return current


def _center(engine: AutomationEngine, name: str) -> tuple[int, int]:
    elements = engine.find_ui_elements(query=name, app_name=PROBE_SELECTOR)
    matches = re.findall(
        rf'^- \[[^]]+] "{re.escape(name)}" @ screen {_RECT}(?:\s|$)', elements, re.MULTILINE
    )
    assert len(matches) == 1, elements[:1500]
    x, y, width, height = map(int, matches[0])
    return x + width // 2, y + height // 2


class _KeyboardLayoutTrigger:
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
                time.sleep(POLL_INTERVAL_SECONDS)
        assert self._announced >= expected, "KWin did not reconfigure its keyboard layouts"

    def _barrier(self) -> None:
        kwin = self._bus.get_object("org.kde.KWin", "/org/kde/KWin")
        dbus.Interface(kwin, "org.freedesktop.DBus.Introspectable").Introspect(timeout=5)

    def _on_layout_list_changed(self, *_args: object) -> None:
        self._announced += 1


def test_keyboard_input_survives_layout_reconfigures(
    engine: AutomationEngine, probe_session: SessionInfo
) -> None:
    engine.keyboard_type("before")
    _wait_for_entry_text(engine, "before")

    trigger = _KeyboardLayoutTrigger(probe_session.dbus_address)
    try:
        trigger.reconfigure()
        engine.keyboard_type(" after")
        _wait_for_entry_text(engine, "before after")

        trigger.reconfigure()
        engine.keyboard_type(" again")
        _wait_for_entry_text(engine, "before after again")
    finally:
        trigger.close()

    assert engine.session_stop() == "Session stopped."


def test_touch_first_then_click_survive_output_change(
    engine: AutomationEngine, probe_session: SessionInfo, tmp_path: Path
) -> None:
    click_status = _status(_tree(engine), "click_status:")
    engine.mouse_click(*_center(engine, "Click Target"))
    control = _wait_for_status_change(engine, "click_status:", click_status)
    assert "button=left count=1" in control, control

    _apply_output_scale(
        dbus_address=probe_session.dbus_address,
        wayland_display=probe_session.wayland_socket,
        scale=OUTPUT_SCALE,
        log_path=tmp_path / "kscreen-doctor.log",
        isolated_home={},
    )

    # Touch goes first: it resolves the replaced absolute device before any
    # pointer call could have picked up the new one.
    drag_status = _status(_tree(engine), "drag_status:")
    engine.touch_tap(*_center(engine, "Drag Target"))
    touched = _wait_for_status_change(engine, "drag_status:", drag_status)
    assert "source=touch" in touched, touched

    click_status = _status(_tree(engine), "click_status:")
    engine.mouse_click(*_center(engine, "Click Target"), button="right")
    clicked = _wait_for_status_change(engine, "click_status:", click_status)
    assert "button=right" in clicked, clicked


def test_input_tools_error_after_eis_disconnect(
    engine: AutomationEngine, probe_session: SessionInfo
) -> None:
    engine.keyboard_type("before")
    _wait_for_entry_text(engine, "before")
    target = _center(engine, "Click Target")

    assert engine._input is not None
    cookie = engine._input._client._cookie
    bus = dbus.bus.BusConnection(probe_session.dbus_address)
    try:
        remote_desktop = dbus.Interface(
            bus.get_object("org.kde.KWin", "/org/kde/KWin/EIS/RemoteDesktop"),
            "org.kde.KWin.EIS.RemoteDesktop",
        )
        remote_desktop.disconnect(dbus.Int32(cookie))
    finally:
        bus.close()

    tools: dict[str, Callable[[], object]] = {
        "keyboard_type": lambda: engine.keyboard_type("x"),
        "mouse_click": lambda: engine.mouse_click(*target),
        "touch_tap": lambda: engine.touch_tap(*target),
    }
    for name, call in tools.items():
        started = time.monotonic()
        with pytest.raises(RuntimeError, match=r"(?i)pointer|keyboard|touch|connection"):
            call()
        elapsed = time.monotonic() - started
        assert elapsed < DISCONNECT_FAILURE_SECONDS, (name, elapsed)

    assert engine.session_stop() == "Session stopped."
