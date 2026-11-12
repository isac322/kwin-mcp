"""Session and app argument validation, and atomic session_start rollback, without KWin.

``session_start`` validates the screen size before anything starts, and
``app_launch`` validates ``command`` before it looks up the session. When
``session_start`` fails after validation, the partly started session is stopped
and the original exception propagates, so no session is left behind.
``Session.launch_app`` applies the caller's ``env`` after the DISPLAY handling
and before the session bus is forced (SPEC-GAP-14).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

import pytest

from kwin_mcp import cli, core, session
from kwin_mcp.core import NO_SESSION_TEXT, AutomationEngine
from kwin_mcp.models import KeptPaths
from kwin_mcp.session import Session, SessionInfo

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

    from kwin_mcp.session import SessionConfig


class _ForbiddenSession:
    """Stands in for ``core.Session`` when validation must refuse before any start."""

    def __init__(self) -> None:
        raise AssertionError("Session was constructed although validation should refuse")


class _FakeSession:
    """A ``core.Session`` stand-in whose ``start()`` raises ``start_error``."""

    start_error: ClassVar[Exception] = RuntimeError("KWin failed to start")
    instances: ClassVar[list[_FakeSession]] = []

    def __init__(self) -> None:
        self.stop_calls = 0
        _FakeSession.instances.append(self)

    @property
    def is_running(self) -> bool:
        return False

    def start(self, config: SessionConfig, *, progress_total: int | None = None) -> SessionInfo:
        raise self.start_error

    def stop(self) -> KeptPaths:
        self.stop_calls += 1
        return KeptPaths()


def _assert_no_session(engine: AutomationEngine) -> None:
    stopped = engine.session_stop()
    assert stopped.stopped is False, stopped
    assert stopped.render() == "No session running.", stopped.render()
    assert engine.session_info().session is None


@pytest.mark.parametrize(
    ("width", "height", "field"),
    [
        (0, 1080, "screen_width"),
        (-5, 100, "screen_width"),
        (1920, 0, "screen_height"),
        (True, 1080, "screen_width"),
    ],
)
def test_session_start_rejects_a_non_positive_screen_size_before_starting(
    monkeypatch: pytest.MonkeyPatch, width: int, height: int, field: str
) -> None:
    monkeypatch.setattr(core, "Session", _ForbiddenSession)
    engine = AutomationEngine()
    value = width if field == "screen_width" else height

    with pytest.raises(ValueError, match=f"^{field} must be a positive integer, got {value!r}$"):
        engine.session_start(screen_width=width, screen_height=height)

    _assert_no_session(engine)


@pytest.mark.qa("QA-APP-002")
@pytest.mark.parametrize(
    ("command", "message"),
    [
        pytest.param("", "command must contain a command to launch", id="empty-command"),
        pytest.param(
            "   ",
            "command must contain a command to launch",
            id="   -app_command must contain a command",
        ),
        pytest.param(
            "\t\n",
            "command must contain a command to launch",
            id="\t\n-app_command must contain a command",
        ),
        pytest.param("'x", "Invalid command: No closing quotation", id="'x-No closing quotation"),
        pytest.param("kcalc\0x", "Invalid command: it contains a NUL byte", id="nul-byte"),
        pytest.param(
            "kcalc 'unterminated",
            "Invalid command: No closing quotation",
            id="unterminated-No closing quotation",
        ),
    ],
)
def test_app_launch_command_validation(
    monkeypatch: pytest.MonkeyPatch, command: str, message: str
) -> None:
    """An empty or unbalanced command is refused with the 0.11 text before the session lookup.

    The command check runs first, so the caller learns that the command itself is
    unusable even when no session is running; nothing is spawned.
    """
    engine = AutomationEngine()
    looked_up: list[str] = []
    original_require_session = engine._require_session

    def _recording_require_session() -> object:
        looked_up.append("session")
        return original_require_session()

    monkeypatch.setattr(engine, "_require_session", _recording_require_session)

    with pytest.raises(ValueError) as excinfo:
        engine.app_launch(command)

    assert str(excinfo.value) == message
    assert str(excinfo.value) != NO_SESSION_TEXT
    assert looked_up == []


@pytest.mark.qa("QA-APP-003")
@pytest.mark.parametrize(
    ("env", "message"),
    [
        pytest.param(
            {"A=B": "1"},
            "Invalid env name 'A=B': it must be non-empty, without '=' or a NUL byte",
            id="equals-in-name",
        ),
        pytest.param(
            {"": "1"},
            "Invalid env name '': it must be non-empty, without '=' or a NUL byte",
            id="empty-name",
        ),
        pytest.param(
            {"A\0B": "1"},
            "Invalid env name 'A\\x00B': it must be non-empty, without '=' or a NUL byte",
            id="nul-in-name",
        ),
        pytest.param(
            {"A": "x\0y"}, "Invalid env value for A: it contains a NUL byte", id="nul-in-value"
        ),
    ],
)
def test_app_launch_env_validation(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], message: str
) -> None:
    """An env entry execve cannot pass is refused before the session lookup.

    No log file is created and nothing is spawned.
    """
    engine = AutomationEngine()
    looked_up: list[str] = []
    monkeypatch.setattr(engine, "_require_session", lambda: looked_up.append("session"))

    with pytest.raises(ValueError) as excinfo:
        engine.app_launch("kcalc", env=env)

    assert str(excinfo.value) == message
    assert looked_up == []


def test_session_start_failure_in_start_leaves_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_FakeSession, "instances", [])
    monkeypatch.setattr(core, "Session", _FakeSession)
    engine = AutomationEngine()

    with pytest.raises(RuntimeError, match=r"^KWin failed to start$"):
        engine.session_start(enable_clipboard=True)

    [started] = _FakeSession.instances
    assert started.stop_calls == 1
    assert engine._session is None
    assert engine._clipboard_enabled is False
    _assert_no_session(engine)
    assert started.stop_calls == 1


class _SpawnedApp:
    pid = 4242


@pytest.mark.qa("QA-APP-003")
def test_app_env_order_pinned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """env lands after the DISPLAY strip and before the session bus is forced (SPEC-GAP-14).

    So env may set DISPLAY and override WAYLAND_DISPLAY, but never redirect the
    session bus; the host DISPLAY never leaks into the app.
    """
    spawned: list[dict[str, str]] = []

    def _spawn(command: list[str], *, env: dict[str, str], **kwargs: object) -> _SpawnedApp:
        spawned.append(env)
        return _SpawnedApp()

    monkeypatch.setattr(Session, "is_running", property(lambda _self: True))
    monkeypatch.setattr(session.process_registry, "spawn", _spawn)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("FOO", raising=False)
    virtual = Session()
    virtual._process = cast("subprocess.Popen[bytes]", object())
    virtual._socket_name = "wayland-session"
    virtual._info = SessionInfo(
        dbus_address="unix:path=/session-bus",
        wayland_socket="wayland-session",
        kwin_pid=0,
        screenshot_dir=tmp_path,
    )

    virtual.launch_app(
        ["env"],
        extra_env={
            "DISPLAY": ":9",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/evil",
            "WAYLAND_DISPLAY": "wayland-evil",
            "FOO": "bar",
        },
    )
    virtual.launch_app(["env"])

    with_env, without_env = spawned
    assert with_env["DISPLAY"] == ":9"
    assert with_env["FOO"] == "bar"
    assert with_env["WAYLAND_DISPLAY"] == "wayland-evil"
    assert with_env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/session-bus"
    assert "DISPLAY" not in without_env
    assert "FOO" not in without_env
    assert without_env["WAYLAND_DISPLAY"] == "wayland-session"
    assert without_env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/session-bus"


def test_session_start_stops_a_dead_session_before_replacing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_FakeSession, "instances", [])
    monkeypatch.setattr(core, "Session", _FakeSession)
    engine = AutomationEngine()
    dead = _FakeSession()  # its KWin exited: is_running is False, stop() never ran
    engine._session = cast("Session", dead)

    with pytest.raises(RuntimeError, match=r"^KWin failed to start$"):
        engine.session_start()

    assert dead.stop_calls == 1


def test_session_connect_stops_a_dead_session_before_replacing_it() -> None:
    engine = AutomationEngine()
    dead = _FakeSession()
    engine._session = cast("Session", dead)

    # An address nothing listens on: the connect fails after the gate, off the host bus.
    with pytest.raises(RuntimeError, match=r"^Cannot reach KWin on D-Bus"):
        engine.session_connect(
            dbus_address="unix:path=/nonexistent/kwin-mcp-unit-test-bus",
            wayland_display="wayland-kwin-mcp-unit-none",
        )

    assert dead.stop_calls == 1


def test_cli_exit_stops_a_dead_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "process_registry", session.OwnedProcessRegistry())
    engine = AutomationEngine()
    dead = _FakeSession()  # its KWin exited: the sockets and dirs are still to remove
    engine._session = cast("Session", dead)

    cli.KwinMcpShell(engine)._cleanup()

    assert dead.stop_calls == 1


def test_failed_stop_does_not_block_later_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    class _StuckSession(_FakeSession):
        def stop(self) -> KeptPaths:
            super().stop()
            raise PermissionError("cannot remove the isolated home")

    monkeypatch.setattr(_FakeSession, "instances", [])
    monkeypatch.setattr(core, "Session", _FakeSession)
    engine = AutomationEngine()
    stuck = _StuckSession()
    engine._session = cast("Session", stuck)

    with pytest.raises(RuntimeError, match=r"^Stopping the previous session, which had exited"):
        engine.session_start()
    assert engine._session is None
    # The next start gets past the gate to its own start.
    with pytest.raises(RuntimeError, match=r"^KWin failed to start$"):
        engine.session_start()
    assert stuck.stop_calls == 1
