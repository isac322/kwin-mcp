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

from kwin_mcp import core, session
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

    monkeypatch.setattr(session.process_registry, "poll", lambda _proc: None)
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
