"""Virtual-output count validation, launch wiring and workspace reporting without KWin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from kwin_mcp import core
from kwin_mcp.core import AutomationEngine
from kwin_mcp.models import ScreenSize
from kwin_mcp.session import Session, SessionConfig, SessionInfo

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("count", [0, -1, True, False, 1.5, "2", None])
def test_output_count_rejected_before_session_construction(
    monkeypatch: pytest.MonkeyPatch, count: Any
) -> None:
    def forbidden() -> None:
        pytest.fail("invalid output_count constructed a session")

    monkeypatch.setattr(core, "Session", forbidden)
    with pytest.raises(ValueError, match="^output_count must be a positive integer"):
        AutomationEngine().session_start(output_count=count)


@pytest.mark.parametrize("count", [1, 2, 1000, 10**30])
def test_wrapper_passes_output_count_without_an_application_limit(count: int) -> None:
    script = Session()._build_wrapper_script(
        SessionConfig(screen_width=937, screen_height=613, output_count=count)
    )
    assert f"--output-count {count}" in script
    assert "--width 937 --height 613" in script


@pytest.fixture
def started_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[AutomationEngine, list[SessionConfig]]:
    configs: list[SessionConfig] = []

    class StartedSession(Session):
        @property
        def is_running(self) -> bool:
            return self._info is not None

        def start(
            self, config: SessionConfig | None = None, *, progress_total: int | None = None
        ) -> SessionInfo:
            assert config is not None
            configs.append(config)
            self._info = SessionInfo(
                dbus_address="unix:path=/fake-bus",
                wayland_socket="wayland-fake",
                kwin_pid=1234,
                screenshot_dir=tmp_path,
            )
            return self._info

    monkeypatch.setattr(core, "Session", StartedSession)
    monkeypatch.setattr(core, "InputBackend", lambda _address: None)
    monkeypatch.setattr(core, "accessibility_enabled", lambda _address: True)
    return AutomationEngine(), configs


@pytest.mark.parametrize("kwargs", [{}, {"output_count": 1}])
def test_single_output_keeps_requested_size_without_querying_kwin(
    monkeypatch: pytest.MonkeyPatch,
    started_engine: tuple[AutomationEngine, list[SessionConfig]],
    kwargs: dict[str, int],
) -> None:
    engine, configs = started_engine

    def forbidden(_request: dict[str, object]) -> dict[str, Any]:
        pytest.fail("single-output startup queried workspace size")

    monkeypatch.setattr(engine, "_run_kwin_query", forbidden)
    info = engine.session_start(screen_width=937, screen_height=613, **kwargs)
    assert info.screen == ScreenSize(width=937, height=613)
    assert configs[0].output_count == 1


@pytest.mark.parametrize("count", [2, 1000, 10**30])
def test_multiple_outputs_report_kwin_union_instead_of_assuming_a_layout(
    monkeypatch: pytest.MonkeyPatch,
    started_engine: tuple[AutomationEngine, list[SessionConfig]],
    count: int,
) -> None:
    engine, configs = started_engine
    requests: list[dict[str, object]] = []

    def query(request: dict[str, object]) -> dict[str, Any]:
        requests.append(request)
        return {"ok": True, "result": {"virtual": [-300, -200, 1700, 1400]}}

    monkeypatch.setattr(engine, "_run_kwin_query", query)
    info = engine.session_start(screen_width=937, screen_height=613, output_count=count)
    assert configs[0].output_count == count
    assert requests == [{"op": "outputs"}]
    assert info.screen == ScreenSize(width=1700, height=1400)
    assert engine.session_info().session == info


@pytest.mark.parametrize(
    ("response", "warnings"),
    [
        (
            {"ok": False, "error": "query unavailable"},
            ["workspace size unavailable (query unavailable)"],
        ),
        ({"ok": True, "result": {"virtual": None}}, []),
    ],
)
def test_multi_output_workspace_unavailable_does_not_report_per_output_size(
    monkeypatch: pytest.MonkeyPatch,
    started_engine: tuple[AutomationEngine, list[SessionConfig]],
    response: dict[str, Any],
    warnings: list[str],
) -> None:
    engine, _configs = started_engine
    monkeypatch.setattr(engine, "_run_kwin_query", lambda _request: response)
    info = engine.session_start(output_count=2)
    assert info.screen is None
    assert info.warnings == warnings
    assert engine.session_info().session == info
