"""End-to-end coverage for window_list: every KWin window, its fields and the filter.

Expected values come from ``kwin_oracle`` (a test-owned KWin script on the
session bus), never from the tool under test. Windows come from kcalc, the
window zoo (dialog, popup, panel) and small GTK probes.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from _suite_policy import require_or_skip
from kwin_oracle import KWinOracle, OracleWindow
from kwin_rules import WindowRule, install_rules
from window_zoo import APP_ID, MAIN_CAPTION, ZooProcess

import kwin_mcp.core as core
from kwin_mcp.models import Locator, Window, WindowSelector

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import AppLaunchResult, ElementList, SessionInfo

KCALC_CLIENT_SIZE = (640, 480)
STATE_TIMEOUT_S = 10.0
POLL_S = 0.2
LAYER_SHELL_LIMIT = "known KWin limit: layer-shell surface unavailable"
PANEL_LIMIT = "KWin never activates layer-shell panels"


# ── helpers ──────────────────────────────────────────────────────────────


def _info(engine: AutomationEngine) -> SessionInfo:
    session = engine.session_info().session
    assert session is not None
    return session


def _of(windows: list[OracleWindow], pid: int) -> list[OracleWindow]:
    return [w for w in windows if w.pid == pid]


def _captioned(windows: list[OracleWindow], caption: str) -> list[OracleWindow]:
    return [w for w in windows if w.caption == caption]


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


def _stable_list(
    engine: AutomationEngine, oracle: KWinOracle, selector: WindowSelector | None = None
) -> tuple[list[Window], list[OracleWindow]]:
    """window_list once KWin reports the same windows before and after the call."""
    deadline = time.monotonic() + STATE_TIMEOUT_S
    while True:
        before = oracle.windows()
        listed = engine.window_list(selector).windows
        after = oracle.windows()
        if before == after:
            return listed, after
        assert time.monotonic() < deadline, (before, after)
        time.sleep(POLL_S)


def _wait_active(oracle: KWinOracle, window_id: str | None) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_S
    active = oracle.active_window()
    while (active.id if active else None) != window_id:
        assert time.monotonic() < deadline, (window_id, active)
        time.sleep(POLL_S)
        active = oracle.active_window()


def _settled_active(oracle: KWinOracle) -> OracleWindow | None:
    """KWin's active window once two reads a poll apart agree."""
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
    """Run a zoo command and return the one window KWin added for it."""
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


def _kcalc(oracle: KWinOracle) -> OracleWindow:
    windows = oracle.wait_for_windows(
        lambda ws: len([w for w in ws if "kcalc" in w.resource_class.lower()]) == 1
    )
    return next(w for w in windows if "kcalc" in w.resource_class.lower())


# ── fixtures ─────────────────────────────────────────────────────────────


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


def _engine_with_timed_out_kwin_query(monkeypatch: pytest.MonkeyPatch) -> AutomationEngine:
    engine = core.AutomationEngine()
    monkeypatch.setattr(engine, "_require_session", lambda: object())
    monkeypatch.setattr(engine, "_session_env", lambda: {})

    def time_out(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="kwin_mcp.geometry", timeout=30)

    monkeypatch.setattr(core.subprocess, "run", time_out)
    return engine


# ── active marker (ported from test_observation_tools.py) ────────────────


def test_window_list_marks_the_active_window(
    kcalc_session: AutomationEngine, oracle: KWinOracle
) -> None:
    kcalc = _kcalc(oracle)
    _wait_active(oracle, kcalc.id)

    listed = kcalc_session.window_list(WindowSelector(app_id="kcalc"))
    assert [w.id for w in listed.windows] == [kcalc.id], listed
    assert listed.windows[0].active is True, listed
    assert f'- {kcalc.resource_class} "{kcalc.caption}" [active]' in listed.render()


def test_window_list_marks_a_focused_top_level_window(
    kcalc_session: AutomationEngine,
    oracle: KWinOracle,
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
    tmp_path: Path,
) -> None:
    probe = tmp_path / "focused_window_probe.py"
    probe.write_text(
        "import gi\n"
        "gi.require_version('Gtk', '3.0')\n"
        "from gi.repository import GLib, Gtk\n"
        "GLib.set_application_name('focused_window_probe.py')\n"
        "window = Gtk.Window(title='Focused Window Probe')\n"
        "window.set_can_focus(True)\n"
        "window.set_focus_on_map(True)\n"
        "window.set_default_size(320, 160)\n"
        "window.connect('destroy', Gtk.main_quit)\n"
        "window.show_all()\n"
        "def focus_window():\n"
        "    window.present()\n"
        "    window.grab_focus()\n"
        "    return False\n"
        "GLib.timeout_add(100, focus_window)\n"
        "Gtk.main()\n",
        encoding="utf-8",
    )
    launch(f"/usr/bin/python3 {probe}")
    wait_for_app("focused_window_probe.py")
    windows = oracle.wait_for_windows(lambda ws: bool(_captioned(ws, "Focused Window Probe")))
    [probe_window] = _captioned(windows, "Focused Window Probe")
    _wait_active(oracle, probe_window.id)

    listed = kcalc_session.window_list(WindowSelector(caption="Focused Window Probe"))
    assert [(w.id, w.active) for w in listed.windows] == [(probe_window.id, True)], listed
    others = kcalc_session.window_list(WindowSelector(active=True)).windows
    assert [w.id for w in others] == [probe_window.id], others


# ── geometry (ported from test_window_geometry.py) ───────────────────────


def test_client_geometry_is_centred_on_the_screen(
    kcalc_session: AutomationEngine, screen_size: tuple[int, int]
) -> None:
    [window] = kcalc_session.window_list(WindowSelector(app_id="kcalc")).windows
    client = window.client
    assert (client.width, client.height) == KCALC_CLIENT_SIZE, window

    # KWin centres the window horizontally; vertically it leaves room for the
    # panel strip, so only the horizontal centring is pinned exactly.
    screen_width, screen_height = screen_size
    assert client.x == (screen_width - client.width) // 2, window
    assert 0 < client.y < screen_height - client.height, window


def test_frame_encloses_the_client_area(kcalc_session: AutomationEngine) -> None:
    [window] = kcalc_session.window_list(WindowSelector(app_id="kcalc")).windows
    frame, client = window.frame, window.client

    # The frame carries the decoration, so it must enclose the client area.
    assert frame.x <= client.x and frame.y <= client.y, window
    assert frame.x + frame.width >= client.x + client.width, window
    assert frame.y + frame.height >= client.y + client.height, window
    assert (frame.width, frame.height) != (client.width, client.height), window


def test_accessibility_rectangles_are_global_screen_coordinates(
    kcalc_session: AutomationEngine,
) -> None:
    """AT-SPI2 rectangles are translated to screen coordinates by kwin-mcp."""
    elements = kcalc_session.element_query(Locator(app="kcalc", role="frame"), max_depth=1)
    top_levels = [e for e in elements.elements if e.depth == 1]
    assert len(top_levels) == 1, elements.render()[:500]
    rect = top_levels[0].rect
    assert rect is not None, top_levels[0]

    # The window's top-level rectangle lands exactly on the client origin KWin
    # reports, with the same size.
    [window] = kcalc_session.window_list(WindowSelector(app_id="kcalc")).windows
    assert (rect.x, rect.y) == (window.client.x, window.client.y), (rect, window)
    assert (rect.width, rect.height) == (window.client.width, window.client.height)
    assert top_levels[0].window_id == window.id, (top_levels[0], window)


@pytest.mark.qa("QA-WIN-007")
def test_unknown_app_id_returns_empty_list_not_error(kcalc_session: AutomationEngine) -> None:
    listed = kcalc_session.window_list(WindowSelector(app_id="no-such-application"))
    assert listed.windows == [], listed
    # The 0.11 window_geometry empty text, unchanged.
    assert listed.render() == "No windows found for 'no-such-application'."


def test_window_list_reports_kwin_query_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine_with_timed_out_kwin_query(monkeypatch)

    with pytest.raises(
        RuntimeError, match=r"^Window list unavailable: KWin query timed out after 30s$"
    ):
        engine.window_list(WindowSelector(app_id="kcalc"))


# ── fields and filters (QA list) ─────────────────────────────────────────


@pytest.mark.qa("QA-WIN-001")
def test_moveable_resizeable_closeable_match_kwin(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    info = start_session(home="isolated")
    assert info.home_dir is not None, info
    oracle = KWinOracle(info.dbus_address)
    with ZooProcess(wayland_display=info.wayland_socket, dbus_address=info.dbus_address) as app:
        app.expect(f"window: {MAIN_CAPTION}")
        app.command("refuser", "window: Zoo Refuser")
        app.command("undeletable", "window: Zoo Undeletable")
        oracle.wait_for_windows(lambda ws: len(_of(ws, app.pid)) == 3)

        rules = [
            WindowRule(
                description="kwin-mcp e2e: not closeable, fixed position",
                wmclass=APP_ID,
                title="Zoo Refuser",
                closeable=False,
                position=(100, 120),
            ),
            WindowRule(
                description="kwin-mcp e2e: fixed size",
                wmclass=APP_ID,
                title="Zoo Undeletable",
                size=(400, 260),
            ),
        ]
        install_rules(Path(info.home_dir) / ".config", info.dbus_address, rules)

        def ruled(ws: list[OracleWindow]) -> bool:
            refuser = _captioned(_of(ws, app.pid), "Zoo Refuser")
            fixed = _captioned(_of(ws, app.pid), "Zoo Undeletable")
            return (
                bool(refuser and fixed)
                and not refuser[0].closeable
                and not refuser[0].moveable
                and not fixed[0].resizeable
            )

        oracle.wait_for_windows(ruled)
        listed, expected = _stable_list(engine, oracle, WindowSelector(app_id=APP_ID))
        zoo_windows = _of(expected, app.pid)
        assert [w.id for w in listed] == [w.id for w in zoo_windows], (listed, zoo_windows)
        for window, oracle_window in zip(listed, zoo_windows, strict=True):
            _assert_same(window, oracle_window)
        by_caption = {w.caption: w for w in listed}
        # The forced rules really show up, so the comparison is not vacuous.
        assert by_caption[MAIN_CAPTION].closeable and by_caption[MAIN_CAPTION].moveable
        assert by_caption["Zoo Refuser"].closeable is False
        assert by_caption["Zoo Refuser"].moveable is False
        assert by_caption["Zoo Undeletable"].resizeable is False
        assert by_caption["Zoo Undeletable"].closeable is True


@pytest.mark.qa("QA-WIN-002")
def test_filter_kind_normal_equals_0_11_window_geometry_set(
    kcalc_session: AutomationEngine, oracle: KWinOracle, zoo: ZooProcess
) -> None:
    _open(oracle, zoo, "dialog", "Zoo Dialog")
    _open(oracle, zoo, "popup", "Zoo Popup")

    listed, expected = _stable_list(kcalc_session, oracle, WindowSelector(kind=["normal"]))
    # 0.11's window_geometry query kept exactly KWin's normalWindow entries.
    normal = [w for w in expected if w.normal]
    assert [w.id for w in listed] == [w.id for w in normal], (listed, normal)
    assert all(w.kind == "normal" for w in listed), listed
    # The popup exists but is not part of the normal set.
    assert any(w.kind == "popup" for w in expected), expected


@pytest.mark.qa("QA-WIN-003")
def test_app_id_substring_id_exact_and(
    kcalc_session: AutomationEngine, oracle: KWinOracle, zoo: ZooProcess
) -> None:
    kcalc = _kcalc(oracle)
    [zoo_main] = _of(oracle.windows(), zoo.pid)

    upper = kcalc_session.window_list(WindowSelector(app_id="KCAL")).windows
    assert [w.id for w in upper] == [kcalc.id], upper

    exact = kcalc_session.window_list(WindowSelector(id=kcalc.id)).windows
    assert [w.id for w in exact] == [kcalc.id], exact

    prefix = kcalc.id[: len(kcalc.id) // 2]
    assert kcalc_session.window_list(WindowSelector(id=prefix)).windows == []

    # Fields are AND-ed: kcalc's app_id with the zoo's id matches nothing.
    both = kcalc_session.window_list(WindowSelector(app_id="kcalc", id=zoo_main.id))
    assert both.windows == [], both


@pytest.mark.qa("QA-WIN-004")
def test_caption_separate_from_app_id(
    kcalc_session: AutomationEngine, oracle: KWinOracle, zoo: ZooProcess
) -> None:
    kcalc = _kcalc(oracle)
    assert kcalc.caption, kcalc
    caption_word = kcalc.caption.split()[0]

    by_caption = kcalc_session.window_list(WindowSelector(caption=caption_word.upper())).windows
    assert [w.id for w in by_caption] == [
        w.id for w in oracle.windows() if caption_word.lower() in w.caption.lower()
    ], by_caption
    assert kcalc.id in [w.id for w in by_caption], by_caption

    # 0.11 matched app_name against "class caption"; app_id is only the class now.
    combined = f"kcalc {caption_word}"
    assert kcalc_session.window_list(WindowSelector(app_id=combined)).windows == []

    active_before = _settled_active(oracle)
    with pytest.raises(ValueError, match=r"^No window matches \{app_id='window-zoo Zoo'\}\.\n"):
        kcalc_session.window_focus(WindowSelector(app_id=f"{APP_ID} {MAIN_CAPTION}"))
    assert oracle.active_window() == active_before


@pytest.mark.qa("QA-WIN-005")
def test_filter_active_true_reports_the_active_window_kind(
    kcalc_session: AutomationEngine, oracle: KWinOracle, zoo: ZooProcess
) -> None:
    [main] = _of(oracle.windows(), zoo.pid)
    dialog = _open(oracle, zoo, "dialog", "Zoo Dialog")

    def check() -> None:
        active = _settled_active(oracle)
        assert active is not None
        listed = kcalc_session.window_list(WindowSelector(active=True))
        assert len(listed.windows) == 1, listed
        _assert_same(listed.windows[0], active)
        assert listed.render().startswith("Active window:\n"), listed.render()

    assert oracle.activate(dialog.id)
    _wait_active(oracle, dialog.id)
    check()

    # Opening the popup is read-only for kwin-mcp: every reading is KWin's.
    _open(oracle, zoo, "popup", "Zoo Popup")
    check()

    assert oracle.activate(main.id)
    _wait_active(oracle, main.id)
    check()

    oracle.clear_active()
    _wait_active(oracle, None)
    listed = kcalc_session.window_list(WindowSelector(active=True))
    assert listed.windows == [], listed
    assert listed.render() == "No active window."


@pytest.mark.qa("QA-WIN-006")
def test_filter_active_false(
    kcalc_session: AutomationEngine, oracle: KWinOracle, zoo: ZooProcess
) -> None:
    _open(oracle, zoo, "dialog", "Zoo Dialog")
    _settled_active(oracle)
    listed, expected = _stable_list(kcalc_session, oracle, WindowSelector(active=False))
    inactive = [w for w in expected if not w.active]
    assert any(w.active for w in expected), expected
    assert [w.id for w in listed] == [w.id for w in inactive], (listed, expected)
    assert all(w.active is False for w in listed), listed


@pytest.mark.qa("QA-WIN-051")
@pytest.mark.slow
@pytest.mark.timeout(600)
def test_filter_active_true_reports_shell_when_panel_active(
    kcalc_session: AutomationEngine, oracle: KWinOracle, zoo: ZooProcess
) -> None:
    _require_layer_shell(zoo)
    panel = _open(oracle, zoo, "panel", "Zoo Panel")
    assert panel.kind == "shell", panel
    assert oracle.activate(panel.id)
    deadline = time.monotonic() + 3
    active = oracle.active_window()
    while (active is None or active.id != panel.id) and time.monotonic() < deadline:
        time.sleep(0.1)
        active = oracle.active_window()
    if active is None or active.id != panel.id:
        pytest.skip(f"{PANEL_LIMIT} (active after request: {active})")

    listed = kcalc_session.window_list(WindowSelector(active=True))
    assert len(listed.windows) == 1, listed
    assert listed.windows[0].id == panel.id, listed
    assert listed.windows[0].kind == "shell", listed
