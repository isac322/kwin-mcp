"""End-to-end coverage for window_focus: activate exactly one window, then read it back.

KWin's own state comes from ``kwin_oracle`` (a test-owned KWin script), so
every expectation is independent of the tool under test.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import TYPE_CHECKING

import pytest
from _suite_policy import require_or_skip
from kwin_oracle import KWinOracle, OracleWindow
from window_zoo import MAIN_CAPTION, ZooProcess

import kwin_mcp.core as core
from kwin_mcp.models import WindowSelector

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import AppLaunchResult, ElementList, SessionInfo, WindowFocusResult

STATE_TIMEOUT_S = 10.0
POLL_S = 0.2
LAYER_SHELL_LIMIT = "known KWin limit: layer-shell surface unavailable"
VANISHING_CAPTION = "Vanishing Probe"
# Destroys its window on the first focus-in after it printed "armed"; it never
# arms while it still has the focus it may get when it maps.
VANISHING_PROBE = f"""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk
GLib.set_application_name("vanishing_probe.py")
window = Gtk.Window(title={VANISHING_CAPTION!r})
window.set_default_size(320, 160)
armed = []
def arm():
    if window.is_active():
        return True
    armed.append(True)
    print("armed", flush=True)
    return False
def focused(widget, _event):
    if armed:
        widget.destroy()
    return False
window.connect("focus-in-event", focused)
window.connect("destroy", Gtk.main_quit)
window.show_all()
GLib.timeout_add(500, arm)
Gtk.main()
"""


def _info(engine: AutomationEngine) -> SessionInfo:
    session = engine.session_info().session
    assert session is not None
    return session


def _of(windows: list[OracleWindow], pid: int) -> list[OracleWindow]:
    return [w for w in windows if w.pid == pid]


def _kcalc_windows(oracle: KWinOracle, count: int) -> list[OracleWindow]:
    windows = oracle.wait_for_windows(
        lambda ws: len([w for w in ws if "kcalc" in w.resource_class.lower()]) == count
    )
    return [w for w in windows if "kcalc" in w.resource_class.lower()]


def _wait_active(oracle: KWinOracle, window_id: str) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_S
    active = oracle.active_window()
    while active is None or active.id != window_id:
        assert time.monotonic() < deadline, (window_id, active)
        time.sleep(POLL_S)
        active = oracle.active_window()


def _settled_active(oracle: KWinOracle) -> OracleWindow | None:
    previous = oracle.active_window()
    deadline = time.monotonic() + STATE_TIMEOUT_S
    while True:
        time.sleep(POLL_S)
        current = oracle.active_window()
        if current == previous:
            return current
        assert time.monotonic() < deadline, (previous, current)
        previous = current


def _open(oracle: KWinOracle, zoo: ZooProcess, command: str, title: str) -> OracleWindow:
    before = {w.id for w in _of(oracle.windows(), zoo.pid)}
    zoo.command(command, f"window: {title}")
    windows = oracle.wait_for_windows(lambda ws: any(w.id not in before for w in _of(ws, zoo.pid)))
    added = [w for w in _of(windows, zoo.pid) if w.id not in before]
    assert len(added) == 1, added
    return added[0]


def _require_layer_shell(zoo: ZooProcess) -> None:
    status = zoo.layer_shell()
    if status != "layer-shell: available":
        require_or_skip(
            "layer-shell",
            f"{LAYER_SHELL_LIMIT}: {status.removeprefix('layer-shell: unavailable: ')}",
            os.environ,
        )


def _assert_observed(result: WindowFocusResult, expected: OracleWindow) -> None:
    """The result's window is KWin's state read after the activation."""
    window = result.window
    assert window is not None, result
    assert result.observation_error is None, result
    assert (window.id, window.active, window.desktops, window.on_all_desktops) == (
        expected.id,
        expected.active,
        list(expected.desktops),
        expected.on_all_desktops,
    ), (result, expected)


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


# ── ported from test_window_control.py / test_window_geometry.py ─────────


def test_window_focus_activates_the_requested_window(
    kcalc_session: AutomationEngine,
    oracle: KWinOracle,
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
) -> None:
    [kcalc] = _kcalc_windows(oracle, 1)
    launch("kwrite")
    wait_for_app("kwrite")
    windows = oracle.wait_for_windows(lambda ws: any("kwrite" in w.resource_class for w in ws))
    kwrite = next(w for w in windows if "kwrite" in w.resource_class)
    _wait_active(oracle, kwrite.id)

    result = kcalc_session.window_focus(WindowSelector(app_id="kcalc"))
    assert result.window_id == kcalc.id, result
    _wait_active(oracle, kcalc.id)
    [listed] = kcalc_session.window_list(WindowSelector(app_id="kcalc")).windows
    assert listed.active is True, listed
    assert result.render().startswith(f"Focused: {kcalc.resource_class} {kcalc.caption}\n")


def test_window_focus_reports_kwin_query_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = core.AutomationEngine()
    monkeypatch.setattr(engine, "_require_session", lambda: object())
    monkeypatch.setattr(engine, "_session_env", lambda: {})

    def time_out(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="kwin_mcp.geometry", timeout=30)

    monkeypatch.setattr(core.subprocess, "run", time_out)

    # The selector is resolved against KWin's window list first, so the
    # timed-out query is that list.
    with pytest.raises(
        RuntimeError, match=r"^Window list unavailable: KWin query timed out after 30s$"
    ):
        engine.window_focus(WindowSelector(app_id="kcalc"))


# ── QA list ──────────────────────────────────────────────────────────────


@pytest.mark.qa("QA-WIN-013")
def test_focus_by_id_activates_exactly_that_window(
    kcalc_session: AutomationEngine, oracle: KWinOracle, launch: Callable[..., AppLaunchResult]
) -> None:
    launch("kcalc")
    first, second = _kcalc_windows(oracle, 2)

    for target in (first, second, first):
        result = kcalc_session.window_focus(WindowSelector(id=target.id))
        assert result.window_id == target.id, result
        active = oracle.active_window()
        assert active is not None and active.id == target.id, (target, active)
        _assert_observed(result, active)
        assert result.window is not None and result.window.active is True, result
        # Exactly one window is active, and it is the target.
        assert [w.id for w in oracle.windows() if w.active] == [target.id]


@pytest.mark.qa("QA-WIN-014")
def test_focus_dialog_is_allowed_no_normal_window_guard(
    kcalc_session: AutomationEngine, oracle: KWinOracle, zoo: ZooProcess
) -> None:
    _require_layer_shell(zoo)
    [main] = _of(oracle.windows(), zoo.pid)
    dialog = _open(oracle, zoo, "panel dialog", "Zoo Panel dialog")
    assert dialog.kind == "dialog", dialog

    listed = kcalc_session.window_list(WindowSelector(kind=["dialog"])).windows
    assert [w.id for w in listed] == [dialog.id], listed

    assert oracle.activate(main.id)
    _wait_active(oracle, main.id)
    result = kcalc_session.window_focus(WindowSelector(id=dialog.id))
    assert result.window_id == dialog.id, result
    active = oracle.active_window()
    assert active is not None and active.id == dialog.id, active
    _assert_observed(result, active)

    # A popup and a dock panel are reachable by id too: the request is made
    # (no normal-window guard) and the result reports what KWin did.
    popup = _open(oracle, zoo, "popup", "Zoo Popup")
    panel = _open(oracle, zoo, "panel", "Zoo Panel")
    for target in (popup, panel):
        assert oracle.activate(main.id)
        _wait_active(oracle, main.id)
        result = kcalc_session.window_focus(WindowSelector(id=target.id))
        assert result.window_id == target.id, result
        after = oracle.window(target.id)
        assert after is not None, target
        _assert_observed(result, after)
        active = oracle.active_window()
        assert after.active == (active is not None and active.id == target.id)


@pytest.mark.qa("QA-WIN-017")
def test_focus_zero_matches_error(kcalc_session: AutomationEngine, oracle: KWinOracle) -> None:
    assert not any("nope" in w.resource_class.lower() for w in oracle.windows())
    before = _settled_active(oracle)

    with pytest.raises(ValueError, match=r"^No window matches \{app_id='nope'\}\.\n") as error:
        kcalc_session.window_focus(WindowSelector(app_id="nope"))
    assert str(error.value).splitlines() == [
        "No window matches {app_id='nope'}.",
        "List windows with window_list.",
    ]
    assert oracle.active_window() == before


@pytest.mark.qa("QA-WIN-018")
def test_focus_vanished_window_reports_window_null_non_error(
    kcalc_session: AutomationEngine,
    oracle: KWinOracle,
    launch: Callable[..., AppLaunchResult],
    tmp_path: Path,
) -> None:
    [kcalc] = _kcalc_windows(oracle, 1)
    probe = tmp_path / "vanishing_probe.py"
    probe.write_text(VANISHING_PROBE, encoding="utf-8")
    launched = launch(f"/usr/bin/python3 {probe}")
    windows = oracle.wait_for_windows(lambda ws: any(w.caption == VANISHING_CAPTION for w in ws))
    vanishing = next(w for w in windows if w.caption == VANISHING_CAPTION)

    # Give the focus to kcalc, then wait for the probe to arm itself.
    assert oracle.activate(kcalc.id)
    _wait_active(oracle, kcalc.id)
    deadline = time.monotonic() + STATE_TIMEOUT_S
    while "armed" not in kcalc_session.app_log(launched.pid, 0).lines:
        assert time.monotonic() < deadline, kcalc_session.app_log(launched.pid, 0)
        time.sleep(POLL_S)

    result = kcalc_session.window_focus(WindowSelector(id=vanishing.id))
    assert result.window_id == vanishing.id, result
    assert result.window is None, result
    assert result.observation_error is None, result
    assert oracle.window(vanishing.id) is None
    assert result.render() == (
        f"Focused: {vanishing.id}\n"
        "observed: window no longer exists (it closed or its client exited)"
    )


def _add_desktop(oracle: KWinOracle, name: str) -> str:
    count = len(oracle.desktops())
    oracle.run(f"workspace.createDesktop({count}, {json.dumps(name)});\nreport(true);")
    desktops = oracle.desktops()
    assert len(desktops) == count + 1, desktops
    return desktops[count].id


@pytest.mark.qa("QA-WIN-019")
@pytest.mark.slow
@pytest.mark.timeout(600)
def test_focus_other_desktop_reports_observed_after(
    kcalc_session: AutomationEngine, oracle: KWinOracle
) -> None:
    [kcalc] = _kcalc_windows(oracle, 1)
    first = oracle.current_desktop().id
    second = _add_desktop(oracle, "Two")

    # (a) The window lives on another desktop: KWin's activation switches to
    # it (0.11 semantics) and the result is the state observed afterwards.
    oracle.run(
        f"var w = findWindow({json.dumps(kcalc.id)});\n"
        "var target = null;\n"
        "for (var i = 0; i < workspace.desktops.length; i++) {\n"
        f"    if (String(workspace.desktops[i].id) === {json.dumps(second)}) "
        "{ target = workspace.desktops[i]; }\n"
        "}\n"
        "w.desktops = [target];\n"
        "report(true);"
    )
    moved = oracle.window(kcalc.id)
    assert moved is not None and moved.desktops == (second,), moved
    assert oracle.current_desktop().id == first

    result = kcalc_session.window_focus(WindowSelector(id=kcalc.id))
    after = oracle.window(kcalc.id)
    assert after is not None
    _assert_observed(result, after)
    assert oracle.current_desktop().id == second
    assert after.active, after

    # (b) The window is minimized: activation is KWin's to decide; the result
    # reports what KWin then shows (active and minimized or not).
    oracle.run(f"var w = findWindow({json.dumps(kcalc.id)});\nw.minimized = true;\nreport(true);")
    minimized = oracle.window(kcalc.id)
    assert minimized is not None and "minimized" in minimized.flags, minimized
    result = kcalc_session.window_focus(WindowSelector(id=kcalc.id))
    after = oracle.window(kcalc.id)
    assert after is not None
    _assert_observed(result, after)
    active = oracle.active_window()
    assert after.active == (active is not None and active.id == kcalc.id)
