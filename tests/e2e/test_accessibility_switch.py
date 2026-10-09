"""The ``org.a11y.Status.IsEnabled`` switch in virtual and live sessions.

Firefox exposes no accessibility tree while the switch is off, and
a stock Plasma session leaves it off. Virtual sessions own a private bus, so
they switch it on; a live connection changes it only on request and switches
it back off at ``session_stop``.
"""

from __future__ import annotations

import os
import shlex
import shutil
from typing import TYPE_CHECKING

import dbus
import pytest
from session_harness import live_kwin

from kwin_mcp import core
from kwin_mcp.core import AutomationEngine
from kwin_mcp.session import LiveSession, accessibility_enabled, set_accessibility_enabled

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _accessibility_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("Accessibility: ")]


def test_virtual_session_switches_accessibility_on(
    engine: AutomationEngine, start_session: Callable[..., str]
) -> None:
    output = start_session()
    assert "Session started. Wayland socket: " in output, output
    assert not [line for line in output.splitlines() if line.startswith("Warning: ")], output

    reply = engine.dbus_call(
        service="org.a11y.Bus",
        path="/org/a11y/bus",
        interface="org.freedesktop.DBus.Properties",
        method="Get",
        args=["string:org.a11y.Status", "string:IsEnabled"],
    )
    assert reply.split()[-1:] == ["true"], reply


def test_live_connect_reports_the_switch_off_and_leaves_it_off(engine: AutomationEngine) -> None:
    with live_kwin() as live:
        assert not accessibility_enabled(live.dbus_address)

        output = engine.session_connect(
            dbus_address=live.dbus_address, wayland_display=live.wayland_display
        )
        assert _accessibility_lines(output) == [
            "Accessibility: off (org.a11y.Status.IsEnabled is false), so apps that check it, "
            "such as Firefox, expose no accessibility tree. Reconnect with "
            "enable_accessibility=true to switch it on until session_stop."
        ], output
        assert not accessibility_enabled(live.dbus_address)

        assert engine.session_stop() == "Disconnected from live session."
        assert not accessibility_enabled(live.dbus_address)


def test_live_connect_switches_accessibility_on_until_session_stop(
    engine: AutomationEngine,
) -> None:
    with live_kwin() as live:
        output = engine.session_connect(
            dbus_address=live.dbus_address,
            wayland_display=live.wayland_display,
            enable_accessibility=True,
        )
        lines = _accessibility_lines(output)
        assert len(lines) == 1, output
        assert lines[0].startswith("Accessibility: switched on for this connection"), output
        assert accessibility_enabled(live.dbus_address)

        assert engine.session_stop() == "Disconnected from live session."
        assert not accessibility_enabled(live.dbus_address)


def test_live_connect_leaves_a_switch_that_was_already_on(engine: AutomationEngine) -> None:
    with live_kwin() as live:
        set_accessibility_enabled(live.dbus_address, True)

        output = engine.session_connect(
            dbus_address=live.dbus_address,
            wayland_display=live.wayland_display,
            enable_accessibility=True,
        )
        assert _accessibility_lines(output) == ["Accessibility: on (org.a11y.Status.IsEnabled)"], (
            output
        )

        assert engine.session_stop() == "Disconnected from live session."
        assert accessibility_enabled(live.dbus_address)


def test_virtual_session_reports_a_switch_it_could_not_set(
    start_session: Callable[..., str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the wrapper resolves dbus-send through the patched PATH; every other
    # call, including the KWin ownership probes, reaches the real dbus-send.
    real_dbus_send = shutil.which("dbus-send")
    assert real_dbus_send is not None, "dbus-send not found in PATH"
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    stub = stub_dir / "dbus-send"
    stub.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "    *string:IsEnabled*)\n"
        "        echo 'Error org.freedesktop.DBus.Error.AccessDenied: stub refuses' >&2\n"
        "        exit 1;;\n"
        "esac\n"
        f'exec {shlex.quote(real_dbus_send)} "$@"\n'
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")

    output = start_session()

    assert "Session started. Wayland socket: " in output, output
    assert [line for line in output.splitlines() if line.startswith("Warning: ")] == [
        "Warning: AT-SPI accessibility could not be switched on, apps that check it (such as"
        " Firefox) may expose no accessibility tree: Error"
        " org.freedesktop.DBus.Error.AccessDenied: stub refuses"
    ], output


@pytest.mark.parametrize("enable", [False, True])
def test_live_connect_reports_an_unreadable_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enable: bool
) -> None:
    def unavailable(*_args: object) -> bool:
        raise dbus.exceptions.DBusException("no accessibility bus")

    monkeypatch.setattr(core, "accessibility_enabled", unavailable)
    monkeypatch.setattr(LiveSession, "enable_accessibility", unavailable)
    session = LiveSession("unix:path=/nonexistent", "wayland-x", tmp_path)

    line = AutomationEngine._live_accessibility(session, "unix:path=/nonexistent", enable=enable)

    assert line == "Accessibility: org.a11y.Status unavailable (no accessibility bus)"


def test_stop_survives_a_switch_it_cannot_restore(tmp_path: Path) -> None:
    """A bus that went away leaves nothing to restore; stop still completes."""
    screenshots = tmp_path / "screenshots"
    screenshots.mkdir()
    session = LiveSession("unix:path=/nonexistent/bus", "wayland-x", screenshots)
    session._restore_accessibility = True

    session.stop()

    assert not session.is_running
    assert not session._restore_accessibility
    assert not screenshots.exists()
