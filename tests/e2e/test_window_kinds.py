"""End-to-end coverage for window kinds across window_list/focus/place/close.

The window zoo opens every kind KWin can hold (normal, transient dialog,
popup, tooltip, layer-shell surfaces). What KWin makes of each window is read
with ``kwin_oracle``; the tools must report exactly that, and their selectors
must reach every kind by id while ambiguity stays an error.
"""

from __future__ import annotations

import os
import re
import time
from typing import TYPE_CHECKING

import pytest
from _suite_policy import require_or_skip
from kwin_oracle import KWinOracle, OracleWindow
from window_zoo import APP_ID, MAIN_CAPTION, ZooProcess

from kwin_mcp.models import Locator, PartialRect, Window, WindowSelector

if TYPE_CHECKING:
    from collections.abc import Iterator

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import SessionInfo

STATE_TIMEOUT_S = 10.0
POLL_S = 0.2
SETTLE_S = 1.0
LAYER_SHELL_LIMIT = "known KWin limit: layer-shell surface unavailable"
NARROW_HINT = 'Narrow the selector: add kind:["normal"] or use id.'


def _info(engine: AutomationEngine) -> SessionInfo:
    session = engine.session_info().session
    assert session is not None
    return session


def _of(windows: list[OracleWindow], pid: int) -> list[OracleWindow]:
    return [w for w in windows if w.pid == pid]


def _frame(window: OracleWindow | None) -> tuple[int, int, int, int]:
    assert window is not None and window.frame is not None, window
    return window.frame.as_ints()


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


def _settled_windows(oracle: KWinOracle) -> list[OracleWindow]:
    deadline = time.monotonic() + STATE_TIMEOUT_S
    previous = oracle.windows()
    while True:
        time.sleep(POLL_S)
        current = oracle.windows()
        if current == previous:
            return current
        assert time.monotonic() < deadline, (previous, current)
        previous = current


def _assert_same(window: Window, expected: OracleWindow) -> None:
    """Every Window field equals what KWin's own scripting API reports."""
    assert expected.frame is not None and expected.client is not None, expected
    observed = (
        window.id,
        window.app_id,
        window.caption,
        window.kind,
        window.active,
        (window.frame.x, window.frame.y, window.frame.width, window.frame.height),
        (window.client.x, window.client.y, window.client.width, window.client.height),
        window.moveable,
        window.resizeable,
        window.closeable,
        window.desktops,
        window.on_all_desktops,
    )
    wanted = (
        expected.id,
        expected.resource_class,
        expected.caption,
        expected.kind,
        expected.active,
        tuple(max(1, v) if i >= 2 else v for i, v in enumerate(expected.frame.as_ints())),
        tuple(max(1, v) if i >= 2 else v for i, v in enumerate(expected.client.as_ints())),
        expected.moveable,
        expected.resizeable,
        expected.closeable,
        list(expected.desktops),
        expected.on_all_desktops,
    )
    assert observed == wanted, (window, expected)


def _candidate(window: OracleWindow) -> str:
    return f'{window.id} {window.resource_class} "{window.caption}" ({window.kind})'


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


@pytest.mark.qa("QA-WIN-001")
def test_dialog_popup_tooltip_listed_with_kind(
    kcalc_session: AutomationEngine, oracle: KWinOracle, zoo: ZooProcess
) -> None:
    _require_layer_shell(zoo)
    opened = [
        _open(oracle, zoo, "dialog", "Zoo Dialog"),
        _open(oracle, zoo, "panel", "Zoo Panel"),
        _open(oracle, zoo, "panel dialog", "Zoo Panel dialog"),
        _open(oracle, zoo, "popup", "Zoo Popup"),
    ]
    # GTK3 maps a TOOLTIP-hinted popup as a wl_subsurface of its parent, so
    # KWin holds no window for it; the listing must still equal KWin's.
    zoo.command("tooltip", "window: Zoo Tooltip")
    # Read-only from here on: popups are dismissed by input, so the readings
    # are KWin's state while they are open.
    expected = _settled_windows(oracle)
    listed = kcalc_session.window_list().windows
    assert oracle.windows() == expected, "KWin changed while window_list ran"

    assert [w.id for w in listed] == [w.id for w in expected], (listed, expected)
    for window, oracle_window in zip(listed, expected, strict=True):
        _assert_same(window, oracle_window)
    kinds = {w.id: w.kind for w in listed}
    assert {kinds[w.id] for w in opened} >= {"normal", "shell", "dialog", "popup"}, kinds
    assert any("kcalc" in w.app_id for w in listed), listed


@pytest.mark.qa("QA-WIN-015")
def test_app_id_selector_ambiguous_while_menu_open_lists_kinds(
    kcalc_session: AutomationEngine, oracle: KWinOracle, zoo: ZooProcess
) -> None:
    [main] = _of(oracle.windows(), zoo.pid)
    popup = _open(oracle, zoo, "popup", "Zoo Popup")
    # Every reading before the calls comes from KWin; kwin-mcp injects nothing.
    candidates = [w for w in _settled_windows(oracle) if APP_ID in w.resource_class]
    assert {w.id for w in candidates} == {main.id, popup.id}, candidates
    active = oracle.active_window()
    cursor = oracle.cursor_pos()

    for call in (kcalc_session.window_focus, kcalc_session.window_close):
        with pytest.raises(ValueError) as error:
            call(WindowSelector(app_id=APP_ID))
        first, hint = str(error.value).splitlines()
        assert first.startswith(f"{len(candidates)} windows match {{app_id='{APP_ID}'}}: "), first
        for window in candidates:
            assert _candidate(window) in first, (first, window)
        assert hint == NARROW_HINT, hint

    # Nothing was activated, closed or moved by the ambiguous calls.
    assert oracle.active_window() == active
    assert {w.id for w in oracle.windows() if APP_ID in w.resource_class} >= {main.id, popup.id}
    assert oracle.cursor_pos() == cursor

    result = kcalc_session.window_focus(WindowSelector(app_id=APP_ID, kind=["normal"]))
    assert result.window_id == main.id, result
    assert result.window is not None and result.window.kind == "normal", result
    after = oracle.active_window()
    assert after is not None and after.id == main.id, after


@pytest.mark.qa("QA-WIN-016")
def test_same_caption_two_normal_windows_ambiguous(
    kcalc_session: AutomationEngine, oracle: KWinOracle, zoo: ZooProcess
) -> None:
    second = _open(oracle, zoo, "same-caption", MAIN_CAPTION)
    windows = _settled_windows(oracle)
    twins = [w for w in _of(windows, zoo.pid) if w.caption == MAIN_CAPTION]
    assert len(twins) == 2 and second.id in {w.id for w in twins}, twins
    frames = {w.id: _frame(w) for w in twins}

    selector = WindowSelector(app_id=APP_ID, caption=MAIN_CAPTION, kind=["normal"])
    with pytest.raises(ValueError) as error:
        kcalc_session.window_place(selector, frame=PartialRect(x=0))
    first, hint = str(error.value).splitlines()
    assert first.startswith(
        f"2 windows match {{app_id='{APP_ID}', caption='{MAIN_CAPTION}', kind=['normal']}}: "
    ), first
    for window in twins:
        assert _candidate(window) in first, (first, window)
    assert hint == NARROW_HINT, hint

    # No first-match fallback: neither window moved.
    time.sleep(SETTLE_S)
    assert {w.id: _frame(oracle.window(w.id)) for w in twins} == frames


@pytest.mark.qa("QA-WIN-030")
def test_shell_surface_kind_and_close_refusal(
    kcalc_session: AutomationEngine, oracle: KWinOracle, zoo: ZooProcess
) -> None:
    _require_layer_shell(zoo)
    panel = _open(oracle, zoo, "panel", "Zoo Panel")
    assert panel.kind == "shell", panel

    [listed] = kcalc_session.window_list(WindowSelector(id=panel.id)).windows
    assert listed.kind == "shell", listed
    assert listed.moveable is panel.moveable is False, (listed, panel)

    placed = kcalc_session.window_place(WindowSelector(id=panel.id), frame=PartialRect(x=50))
    assert placed.before.moveable is False, placed
    assert placed.window is not None and placed.window.kind == "shell", placed

    # KWin decides whether a panel can be closed; the result reports it.
    closed = kcalc_session.window_close(WindowSelector(id=panel.id))
    assert closed.window_id == panel.id, closed
    if panel.closeable:
        assert closed.requested is True, closed
        assert closed.consent == "not_required", closed
    else:
        assert closed.requested is False, closed
        assert closed.refusal == "not_closeable", closed
        assert closed.render() == (
            f'Window {panel.resource_class} "{panel.caption}" ({panel.id}) cannot be closed.'
        )
        time.sleep(SETTLE_S)
        assert oracle.window(panel.id) is not None


@pytest.mark.qa("QA-WIN-046")
def test_element_window_id_to_window_close(
    kcalc_session: AutomationEngine, oracle: KWinOracle, zoo: ZooProcess
) -> None:
    [main] = _of(oracle.windows(), zoo.pid)
    dialog = _open(oracle, zoo, "dialog", "Zoo Dialog")

    deadline = time.monotonic() + STATE_TIMEOUT_S
    while True:
        found = kcalc_session.element_query(Locator(app=APP_ID, role="dialog"), max_depth=1)
        dialogs = [e for e in found.elements if e.depth == 1]
        if dialogs and dialogs[0].window_id is not None:
            break
        assert time.monotonic() < deadline, found.render()[:500]
        time.sleep(POLL_S)
    assert len(dialogs) == 1, found.render()[:500]
    assert dialogs[0].window_id == dialog.id, (dialogs[0], dialog)

    closed = kcalc_session.window_close(WindowSelector(id=dialogs[0].window_id))
    assert closed.requested is True, closed
    assert closed.consent == "not_required", closed
    zoo.expect("closed: Zoo Dialog")
    oracle.wait_for_windows(lambda ws: dialog.id not in {w.id for w in ws})
    assert oracle.window(main.id) is not None


# Each would close windows if it were pasted into the KWin script source
# instead of being passed as data.
INJECTED_ID = "x'); workspace.activeWindow.closeWindow(); ('"
INJECTED_CAPTION = "'); workspace.windowList().forEach(w=>w.closeWindow()); ('"
# A desktop id reaches _SET_DESKTOP_SCRIPT_TEMPLATE through window_place without
# any selector validation; pasted between single quotes it is a complete script.
INJECTED_DESKTOP_ID = "x'; workspace.windowList().forEach(w=>w.closeWindow()); var z='"


@pytest.mark.qa("QA-WIN-047")
def test_kwin_script_injection_inert(
    kcalc_session: AutomationEngine, oracle: KWinOracle, zoo: ZooProcess
) -> None:
    [main] = _of(oracle.windows(), zoo.pid)
    assert oracle.activate(main.id)
    deadline = time.monotonic() + STATE_TIMEOUT_S
    while (active := oracle.active_window()) is None or active.id != main.id:
        assert time.monotonic() < deadline, active
        time.sleep(POLL_S)
    before = {w.id for w in _settled_windows(oracle)}

    closed = kcalc_session.window_close(WindowSelector(id=INJECTED_ID))
    assert (closed.requested, closed.refusal) == (False, "no_window"), closed
    assert closed.render() == f"No window with id {INJECTED_ID!r}."

    with pytest.raises(ValueError, match=re.escape(f"No window with id {INJECTED_ID!r}.")):
        kcalc_session.window_place(WindowSelector(id=INJECTED_ID), frame=PartialRect(x=0))

    listed = kcalc_session.window_list(WindowSelector(caption=INJECTED_CAPTION))
    assert listed.windows == [], listed

    # The desktop id is the one caller-controlled string that reaches a KWin
    # script literal: it must report an unknown id, never run as script.
    with pytest.raises(RuntimeError, match="unknown desktop id"):
        kcalc_session.window_place(WindowSelector(id=main.id), desktop=INJECTED_DESKTOP_ID)

    # Nothing was closed and the active window is unchanged.
    time.sleep(2 * SETTLE_S)
    assert {w.id for w in oracle.windows()} == before
    after = oracle.active_window()
    assert after is not None and after.id == main.id, after
