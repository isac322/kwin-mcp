"""Unit coverage for window_set_geometry input validation and reporting.

These tests stub the KWin query worker, so they run without a KWin session.
They exercise values the CLI can forward from raw JSON, bypassing MCP schema
validation: bools, floats, strings and magnitudes beyond the signed 32-bit
range KWin's geometry fields use. Invalid arguments raise ValueError before
anything reaches the compositor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import kwin_mcp.core as core

if TYPE_CHECKING:
    from collections.abc import Mapping

    from kwin_mcp.core import AutomationEngine

_SET_RESULT: dict[str, object] = {
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
    set_result: Mapping[str, object] | None = None,
) -> tuple[AutomationEngine, list[dict[str, object]]]:
    """Engine whose KWin worker is recorded and answers a fixed window."""
    engine = core.AutomationEngine()
    monkeypatch.setattr(engine, "_get_session", lambda: object())

    requests: list[dict[str, object]] = []

    def run_query(request: dict[str, object]) -> dict[str, object]:
        requests.append(request)
        if request.get("op") == "set_geometry":
            return {"ok": True, "result": dict(set_result or _SET_RESULT)}
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

    with pytest.raises(ValueError, match="must be an integer"):
        _set_geometry(engine, **{field: value})

    assert requests == [], requests


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("x", 10**400),
        ("x", -(10**400)),
        ("x", -(2**31) - 1),
        ("y", 10**400),
        ("y", -(10**400)),
        ("y", -(2**31) - 1),
        ("width", 2**31),
        ("height", 2**31),
    ],
)
def test_out_of_range_field_is_rejected_without_dispatch(
    monkeypatch: pytest.MonkeyPatch, field: str, value: int
) -> None:
    engine, requests = _engine_with_stubbed_kwin(monkeypatch)

    with pytest.raises(ValueError, match="32-bit"):
        _set_geometry(engine, **{field: value})

    assert requests == [], requests


@pytest.mark.parametrize(
    ("x", "y"), [(-(2**31), -(2**31)), (2**31 - 1, 2**31 - 1)], ids=["int32-min", "int32-max"]
)
def test_int32_bounds_are_dispatched(monkeypatch: pytest.MonkeyPatch, x: int, y: int) -> None:
    engine, requests = _engine_with_stubbed_kwin(monkeypatch)

    _set_geometry(engine, x=x, y=y)

    assert requests[0]["op"] == "set_geometry", requests
    assert requests[0]["x"] == x and requests[0]["y"] == y, requests


@pytest.mark.parametrize("field", ["width", "height"])
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_size_is_rejected_without_dispatch(
    monkeypatch: pytest.MonkeyPatch, field: str, value: int
) -> None:
    engine, requests = _engine_with_stubbed_kwin(monkeypatch)

    with pytest.raises(ValueError, match="must be greater than zero"):
        _set_geometry(engine, **{field: value})

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


def test_vanished_window_readback_says_it_no_longer_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, requests = _engine_with_stubbed_kwin(monkeypatch, observed_result=[])

    output = _set_geometry(engine, width=500)

    assert [request.get("op") for request in requests] == ["set_geometry", None], requests
    assert "Geometry update requested:" in output, output[:500]
    assert (
        "observed: window no longer exists (it closed or its client exited)"
        in output.splitlines()
    ), output[:500]


@pytest.mark.parametrize(("field", "limit"), [("width", 1280), ("height", 800)])
def test_size_beyond_the_workspace_is_rejected_without_observation(
    monkeypatch: pytest.MonkeyPatch, field: str, limit: int
) -> None:
    engine, requests = _engine_with_stubbed_kwin(
        monkeypatch, set_result={**_SET_RESULT, "rejected": field, "limit": limit}
    )

    with pytest.raises(ValueError, match=rf"{field} must be at most {limit} .*got {limit + 1}\."):
        _set_geometry(engine, **{field: limit + 1})

    # The script refused without assigning, so no readback query follows.
    assert len(requests) == 1, requests
    assert requests[0]["op"] == "set_geometry", requests


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"window_id": ""}, "needs a window_id"),
        ({"window_id": "id"}, "needs at least one of x, y, width or height"),
    ],
    ids=["empty-id", "no-field"],
)
def test_missing_arguments_are_rejected_before_the_session(
    fields: dict[str, str], message: str
) -> None:
    # No session is stubbed: the argument check must not depend on session state.
    with pytest.raises(ValueError, match=message):
        core.AutomationEngine().window_set_geometry(**fields)
