"""Self-tests of the independent oracles (OX-1..OX-4) on a test-owned KWin.

Every window, accessibility and input fact the 1.0 tool tests assert is read
through ``kwin_oracle``, ``atspi_oracle``, the window zoo or the interaction
probe's raw event log. This file proves those work on each image before any
tool test depends on them. Apps are started directly in the session, so the
only kwin-mcp code used here is the input injection that drives the probe.

``test_window_zoo_reports_layer_shell_available`` runs first in shard 1
(amendment A9): a broken GtkLayerShell install or LD_PRELOAD shim fails fast.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import TYPE_CHECKING

import atspi_oracle
import pytest
from _suite_policy import require_or_skip
from atspi_oracle import AtspiNode, AtspiOracle
from kwin_oracle import KWinOracle, OracleWindow, Rect
from kwin_rules import WindowRule, install_rules
from session_harness import live_kwin
from window_zoo import (
    APP_ID,
    DEEP_BUTTON,
    DEEP_LABELS,
    MAIN_CAPTION,
    UNICODE_BUTTON,
    UNICODE_CAPTION,
    UNICODE_LABEL,
    ZooProcess,
    zoo_env,
)

from kwin_mcp.models import Point

if TYPE_CHECKING:
    from collections.abc import Iterator

    from session_harness import LiveKWin

    from kwin_mcp.core import AutomationEngine

E2E_DIR = Path(__file__).resolve().parent
PROBE_SCRIPT = E2E_DIR / "interaction_probe.py"
PROBE_APP_ID = "interaction_probe.py"
SCREEN = (1280, 800)
LAYER_SHELL_LIMIT = "known KWin limit: layer-shell surface unavailable"


@pytest.fixture
def live() -> Iterator[LiveKWin]:
    with live_kwin() as session:
        yield session


@pytest.fixture
def oracle(live: LiveKWin) -> KWinOracle:
    return KWinOracle(live.dbus_address)


@pytest.fixture
def zoo(live: LiveKWin, oracle: KWinOracle) -> Iterator[ZooProcess]:
    with ZooProcess(wayland_display=live.wayland_display, dbus_address=live.dbus_address) as app:
        app.layer_shell()
        app.expect(f"window: {MAIN_CAPTION}")
        oracle.wait_for_windows(lambda windows: len(_of(windows, app.pid)) == 1)
        yield app


def _of(windows: list[OracleWindow], pid: int) -> list[OracleWindow]:
    return [window for window in windows if window.pid == pid]


def _captioned(windows: list[OracleWindow], caption: str) -> list[OracleWindow]:
    return [window for window in windows if window.caption == caption]


# ── window zoo + kwin_oracle ─────────────────────────────────────────────


def _require_layer_shell(zoo: ZooProcess) -> None:
    status = zoo.layer_shell()
    if status != "layer-shell: available":
        require_or_skip(
            "layer-shell",
            f"{LAYER_SHELL_LIMIT}: {status.removeprefix('layer-shell: unavailable: ')}",
            os.environ,
        )


def _open_panel(oracle: KWinOracle, zoo: ZooProcess, scope: str = "") -> OracleWindow:
    """Open a layer-shell panel and return it; KWin gives layer surfaces an empty caption."""
    before = {w.id for w in _of(oracle.windows(), zoo.pid)}
    command = f"panel {scope}" if scope else "panel"
    title = f"Zoo Panel {scope}" if scope and scope != "dock" else "Zoo Panel"
    zoo.command(command, f"window: {title}")
    windows = oracle.wait_for_windows(lambda ws: any(w.id not in before for w in _of(ws, zoo.pid)))
    added = [w for w in _of(windows, zoo.pid) if w.id not in before]
    assert len(added) == 1, added
    return added[0]


def test_window_zoo_reports_layer_shell_available(oracle: KWinOracle, zoo: ZooProcess) -> None:
    _require_layer_shell(zoo)
    panel = _open_panel(oracle, zoo)
    # KWin types a layer surface by its namespace; "dock" makes it a dock.
    assert "dock" in panel.flags, (panel.flags, panel.window_type)
    assert panel.kind == "shell", (panel.flags, panel.window_type)
    assert panel.on_all_desktops, panel.flags


def test_layer_shell_panel_activation(oracle: KWinOracle, zoo: ZooProcess) -> None:
    """Decides QA-WIN-051: does KWin ever report a layer-shell panel as active?"""
    _require_layer_shell(zoo)
    panel = _open_panel(oracle, zoo)
    assert oracle.activate(panel.id)
    deadline = time.monotonic() + 3
    active = oracle.active_window()
    while (active is None or active.id != panel.id) and time.monotonic() < deadline:
        time.sleep(0.1)
        active = oracle.active_window()
    if active is None or active.id != panel.id:
        pytest.skip(f"KWin never activates layer-shell panels (active after request: {active})")
    assert active.kind == "shell"


def test_kwin_oracle_lists_the_window_zoo_layout(oracle: KWinOracle, zoo: ZooProcess) -> None:
    for command, title in (
        ("same-caption", MAIN_CAPTION),
        ("dialog", "Zoo Dialog"),
        ("undeletable", "Zoo Undeletable"),
        ("refuser", "Zoo Refuser"),
    ):
        zoo.command(command, f"window: {title}")
    windows = _of(oracle.wait_for_windows(lambda ws: len(_of(ws, zoo.pid)) == 5), zoo.pid)

    assert {w.resource_class for w in windows} == {APP_ID}
    assert sorted(w.caption for w in windows) == sorted(
        [MAIN_CAPTION, MAIN_CAPTION, "Zoo Dialog", "Zoo Refuser", "Zoo Undeletable"]
    )
    mains = _captioned(windows, MAIN_CAPTION)
    assert len({w.id for w in mains}) == 2
    dialog = _captioned(windows, "Zoo Dialog")[0]
    assert dialog.transient_for in {w.id for w in mains}, dialog
    for window in windows:
        assert window.frame is not None and window.client is not None, window
        assert window.frame.width >= window.client.width > 0, window
        assert window.frame.height >= window.client.height > 0, window

    # One window is active, and the oracle agrees with itself across readers.
    active = oracle.active_window()
    assert active is not None
    assert [w.id for w in windows if w.active] in ([active.id], [])
    single = oracle.window(dialog.id)
    assert single is not None and single.caption == "Zoo Dialog"
    assert oracle.window("{00000000-0000-0000-0000-000000000000}") is None


def test_gtk_dialog_and_utility_are_normal_transients(oracle: KWinOracle, zoo: ZooProcess) -> None:
    """KWin has no window type for Wayland xdg toplevels: a GTK3 modal dialog or a
    UTILITY-hinted window is a normal window transient for its parent (no dialog,
    utility or modal flag). Tests expecting kind 'dialog' use ``panel dialog``."""
    main_id = _of(oracle.windows(), zoo.pid)[0].id
    zoo.command("dialog", "window: Zoo Dialog")
    zoo.command("utility", "window: Zoo Utility")
    windows = _of(oracle.wait_for_windows(lambda ws: len(_of(ws, zoo.pid)) == 3), zoo.pid)
    for caption in ("Zoo Dialog", "Zoo Utility"):
        window = _captioned(windows, caption)[0]
        assert window.kind == "normal", (caption, window.flags, window.window_type)
        assert window.transient_for == main_id, window
        assert not window.flags & {"dialog", "utility", "modal"}, window.flags


@pytest.mark.parametrize(
    ("scope", "kind", "flag"),
    [("dialog", "dialog", "dialog"), ("notification", "shell", "notification")],
)
def test_layer_shell_scope_sets_the_kind(
    oracle: KWinOracle, zoo: ZooProcess, scope: str, kind: str, flag: str
) -> None:
    _require_layer_shell(zoo)
    window = _open_panel(oracle, zoo, scope)
    assert flag in window.flags, (window.flags, window.window_type)
    assert window.kind == kind, (window.flags, window.window_type)


def test_undeletable_gtk_window_stays_closeable(oracle: KWinOracle, zoo: ZooProcess) -> None:
    """``set_deletable(False)`` is only a decoration hint on Wayland: KWin still
    reports the window closeable. Non-closeable windows come from kwin_rules."""
    zoo.command("undeletable", "window: Zoo Undeletable")
    windows = oracle.wait_for_windows(
        lambda ws: bool(_captioned(_of(ws, zoo.pid), "Zoo Undeletable"))
    )
    assert _captioned(_of(windows, zoo.pid), "Zoo Undeletable")[0].closeable


def test_window_zoo_popup_is_a_popup_kind(oracle: KWinOracle, zoo: ZooProcess) -> None:
    main_id = _of(oracle.windows(), zoo.pid)[0].id
    zoo.command("popup", "window: Zoo Popup")
    windows = oracle.wait_for_windows(lambda ws: len(_of(ws, zoo.pid)) == 2)
    popup = next(w for w in _of(windows, zoo.pid) if w.id != main_id)
    assert popup.kind == "popup", (popup.caption, popup.flags, popup.window_type)


def test_kwin_oracle_scene_setters_round_trip(oracle: KWinOracle, zoo: ZooProcess) -> None:
    main = _of(oracle.windows(), zoo.pid)[0]
    assert main.frame is not None
    target = Rect(37.0, 61.0, main.frame.width, main.frame.height)
    assert oracle.set_frame(main.id, target)
    deadline = time.monotonic() + 5
    moved = oracle.window(main.id)
    while (moved is None or moved.frame is None or moved.frame.as_ints()[:2] != (37, 61)) and (
        time.monotonic() < deadline
    ):
        time.sleep(0.1)
        moved = oracle.window(main.id)
    assert moved is not None and moved.frame is not None
    assert moved.frame.as_ints()[:2] == (37, 61), moved.frame

    zoo.command("refuser", "window: Zoo Refuser")
    windows = oracle.wait_for_windows(lambda ws: bool(_captioned(_of(ws, zoo.pid), "Zoo Refuser")))
    refuser = _captioned(_of(windows, zoo.pid), "Zoo Refuser")[0]
    with_lines = len(zoo.lines)
    assert oracle.close(refuser.id)
    zoo.expect("refused-close: Zoo Refuser", start=with_lines)
    assert oracle.window(refuser.id) is not None


def test_kwin_oracle_desktops_and_cursor(live: LiveKWin, oracle: KWinOracle) -> None:
    desktops = oracle.desktops()
    assert desktops, desktops
    assert len({d.id for d in desktops}) == len(desktops)
    assert oracle.current_desktop().id in {d.id for d in desktops}
    x, y = oracle.cursor_pos()
    assert 0 <= x <= SCREEN[0] and 0 <= y <= SCREEN[1], (x, y)


def test_kwin_rules_force_closeable_and_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    # live_kwin() copies os.environ, so KWin reads its rules from this directory.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    with live_kwin() as session:
        oracle = KWinOracle(session.dbus_address)
        with ZooProcess(
            wayland_display=session.wayland_display, dbus_address=session.dbus_address
        ) as app:
            app.expect(f"window: {MAIN_CAPTION}")
            app.command("refuser", "window: Zoo Refuser")
            windows = oracle.wait_for_windows(
                lambda ws: bool(_captioned(_of(ws, app.pid), "Zoo Refuser"))
            )
            assert _captioned(_of(windows, app.pid), "Zoo Refuser")[0].closeable

            rule = WindowRule(
                description="kwin-mcp oracle self-test",
                wmclass=APP_ID,
                title="Zoo Refuser",
                closeable=False,
                position=(100, 120),
            )
            rules_file = install_rules(config_home, session.dbus_address, [rule])
            assert "closeablerule=2" in rules_file.read_text(encoding="utf-8")

            # KWin reloads the rules after its reconfigure delay and re-applies
            # them to existing windows, so wait for the effect itself.
            def ruled(ws: list[OracleWindow]) -> bool:
                matches = _captioned(_of(ws, app.pid), "Zoo Refuser")
                return bool(matches) and not matches[0].closeable

            windows = oracle.wait_for_windows(ruled)
            refuser = _captioned(_of(windows, app.pid), "Zoo Refuser")[0]
            main = _captioned(_of(windows, app.pid), MAIN_CAPTION)[0]
            assert refuser.frame is not None, refuser
            assert refuser.frame.as_ints()[:2] == (100, 120), refuser
            # A forced position also makes the window unmovable.
            assert not refuser.moveable, refuser.flags
            assert main.closeable and main.moveable, main.flags


# ── atspi_oracle ─────────────────────────────────────────────────────────


def _named(nodes: list[AtspiNode], name: str) -> list[AtspiNode]:
    return [node for node in nodes if node.name == name]


@pytest.mark.parametrize(
    ("timeout_s", "walk_duration_s", "ready_at_s", "expected_starts", "expected_sleeps"),
    [
        (15.0, 16.0, 14.0, [0.0, 16.0], []),
        (15.0, 16.0, None, [0.0, 16.0], []),
        (0.0, 16.0, None, [0.0], []),
        (15.0, 16.0, 0.0, [0.0], []),
        (15.0, 14.9, 14.95, [0.0, 15.0], [0.1]),
    ],
)
def test_atspi_wait_for_uses_a_deadline_observation(
    monkeypatch: pytest.MonkeyPatch,
    timeout_s: float,
    walk_duration_s: float,
    ready_at_s: float | None,
    expected_starts: list[float],
    expected_sleeps: list[float],
) -> None:
    """A slow stale walk cannot establish failure without a deadline observation."""
    clock = [0.0]
    starts: list[float] = []
    sleeps: list[float] = []
    observed_texts: list[str | None] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    def walk(self: AtspiOracle, app: str = "", max_depth: int = 64) -> list[AtspiNode]:
        assert app == PROBE_APP_ID
        assert max_depth == 3
        starts.append(clock[0])
        # Capture the independently scheduled entry state before the tree walk
        # completes; its result can already be stale when delivered to the poller.
        text = "ready" if ready_at_s is not None and clock[0] >= ready_at_s else "ready pending"
        clock[0] += walk_duration_s
        return [
            AtspiNode(
                app=app,
                pid=1,
                path=(0, 0),
                depth=1,
                role="text",
                name="Keyboard Target",
                description=None,
                states=frozenset(),
                screen=None,
                window=None,
                text=text,
                value=None,
                value_max=None,
                actions=(),
                child_count=0,
                error=None,
            )
        ]

    def has_ready_text(nodes: list[AtspiNode]) -> bool:
        observed_texts.extend(node.text for node in nodes)
        return any(node.name == "Keyboard Target" and node.text == "ready" for node in nodes)

    monkeypatch.setattr(
        atspi_oracle, "time", types.SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)
    )
    monkeypatch.setattr(AtspiOracle, "walk", walk)
    oracle = AtspiOracle("unix:path=/nonexistent-atspi-oracle-test-bus")
    if ready_at_s is None:
        with pytest.raises(AssertionError, match="AT-SPI tree never satisfied the predicate"):
            oracle.wait_for(has_ready_text, app=PROBE_APP_ID, max_depth=3, timeout_s=timeout_s)
        assert observed_texts == ["ready pending"] * len(expected_starts)
    else:
        nodes = oracle.wait_for(has_ready_text, app=PROBE_APP_ID, max_depth=3, timeout_s=timeout_s)
        assert [node.text for node in nodes] == ["ready"]
        assert observed_texts == ["ready pending"] * (len(expected_starts) - 1) + ["ready"]
    assert starts == pytest.approx(expected_starts)
    assert sleeps == pytest.approx(expected_sleeps)


def test_atspi_oracle_reads_the_window_zoo_tree(live: LiveKWin, zoo: ZooProcess) -> None:
    atspi = AtspiOracle(live.dbus_address)
    nodes = atspi.wait_for(lambda ns: bool(_named(ns, DEEP_BUTTON)), app=APP_ID)
    apps = [node for node in nodes if node.depth == 0]
    assert [node.name for node in apps] == [APP_ID]
    assert apps[0].pid == zoo.pid

    depths = {name: [n.depth for n in _named(nodes, name)] for name in DEEP_LABELS.values()}
    assert depths == {"Deep Label 14": [14], "Deep Label 16": [16]}, depths
    assert [n.depth for n in _named(nodes, DEEP_BUTTON)] == [40]
    assert atspi.find(app=APP_ID, name="Deep Label 16", max_depth=15) == []

    ok_label = [n for n in _named(nodes, "OK?") if n.role == "label"]
    assert len(ok_label) == 1 and ok_label[0].actions == (), ok_label
    assert len(_named(nodes, "Apply")) == 2
    ok_button = [n for n in _named(nodes, "OK") if n.role in {"push button", "button"}]
    assert len(ok_button) == 1 and ok_button[0].actions, ok_button
    assert "visible" in ok_button[0].states and "showing" in ok_button[0].states


def test_window_zoo_unicode_names_round_trip(
    live: LiveKWin, oracle: KWinOracle, zoo: ZooProcess
) -> None:
    zoo.command("unicode", f"window: {UNICODE_CAPTION}")
    windows = oracle.wait_for_windows(
        lambda ws: bool(_captioned(_of(ws, zoo.pid), UNICODE_CAPTION))
    )
    assert len(_captioned(_of(windows, zoo.pid), UNICODE_CAPTION)) == 1
    nodes = AtspiOracle(live.dbus_address).wait_for(
        lambda ns: bool(_named(ns, UNICODE_BUTTON)) and bool(_named(ns, UNICODE_LABEL)),
        app=APP_ID,
    )
    assert [n.role for n in _named(nodes, UNICODE_BUTTON)] == ["button"]
    assert [n.role for n in _named(nodes, UNICODE_LABEL)] == ["label"]


def test_atspi_oracle_finds_kcalc_seven(live: LiveKWin) -> None:
    env = {
        **os.environ,
        "WAYLAND_DISPLAY": live.wayland_display,
        "DBUS_SESSION_BUS_ADDRESS": live.dbus_address,
        "QT_QPA_PLATFORM": "wayland",
        "QT_ACCESSIBILITY": "1",
        "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
    }
    env.pop("DISPLAY", None)
    env.pop("AT_SPI_BUS_ADDRESS", None)
    kcalc = subprocess.Popen(
        ["kcalc"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        atspi = AtspiOracle(live.dbus_address)
        # KCalc (Qt) names its digit buttons in words: the '7' key is "Seven".
        nodes = atspi.wait_for(
            lambda ns: any(n.name == "Seven" and n.role == "button" for n in ns),
            app="kcalc",
            timeout_s=30,
        )
        seven = [n for n in nodes if n.name == "Seven" and n.role == "button"]
        assert len(seven) == 1, seven
        assert seven[0].app.lower().startswith("kcalc")
        assert "Press" in seven[0].actions, seven[0]
        assert seven[0].window is not None and seven[0].window[2] > 0, seven[0]
    finally:
        kcalc.terminate()
        try:
            kcalc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kcalc.kill()
            kcalc.wait(timeout=5)


# ── interaction probe raw event log (PEL) ────────────────────────────────


def _status(nodes: list[AtspiNode], prefix: str) -> str:
    names = [n.name for n in nodes if n.name is not None and n.name.startswith(prefix)]
    assert len(names) == 1, names
    return names[0]


def _read_log(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_interaction_probe_logs_a_known_injected_sequence(
    live: LiveKWin, engine: AutomationEngine, tmp_path: Path
) -> None:
    log_path = tmp_path / "probe-events.jsonl"
    env = zoo_env(os.environ, wayland_display=live.wayland_display, dbus_address=live.dbus_address)
    # The probe needs the zoo's session environment but not its layer-shell preload.
    env.pop("LD_PRELOAD", None)
    env["KWIN_MCP_PROBE_EVENT_LOG"] = str(log_path)
    probe = subprocess.Popen(
        [sys.executable, str(PROBE_SCRIPT)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        oracle = KWinOracle(live.dbus_address)
        atspi = AtspiOracle(live.dbus_address)
        windows = oracle.wait_for_windows(
            lambda ws: any(w.pid == probe.pid and w.active for w in ws)
        )
        client = next(w for w in windows if w.pid == probe.pid).client
        assert client is not None
        atspi.wait_for(
            lambda ns: any(n.name and n.name.startswith("raw_events:") for n in ns),
            app=PROBE_APP_ID,
        )

        info = engine.session_connect(
            dbus_address=live.dbus_address, wayland_display=live.wayland_display
        )
        assert info.input_backend == "eis", info
        x, y, width, height = client.as_ints()
        cx, cy = x + width // 2, y + height // 2
        engine.pointer_click(target=Point(x=cx, y=cy))
        # Longer than GTK's double-click time, so the hold is not a second click.
        time.sleep(1.2)
        engine.pointer_hold(phase="press", target=Point(x=cx, y=cy))
        engine.pointer_hold(phase="move", target=Point(x=cx + 30, y=cy))
        engine.pointer_hold(phase="release", target=Point(x=cx + 30, y=cy))
        engine.keyboard_press(keys="ctrl+shift+t")

        def settled(nodes: list[AtspiNode]) -> bool:
            raw = [n.name for n in nodes if n.name and n.name.startswith("raw_events:")]
            return bool(raw) and " kr=3 " in raw[0] and "held_mods=none" in raw[0]

        nodes = atspi.wait_for(settled, app=PROBE_APP_ID)
    finally:
        probe.terminate()
        try:
            probe.wait(timeout=5)
        except subprocess.TimeoutExpired:
            probe.kill()
            probe.wait(timeout=5)

    hold = _status(nodes, "hold_status:")
    assert "left=2/2" in hold and "held=none" in hold, hold
    motions_held = int(hold.rsplit("motions_held=", 1)[1])
    assert motions_held >= 1, hold
    raw = _status(nodes, "raw_events:")
    assert raw.startswith("raw_events: bp=2 br=2 kp=3 kr=3 "), raw
    key_log = _status(nodes, "key_log:")
    assert "Control_L=1/1" in key_log and "Shift_L=1/1" in key_log, key_log
    assert "repeats=0" in key_log, key_log

    events = _read_log(log_path)
    pointer = {"button_press", "button_release"}
    buttons = [(e["kind"], e["button"]) for e in events if e["kind"] in pointer]
    assert buttons == [
        ("button_press", "left"),
        ("button_release", "left"),
        ("button_press", "left"),
        ("button_release", "left"),
    ], buttons
    drag = [e for e in events if e["kind"] == "motion" and e["held"] == ["left"]]
    assert drag, events

    # The key chord's core fields equal the 0.11 golden captured from the same call.
    golden_path = E2E_DIR / "golden" / "key_chord_ctrl_shift_t.json"
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    keys = [e for e in events if e["kind"] in {"key_press", "key_release"}]
    assert len(keys) == len(golden["sequence"]), keys
    projected = [
        {field: e[field] for field in g} for e, g in zip(keys, golden["sequence"], strict=True)
    ]
    assert projected == golden["sequence"]
    assert [e["held_mods"] for e in keys] == [
        "ctrl",
        "ctrl+shift",
        "ctrl+shift",
        "ctrl+shift",
        "ctrl",
        "none",
    ], keys
    mono = [int(str(e["mono_ns"])) for e in events]
    assert mono == sorted(mono)
    now_ns = time.clock_gettime_ns(time.CLOCK_REALTIME)
    for event in events:
        assert 0 < now_ns - int(str(event["real_ns"])) < 600 * 10**9, event
