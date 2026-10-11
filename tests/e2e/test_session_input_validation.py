"""Session argument validation and atomic session_start rollback, without a KWin session.

``session_start`` validates the screen size and ``app_command`` before anything
starts, and ``launch_app`` validates ``command`` before it looks up the session.
When ``session_start`` fails after validation, the partly started session is
stopped and the original exception propagates, so no session is left behind.
"""

from __future__ import annotations

import errno
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest

from kwin_mcp import core
from kwin_mcp.core import AutomationEngine
from kwin_mcp.session import SessionInfo

if TYPE_CHECKING:
    from kwin_mcp.session import SessionConfig

MISSING_APP = "/nonexistent-app"


class _ForbiddenSession:
    """Stands in for ``core.Session`` when validation must refuse before any start."""

    def __init__(self) -> None:
        raise AssertionError("Session was constructed although validation should refuse")


class _FakeSession:
    """A ``core.Session`` stand-in recording ``stop()`` calls.

    ``start_error`` makes ``start()`` raise; otherwise ``start()`` succeeds and
    ``launch_app()`` raises the missing-binary error ``Popen`` would raise.
    """

    start_error: ClassVar[Exception | None] = None
    instances: ClassVar[list[_FakeSession]] = []

    def __init__(self) -> None:
        self.running = False
        self.stop_calls = 0
        self.launched: list[list[str]] = []
        _FakeSession.instances.append(self)

    @property
    def is_running(self) -> bool:
        return self.running

    def start(self, config: SessionConfig, *, progress_total: int | None = None) -> SessionInfo:
        if self.start_error is not None:
            raise self.start_error
        self.running = True
        return SessionInfo(
            dbus_address="unix:path=/nonexistent-bus",
            wayland_socket="wayland-fake",
            kwin_pid=0,
            screenshot_dir=Path("/nonexistent-screenshots"),
        )

    def launch_app(self, command: list[str], extra_env: dict[str, str] | None = None) -> None:
        self.launched.append(command)
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", command[0])

    def stop(self) -> None:
        self.stop_calls += 1
        self.running = False


@pytest.fixture
def fake_session(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSession]:
    monkeypatch.setattr(_FakeSession, "start_error", None)
    monkeypatch.setattr(_FakeSession, "instances", [])
    monkeypatch.setattr(core, "Session", _FakeSession)
    return _FakeSession


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

    with pytest.raises(ValueError, match=f"{field} must be a positive integer"):
        engine.session_start(screen_width=width, screen_height=height)

    assert engine.session_stop() == "No session running."


@pytest.mark.parametrize(
    ("app_command", "fragment"),
    [
        ("   ", "app_command must contain a command"),
        ("\t\n", "app_command must contain a command"),
        ("'x", "No closing quotation"),
    ],
)
def test_session_start_rejects_an_unusable_app_command_before_starting(
    monkeypatch: pytest.MonkeyPatch, app_command: str, fragment: str
) -> None:
    monkeypatch.setattr(core, "Session", _ForbiddenSession)
    engine = AutomationEngine()

    with pytest.raises(ValueError, match=fragment):
        engine.session_start(app_command=app_command)

    assert engine.session_stop() == "No session running."


def test_launch_app_rejects_an_empty_or_malformed_command_before_the_session_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = AutomationEngine()
    looked_up: list[str] = []
    launched: list[list[str]] = []

    class _RecordingSession:
        def launch_app(self, command: list[str], extra_env: dict[str, str] | None = None) -> None:
            launched.append(command)
            raise AssertionError("launch_app reached the session")

    def _get_session() -> _RecordingSession:
        looked_up.append("session")
        return _RecordingSession()

    monkeypatch.setattr(engine, "_get_session", _get_session)

    for command in ("", "   "):
        with pytest.raises(ValueError, match="command must contain a command to launch"):
            engine.launch_app(command)
    with pytest.raises(ValueError, match="No closing quotation"):
        engine.launch_app("'x")

    assert looked_up == []
    assert launched == []


def test_session_start_failure_in_start_leaves_no_session(
    fake_session: type[_FakeSession],
) -> None:
    fake_session.start_error = RuntimeError("KWin failed to start")
    engine = AutomationEngine()

    with pytest.raises(RuntimeError, match="KWin failed to start"):
        engine.session_start(enable_clipboard=True)

    assert engine._session is None
    assert engine._clipboard_enabled is False
    assert engine.session_stop() == "No session running."


def test_session_start_failure_in_launch_app_stops_the_started_session(
    fake_session: type[_FakeSession],
) -> None:
    engine = AutomationEngine()

    with pytest.raises(FileNotFoundError) as exc_info:
        engine.session_start(app_command=MISSING_APP, enable_clipboard=True)

    assert exc_info.value.filename == MISSING_APP
    [started] = fake_session.instances
    assert started.launched == [[MISSING_APP]]
    assert started.stop_calls == 1
    assert engine._session is None
    assert engine._clipboard_enabled is False
    assert engine.session_stop() == "No session running."
    assert started.stop_calls == 1
