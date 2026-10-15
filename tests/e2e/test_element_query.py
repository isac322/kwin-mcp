"""End-to-end contracts of ``element_query``: tree, filters, depth, refs and waits.

Every expectation is checked against an oracle the test owns: ``atspi_oracle``
walks the accessibility bus in its own process, ``kwin_oracle`` reads KWin's
window list with test-owned scripts, and the session bus is watched with
``dbus-monitor`` for the worker's poll cadence. A tool's own report is never
the evidence for itself.

The element scenes come from KCalc, ``window_zoo`` and a small GTK fixture
defined below (``FIXTURE_APP``): a 500-character entry, a 30/100 scale, a button
whose only match for "zebra" is its description, a window that closes itself on
command and a burst of windows opened on command.

Deviations from the QA items, forced by the image or by the design S:

- KCalc names its 7 key ``Seven`` and Qt 6 / GTK 3 report push buttons with the
  AT-SPI role ``button``: the items' ``query:'7'`` / ``role:'push button'``
  become ``query:'seven'`` / ``role:'button'`` where they must select the key
  (``query:'7'`` is still exercised as written in QA-ELEM-005).
- Long text is cut at 200 characters with ``…`` (U+2026), the 0.11 marker the
  worker keeps, not ``...``.
- Every KWin session registers KWin itself as the AT-SPI application ``kwin``,
  so "no accessible applications" needs a test-owned compositor whose KWin runs
  without Qt accessibility (``live_kwin(accessible_kwin=False)``).
- QA-ELEM-018: S keeps 0.11's settle rule (§5.4 #37: matches reported
  ``windows-changed`` are re-polled within 1 s; an element that is not there yet
  is polled at ``poll_interval_ms``). The test therefore changes the app's KWin
  window set during the first poll of a present match and checks the 1 s re-poll
  beats ``poll_interval_ms=5000``.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from _asserts import element_count
from atspi_oracle import AtspiOracle
from kwin_oracle import KWinOracle, Rect
from session_harness import live_kwin
from window_zoo import APP_ID as ZOO_APP
from window_zoo import DEEP_BUTTON, DEEP_LABELS, MAIN_CAPTION, ZooProcess, zoo_env

from kwin_mcp.models import Locator, RefAddr, Wait, WindowSelector

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from atspi_oracle import AtspiNode

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import AppLaunchResult, ElementList, SessionInfo, UIElement

E2E_DIR = Path(__file__).resolve().parent
GUI_PROBE = E2E_DIR / "gui_probe.py"
GTK_ENV = {
    "GDK_BACKEND": "wayland",
    "GTK_MODULES": "gail:atk-bridge",
    "NO_AT_BRIDGE": "0",
    "XDG_SESSION_TYPE": "wayland",
}
LONG_DOCUMENT_LINES = 200
TEXT_LIMIT = 200
ELLIPSIS = "\u2026"
FIXTURE = "eq-fixture"
CHILDLESS = "eq-childless"
FIXTURE_CAPTION = "EQ Fixture"
VANISH_CAPTION = "EQ Vanish"
VANISH_BUTTON = "Vanish Button"
DESCRIPTION = "zebra-description-only"
# Every reason the worker gives for a null rect (0.11's codes plus the app node's own).
UNAVAILABLE_REASONS = frozenset(
    {
        "no-extents",
        "empty-extents",
        "not-a-window",
        "pid-unavailable",
        "kwin-query-failed",
        "window-set-mismatch",
        "no-kwin-window",
        "ambiguous",
        "ambiguous-window-match",
        "windows-changed",
        "unmapped",
    }
)
_REF = re.compile(r"e(\d+)\.(\d+)")
_LINE_ROLE = re.compile(r"^\s*- \[([^]]+)]")
_SCRIPT_CALL = re.compile(r"^method call time=(\d+\.\d+) .*member=(loadScript|unloadScript)$")

# A GTK 3 app for the scenes KCalc and window_zoo do not have. argv: mode, AT-SPI name.
# Commands on stdin (one per line): "vanish <ms>" closes the "EQ Vanish" window after
# <ms>; "churn <count> <ms>" opens <count> uniquely captioned windows <ms> apart;
# "labels <count>" opens "EQ Labels" holding <count> labels.
# Every event is printed as "<event> <time.monotonic()>".
FIXTURE_APP = r"""
import os
import sys
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

mode, name = sys.argv[1], sys.argv[2]
GLib.set_prgname(name)

def emit(event):
    print(f"{event} {time.monotonic():.6f}", flush=True)


def named(widget, accessible_name, description=None):
    accessible = widget.get_accessible()
    accessible.set_name(accessible_name)
    if description is not None:
        accessible.set_description(description)
    return widget


windows = {}


def track(title, window):
    windows[title] = window
    window.connect("destroy", lambda _w: emit(f"closed:{title}"))
    window.show_all()
    emit(f"window:{title}")


def build():
    # The main window maps last, so KWin activates it and the entry holds the focus.
    vanish = Gtk.Window(title="EQ Vanish")
    vanish.set_default_size(240, 120)
    vanish.add(named(Gtk.Button(label="Vanish Button"), "Vanish Button"))
    track("EQ Vanish", vanish)

    main = Gtk.Window(title="EQ Fixture")
    main.set_default_size(480, 260)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    entry = named(Gtk.Entry(), "Long Entry")
    entry.set_text("x" * 500)
    box.pack_start(entry, False, False, 0)
    adjustment = Gtk.Adjustment(
        value=30, lower=0, upper=100, step_increment=1, page_increment=10, page_size=0
    )
    scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adjustment)
    box.pack_start(named(scale, "Thirty Scale"), False, False, 0)
    described = Gtk.Button(label="Described")
    box.pack_start(named(described, "Described", "zebra-description-only"), False, False, 0)
    main.add(box)
    main.connect("destroy", lambda _w: Gtk.main_quit())

    def show_main():
        track("EQ Fixture", main)
        entry.grab_focus()
        return False

    GLib.timeout_add(300, show_main)


def churn(count, interval_ms):
    opened = [0]

    def one():
        index = opened[0]
        window = Gtk.Window(title=f"EQ Churn {index:02d}")
        window.set_default_size(160 + 7 * index, 90 + 3 * index)
        track(f"EQ Churn {index:02d}", window)
        opened[0] += 1
        if opened[0] < count:
            return True
        emit("churn-done")
        return False

    one()
    GLib.timeout_add(interval_ms, one)


def vanish(delay_ms):
    def close():
        windows.pop("EQ Vanish").destroy()
        return False

    emit("vanish-armed")
    GLib.timeout_add(delay_ms, close)


def labels(count):
    window = Gtk.Window(title="EQ Labels")
    window.set_default_size(300, 200)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    for index in range(count):
        box.pack_start(Gtk.Label(label=f"EQ Label {index:04d}"), False, False, 0)
    scrolled = Gtk.ScrolledWindow()
    scrolled.add(box)
    window.add(scrolled)
    track("EQ Labels", window)


def on_stdin(_channel, _condition):
    data = os.read(sys.stdin.fileno(), 65536)
    if not data:
        return False
    for line in data.decode().splitlines():
        command, *args = line.split()
        if command == "vanish":
            vanish(int(args[0]))
        elif command == "churn":
            churn(int(args[0]), int(args[1]))
        elif command == "labels":
            labels(int(args[0]))
        elif command == "quit":
            Gtk.main_quit()
    return True


GLib.io_add_watch(GLib.IOChannel.unix_new(sys.stdin.fileno()), GLib.IO_IN | GLib.IO_HUP, on_stdin)
if mode == "fixture":
    build()
else:
    GLib.idle_add(lambda: emit("ready") or False)
Gtk.main()
"""


# ── helpers ───────────────────────────────────────────────────────────────


@dataclass
class FixtureApp:
    """A running ``FIXTURE_APP`` with its stdin command channel and event lines."""

    process: subprocess.Popen[str]
    name: str
    lines: list[tuple[str, float]] = field(default_factory=list)
    _changed: threading.Condition = field(default_factory=threading.Condition)

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            with self._changed:
                event = line.rsplit(" ", 1)[0]
                self.lines.append((event, time.monotonic()))
                self._changed.notify_all()

    def send(self, command: str) -> int:
        """Send one command; return the event index from which its answers count."""
        with self._changed:
            start = len(self.lines)
        assert self.process.stdin is not None
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()
        return start

    def expect(self, event: str, *, start: int = 0, timeout_s: float = 15.0) -> float:
        """Wait for ``event`` (at index >= ``start``); return when the test read it."""
        deadline = time.monotonic() + timeout_s
        with self._changed:
            while True:
                for seen, at in self.lines[start:]:
                    if seen == event:
                        return at
                remaining = deadline - time.monotonic()
                assert remaining > 0 and self.process.poll() is None, (event, self.lines)
                self._changed.wait(min(remaining, 0.5))


@contextlib.contextmanager
def fixture_app(
    info: SessionInfo, script: Path, *, mode: str = "fixture", name: str = FIXTURE
) -> Generator[FixtureApp]:
    """Start ``FIXTURE_APP`` in the session (outside app_launch, to keep its stdin)."""
    env = zoo_env(os.environ, wayland_display=info.wayland_socket, dbus_address=info.dbus_address)
    env.pop("LD_PRELOAD", None)
    process = subprocess.Popen(
        [sys.executable, str(script), mode, name],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
        start_new_session=True,
    )
    app = FixtureApp(process=process, name=name)
    threading.Thread(target=app._read, daemon=True).start()
    try:
        app.expect(f"window:{FIXTURE_CAPTION}" if mode == "fixture" else "ready")
        yield app
    finally:
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


@pytest.fixture
def fixture_script(tmp_path: Path) -> Path:
    script = tmp_path / "eq_fixture.py"
    script.write_text(FIXTURE_APP, encoding="utf-8")
    return script


def _info(engine: AutomationEngine) -> SessionInfo:
    status = engine.session_info()
    assert status.session is not None, status.render()
    return status.session


def _atspi(engine: AutomationEngine) -> AtspiOracle:
    return AtspiOracle(_info(engine).dbus_address)


def _kwin(engine: AutomationEngine) -> KWinOracle:
    return KWinOracle(_info(engine).dbus_address)


def _shape(elements: list[UIElement]) -> list[tuple[int, str, str, str]]:
    return [(e.depth, e.app, e.role, e.name) for e in elements]


def _ats_shape(nodes: list[AtspiNode]) -> list[tuple[int, str, str, str]]:
    return [(n.depth, n.app, n.role or "", n.name or "") for n in nodes if n.error is None]


def _query_matches(node: AtspiNode, query: str) -> bool:
    needle = query.lower()
    return any(
        needle in (value or "").lower() for value in (node.name, node.role, node.description)
    )


def _named(found: ElementList, name: str) -> list[UIElement]:
    return [e for e in found.elements if e.name == name]


def _one_named(found: ElementList, name: str) -> UIElement:
    matches = _named(found, name)
    assert len(matches) == 1, found.render()[:2000]
    return matches[0]


def _wait_for(engine: AutomationEngine, locator: Locator, max_depth: int = 15) -> ElementList:
    found = engine.element_query(locator, max_depth=max_depth, wait=Wait(timeout_ms=15000))
    assert found.wait is not None and found.wait.satisfied, found.render()[:1000]
    return found


def _cpu_seconds(pid: int) -> float:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def _kcalc_window_id(engine: AutomationEngine) -> str:
    windows = _kwin(engine).wait_for_windows(
        lambda ws: any("kcalc" in w.resource_class.lower() for w in ws)
    )
    ids = [w.id for w in windows if "kcalc" in w.resource_class.lower()]
    assert len(ids) == 1, windows
    return ids[0]


def _launch_kwrite(
    engine: AutomationEngine,
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
    document: Path | None = None,
) -> None:
    command = "kwrite" if document is None else f"kwrite {shlex.quote(str(document))}"
    launched = launch(command)
    assert launched.pid > 0, launched
    wait_for_app("kwrite")


# ── ported 0.11 contracts ─────────────────────────────────────────────────


def test_element_query_tree_role_filter_narrows_rendered_elements(
    kcalc_session: AutomationEngine,
) -> None:
    unfiltered = kcalc_session.element_query(Locator(app="kcalc"))
    buttons_only = kcalc_session.element_query(Locator(app="kcalc", role="button"))

    assert 0 < element_count(buttons_only) < element_count(unfiltered), buttons_only.render()[:500]
    assert {e.role for e in buttons_only.elements} == {"button"}, buttons_only.render()[:500]
    rendered_roles = [
        match.group(1)
        for line in buttons_only.render().splitlines()
        if (match := _LINE_ROLE.match(line)) is not None
    ]
    assert set(rendered_roles) == {"button"}, buttons_only.render()[:500]


def test_element_query_tree_max_depth_limits_traversal(
    kcalc_session: AutomationEngine,
) -> None:
    root_only = kcalc_session.element_query(Locator(app="kcalc"), max_depth=0)
    one_level = kcalc_session.element_query(Locator(app="kcalc"), max_depth=1)

    assert [e.role for e in root_only.elements] == ["application"], root_only.render()[:500]
    assert len(one_level.elements) > 1, one_level.render()[:500]
    assert any(e.role == "frame" and e.depth == 1 for e in one_level.elements)
    assert "  - [frame]" in one_level.render(), one_level.render()[:500]
    assert max(e.depth for e in one_level.elements) == 1, one_level.render()[:500]
    assert not any(line.startswith("    - [") for line in one_level.render().splitlines()), (
        one_level.render()[:500]
    )


def test_element_query_tree_without_app_filter_reports_all_launched_apps(
    kcalc_session: AutomationEngine,
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
) -> None:
    _launch_kwrite(kcalc_session, launch, wait_for_app)

    tree = kcalc_session.element_query()
    app_names = {e.name.lower() for e in tree.elements if e.depth == 0}
    assert any("kcalc" in name for name in app_names), tree.render()[:500]
    assert any("kwrite" in name for name in app_names), tree.render()[:500]
    assert tree.render().startswith(f"# Accessibility Tree ({len(tree.elements)} elements)\n")


def test_element_query_tree_reports_nonexistent_app(
    kcalc_session: AutomationEngine,
) -> None:
    app = "definitely-not-an-accessible-app"
    output = kcalc_session.element_query(Locator(app=app))

    # 1.0: a non-empty locator gets the "No elements found" header (S §2 One renderer).
    assert output.elements == []
    assert output.render() == f"No elements found matching app='{app}'"


def test_element_query_targeted_query_returns_equals_button(
    kcalc_session: AutomationEngine,
) -> None:
    all_elements = kcalc_session.element_query(Locator(app="kcalc", query=""))
    equals_elements = kcalc_session.element_query(Locator(app="kcalc", query="Equals"))

    assert 0 < element_count(equals_elements) < element_count(all_elements)
    assert any(e.name == "Equals" for e in equals_elements.elements), equals_elements.render()
    element_lines = [
        line.strip() for line in equals_elements.render().splitlines() if "- [" in line
    ]
    assert any(re.match(r'^- \[[^]]+] "Equals"(?: |$)', line) for line in element_lines), (
        equals_elements.render()[:500]
    )


def test_element_query_focused_state_is_strict_subset(
    kcalc_session: AutomationEngine,
) -> None:
    all_elements = kcalc_session.element_query(Locator(app="kcalc", query=""))
    focused_elements = kcalc_session.element_query(Locator(app="kcalc", states=["focused"]))

    assert 0 < element_count(focused_elements) < element_count(all_elements), (
        focused_elements.render()[:500]
    )
    assert all("focused" in e.states for e in focused_elements.elements)


def test_element_query_reports_nonexistent_query(
    kcalc_session: AutomationEngine,
) -> None:
    query = "definitely-not-a-real-kcalc-element"
    output = kcalc_session.element_query(Locator(app="kcalc", query=query))

    assert output.elements == []
    assert output.render() == f"No elements found matching query='{query}', app='kcalc'"


def test_element_query_requires_every_requested_state(
    kcalc_session: AutomationEngine,
) -> None:
    _wait_for(kcalc_session, Locator(app="kcalc", states=["focused", "focusable"]))
    focused = kcalc_session.element_query(Locator(app="kcalc", states=["focused"]))
    focusable = kcalc_session.element_query(Locator(app="kcalc", states=["focusable"]))
    focused_and_focusable = kcalc_session.element_query(
        Locator(app="kcalc", states=["focused", "focusable"])
    )

    combined_count = element_count(focused_and_focusable)
    assert 0 < combined_count <= element_count(focused), focused_and_focusable.render()[:500]
    assert combined_count <= element_count(focusable), focused_and_focusable.render()[:500]
    assert all({"focused", "focusable"} <= set(e.states) for e in focused_and_focusable.elements)
    assert "matching states=['focused', 'focusable']" in focused_and_focusable.render()


def test_element_query_wait_succeeds_when_expected_states_are_present(
    kcalc_session: AutomationEngine,
) -> None:
    output = kcalc_session.element_query(
        Locator(app="kcalc", states=["focused", "focusable"]),
        wait=Wait(timeout_ms=1000, poll_interval_ms=50),
    )

    assert output.wait is not None and output.wait.satisfied, output.render()[:500]
    assert element_count(output) > 0
    text = output.render()
    assert "matching states=['focused', 'focusable']" in text
    assert re.fullmatch(r"Wait satisfied \(until=present\) after \d+ ms", text.splitlines()[-1]), (
        text[-200:]
    )


@pytest.mark.qa("QA-ELEM-010")
def test_list_windows_superset_at_max_depth_one(
    kcalc_session: AutomationEngine,
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
) -> None:
    """``max_depth=1`` carries everything 0.11's ``list_windows`` printed, and more."""
    _launch_kwrite(kcalc_session, launch, wait_for_app)
    kwin = _kwin(kcalc_session)
    kwin.wait_for_windows(lambda ws: any("kwrite" in w.resource_class.lower() for w in ws))

    windows = kcalc_session.element_query(max_depth=1)
    apps = [e for e in windows.elements if e.depth == 0]
    assert any("kcalc" in e.name.lower() for e in apps), windows.render()[:800]
    assert any("kwrite" in e.name.lower() for e in apps), windows.render()[:800]

    ats = _atspi(kcalc_session).walk(max_depth=1)
    ats_children = {
        n.name: [c.name or "" for c in ats if c.depth == 1 and c.path[0] == n.path[0]]
        for n in ats
        if n.depth == 0
    }
    groups: dict[str, list[UIElement]] = {}
    current = ""
    for element in windows.elements:
        if element.depth == 0:
            current = element.name
            groups[current] = []
        else:
            groups[current].append(element)
    for app in apps:
        if not any(name in app.name.lower() for name in ("kcalc", "kwrite")):
            continue
        tops = groups[app.name]
        # 0.11's "(N windows)" is the number of top-levels; 1.0 carries it as child_count.
        assert len(tops) >= 1 and app.child_count == len(tops), windows.render()[:800]
        assert [t.name for t in tops] == ats_children[app.name], (tops, ats_children)
        for top in tops:
            assert top.window_id is not None, top
    active = kwin.active_window()
    assert active is not None
    marked = [e for e in windows.elements if e.depth == 1 and "active" in e.states]
    assert [e.window_id for e in marked] == [active.id], windows.render()[:800]


def test_element_query_depth_one_reports_when_no_accessible_apps_exist(
    engine: AutomationEngine,
) -> None:
    with live_kwin(accessible_kwin=False) as live:
        info = engine.session_connect(
            dbus_address=live.dbus_address, wayland_display=live.wayland_display
        )
        assert info.kind == "live", info.render()
        try:
            assert _atspi(engine).applications() == []
            output = engine.element_query(max_depth=1)
            assert output.elements == []
            assert output.render() == "(no accessible applications found)"
        finally:
            engine.session_stop()


def test_element_query_wait_longer_than_the_atspi_request_timeout_times_out_softly(
    kcalc_session: AutomationEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wait longer than the per-request bound returns its own timeout, not a worker kill."""
    from kwin_mcp import core

    monkeypatch.setattr(core, "_ATSPI_TIMEOUT_S", 1.0)
    generation = kcalc_session._atspi_generation

    result = kcalc_session.element_query(Locator(query="zzz"), wait=Wait(timeout_ms=2500))

    assert result.wait is not None and not result.wait.satisfied, result.render()
    assert result.wait.elapsed_ms >= 2500, result.wait
    assert result.render().startswith("NOT SATISFIED: Timeout after 2500 ms"), result.render()
    assert kcalc_session._atspi_generation == generation


@pytest.mark.qa("QA-ELEM-008")
def test_scrollbar_value_and_value_max(
    kcalc_session: AutomationEngine,
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
    fixture_script: Path,
    tmp_path: Path,
) -> None:
    """Text is capped at 200 characters; Value and Action are reported as AT-SPI has them."""
    document = tmp_path / "kwin-mcp-scroll.txt"
    document.write_text(
        "\n".join(f"line {i:03d} ---------------------------" for i in range(LONG_DOCUMENT_LINES)),
        encoding="utf-8",
    )
    _launch_kwrite(kcalc_session, launch, wait_for_app, document)
    atspi = _atspi(kcalc_session)
    # The scroll range is published once the editor has laid the document out.
    atspi.wait_for(
        lambda ns: any(n.role == "scroll bar" and (n.value_max or 0) > 0 for n in ns),
        app="kwrite",
        max_depth=15,
    )
    elements = kcalc_session.element_query(Locator(app="kwrite", role="scroll bar"))
    scrollbars = [e for e in elements.elements if e.value is not None and e.value_max]
    assert scrollbars, elements.render()[:800]
    ats_values = {
        (n.value, n.value_max)
        for n in atspi.walk("kwrite", 15)
        if n.role == "scroll bar" and n.value is not None
    }
    assert {(e.value, e.value_max) for e in elements.elements} <= ats_values, ats_values
    usable = [e for e in scrollbars if e.rect is not None and e.value_max and e.value_max > 0]
    assert usable, elements.render()[:800]
    assert re.search(r"\[scroll bar\][^\n]*? value=\d+(?:\.\d+)?/\d+", elements.render())

    with fixture_app(_info(kcalc_session), fixture_script):
        found = _wait_for(kcalc_session, Locator(app=FIXTURE, query="Thirty Scale"))
        fixture_elements = kcalc_session.element_query(Locator(app=FIXTURE))
        nodes = atspi.walk(FIXTURE, 15)

        entry = _one_named(fixture_elements, "Long Entry")
        (entry_node,) = [n for n in nodes if n.name == "Long Entry"]
        assert entry_node.text == "x" * 500, entry_node.text
        assert entry.text == "x" * TEXT_LIMIT + ELLIPSIS, entry.text
        scale = _one_named(found, "Thirty Scale")
        (scale_node,) = [n for n in nodes if n.name == "Thirty Scale"]
        assert (scale.value, scale.value_max) == (30.0, 100.0), scale
        assert (scale_node.value, scale_node.value_max) == (30.0, 100.0), scale_node
        assert "value=30/100" in found.render(), found.render()
        assert _shape(fixture_elements.elements) == _ats_shape(nodes)
        assert [e.actions for e in fixture_elements.elements] == [
            list(n.actions) for n in nodes if n.error is None
        ]


# ── QA items ──────────────────────────────────────────────────────────────


@pytest.mark.qa("QA-ELEM-001")
def test_empty_locator_is_full_tree_with_tree_header(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
) -> None:
    info = start_session()
    launch(f"python3 {shlex.quote(str(GUI_PROBE))}", env=GTK_ENV)
    atspi = AtspiOracle(info.dbus_address)
    atspi.wait_for(lambda ns: any(n.name == "Hover Target" for n in ns), app="probe")

    tree = engine.element_query()
    nodes = atspi.walk(max_depth=15)

    assert [e.name for e in tree.elements if e.depth == 0] == [
        n.name for n in nodes if n.depth == 0
    ]
    probe = [e for e in tree.elements if "probe" in e.app.lower()]
    assert probe and probe[0].depth == 0 and probe[0].role == "application"
    assert _shape(probe) == _ats_shape([n for n in nodes if "probe" in n.app.lower()])

    text = tree.render()
    lines = text.splitlines()
    assert lines[0] == f"# Accessibility Tree ({len(tree.elements)} elements)", lines[0]
    assert lines[1] == ""
    body = lines[2:]
    assert len(body) == len(tree.elements)
    for line, element in zip(body, tree.elements, strict=True):
        indent = "  " * element.depth
        assert line.startswith(f"{indent}- [{element.role}] "), (line, element)
        if element.rect is None:
            assert f" @ unavailable ({element.unavailable})" in line, line
        else:
            r = element.rect
            assert f" @ screen ({r.x}, {r.y}, {r.width}x{r.height})" in line, line
        if element.states:
            assert f" ({', '.join(element.states)}) @ " in line, line


@pytest.mark.qa("QA-ELEM-002")
def test_max_depth_zero_returns_application_nodes_only(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    info = start_session()
    with ZooProcess(wayland_display=info.wayland_socket, dbus_address=info.dbus_address) as zoo:
        zoo.expect(f"window: {MAIN_CAPTION}")
        atspi = AtspiOracle(info.dbus_address)
        nodes = atspi.wait_for(lambda ns: any(n.name == DEEP_BUTTON for n in ns), app=ZOO_APP)
        depth_of = {n.name: n.depth for n in nodes if n.name}
        apps = [n.name for n in atspi.applications()]

        zero = engine.element_query(max_depth=0)
        negative = engine.element_query(max_depth=-3)
        assert _shape(zero.elements) == _shape(negative.elements)
        assert [e.name for e in zero.elements] == apps
        assert {(e.depth, e.role) for e in zero.elements} == {(0, "application")}

        one = engine.element_query(max_depth=1)
        assert {e.depth for e in one.elements} == {0, 1}, one.render()[:800]
        assert [e.name for e in one.elements if e.depth == 0] == apps
        assert any(e.app == ZOO_APP and e.name == MAIN_CAPTION for e in one.elements)

        ok_depth = depth_of["OK"]
        above = engine.element_query(Locator(app=ZOO_APP, query="OK"), max_depth=ok_depth - 1)
        at = engine.element_query(Locator(app=ZOO_APP, query="OK"), max_depth=ok_depth)
        assert not _named(above, "OK"), above.render()
        assert [e.depth for e in _named(at, "OK")] == [ok_depth], at.render()

        label_14, label_16 = DEEP_LABELS.values()
        assert (depth_of[label_14], depth_of[label_16]) == (14, 16)
        default = engine.element_query(Locator(app=ZOO_APP, query="Deep Label"))
        assert [e.name for e in default.elements] == [label_14], default.render()
        deeper = engine.element_query(Locator(app=ZOO_APP, query="Deep Label"), max_depth=20)
        assert [e.name for e in deeper.elements] == [label_14, label_16], deeper.render()
        assert not engine.element_query(Locator(app=ZOO_APP, query=DEEP_BUTTON)).elements
        deepest = engine.element_query(Locator(app=ZOO_APP, query=DEEP_BUTTON), max_depth=64)
        assert [e.depth for e in deepest.elements] == [depth_of[DEEP_BUTTON]] == [40]


@pytest.mark.qa("QA-ELEM-003")
def test_app_query_role_case_insensitive_states_exact_case_sensitive(
    kcalc_session: AutomationEngine,
) -> None:
    upper = kcalc_session.element_query(Locator(app="KCAL"))
    lower = kcalc_session.element_query(Locator(app="kcalc"))
    nodes = _atspi(kcalc_session).walk("kcal", 15)

    assert element_count(upper) > 1
    assert {e.app for e in upper.elements} == {"kcalc"}, upper.render()[:500]
    assert _shape(upper.elements) == _shape(lower.elements) == _ats_shape(nodes)


@pytest.mark.qa("QA-ELEM-004")
def test_role_exact_not_substring(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    info = start_session()
    with ZooProcess(wayland_display=info.wayland_socket, dbus_address=info.dbus_address) as zoo:
        zoo.expect(f"window: {MAIN_CAPTION}")
        atspi = AtspiOracle(info.dbus_address)
        nodes = atspi.wait_for(lambda ns: any(n.name == DEEP_BUTTON for n in ns), app=ZOO_APP)
        buttons = [n for n in nodes if n.role == "button"]
        # Every button sits below non-button containers (frame, fillers) the filter hides.
        assert buttons and min(n.depth for n in buttons) >= 3

        found = engine.element_query(Locator(app=ZOO_APP, role="BUTTON"), max_depth=64)
        assert _shape(found.elements) == _ats_shape(buttons), found.render()[:800]
        for partial in ("butto", "utton", "push", "push button", "BUTTONS"):
            assert engine.element_query(Locator(app=ZOO_APP, role=partial)).elements == [], partial


@pytest.mark.qa("QA-ELEM-005")
def test_query_substring_name_role_description(
    kcalc_session: AutomationEngine, fixture_script: Path
) -> None:
    atspi = _atspi(kcalc_session)
    with fixture_app(_info(kcalc_session), fixture_script):
        _wait_for(kcalc_session, Locator(app=FIXTURE, query="Described"))

        seven = kcalc_session.element_query(Locator(app="kcalc", query="7"))
        kcalc_nodes = atspi.walk("kcalc", 15)
        expected = [n for n in kcalc_nodes if _query_matches(n, "7")]
        # KCalc's '7' matches only through descriptions (its key is named "Seven").
        assert expected and all("7" not in (n.name or "") for n in expected)
        assert _shape(seven.elements) == _ats_shape(expected), seven.render()[:800]
        assert [e.description for e in seven.elements] == [n.description for n in expected]

        empty = kcalc_session.element_query(Locator(app="kcalc", query=""))
        unset = kcalc_session.element_query(Locator(app="kcalc"))
        assert _shape(empty.elements) == _shape(unset.elements) == _ats_shape(kcalc_nodes)

        by_description = kcalc_session.element_query(Locator(query="ZEBRA-DESC"))
        described = _one_named(by_description, "Described")
        assert by_description.elements == [described], by_description.render()
        assert described.description == DESCRIPTION
        assert "zebra" not in described.name.lower() and "zebra" not in described.role

        by_role = kcalc_session.element_query(Locator(app=FIXTURE, query="SLIDE"))
        assert [(e.role, e.name) for e in by_role.elements] == [("slider", "Thirty Scale")]
        by_name = kcalc_session.element_query(Locator(app=FIXTURE, query="thirty"))
        assert [e.name for e in by_name.elements] == ["Thirty Scale"]


@pytest.mark.qa("QA-ELEM-006")
def test_states_exact_case_sensitive_and_fields_anded(
    kcalc_session: AutomationEngine, fixture_script: Path
) -> None:
    atspi = _atspi(kcalc_session)
    with fixture_app(_info(kcalc_session), fixture_script):
        found = _wait_for(kcalc_session, Locator(states=["focused", "editable"]))
        expected = [n for n in atspi.walk(max_depth=15) if {"focused", "editable"} <= n.states]
        assert [e.name for e in found.elements] == [n.name for n in expected] == ["Long Entry"]
        assert kcalc_session.element_query(Locator(states=["FOCUSED"])).elements == []

    seven = kcalc_session.element_query(
        Locator(app="KCALC", query="SEVEN", role="BUTTON", states=["focusable"])
    )
    (node,) = atspi.find(app="kcalc", name="Seven", role="button", max_depth=15)
    assert "focusable" in node.states
    assert [(e.app, e.role, e.name) for e in seven.elements] == [("kcalc", "button", "Seven")]
    shouted = Locator(app="KCALC", query="SEVEN", role="BUTTON", states=["FOCUSABLE"])
    assert kcalc_session.element_query(shouted).elements == []

    anded = kcalc_session.element_query(Locator(app="kcalc", role="button", query="seven"))
    assert [(e.depth, e.name) for e in anded.elements] == [(node.depth, "Seven")]
    # The '7' descriptions belong to check boxes: AND with role=button leaves nothing.
    assert (
        kcalc_session.element_query(Locator(app="kcalc", role="button", query="7")).elements == []
    )


@pytest.mark.qa("QA-ELEM-007")
def test_global_rects_and_unavailable_reasons(
    kcalc_session: AutomationEngine, fixture_script: Path
) -> None:
    atspi = _atspi(kcalc_session)
    kwin = _kwin(kcalc_session)
    with fixture_app(_info(kcalc_session), fixture_script) as app:
        windows = kwin.wait_for_windows(
            lambda ws: any(w.pid == app.process.pid and w.caption == FIXTURE_CAPTION for w in ws)
        )
        window = next(w for w in windows if w.caption == FIXTURE_CAPTION)
        assert window.frame is not None
        moved = Rect(300, 200, window.frame.width, window.frame.height)
        assert kwin.set_frame(window.id, moved)
        kwin.wait_for_windows(
            lambda ws: any(
                w.id == window.id and w.frame is not None and w.frame.as_ints()[:2] == (300, 200)
                for w in ws
            )
        )
        placed = kwin.window(window.id)
        assert placed is not None and placed.client is not None
        cx, cy, _, _ = placed.client.as_ints()

        found = kcalc_session.element_query(Locator(app=FIXTURE))
        nodes = [n for n in atspi.walk(FIXTURE, 15) if n.error is None]
        assert _shape(found.elements) == _ats_shape(nodes)
        (frame,) = [n for n in nodes if n.depth == 1 and n.name == FIXTURE_CAPTION]
        checked = 0
        for element, node in zip(found.elements, nodes, strict=True):
            # Only the moved window: the fixture's other window keeps its own origin.
            if node.path[:2] != frame.path or node.window is None:
                continue
            assert element.rect is not None, element
            wx, wy, width, height = node.window
            box = (element.rect.x, element.rect.y, element.rect.width, element.rect.height)
            assert box == (cx + wx, cy + wy, width, height), (element, node, placed)
            checked += 1
        assert checked >= 4, found.render()

    kcalc = kcalc_session.element_query(Locator(app="kcalc"))
    kcalc_nodes = [n for n in atspi.walk("kcalc", 15) if n.error is None]
    assert _shape(kcalc.elements) == _ats_shape(kcalc_nodes)
    text = kcalc.render()
    hidden = 0
    for element, node in zip(kcalc.elements, kcalc_nodes, strict=True):
        if node.depth > 0 and node.window is None:
            assert element.rect is None, (element, node)
            assert element.unavailable in UNAVAILABLE_REASONS, element
            assert f" @ unavailable ({element.unavailable})" in text
            hidden += 1
        if element.rect is None:
            assert element.unavailable in UNAVAILABLE_REASONS, element
    # KCalc keeps its bit check boxes and mode labels unshown (empty extents).
    assert hidden > 0, text[:1500]


@pytest.mark.qa("QA-ELEM-009")
def test_elements_carry_window_id_matching_window_list(
    kcalc_session: AutomationEngine,
) -> None:
    window_id = _kcalc_window_id(kcalc_session)

    found = kcalc_session.element_query(Locator(app="kcalc", query="seven"))
    seven = _one_named(found, "Seven")
    (node,) = _atspi(kcalc_session).find(app="kcalc", name="Seven", max_depth=15)

    match = _REF.fullmatch(seven.ref)
    assert match is not None, seven.ref
    assert int(match.group(1)) == kcalc_session._atspi_generation
    assert seven.window_id == window_id
    assert window_id in [w.id for w in kcalc_session.window_list().windows]
    assert seven.app == "kcalc"
    assert seven.child_count == node.child_count
    assert f"window={window_id}" in found.render()


@pytest.mark.qa("QA-ELEM-010")
def test_childless_apps_appear_with_child_count_zero(
    kcalc_session: AutomationEngine, fixture_script: Path
) -> None:
    info = _info(kcalc_session)
    atspi = _atspi(kcalc_session)
    window_id = _kcalc_window_id(kcalc_session)
    with (
        fixture_app(info, fixture_script, mode="childless", name=CHILDLESS),
        fixture_app(info, fixture_script, mode="childless", name="") as unnamed,
    ):
        atspi.wait_for(
            lambda ns: (
                any(n.name == CHILDLESS for n in ns)
                and any(n.pid == unnamed.process.pid for n in ns)
            ),
            max_depth=0,
        )
        found = kcalc_session.element_query(max_depth=1)
        apps = atspi.applications()

    roots = [e for e in found.elements if e.depth == 0]
    assert [(e.name, e.child_count) for e in roots] == [(n.name, n.child_count) for n in apps]
    text_lines = found.render().splitlines()[2:]
    index = next(i for i, e in enumerate(found.elements) if e.name == CHILDLESS)
    childless = found.elements[index]
    assert childless.child_count == 0 and childless.role == "application"
    assert index + 1 == len(found.elements) or found.elements[index + 1].depth == 0
    assert "children=0" in text_lines[index], text_lines[index]

    (pid_node,) = [n for n in apps if n.pid == unnamed.process.pid]
    assert pid_node.name == ""
    index = next(
        i for i, e in enumerate(roots) if (e.name, e.child_count) == ("", pid_node.child_count)
    )
    nameless = roots[index]
    assert nameless.name == "" and nameless.child_count == 0
    line = text_lines[found.elements.index(nameless)]
    assert line.startswith('- [application] "(unnamed)"'), line

    (frame,) = [e for e in found.elements if e.depth == 1 and e.app == "kcalc"]
    assert frame.window_id == window_id
    # KCalc names its frame "" on Debian's Qt and "KCalc" on newer ones: the
    # independent walk decides, and an empty name renders as "(untitled)".
    (oracle_frame,) = [n for n in atspi.walk("kcalc", max_depth=1) if n.depth == 1]
    assert frame.name == (oracle_frame.name or "")
    shown = f'"{frame.name}"' if frame.name else '"(untitled)"'
    assert f"  - [frame] {shown}" in text_lines[found.elements.index(frame)]
    active = _kwin(kcalc_session).active_window()
    for top in (e for e in found.elements if e.depth == 1 and e.window_id is not None):
        assert ("active" in top.states) == (active is not None and active.id == top.window_id)


@pytest.mark.qa("QA-ELEM-011")
def test_empty_results_texts(engine: AutomationEngine) -> None:
    with live_kwin(accessible_kwin=False) as live:
        engine.session_connect(dbus_address=live.dbus_address, wayland_display=live.wayland_display)
        try:
            empty = engine.element_query()
            assert _atspi(engine).applications() == []
            assert empty.elements == []
            assert empty.render() == "(no accessible applications found)"

            launched = engine.app_launch("kcalc")
            _wait_for(engine, Locator(app="kcalc", query="seven"))
            missing = engine.element_query(Locator(query="zzz-no-match"))
            assert missing.elements == []
            assert missing.render() == "No elements found matching query='zzz-no-match'"
            os.kill(launched.pid, signal.SIGTERM)
        finally:
            engine.session_stop()


@pytest.mark.qa("QA-ELEM-013")
def test_wait_present_satisfied(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
) -> None:
    info = start_session()
    launch("kcalc")
    started = time.monotonic()
    found = engine.element_query(Locator(app="kcalc", query="seven"), wait=Wait(timeout_ms=15000))
    wall_ms = (time.monotonic() - started) * 1000

    assert found.wait is not None and found.wait.satisfied, found.render()[:500]
    assert 0 <= found.wait.elapsed_ms < 15000
    assert found.wait.elapsed_ms <= wall_ms
    assert [e.name for e in found.elements if e.role == "button"] == ["Seven"]
    assert AtspiOracle(info.dbus_address).find(app="kcalc", name="Seven", role="button")


@pytest.mark.qa("QA-ELEM-014")
def test_wait_timeout_is_not_satisfied_data_not_error(
    kcalc_session: AutomationEngine,
) -> None:
    started = time.monotonic()
    never = kcalc_session.element_query(Locator(query="never"), wait=Wait(timeout_ms=500))
    wall_s = time.monotonic() - started

    assert never.wait is not None and not never.wait.satisfied
    assert never.wait.elapsed_ms >= 500 and wall_s >= 0.5
    # A15: upper bound 3x the stated value.
    assert wall_s <= 1.5, wall_s
    assert never.elements == []
    first = never.render().splitlines()[0]
    assert re.fullmatch(
        r"NOT SATISFIED: Timeout after 500 ms \(until=present, elapsed \d+ ms\)", first
    ), first

    # SPEC-GAP-9: the elements are the full Locator's matches, states included.
    unchecked = Locator(app="kcalc", query="seven", states=["checked"])
    checked = kcalc_session.element_query(unchecked, wait=Wait(timeout_ms=3000))
    assert checked.wait is not None and not checked.wait.satisfied
    assert checked.wait.elapsed_ms >= 3000
    assert checked.elements == []
    assert checked.render().startswith("NOT SATISFIED: Timeout after 3000 ms")
    (node,) = _atspi(kcalc_session).find(app="kcalc", name="Seven", max_depth=15)
    assert "checked" not in node.states


@pytest.mark.qa("QA-ELEM-015")
def test_wait_defaults(kcalc_session: AutomationEngine) -> None:
    """Defaults: 5000 ms total, a poll every 200 ms.

    Each poll snapshots KWin before and after its walk (one loadScript ... unloadScript
    script each), so the idle time between polls is the gap from a poll's last
    unloadScript to the next poll's first loadScript.
    """
    info = _info(kcalc_session)
    kcalc_session.element_query(Locator(app="kcalc", query="seven"))  # worker up
    monitor = subprocess.Popen(
        [
            "dbus-monitor",
            "--address",
            info.dbus_address,
            "type='method_call',interface='org.kde.kwin.Scripting',member='loadScript'",
            "type='method_call',interface='org.kde.kwin.Scripting',member='unloadScript'",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    lines: list[str] = []
    stdout = monitor.stdout
    assert stdout is not None
    reader = threading.Thread(
        target=lambda: lines.extend(iter(stdout.readline, "")),
        daemon=True,
    )
    reader.start()
    try:
        deadline = time.monotonic() + 10
        while not lines and time.monotonic() < deadline:
            time.sleep(0.05)
        # dbus-monitor prints its NameAcquired/NameLost preamble once it is attached.
        assert lines, "dbus-monitor never started"
        time.sleep(0.5)
        started = time.monotonic()
        result = kcalc_session.element_query(Locator(query="never"), wait=Wait())
        wall_s = time.monotonic() - started
        time.sleep(0.3)
    finally:
        monitor.terminate()
        monitor.wait(timeout=5)
        reader.join(timeout=5)

    assert result.wait is not None and not result.wait.satisfied
    assert result.wait.timeout_ms == 5000
    assert 4500 <= result.wait.elapsed_ms <= 15000, result.wait
    assert 4.5 <= wall_s <= 15.0, wall_s
    calls = [
        (match.group(2), float(match.group(1)))
        for line in lines
        if (match := _SCRIPT_CALL.match(line.strip())) is not None
    ]
    # One poll = two snapshots = load, unload, load, unload.
    members = [member for member, _ in calls]
    assert len(calls) % 4 == 0 and len(calls) >= 12, lines[:20]
    assert members == ["loadScript", "unloadScript"] * (len(calls) // 2), members
    idle_ms = [(calls[i + 1][1] - calls[i][1]) * 1000 for i in range(3, len(calls) - 1, 4)]
    # A15: the stated 200 ms minus 10 %, at most 3x. The last idle ends at the
    # deadline instead, so it may be shorter.
    assert len(idle_ms) >= 2, idle_ms
    for gap in idle_ms[:-1]:
        assert 180 <= gap <= 600, idle_ms
    assert 0 <= idle_ms[-1] <= 600, idle_ms


@pytest.mark.qa("QA-ELEM-016")
def test_wait_absent_after_window_closes(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo], fixture_script: Path
) -> None:
    info = start_session()
    atspi = AtspiOracle(info.dbus_address)
    with fixture_app(info, fixture_script) as app:
        _wait_for(engine, Locator(app=FIXTURE, query=VANISH_BUTTON))

        missing = engine.element_query(
            Locator(app=FIXTURE, query="nothing-like-this"),
            wait=Wait(until="absent", timeout_ms=2000),
        )
        assert missing.wait is not None and missing.wait.satisfied
        assert missing.wait.elapsed_ms < 1000, missing.wait
        assert missing.elements == []

        start = app.send("vanish 700")
        armed_at = app.expect("vanish-armed", start=start)
        called_at = time.monotonic()
        gone = engine.element_query(
            Locator(app=FIXTURE, query=VANISH_BUTTON), wait=Wait(until="absent", timeout_ms=2000)
        )
        assert gone.wait is not None and gone.wait.satisfied, gone.render()
        lead_ms = (called_at - armed_at) * 1000
        assert gone.wait.elapsed_ms >= (700 - lead_ms) * 0.9, (gone.wait, lead_ms)
        assert gone.wait.elapsed_ms < 2000, gone.wait
        assert gone.elements == []
        app.expect(f"closed:{VANISH_CAPTION}", start=start)
        assert not atspi.find(app=FIXTURE, name=VANISH_BUTTON, max_depth=15)

        stays = engine.element_query(
            Locator(app=FIXTURE, query="Described"), wait=Wait(until="absent", timeout_ms=1000)
        )
        assert stays.wait is not None and not stays.wait.satisfied
        assert stays.wait.elapsed_ms >= 1000
        assert stays.render().startswith("NOT SATISFIED: Timeout after 1000 ms (until=absent")
        assert [e.name for e in stays.elements] == ["Described"]
        assert atspi.find(app=FIXTURE, name="Described", max_depth=15)


@pytest.mark.qa("QA-ELEM-017")
def test_timeout_ms_zero_polls_once(
    kcalc_session: AutomationEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    kcalc_session.element_query(Locator(app="kcalc", query="seven"))  # worker up
    run_atspi = kcalc_session._run_atspi
    requests: list[str] = []

    def counting(op: str, **kwargs: Any) -> dict[str, Any]:
        requests.append(op)
        return run_atspi(op, **kwargs)

    monkeypatch.setattr(kcalc_session, "_run_atspi", counting)
    present = Locator(app="kcalc", query="seven", role="button")
    absent = Locator(app="kcalc", query="never")
    for timeout_ms in (0, -5, -999999):
        for locator, satisfied in ((present, True), (absent, False)):
            requests.clear()
            started = time.monotonic()
            result = kcalc_session.element_query(locator, wait=Wait(timeout_ms=timeout_ms))
            wall_s = time.monotonic() - started
            assert requests == ["wait"], requests
            assert result.wait is not None and result.wait.satisfied is satisfied, result.render()
            assert result.wait.timeout_ms == timeout_ms
            assert wall_s < 1.0 and result.wait.elapsed_ms < 1000, (timeout_ms, wall_s)
            assert bool(result.elements) is satisfied
            if not satisfied:
                assert result.render().startswith(
                    f"NOT SATISFIED: Timeout after {timeout_ms} ms"
                ), result.render()

    worker = kcalc_session._atspi_proc
    assert worker is not None
    cpu_before = _cpu_seconds(worker.pid) + _cpu_seconds(os.getpid())
    started = time.monotonic()
    result = kcalc_session.element_query(absent, wait=Wait(timeout_ms=300, poll_interval_ms=-53))
    wall_s = time.monotonic() - started
    cpu_s = _cpu_seconds(worker.pid) + _cpu_seconds(os.getpid()) - cpu_before
    assert result.wait is not None and not result.wait.satisfied
    # A15: at most 3x the stated 300 ms (the last poll may run past the deadline).
    assert result.wait.elapsed_ms >= 300 and wall_s <= 0.9, (result.wait, wall_s)
    assert cpu_s < 0.5 * wall_s, (cpu_s, wall_s)


@pytest.mark.qa("QA-ELEM-018")
def test_windows_changed_repoll(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo], fixture_script: Path
) -> None:
    info = start_session()
    target = Locator(app=FIXTURE, query="Described")
    with fixture_app(info, fixture_script) as app:
        # 200 extra labels stretch each walk, so the window burst below lands between
        # the walk's two KWin snapshots.
        app.expect("window:EQ Labels", start=app.send("labels 200"))
        _wait_for(engine, Locator(app=FIXTURE, query="EQ Label 0199"))
        _wait_for(engine, target)
        walk_started = time.monotonic()
        settled = engine.element_query(target)
        walk_ms = (time.monotonic() - walk_started) * 1000
        assert _one_named(settled, "Described").rect is not None, settled.render()

        start = app.send("churn 15 20")
        app.expect("window:EQ Churn 01", start=start)
        result = engine.element_query(target, wait=Wait(timeout_ms=5000, poll_interval_ms=5000))
        app.expect("churn-done", start=start)

    assert result.wait is not None and result.wait.satisfied, result.render()
    # The first poll saw the app's window set change, so it re-polled after the 1 s
    # settle instead of returning at once, and well before the 5 s poll interval.
    assert result.wait.elapsed_ms >= 900, (result.wait, walk_ms)
    assert result.wait.elapsed_ms < 3000, (result.wait, walk_ms)
    (described,) = result.elements
    assert described.name == "Described"
    # The re-poll found the window set stable again and placed the element.
    assert described.rect is not None, described


@pytest.mark.qa("QA-ELEM-024")
def test_element_query_on_live_session(engine: AutomationEngine) -> None:
    with live_kwin() as live:
        info = engine.session_connect(
            dbus_address=live.dbus_address,
            wayland_display=live.wayland_display,
            enable_accessibility=True,
        )
        assert info.kind == "live", info.render()
        launched = engine.app_launch("kcalc")
        try:
            atspi = AtspiOracle(live.dbus_address)
            _wait_for(engine, Locator(app="kcalc", query="seven", role="button"))
            seven_desc = engine.element_query(Locator(app="kcalc", query="7"))
            nodes = atspi.walk("kcalc", 15)
            expected = [n for n in nodes if _query_matches(n, "7")]
            assert expected
            assert _shape(seven_desc.elements) == _ats_shape(expected)

            found = engine.element_query(Locator(app="kcalc", query="seven", role="button"))
            seven = _one_named(found, "Seven")
            assert seven.rect is not None and seven.window_id is not None, seven
            assert seven.window_id in [w.id for w in live.oracle().windows()]
            acted = engine.element_act(RefAddr(ref=seven.ref), "Press")
            assert acted.dispatch == "confirmed", acted.render()
            atspi.wait_for(
                lambda ns: any(n.role == "label" and n.name == "7" for n in ns), app="kcalc"
            )
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(launched.pid, signal.SIGTERM)
            engine.session_stop()


@pytest.mark.qa("QA-ELEM-025")
@pytest.mark.slow
@pytest.mark.timeout(600)
def test_huge_locator_query(kcalc_session: AutomationEngine) -> None:
    kwin = _kwin(kcalc_session)
    before = sorted(w.id for w in kwin.windows())
    hostile = "\"'\\</script>`workspace.windowList().forEach(w=>w.closeWindow())`${1}"
    megabyte = (hostile * (1_000_000 // len(hostile) + 1))[:1_000_000]

    for query in (megabyte, hostile):
        started = time.monotonic()
        found = kcalc_session.element_query(Locator(query=query))
        assert time.monotonic() - started < 5.0
        assert found.elements == []

        started = time.monotonic()
        windows = kcalc_session.window_list(WindowSelector(caption=query))
        assert time.monotonic() - started < 5.0
        assert windows.windows == []

    assert sorted(w.id for w in kwin.windows()) == before
    alive = kcalc_session.element_query(Locator(app="kcalc", query="seven", role="button"))
    assert [e.name for e in alive.elements] == ["Seven"]
    assert kcalc_session.session_info().session is not None
