"""End-to-end coverage for ``session_connect``: attaching to a live KWin compositor.

Every live compositor is a test-owned ``session_harness.live_kwin`` instance. The
window oracle is ``kwin_oracle`` on that compositor's bus, never the tool under test.
"""

from __future__ import annotations

import contextlib
import os
import socket
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from session_harness import live_kwin

from kwin_mcp.core import SESSION_RUNNING_TEXT, AutomationEngine
from kwin_mcp.models import Retention, SessionInfo

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from kwin_mcp.models import ElementList

KCALC_CLASS = "org.kde.kcalc"
MISSING_DBUS_TEXT = (
    "No D-Bus address available. Provide dbus_address parameter "
    "or ensure $DBUS_SESSION_BUS_ADDRESS is set."
)
MISSING_WAYLAND_TEXT = (
    "No Wayland display available. Provide wayland_display parameter "
    "or ensure $WAYLAND_DISPLAY is set."
)
RETENTION_HOME_TEXT = (
    "retention.home is not available on session_connect: live sessions use "
    "the host HOME.\nDrop retention.home."
)


def _runtime_dir() -> Path:
    return Path(os.environ["XDG_RUNTIME_DIR"])


def _assert_no_session(engine: AutomationEngine) -> None:
    """A failed connect leaves nothing behind: no session and an idempotent stop."""
    info = engine.session_info()
    assert info.session is None, info.render()
    assert info.apps == [], info.render()
    stopped = engine.session_stop()
    assert stopped.stopped is False, stopped.render()
    assert stopped.render() == "No session running."


@contextlib.contextmanager
def _plain_dbus_daemon() -> Generator[str]:
    """A private session bus where nothing owns ``org.kde.KWin``; yields its address."""
    process = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert process.stdout is not None
        address = process.stdout.readline().decode().strip()
        assert address.startswith("unix:"), address
        yield address
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()


def test_connects_to_the_second_compositor_without_owning_it(
    engine: AutomationEngine, wait_for_app: Callable[[str], ElementList]
) -> None:
    first_engine = AutomationEngine()
    first_info = first_engine.session_start(screen_width=1280, screen_height=800)
    first_display = first_info.wayland_socket
    first_socket = _runtime_dir() / first_display

    try:
        with live_kwin() as live:
            assert live.wayland_display != first_display
            assert first_socket.is_socket()
            assert live.process.poll() is None
            assert live.socket_path.is_socket()

            info = engine.session_connect(
                dbus_address=live.dbus_address,
                wayland_display=live.wayland_display,
            )
            assert info.kind == "live", info.render()
            assert info.dbus_address == live.dbus_address
            assert info.wayland_socket == live.wayland_display
            assert info.render().splitlines()[0] == (
                "Connected to live KWin session. "
                f"D-Bus: {live.dbus_address}, Wayland: {live.wayland_display}"
            )

            launched = engine.app_launch("kcalc")
            assert launched.command == "kcalc", launched
            assert launched.render().startswith(f"App launched: kcalc (PID={launched.pid})")
            wait_for_app("kcalc")

            stopped = engine.session_stop()
            assert stopped.stopped is True
            assert stopped.kind == "live"
            assert stopped.render() == "Disconnected from live session."
            assert live.process.poll() is None
            assert live.socket_path.is_socket()
            assert first_socket.is_socket()

        assert live.process.poll() is not None
        assert not live.socket_path.exists()
        assert first_socket.is_socket()
    finally:
        first_engine.session_stop()

    assert not first_socket.exists()


def test_session_connect_rejects_an_explicit_unreachable_dbus_address(
    engine: AutomationEngine, tmp_path: Path
) -> None:
    dbus_address = f"unix:path={tmp_path / 'missing-session-bus'}"

    with pytest.raises(RuntimeError) as exc_info:
        engine.session_connect(
            dbus_address=dbus_address,
            wayland_display="wayland-missing",
        )

    message = str(exc_info.value)
    assert message.startswith(f"Cannot reach KWin on D-Bus ({dbus_address}):"), message
    _assert_no_session(engine)


def test_session_connect_rejects_an_explicit_missing_wayland_socket(
    engine: AutomationEngine,
) -> None:
    with live_kwin() as live:
        missing_display = f"wayland-missing-{uuid4().hex}"
        with pytest.raises(RuntimeError) as exc_info:
            engine.session_connect(
                dbus_address=live.dbus_address,
                wayland_display=missing_display,
            )

        message = str(exc_info.value)
        assert message.startswith(f"Cannot reach Wayland display ({missing_display}):"), message
        _assert_no_session(engine)
        assert live.process.poll() is None
        assert live.socket_path.is_socket()


def test_session_connect_auto_discovery_reports_each_missing_environment_variable(
    engine: AutomationEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        engine.session_connect()
    assert str(exc_info.value) == MISSING_DBUS_TEXT

    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/not-consulted")
    with pytest.raises(RuntimeError) as exc_info:
        engine.session_connect()
    assert str(exc_info.value) == MISSING_WAYLAND_TEXT
    _assert_no_session(engine)


def test_session_connect_rejects_replacing_an_active_virtual_session(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    info = start_session()
    socket_path = _runtime_dir() / info.wayland_socket

    with live_kwin() as live:
        with pytest.raises(RuntimeError) as exc_info:
            engine.session_connect(
                dbus_address=live.dbus_address,
                wayland_display=live.wayland_display,
            )
        assert str(exc_info.value) == SESSION_RUNNING_TEXT
        assert socket_path.is_socket()
        assert live.process.poll() is None
        assert live.socket_path.is_socket()

    # The virtual session is untouched by the rejected connect.
    current = engine.session_info()
    assert current.session is not None, current.render()
    assert current.session.kind == "virtual"
    assert current.session.wayland_socket == info.wayland_socket


@pytest.mark.qa("QA-SES-009")
def test_connect_reports_live_kind_clipboard_true_and_eis(engine: AutomationEngine) -> None:
    with live_kwin() as live:
        info = engine.session_connect(
            dbus_address=live.dbus_address, wayland_display=live.wayland_display
        )
        assert isinstance(info, SessionInfo)
        assert info.kind == "live", info.render()
        assert info.clipboard is True, info.render()
        assert info.input_backend == "eis", info.render()
        assert info.home_dir is None
        assert info.retention == Retention()
        assert info.dbus_address == live.dbus_address
        assert info.wayland_socket == live.wayland_display

        text = info.render()
        lines = text.splitlines()
        assert lines[0] == (
            "Connected to live KWin session. "
            f"D-Bus: {live.dbus_address}, Wayland: {live.wayland_display}"
        )
        assert "Clipboard: enabled" in lines
        assert "Input backend: KWin EIS" in lines
        # No ydotool banner (#12): 1.0 has no ydotool fallback at all.
        assert "ydotool" not in text.lower()
        assert "ydotool" not in info.model_dump_json().lower()

        # A window of our own, so the comparison is not over an empty list.
        launched = engine.app_launch("kcalc")
        live.wait_for_windows(
            lambda ws: any(w.pid == launched.pid and w.resource_class == KCALC_CLASS for w in ws)
        )
        expected = {w.id for w in live.oracle().windows()}
        listed = engine.window_list()
        assert {w.id for w in listed.windows} == expected, listed.render()


@pytest.mark.qa("QA-SES-010")
def test_dbus_address_and_wayland_display_defaults(
    engine: AutomationEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    with live_kwin() as live:
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", live.dbus_address)
        monkeypatch.setenv("WAYLAND_DISPLAY", live.wayland_display)

        info = engine.session_connect()

        assert info.kind == "live", info.render()
        assert info.dbus_address == live.dbus_address
        # The socket is reported as given in $WAYLAND_DISPLAY; it resolves under
        # $XDG_RUNTIME_DIR to the compositor's listening socket.
        assert info.wayland_socket == live.wayland_display
        resolved = _runtime_dir() / info.wayland_socket
        assert resolved == live.socket_path
        assert stat.S_ISSOCK(resolved.stat().st_mode)
        assert info.render().splitlines()[0] == (
            "Connected to live KWin session. "
            f"D-Bus: {live.dbus_address}, Wayland: {live.wayland_display}"
        )


@pytest.mark.qa("QA-SES-011")
def test_connect_errors_leave_no_session(
    engine: AutomationEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Missing bus address.
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    with pytest.raises(RuntimeError) as exc_info:
        engine.session_connect(wayland_display="wayland-unused")
    assert str(exc_info.value) == MISSING_DBUS_TEXT
    _assert_no_session(engine)

    # A bus where nothing owns org.kde.KWin.
    with _plain_dbus_daemon() as plain_bus:
        with pytest.raises(RuntimeError) as exc_info:
            engine.session_connect(dbus_address=plain_bus, wayland_display="wayland-unused")
        message = str(exc_info.value)
        assert message.startswith(f"Cannot reach KWin on D-Bus ({plain_bus}): "), message
        _assert_no_session(engine)

    with live_kwin() as live:
        # A regular file is not a Wayland socket.
        not_a_socket = tmp_path / "not-a-socket"
        not_a_socket.write_text("")
        with pytest.raises(RuntimeError) as exc_info:
            engine.session_connect(
                dbus_address=live.dbus_address, wayland_display=str(not_a_socket)
            )
        assert str(exc_info.value) == (
            f"Cannot reach Wayland display ({not_a_socket}): {not_a_socket} is not a Unix socket"
        )
        _assert_no_session(engine)

        # A bound socket without a listener refuses the 0.5 s connect probe.
        dead_socket = tmp_path / "dead.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as unbound:
            unbound.bind(str(dead_socket))
            with pytest.raises(RuntimeError) as exc_info:
                engine.session_connect(
                    dbus_address=live.dbus_address, wayland_display=str(dead_socket)
                )
        message = str(exc_info.value)
        assert message.startswith(f"Cannot reach Wayland display ({dead_socket}): "), message
        assert "Connection refused" in message, message
        _assert_no_session(engine)

        # The live compositor and its bus were never touched.
        assert live.process.poll() is None
        assert live.socket_path.is_socket()


def test_connect_rejects_retention_home_on_live(engine: AutomationEngine) -> None:
    with live_kwin() as live:
        with pytest.raises(ValueError, match=r"retention\.home") as exc_info:
            engine.session_connect(
                dbus_address=live.dbus_address,
                wayland_display=live.wayland_display,
                retention=Retention(home=True),
            )
        assert str(exc_info.value) == RETENTION_HOME_TEXT
        _assert_no_session(engine)
        assert live.process.poll() is None
