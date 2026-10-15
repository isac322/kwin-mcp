"""End-to-end coverage for window_place: frame changes, readback and validation.

KWin applies frame changes asynchronously, so every expectation is read from
``kwin_oracle`` (a test-owned KWin script) once KWin has settled.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from _suite_policy import require_or_skip
from kwin_oracle import KWinOracle, OracleWindow
from kwin_rules import WindowRule, install_rules
from pydantic import ValidationError
from window_zoo import APP_ID, MAIN_CAPTION, ZooProcess

from kwin_mcp.models import PartialRect, WindowSelector

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import SessionInfo, WindowPlaceResult

STATE_TIMEOUT_S = 10.0
POLL_S = 0.2
SETTLE_S = 1.0
LAYER_SHELL_LIMIT = "known KWin limit: layer-shell surface unavailable"


def _info(engine: AutomationEngine) -> SessionInfo:
    session = engine.session_info().session
    assert session is not None
    return session


def _of(windows: list[OracleWindow], pid: int) -> list[OracleWindow]:
    return [w for w in windows if w.pid == pid]


def _frame(window: OracleWindow | None) -> tuple[int, int, int, int]:
    assert window is not None and window.frame is not None, window
    return window.frame.as_ints()


def _settled(oracle: KWinOracle, window_id: str) -> OracleWindow:
    """The window once two reads a poll apart agree (KWin may still be placing it)."""
    deadline = time.monotonic() + STATE_TIMEOUT_S
    previous = oracle.window(window_id)
    while True:
        time.sleep(POLL_S)
        current = oracle.window(window_id)
        assert current is not None, window_id
        if current == previous:
            return current
        assert time.monotonic() < deadline, (previous, current)
        previous = current


def _wait_frame(oracle: KWinOracle, window_id: str, frame: tuple[int, int, int, int]) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_S
    while _frame(oracle.window(window_id)) != frame:
        assert time.monotonic() < deadline, (oracle.window(window_id), frame)
        time.sleep(POLL_S)


def _kcalc(oracle: KWinOracle) -> OracleWindow:
    windows = oracle.wait_for_windows(
        lambda ws: len([w for w in ws if "kcalc" in w.resource_class.lower()]) == 1
    )
    kcalc = next(w for w in windows if "kcalc" in w.resource_class.lower())
    return _settled(oracle, kcalc.id)


def _open(oracle: KWinOracle, zoo: ZooProcess, command: str, title: str) -> OracleWindow:
    before = {w.id for w in _of(oracle.windows(), zoo.pid)}
    zoo.command(command, f"window: {title}")
    windows = oracle.wait_for_windows(lambda ws: any(w.id not in before for w in _of(ws, zoo.pid)))
    added = [w for w in _of(windows, zoo.pid) if w.id not in before]
    assert len(added) == 1, added
    return _settled(oracle, added[0].id)


def _assert_readback_consistent(result: WindowPlaceResult, requested: dict[str, int]) -> None:
    """applied/pending are derived from the observed frame, field by field."""
    window = result.window
    assert window is not None, result
    pending = [field for field, value in requested.items() if getattr(window.frame, field) != value]
    assert result.pending == pending, result
    assert result.applied.frame is (not pending), result
    assert result.applied.desktop is None, result
    if pending:
        assert (
            f"not yet applied: {', '.join(pending)} (the client may still be applying "
            "the change; verify with window_list)"
        ) in result.render()


def _assert_reports_kwin_frame(
    engine: AutomationEngine, oracle: KWinOracle, window_id: str
) -> None:
    """Once KWin has settled, window_list reports exactly KWin's frame for the window."""
    settled = _settled(oracle, window_id)
    [listed] = engine.window_list(WindowSelector(id=window_id)).windows
    frame = listed.frame
    assert (frame.x, frame.y, frame.width, frame.height) == _frame(settled), (listed, settled)


@pytest.fixture
def oracle(kcalc_session: AutomationEngine) -> KWinOracle:
    return KWinOracle(_info(kcalc_session).dbus_address)


@pytest.fixture
def zoo(kcalc_session: AutomationEngine, oracle: KWinOracle) -> Iterator[ZooProcess]:
    info = _info(kcalc_session)
    with ZooProcess(wayland_display=info.wayland_socket, dbus_address=info.dbus_address) as app:
        app.layer_shell()
        app.expect(f"window: {MAIN_CAPTION}")
        oracle.wait_for_windows(lambda ws: len(_of(ws, app.pid)) == 1)
        yield app


# ── ported from test_window_geometry.py ──────────────────────────────────


def test_window_place_before_line_is_pre_call_frame(
    kcalc_session: AutomationEngine, oracle: KWinOracle
) -> None:
    kcalc = _kcalc(oracle)
    x, y, width, height = _frame(kcalc)
    target = (x + 37, y + 23)

    result = kcalc_session.window_place(
        WindowSelector(id=kcalc.id), frame=PartialRect(x=target[0], y=target[1])
    )

    # before is the frame read just before the request, not the requested
    # position echoed back from the live geometry wrapper.
    before = result.before.frame
    assert (before.x, before.y, before.width, before.height) == (x, y, width, height), result
    assert (before.x, before.y) != target, result
    assert f"before:    ({x}, {y}, {width}x{height})" in result.render().splitlines()


@pytest.mark.qa("QA-WIN-022")
def test_beyond_workspace_rejected_without_dispatch(
    kcalc_session: AutomationEngine, oracle: KWinOracle, screen_size: tuple[int, int]
) -> None:
    kcalc = _kcalc(oracle)
    selector = WindowSelector(id=kcalc.id)
    screen_width, _ = screen_size

    with pytest.raises(ValueError, match=r"^window_place needs at least one of frame/desktop\.\n"):
        kcalc_session.window_place(selector)

    invalid_frames: list[tuple[dict[str, Any], str]] = [
        ({}, "frame needs at least one of x, y, width or height"),
        ({"width": 0}, "greater than 0"),
        ({"width": -5}, "greater than 0"),
        ({"x": 1.5}, "valid integer"),
        ({"x": True}, "frame fields must be integers, got True"),
    ]
    for raw, message in invalid_frames:
        with pytest.raises(ValidationError, match=message):
            kcalc_session.window_place(selector, frame=PartialRect.model_validate(raw))

    with pytest.raises(
        ValueError,
        match=r"^window_place frame\.x must be within the signed 32-bit range, got 2147483648\.$",
    ):
        kcalc_session.window_place(selector, frame=PartialRect(x=2**31))

    with pytest.raises(
        ValueError,
        match=(
            rf"^window_place frame\.width must be at most {screen_width} \(the KWin workspace "
            r"width; start the session with a larger screen for bigger windows\), "
            rf"got {screen_width + 1}\.$"
        ),
    ):
        kcalc_session.window_place(selector, frame=PartialRect(width=screen_width + 1))
    with pytest.raises(ValueError, match=r"start the session with a larger screen"):
        kcalc_session.window_place(selector, frame=PartialRect(width=5000))

    # Nothing was assigned: the frame is unchanged and kcalc keeps its window.
    time.sleep(SETTLE_S)
    after = oracle.window(kcalc.id)
    assert after is not None, kcalc
    assert _frame(after) == _frame(kcalc), (kcalc, after)


def test_window_place_moves_and_resizes_exact_window(
    kcalc_session: AutomationEngine, oracle: KWinOracle
) -> None:
    kcalc = _kcalc(oracle)

    result = kcalc_session.window_place(
        WindowSelector(id=kcalc.id), frame=PartialRect(x=80, y=90, width=700, height=520)
    )
    assert result.window_id == kcalc.id, result

    _wait_frame(oracle, kcalc.id, (80, 90, 700, 520))
    [listed] = kcalc_session.window_list(WindowSelector(id=kcalc.id)).windows
    frame = listed.frame
    assert (frame.x, frame.y, frame.width, frame.height) == (80, 90, 700, 520), listed


# ── QA list ──────────────────────────────────────────────────────────────


@pytest.mark.qa("QA-WIN-020")
def test_readback_applied_vs_pending(kcalc_session: AutomationEngine, oracle: KWinOracle) -> None:
    kcalc = _kcalc(oracle)
    requested = {"x": 100, "y": 100, "width": 800, "height": 600}

    result = kcalc_session.window_place(WindowSelector(id=kcalc.id), frame=PartialRect(**requested))

    before = result.before
    assert before.frame.model_dump() == dict(
        zip(("x", "y", "width", "height"), _frame(kcalc), strict=True)
    ), result
    assert (before.moveable, before.resizeable) == (kcalc.moveable, kcalc.resizeable), result
    assert (before.app_id, before.caption) == (kcalc.resource_class, kcalc.caption), result
    assert result.observation_error is None, result
    _assert_readback_consistent(result, requested)
    _wait_frame(oracle, kcalc.id, (100, 100, 800, 600))
    if result.applied.frame:
        assert result.window is not None
        assert result.window.frame.model_dump() == requested, result


@pytest.mark.qa("QA-WIN-020")
def test_before_reports_moveable_resizeable(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    info = start_session(home="isolated")
    assert info.home_dir is not None, info
    oracle = KWinOracle(info.dbus_address)
    with ZooProcess(wayland_display=info.wayland_socket, dbus_address=info.dbus_address) as app:
        app.expect(f"window: {MAIN_CAPTION}")
        app.command("undeletable", "window: Zoo Undeletable")
        rule = WindowRule(
            description="kwin-mcp e2e: fixed size and position",
            wmclass=APP_ID,
            title="Zoo Undeletable",
            size=(400, 260),
            position=(60, 70),
        )
        install_rules(Path(info.home_dir) / ".config", info.dbus_address, [rule])

        def ruled(ws: list[OracleWindow]) -> bool:
            fixed = [w for w in _of(ws, app.pid) if w.caption == "Zoo Undeletable"]
            return bool(fixed) and not fixed[0].resizeable and not fixed[0].moveable

        windows = oracle.wait_for_windows(ruled)
        fixed = _settled(
            oracle, next(w.id for w in _of(windows, app.pid) if w.caption == "Zoo Undeletable")
        )
        x, y, width, height = _frame(fixed)
        requested = {"x": x + 40, "y": y + 30, "width": width + 50, "height": height + 40}

        result = engine.window_place(WindowSelector(id=fixed.id), frame=PartialRect(**requested))

        # before reports the capabilities KWin had for the window at the call.
        assert (result.before.moveable, result.before.resizeable) == (
            fixed.moveable,
            fixed.resizeable,
        ), (result, fixed)
        assert (result.before.moveable, result.before.resizeable) == (False, False), result
        assert "moveable: False, resizeable: False" in result.render().splitlines()
        before = result.before.frame
        assert (before.x, before.y, before.width, before.height) == (x, y, width, height)
        # Whether KWin honours a scripted frame on a rule-fixed window is KWin's
        # call: applied/pending follow the readback, which follows KWin.
        _assert_readback_consistent(result, requested)
        _assert_reports_kwin_frame(engine, oracle, fixed.id)


@pytest.mark.qa("QA-WIN-021")
def test_partial_frame_keeps_omitted_fields(
    kcalc_session: AutomationEngine, oracle: KWinOracle
) -> None:
    kcalc = _kcalc(oracle)
    _, y, width, height = _frame(kcalc)

    result = kcalc_session.window_place(WindowSelector(id=kcalc.id), frame=PartialRect(x=50))
    assert result.requested_frame == PartialRect(x=50), result
    assert "requested: x=50, y=None, width=None, height=None" in result.render().splitlines()

    _wait_frame(oracle, kcalc.id, (50, y, width, height))
    time.sleep(SETTLE_S)
    assert _frame(oracle.window(kcalc.id)) == (50, y, width, height)


@pytest.mark.qa("QA-WIN-025")
def test_desktop_then_frame_order(kcalc_session: AutomationEngine, oracle: KWinOracle) -> None:
    kcalc = _kcalc(oracle)
    current = oracle.current_desktop().id
    oracle.run("workspace.createDesktop(1, 'Two');\nreport(true);")
    desktops = oracle.desktops()
    assert len(desktops) == 2, desktops
    second = desktops[1].id
    _, _, width, height = _frame(kcalc)

    result = kcalc_session.window_place(
        WindowSelector(id=kcalc.id), frame=PartialRect(x=10, y=10), desktop=second
    )

    assert result.observation_error is None, result
    assert result.applied.desktop is True, result
    assert result.current_desktop_before == result.current_desktop_after == current, result
    _wait_frame(oracle, kcalc.id, (10, 10, width, height))
    after = oracle.window(kcalc.id)
    assert after is not None and after.desktops == (second,), after
    assert oracle.current_desktop().id == current
    # The text follows the order KWin applies them: desktop first, then frame.
    lines = result.render().splitlines()
    assert lines[0] == f"Window desktop set: {kcalc.id} -> {second}", lines
    assert lines.index(f"before:   ['{current}']") < lines.index(
        f'Geometry update requested: {kcalc.resource_class} "{kcalc.caption}" ({kcalc.id})'
    ), lines


@pytest.mark.qa("QA-WIN-029")
def test_window_null_when_window_closes_during_place(
    kcalc_session: AutomationEngine, oracle: KWinOracle, zoo: ZooProcess
) -> None:
    closer = _open(oracle, zoo, "closer", "Zoo Closer")
    _, _, width, _ = _frame(closer)

    result = kcalc_session.window_place(
        WindowSelector(id=closer.id), frame=PartialRect(width=width + 40)
    )

    zoo.expect("closed: Zoo Closer")
    assert result.window is None, result
    assert result.observation_error is None, result
    assert result.applied.frame is False, result
    assert result.pending == [], result
    assert "observed: window no longer exists (it closed or its client exited)" in (
        result.render().splitlines()
    )
    assert oracle.window(closer.id) is None


@pytest.mark.qa("QA-WIN-029")
def test_observation_error_on_readback_failure(
    kcalc_session: AutomationEngine, oracle: KWinOracle, monkeypatch: pytest.MonkeyPatch
) -> None:
    kcalc = _kcalc(oracle)
    oracle.run("workspace.createDesktop(1, 'Two');\nreport(true);")
    second = oracle.desktops()[1].id
    real_query = kcalc_session._run_kwin_query
    calls: list[str] = []

    def failing_readback(request: dict[str, object]) -> dict[str, Any]:
        # Resolution and the assignment really run; only the readback fails.
        calls.append(str(request["op"]))
        resolving = request["op"] == "windows" and calls.count("windows") == 1
        if request["op"] in ("set_geometry", "set_desktop") or resolving:
            return real_query(request)
        return {"ok": False, "error": "readback broke"}

    monkeypatch.setattr(kcalc_session, "_run_kwin_query", failing_readback)

    frame_result = kcalc_session.window_place(
        WindowSelector(id=kcalc.id), frame=PartialRect(x=30, y=40)
    )
    assert frame_result.window is None, frame_result
    assert frame_result.observation_error == "readback broke", frame_result
    assert frame_result.applied.frame is False, frame_result
    assert frame_result.pending == ["x", "y"], frame_result
    assert (
        "observation failed: readback broke (the window may already have moved)"
        in frame_result.render().splitlines()
    )

    calls.clear()
    desktop_result = kcalc_session.window_place(WindowSelector(id=kcalc.id), desktop=second)
    assert desktop_result.window is None, desktop_result
    assert desktop_result.observation_error == "readback broke", desktop_result
    assert desktop_result.applied.desktop is False, desktop_result
    assert desktop_result.current_desktop_after is None, desktop_result
    # KWin did apply both parts; only the readback failed.
    monkeypatch.undo()
    _wait_frame(oracle, kcalc.id, (30, 40, *_frame(kcalc)[2:]))
    after = oracle.window(kcalc.id)
    assert after is not None and after.desktops == (second,), after


@pytest.mark.qa("QA-WIN-030")
def test_shell_surface_id_accepted(
    kcalc_session: AutomationEngine, oracle: KWinOracle, zoo: ZooProcess
) -> None:
    status = zoo.layer_shell()
    if status != "layer-shell: available":
        require_or_skip(
            "layer-shell",
            f"{LAYER_SHELL_LIMIT}: {status.removeprefix('layer-shell: unavailable: ')}",
            os.environ,
        )
    panel = _open(oracle, zoo, "panel", "Zoo Panel")
    assert panel.kind == "shell", panel

    # 0.11 reached shell surfaces by id; KWin decides what happens to them.
    result = kcalc_session.window_place(WindowSelector(id=panel.id), frame=PartialRect(x=50))
    assert result.window_id == panel.id, result
    assert result.before.moveable is panel.moveable, (result, panel)
    assert result.before.moveable is False, result
    before = result.before.frame
    assert (before.x, before.y, before.width, before.height) == _frame(panel), result
    assert result.window is not None, result
    assert result.window.kind == "shell", result
    # The request was made and KWin decided the outcome; the readback follows it.
    _assert_readback_consistent(result, {"x": 50})
    _assert_reports_kwin_frame(kcalc_session, oracle, panel.id)
