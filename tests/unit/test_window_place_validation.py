"""window_place / desktop_list on a fake KWin script runner (no KWin session).

Covers validation before anything is dispatched (QA-WIN-022), the PR #103
desktop refusals including the X11 group-transient one that has no E2E test
(QA-WIN-028, e2e plan D10), readback failures reported as data (QA-WIN-029)
and the desktop_list failure (QA-WIN-012).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from kwin_mcp import geometry
from kwin_mcp.models import PartialRect, Rect, Window, WindowSelector

from ._fakes import windows_reply

if TYPE_CHECKING:
    from collections.abc import Callable

    from ._fakes import FakeEngine, FakeKWinScriptRunner

WINDOW_ID = "{k1}"
D1 = "11111111-1111-1111-1111-111111111111"
D2 = "22222222-2222-2222-2222-222222222222"
BEFORE_FRAME = [100, 100, 400, 300]
MUTATING_OPS = {"set_geometry", "set_desktop", "activate", "close"}

# Errors the engine raises for bad input (ValueError) or a refused/failed
# operation (RuntimeError); the server turns both into a ToolError.
ENGINE_ERRORS = (ValueError, RuntimeError)


def _window(frame: list[int] = BEFORE_FRAME, desktops: list[str] | None = None) -> Window:
    return Window(
        id=WINDOW_ID,
        app_id="org.kde.kcalc",
        caption="KCalc",
        kind="normal",
        active=True,
        frame=Rect(x=frame[0], y=frame[1], width=frame[2], height=frame[3]),
        client=Rect(x=frame[0], y=frame[1] + 30, width=frame[2], height=frame[3] - 30),
        moveable=True,
        resizeable=True,
        closeable=True,
        desktops=[D1] if desktops is None else desktops,
        on_all_desktops=False,
    )


def _desktops_reply(window_desktops: list[str] | None = None) -> dict[str, object]:
    return {
        "ok": True,
        "result": {
            "desktops": [
                {"id": D1, "name": "Desktop 1", "current": True},
                {"id": D2, "name": "Desktop 2", "current": False},
            ],
            "current": D1,
            "window": {"desktops": window_desktops or [D1], "onAllDesktops": False},
        },
    }


def _set_geometry_reply(**extra: object) -> dict[str, object]:
    return {
        "ok": True,
        "result": {
            "found": True,
            "app": "org.kde.kcalc",
            "caption": "KCalc",
            "moveable": True,
            "resizeable": True,
            "before": BEFORE_FRAME,
            **extra,
        },
    }


def _set_desktop_reply(**extra: object) -> dict[str, object]:
    return {
        "ok": True,
        "result": {
            "windowFound": True,
            "desktopFound": True,
            "current": D1,
            "before": [D1],
            "beforeOnAllDesktops": False,
            **extra,
        },
    }


def _windows_then(
    *replies: dict[str, object],
) -> Callable[[dict[str, object]], dict[str, object]]:
    """Answer the first ``windows`` query (selector resolution) with the window,
    later ones with ``replies`` in turn (the last one repeats)."""
    calls = 0

    def _reply(_request: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return windows_reply(_window())
        return replies[min(calls - 2, len(replies) - 1)]

    return _reply


def _mutations(kwin: FakeKWinScriptRunner) -> list[str]:
    """Mutating requests in order; a check-only desktop request is ``check_desktop``."""
    return [
        "check_desktop" if request.get("check_only") is True else str(request.get("op"))
        for request in kwin.requests
        if request.get("op") in MUTATING_OPS
    ]


# ── QA-WIN-022: validation, nothing dispatched on error ──────────────────


@pytest.mark.qa("QA-WIN-022")
@pytest.mark.parametrize(
    ("fields", "message"),
    [
        pytest.param({}, "frame needs at least one of x, y, width or height", id="empty"),
        pytest.param({"width": 0}, "greater than 0", id="zero-width"),
        pytest.param({"height": -5}, "greater than 0", id="negative-height"),
        pytest.param({"x": 1.5}, "integer", id="fraction"),
        pytest.param({"x": True}, "frame fields must be integers, got True", id="bool"),
    ],
)
def test_frame_model_validation_table(fields: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=re.escape(message)):
        PartialRect.model_validate(fields)


@pytest.mark.qa("QA-WIN-022")
@pytest.mark.parametrize(
    ("frame", "desktop", "message"),
    [
        pytest.param(
            None,
            None,
            "window_place needs at least one of frame/desktop.\n"
            "Pass frame={x, y, width, height} and/or desktop=<id from desktop_list>.",
            id="nothing-requested",
        ),
        pytest.param(
            PartialRect(x=2**31),
            None,
            f"window_place frame.x must be within the signed 32-bit range, got {2**31}.",
            id="x-above-int32",
        ),
        pytest.param(
            PartialRect(y=-(2**31) - 1),
            None,
            f"window_place frame.y must be within the signed 32-bit range, got {-(2**31) - 1}.",
            id="y-below-int32",
        ),
        pytest.param(
            PartialRect(width=2**31),
            None,
            f"window_place frame.width must be within the signed 32-bit range, got {2**31}.",
            id="width-above-int32",
        ),
    ],
)
def test_frame_validation_table(
    fake_engine: FakeEngine, frame: PartialRect | None, desktop: str | None, message: str
) -> None:
    fake_engine.kwin.respond("windows", windows_reply(_window()))
    fake_engine.kwin.respond("set_geometry", _set_geometry_reply())
    fake_engine.kwin.respond("set_desktop", _set_desktop_reply())

    with pytest.raises(ValueError) as excinfo:
        fake_engine.engine.window_place(
            window=WindowSelector(id=WINDOW_ID), frame=frame, desktop=desktop
        )

    assert str(excinfo.value) == message
    assert _mutations(fake_engine.kwin) == []


@pytest.mark.qa("QA-WIN-022")
def test_oversize_frame_is_rejected_by_the_script_without_assignment(
    fake_engine: FakeEngine,
) -> None:
    fake_engine.kwin.respond("windows", windows_reply(_window()))
    fake_engine.kwin.respond("set_geometry", _set_geometry_reply(rejected="width", limit=1280))

    with pytest.raises(ENGINE_ERRORS) as excinfo:
        fake_engine.engine.window_place(
            window=WindowSelector(id=WINDOW_ID), frame=PartialRect(width=5000)
        )

    assert str(excinfo.value) == (
        "window_place frame.width must be at most 1280 (the KWin workspace width; "
        "start the session with a larger screen for bigger windows), got 5000."
    )
    (request,) = [r for r in fake_engine.kwin.requests if r.get("op") == "set_geometry"]
    assert request["window_id"] == WINDOW_ID
    assert request["width"] == 5000


@pytest.mark.qa("QA-WIN-022")
def test_ambiguous_selector_dispatches_nothing(fake_engine: FakeEngine) -> None:
    twin = _window().model_copy(update={"id": "{k2}"})
    fake_engine.kwin.respond("windows", windows_reply(_window(), twin))

    with pytest.raises(ValueError, match=r"^2 windows match \{app_id='kcalc'\}: "):
        fake_engine.engine.window_place(
            window=WindowSelector(app_id="kcalc"), frame=PartialRect(x=0)
        )

    assert _mutations(fake_engine.kwin) == []


# ── QA-WIN-028: PR #103 desktop refusals ─────────────────────────────────


@pytest.mark.qa("QA-WIN-028")
def test_unsupported_relationship_refusal_x11_group(fake_engine: FakeEngine) -> None:
    fake_engine.kwin.respond("windows", windows_reply(_window()))
    fake_engine.kwin.respond("set_desktop", _set_desktop_reply(refused="unsupported"))
    fake_engine.kwin.respond("set_geometry", _set_geometry_reply())
    fake_engine.kwin.respond("desktops", _desktops_reply())

    with pytest.raises(ENGINE_ERRORS) as excinfo:
        fake_engine.engine.window_place(
            window=WindowSelector(id=WINDOW_ID), frame=PartialRect(x=0), desktop=D2
        )

    assert str(excinfo.value) == (
        "Refused: a window with an unresolvable transient relationship (X11 group transient) "
        f"exists, so KWin could move windows besides '{WINDOW_ID}'; nothing was changed."
    )
    # The desktop checks run first; their refusal leaves the frame untouched too.
    assert _mutations(fake_engine.kwin) == ["check_desktop"]
    (request,) = [r for r in fake_engine.kwin.requests if r.get("op") == "set_desktop"]
    assert (request["window_id"], request["desktop_id"]) == (WINDOW_ID, D2)
    assert request["check_only"] is True


@pytest.mark.qa("QA-WIN-028")
def test_unsupported_relationship_refusal_x11_group_script_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The geometry layer accepts the script's refusal as a well-formed reply and
    # rejects a refusal shape the script never produces.
    replies = iter(
        [
            '{"windowFound": true, "desktopFound": true, "current": "d1", "before": ["d1"],'
            ' "beforeOnAllDesktops": false, "refused": "unsupported"}',
            '{"windowFound": true, "desktopFound": true, "current": "d1", "before": ["d1"],'
            ' "beforeOnAllDesktops": false, "refused": "unsupported", "related": []}',
        ]
    )
    sent: list[str] = []

    def _run(js_source: str, timeout: float = 5.0) -> str:
        del timeout
        sent.append(js_source)
        return next(replies)

    monkeypatch.setattr(geometry, "run_kwin_script", _run)

    assert geometry.set_desktop(WINDOW_ID, "d2")["refused"] == "unsupported"
    with pytest.raises(RuntimeError, match="malformed desktop assignment result"):
        geometry.set_desktop(WINDOW_ID, "d2")
    # The guard is in the script itself: any transient without transientFor refuses.
    assert "windows[u].transient && !windows[u].transientFor" in sent[0]
    assert '"unsupported"' in sent[0]


@pytest.mark.qa("QA-WIN-028")
def test_related_window_refusal(fake_engine: FakeEngine) -> None:
    related = [{"id": "{dlg}", "app": "org.kde.kcalc", "caption": "About KCalc", "desktops": [D1]}]
    fake_engine.kwin.respond("windows", windows_reply(_window()))
    fake_engine.kwin.respond("set_desktop", _set_desktop_reply(refused="related", related=related))
    fake_engine.kwin.respond("desktops", _desktops_reply())

    with pytest.raises(ENGINE_ERRORS) as excinfo:
        fake_engine.engine.window_place(window=WindowSelector(id=WINDOW_ID), desktop=D2)

    assert str(excinfo.value) == (
        f"Refused: KWin would also move 1 related window(s) of '{WINDOW_ID}' "
        '(transient or modal relationships): {dlg} org.kde.kcalc "About KCalc"; '
        "nothing was changed."
    )
    assert _mutations(fake_engine.kwin) == ["check_desktop"]


@pytest.mark.qa("QA-WIN-028")
def test_unknown_desktop_lists_known_ids(fake_engine: FakeEngine) -> None:
    fake_engine.kwin.respond("windows", windows_reply(_window()))
    fake_engine.kwin.respond("set_desktop", _set_desktop_reply(desktopFound=False))
    fake_engine.kwin.respond("desktops", _desktops_reply())

    with pytest.raises(ENGINE_ERRORS) as excinfo:
        fake_engine.engine.window_place(window=WindowSelector(id=WINDOW_ID), desktop="nope")

    assert str(excinfo.value) == (
        "No virtual desktop with id 'nope' (unknown desktop id); nothing was changed.\n"
        f"Use an id from desktop_list: '{D1}', '{D2}'."
    )


# ── QA-WIN-029: readback failures are data ───────────────────────────────


@pytest.mark.qa("QA-WIN-029")
def test_readback_failure_paths(fake_engine: FakeEngine) -> None:
    failed: dict[str, object] = {"ok": False, "error": "KWin did not answer the script within 5s"}

    # (b) frame path: the change was sent, the readback failed.
    fake_engine.kwin.respond("windows", _windows_then(failed))
    fake_engine.kwin.respond("set_geometry", _set_geometry_reply())
    fake_engine.kwin.respond("desktops", failed)

    frame_result = fake_engine.engine.window_place(
        window=WindowSelector(id=WINDOW_ID), frame=PartialRect(x=10, width=500)
    )

    assert frame_result.window is None
    assert frame_result.observation_error is not None
    assert "KWin did not answer the script within 5s" in frame_result.observation_error
    assert frame_result.applied.frame is False
    assert frame_result.applied.desktop is None
    assert set(frame_result.pending) == {"x", "width"}
    assert frame_result.before.frame == Rect(x=100, y=100, width=400, height=300)
    text = frame_result.render()
    assert "observation failed: " in text
    assert "(the window may already have moved)" in text

    # (b) desktop path, and both parts together.
    fake_engine.kwin.respond("windows", _windows_then(failed))
    fake_engine.kwin.respond("set_desktop", _set_desktop_reply())
    fake_engine.kwin.respond("desktops", _desktops_reply())

    both = fake_engine.engine.window_place(
        window=WindowSelector(id=WINDOW_ID), frame=PartialRect(y=20), desktop=D2
    )

    assert both.window is None
    assert both.observation_error is not None
    assert both.applied.desktop is False
    assert both.applied.frame is False
    assert both.pending == ["y"]
    assert both.requested_desktop == D2
    assert both.before.desktops == [D1]


@pytest.mark.qa("QA-WIN-029")
def test_readback_vanished_window_is_null_not_error(fake_engine: FakeEngine) -> None:
    fake_engine.kwin.respond("windows", _windows_then(windows_reply()))
    fake_engine.kwin.respond("set_geometry", _set_geometry_reply())

    result = fake_engine.engine.window_place(
        window=WindowSelector(id=WINDOW_ID), frame=PartialRect(x=10)
    )

    assert result.window is None
    assert result.observation_error is None
    assert result.applied.frame is False
    assert "observed: window no longer exists" in result.render()


@pytest.mark.qa("QA-WIN-029")
def test_readback_applied_frame_and_desktop(fake_engine: FakeEngine) -> None:
    moved = _window(frame=[10, 100, 400, 300], desktops=[D2])
    fake_engine.kwin.respond("windows", _windows_then(windows_reply(moved)))
    fake_engine.kwin.respond("set_desktop", _set_desktop_reply())
    fake_engine.kwin.respond("set_geometry", _set_geometry_reply())
    fake_engine.kwin.respond("desktops", _desktops_reply([D2]))

    result = fake_engine.engine.window_place(
        window=WindowSelector(id=WINDOW_ID), frame=PartialRect(x=10), desktop=D2
    )

    assert result.window == moved
    assert result.observation_error is None
    assert (result.applied.frame, result.applied.desktop) == (True, True)
    assert result.pending == []
    # Desktop checks first, then the frame (KWin ignores frames on hidden desktops),
    # then the desktop assignment.
    assert _mutations(fake_engine.kwin) == ["check_desktop", "set_geometry", "set_desktop"]


# ── QA-WIN-012: desktop_list failure ─────────────────────────────────────


@pytest.mark.qa("QA-WIN-012")
def test_desktop_list_failure_is_tool_error(fake_engine: FakeEngine) -> None:
    fake_engine.kwin.respond(
        "desktops", {"ok": False, "error": "KWin returned a malformed desktop list: {}"}
    )

    with pytest.raises(RuntimeError) as excinfo:
        fake_engine.engine.desktop_list()

    assert str(excinfo.value) == (
        "Virtual desktops unavailable: KWin returned a malformed desktop list: {}"
    )
