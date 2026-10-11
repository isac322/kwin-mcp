"""Unit coverage for virtual_desktops and window_set_desktop request handling.

These tests stub the KWin query worker or the KWin script runner, so they run
without a KWin session. They pin which requests reach KWin, that nothing is
assigned unless both ids are found, how the separate readback decides
``applied`` while the current desktop is reported alongside rather than folded
into it, and that malformed script output is refused before the engine reads it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

import kwin_mcp.core as core
from kwin_mcp import geometry

if TYPE_CHECKING:
    from kwin_mcp.core import AutomationEngine

WINDOW = "{3ab98f95-2231-494c-a290-add1284b7450}"
DESK_1 = "10b4aef8-6d6b-4951-aa32-8937b8a8f5b0"
DESK_2 = "ac451902-6ad6-4127-a971-e846eb9af3bc"

_DESKTOPS = [
    {"id": DESK_1, "name": "Desktop 1", "current": True},
    {"id": DESK_2, "name": "Work 2", "current": False},
]


def _set_result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "windowFound": True,
        "desktopFound": True,
        "current": DESK_1,
        "before": [DESK_1],
        "beforeOnAllDesktops": False,
    }
    result.update(overrides)
    return result


def _readback(window_desktops: list[str] | None, current: str = DESK_1) -> dict[str, Any]:
    window = None if window_desktops is None else {"desktops": window_desktops}
    return {"desktops": _DESKTOPS, "current": current, "window": window}


def _engine_with_stubbed_kwin(
    monkeypatch: pytest.MonkeyPatch,
    set_response: dict[str, Any] | None = None,
    desktops_response: dict[str, Any] | None = None,
) -> tuple[AutomationEngine, list[dict[str, object]]]:
    """Engine whose KWin worker is recorded and answers with fixed responses."""
    engine = core.AutomationEngine()
    monkeypatch.setattr(engine, "_get_session", lambda: object())

    requests: list[dict[str, object]] = []

    def run_query(request: dict[str, object]) -> dict[str, Any]:
        requests.append(request)
        if request.get("op") == "set_desktop":
            return set_response or {"ok": True, "result": _set_result()}
        return desktops_response or {"ok": True, "result": _readback([DESK_2])}

    monkeypatch.setattr(engine, "_run_kwin_query", run_query)
    return engine, requests


# ── virtual_desktops ─────────────────────────────────────────────────────


def test_virtual_desktops_reports_ids_names_and_current(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, requests = _engine_with_stubbed_kwin(monkeypatch)

    data = engine.virtual_desktops_data()

    assert requests == [{"op": "desktops"}], requests
    assert data.error is None
    assert [(d.id, d.name, d.current) for d in data.desktops] == [
        (DESK_1, "Desktop 1", True),
        (DESK_2, "Work 2", False),
    ]
    output = engine.virtual_desktops()
    assert '"Desktop 1" [current]' in output, output
    assert DESK_2 in output, output


def test_virtual_desktops_query_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _ = _engine_with_stubbed_kwin(
        monkeypatch, desktops_response={"ok": False, "error": "KWin query timed out after 30s"}
    )

    data = engine.virtual_desktops_data()

    assert data.desktops == []
    assert data.error == "KWin query timed out after 30s"


# ── window_set_desktop: validation ───────────────────────────────────────


@pytest.mark.parametrize(
    ("window_id", "desktop_id", "missing"),
    [
        ("", DESK_2, "window_id"),
        (WINDOW, "", "desktop_id"),
        (None, DESK_2, "window_id"),
        (WINDOW, 2, "desktop_id"),
    ],
)
def test_missing_or_non_string_id_is_rejected_without_dispatch(
    monkeypatch: pytest.MonkeyPatch, window_id: Any, desktop_id: Any, missing: str
) -> None:
    engine, requests = _engine_with_stubbed_kwin(monkeypatch)

    data = engine.window_set_desktop_data(window_id=window_id, desktop_id=desktop_id)

    assert data.applied is False
    assert data.error is not None and f"needs a {missing}" in data.error, data
    assert requests == [], requests


# ── window_set_desktop: assignment and readback ──────────────────────────


def test_assignment_is_dispatched_by_exact_ids_and_read_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, requests = _engine_with_stubbed_kwin(monkeypatch)

    data = engine.window_set_desktop_data(window_id=WINDOW, desktop_id=DESK_2)

    assert requests == [
        {"op": "set_desktop", "window_id": WINDOW, "desktop_id": DESK_2},
        {"op": "desktops", "window_id": WINDOW},
    ], requests
    assert data.applied is True
    assert data.error is None
    assert data.before == [DESK_1]
    assert data.before_on_all_desktops is False
    assert data.observed == [DESK_2]
    assert data.current_desktop_id_before == DESK_1
    assert data.current_desktop_id_after == DESK_1
    assert "Window desktop set:" in engine.window_set_desktop(WINDOW, DESK_2)


def test_unknown_window_reports_error_without_readback(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, requests = _engine_with_stubbed_kwin(
        monkeypatch,
        set_response={"ok": True, "result": _set_result(windowFound=False, before=None)},
    )

    data = engine.window_set_desktop_data(window_id="{missing}", desktop_id=DESK_2)

    assert data.applied is False
    assert data.error == "No window with id '{missing}'."
    assert data.before is None
    assert len(requests) == 1, requests


def test_unknown_desktop_reports_error_without_readback(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, requests = _engine_with_stubbed_kwin(
        monkeypatch, set_response={"ok": True, "result": _set_result(desktopFound=False)}
    )

    data = engine.window_set_desktop_data(window_id=WINDOW, desktop_id="missing-desk")

    assert data.applied is False
    assert data.error == "No virtual desktop with id 'missing-desk'."
    assert data.before == [DESK_1]
    assert len(requests) == 1, requests


def test_failed_assignment_query_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _ = _engine_with_stubbed_kwin(
        monkeypatch, set_response={"ok": False, "error": "KWin did not answer the script within 5s"}
    )

    data = engine.window_set_desktop_data(window_id=WINDOW, desktop_id=DESK_2)

    assert data.applied is False
    assert data.error is not None and "did not answer" in data.error


@pytest.mark.parametrize("observed", [[DESK_1], [], [DESK_1, DESK_2]])
def test_readback_other_than_exactly_the_target_is_not_applied(
    monkeypatch: pytest.MonkeyPatch, observed: list[str]
) -> None:
    engine, _ = _engine_with_stubbed_kwin(
        monkeypatch, desktops_response={"ok": True, "result": _readback(observed)}
    )

    data = engine.window_set_desktop_data(window_id=WINDOW, desktop_id=DESK_2)

    assert data.applied is False
    assert data.observed == observed
    assert data.error is not None and "did not put the window on exactly" in data.error


def test_failed_readback_is_not_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _ = _engine_with_stubbed_kwin(
        monkeypatch, desktops_response={"ok": False, "error": "KWin query timed out after 30s"}
    )

    data = engine.window_set_desktop_data(window_id=WINDOW, desktop_id=DESK_2)

    assert data.applied is False
    assert data.observed is None
    assert data.current_desktop_id_after is None
    assert data.error is not None and "Readback failed" in data.error


def test_window_gone_on_readback_is_not_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _ = _engine_with_stubbed_kwin(
        monkeypatch, desktops_response={"ok": True, "result": _readback(None)}
    )

    data = engine.window_set_desktop_data(window_id=WINDOW, desktop_id=DESK_2)

    assert data.applied is False
    assert data.observed is None
    assert data.error is not None and "not found on readback" in data.error


def test_current_desktop_change_is_reported_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    # The assignment itself succeeded; an unexpected current-desktop change is
    # reported truthfully instead of being hidden inside applied.
    engine, _ = _engine_with_stubbed_kwin(
        monkeypatch, desktops_response={"ok": True, "result": _readback([DESK_2], current=DESK_2)}
    )

    data = engine.window_set_desktop_data(window_id=WINDOW, desktop_id=DESK_2)

    assert data.applied is True
    assert data.error is None
    assert data.current_desktop_id_before == DESK_1
    assert data.current_desktop_id_after == DESK_2


def test_window_on_all_desktops_is_reported_for_restoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _ = _engine_with_stubbed_kwin(
        monkeypatch,
        set_response={"ok": True, "result": _set_result(before=[], beforeOnAllDesktops=True)},
    )

    data = engine.window_set_desktop_data(window_id=WINDOW, desktop_id=DESK_2)

    assert data.applied is True
    assert data.before == []
    assert data.before_on_all_desktops is True
    assert "(all desktops)" in engine.window_set_desktop(WINDOW, DESK_2)


# ── malformed KWin script output ─────────────────────────────────────────


def _script_replies(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    monkeypatch.setattr(geometry, "run_kwin_script", lambda *_a, **_k: json.dumps(payload))


_GOOD_DESKTOPS: dict[str, Any] = {
    "desktops": _DESKTOPS,
    "current": DESK_1,
    "window": {"desktops": [DESK_1], "onAllDesktops": False},
}


@pytest.mark.parametrize(
    "payload",
    [
        _GOOD_DESKTOPS,
        {**_GOOD_DESKTOPS, "window": None},
        {**_GOOD_DESKTOPS, "current": None},
    ],
)
def test_desktops_accepts_well_formed_output(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    _script_replies(monkeypatch, payload)

    assert geometry.desktops(WINDOW) == payload


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {**_GOOD_DESKTOPS, "desktops": None},
        {**_GOOD_DESKTOPS, "desktops": [{"id": DESK_1, "name": "Desktop 1"}]},
        {**_GOOD_DESKTOPS, "desktops": [{"id": 1, "name": "Desktop 1", "current": True}]},
        {**_GOOD_DESKTOPS, "desktops": [{"id": DESK_1, "name": "Desktop 1", "current": 1}]},
        {k: v for k, v in _GOOD_DESKTOPS.items() if k != "current"},
        {**_GOOD_DESKTOPS, "current": 7},
        {k: v for k, v in _GOOD_DESKTOPS.items() if k != "window"},
        {**_GOOD_DESKTOPS, "window": []},
        {**_GOOD_DESKTOPS, "window": {"onAllDesktops": False}},
        {**_GOOD_DESKTOPS, "window": {"desktops": [1], "onAllDesktops": False}},
        {**_GOOD_DESKTOPS, "window": {"desktops": [DESK_1]}},
        {**_GOOD_DESKTOPS, "window": {"desktops": [DESK_1], "onAllDesktops": "no"}},
    ],
)
def test_desktops_refuses_malformed_output(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    _script_replies(monkeypatch, payload)

    with pytest.raises(RuntimeError, match="malformed desktop list"):
        geometry.desktops(WINDOW)


@pytest.mark.parametrize(
    "payload",
    [
        _set_result(),
        _set_result(before=[], beforeOnAllDesktops=True),
        _set_result(desktopFound=False),
        {"windowFound": False, "desktopFound": True, "current": DESK_1},
        {"windowFound": False, "desktopFound": False, "current": None},
    ],
)
def test_set_desktop_accepts_well_formed_output(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    _script_replies(monkeypatch, payload)

    assert geometry.set_desktop(WINDOW, DESK_2) == payload


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {k: v for k, v in _set_result().items() if k != "windowFound"},
        _set_result(windowFound="yes"),
        {k: v for k, v in _set_result().items() if k != "desktopFound"},
        _set_result(desktopFound=1),
        {k: v for k, v in _set_result().items() if k != "current"},
        _set_result(current=["x"]),
        {k: v for k, v in _set_result().items() if k != "before"},
        _set_result(before=DESK_1),
        _set_result(before=[1]),
        {k: v for k, v in _set_result().items() if k != "beforeOnAllDesktops"},
        _set_result(beforeOnAllDesktops=None),
    ],
)
def test_set_desktop_refuses_malformed_output(
    monkeypatch: pytest.MonkeyPatch, payload: Any
) -> None:
    _script_replies(monkeypatch, payload)

    with pytest.raises(RuntimeError, match="malformed desktop assignment result"):
        geometry.set_desktop(WINDOW, DESK_2)
