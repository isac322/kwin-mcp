"""Unit coverage for window_set_geometry input validation and reporting.

These tests stub the KWin query worker, so they run without a KWin session.
They exercise values the CLI can forward from raw JSON, bypassing MCP schema
validation: bools, floats, strings and magnitudes beyond the signed 32-bit
range KWin's geometry fields use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import kwin_mcp.core as core

if TYPE_CHECKING:
    from kwin_mcp.core import AutomationEngine

_SET_RESULT = {
    "found": True,
    "app": "app",
    "caption": "fixed",
    "moveable": False,
    "resizeable": False,
    "before": [10, 20, 300, 200],
}


def _engine_with_stubbed_kwin(
    monkeypatch: pytest.MonkeyPatch,
    observed_result: object = None,
    observed_error: str | None = None,
) -> tuple[AutomationEngine, list[dict[str, object]]]:
    """Engine whose KWin worker is recorded and answers a fixed window."""
    engine = core.AutomationEngine()
    monkeypatch.setattr(engine, "_get_session", lambda: object())

    requests: list[dict[str, object]] = []

    def run_query(request: dict[str, object]) -> dict[str, object]:
        requests.append(request)
        if request.get("op") == "set_geometry":
            return {"ok": True, "result": dict(_SET_RESULT)}
        if observed_error is not None:
            return {"ok": False, "error": observed_error}
        return {"ok": True, "result": observed_result}

    monkeypatch.setattr(engine, "_run_kwin_query", run_query)
    return engine, requests


def _set_geometry(engine: AutomationEngine, **fields: Any) -> str:
    """Call the engine the way the CLI can: with unvalidated JSON values."""
    return engine.window_set_geometry(window_id="id", **fields)


@pytest.mark.parametrize("field", ["x", "y", "width", "height"])
@pytest.mark.parametrize("value", [True, 1.5, "abc", float("nan")])
def test_non_integer_field_is_rejected_without_dispatch(
    monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    engine, requests = _engine_with_stubbed_kwin(monkeypatch)

    output = _set_geometry(engine, **{field: value})

    assert "must be an integer" in output, output[:500]
    assert requests == [], requests


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("x", 10**400),
        ("x", -(10**400)),
        ("y", 10**400),
        ("y", -(10**400)),
        ("width", 2**31),
        ("height", 2**31),
    ],
)
def test_out_of_range_field_is_rejected_without_dispatch(
    monkeypatch: pytest.MonkeyPatch, field: str, value: int
) -> None:
    engine, requests = _engine_with_stubbed_kwin(monkeypatch)

    output = _set_geometry(engine, **{field: value})

    assert "32-bit" in output, output[:500]
    assert requests == [], requests


@pytest.mark.parametrize("field", ["width", "height"])
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_size_is_rejected_without_dispatch(
    monkeypatch: pytest.MonkeyPatch, field: str, value: int
) -> None:
    engine, requests = _engine_with_stubbed_kwin(monkeypatch)

    output = _set_geometry(engine, **{field: value})

    assert "must be greater than zero" in output, output[:500]
    assert requests == [], requests


def test_partial_update_is_dispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, requests = _engine_with_stubbed_kwin(monkeypatch)

    _set_geometry(engine, width=500)

    assert len(requests) == 2, requests
    set_request = requests[0]
    assert set_request["op"] == "set_geometry", set_request
    assert set_request["width"] == 500, set_request
    assert set_request["x"] is None and set_request["y"] is None, set_request


def test_negative_coordinate_is_dispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, requests = _engine_with_stubbed_kwin(monkeypatch)

    _set_geometry(engine, x=-100, y=-200)

    assert requests[0]["x"] == -100 and requests[0]["y"] == -200, requests


def test_unchanged_readback_names_pending_field(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = {"frame": {"x": 10, "y": 20, "width": 300, "height": 200}}
    engine, requests = _engine_with_stubbed_kwin(monkeypatch, observed_result=[observed])

    output = _set_geometry(engine, width=500)

    assert requests[0]["op"] == "set_geometry", requests
    assert "Geometry update requested:" in output, output[:500]
    assert "Geometry set:" not in output, output[:500]
    assert "not yet applied: width" in output, output[:500]


def test_failed_readback_reports_the_error(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, requests = _engine_with_stubbed_kwin(
        monkeypatch, observed_error="KWin query timed out after 30s"
    )

    output = _set_geometry(engine, width=500)

    assert requests[0]["op"] == "set_geometry", requests
    assert "Geometry update requested:" in output, output[:500]
    assert "observation failed" in output, output[:500]
    assert "timed out" in output, output[:500]
