"""Interaction-probe sessions: the raw probe event log (PEL) and the 0.11 golden comparison.

The oracle for every injected-input test is ``interaction_probe.py``: it logs every
raw pointer, key, scroll and touch event to ``KWIN_MCP_PROBE_EVENT_LOG`` (one JSON
line per event, see ``tests/e2e/golden/README.md``) before GTK dispatches it, and
publishes ``hold_status:``/``key_log:``/``raw_events:``/``click_status:``/
``drag_status:``/``zoom_status:``/``horizontal_scroll:`` status labels over AT-SPI.

API (all coordinates are global logical px unless named ``local``):

- ``PROBE_SCRIPT``, ``PROBE_COMMAND``, ``PROBE_APP`` (AT-SPI app name), ``PROBE_ENV``.
- ``start_probe_session(engine, log_dir, **session_start_kwargs) -> Probe``: a virtual
  1280x800 session (input backend EIS asserted) running the probe.
- ``launch_probe(engine, log_dir, *, env=None) -> Probe``: the probe via ``app_launch``
  in the engine's running session (virtual or live).
- ``spawn_probe(*, dbus_address, wayland_display, log_path) -> subprocess.Popen``: the
  probe started by the test itself (not through ``app_launch``, so ``session_stop``
  does not kill it); pair with ``Probe.attach(...)`` and ``stop_process(popen)``.
- ``Probe``: ``pid``, ``log_path``, ``dbus_address``, ``window_id``, ``origin`` (client
  origin); ``oracle()``; ``refresh_origin()``; ``to_global(lx, ly) -> Point``;
  ``to_local(x, y)``; ``events()``; ``mark() -> int``; ``events_since(mark)``;
  ``wait_events(predicate, *, since=0, timeout_s=5)``; ``settle(*, since=0,
  quiet_s=0.4)`` (events once the log stops growing); ``element(name, role=None)
  -> UIElement`` (exact, unique name); ``center(name) -> Point``; ``status(prefix)
  -> str``; ``wait_status(prefix, predicate, *, timeout_s=5)``; ``entry_text()``
  (Keyboard Target text); ``park(lx=40, ly=40) -> int`` (pointer_move to a probe-local
  point, wait past GTK's double-click time, return ``mark()``).
- Status parsing: ``field(status, name) -> str``, ``int_field``, ``float_field``,
  ``point_field``, ``bounds_field``; ``counts(status, name) -> (pressed, released)``
  for ``left=1/1``-style fields.
- Event helpers: ``kinds(events)``, ``of_kind(events, *kinds)``, ``interval_ms(a, b)``
  (``mono_ns`` difference), ``POINTER_KINDS``, ``BUTTON_KINDS``, ``KEY_KINDS``.
- Goldens (amendment A15): ``load_golden(name)``; ``golden_core(events, golden)``
  projects each event onto the golden's fields; ``assert_matches_golden(events,
  golden)`` asserts the exact sequence (kinds, order, buttons, keys, modifiers,
  probe-local coordinates), every deliberate interval >= golden ``mono.min`` - 10 %
  and every interval <= 3x the golden ``mono.max`` (sub-10 ms gaps get a 10 ms
  basis, the dispatch jitter floor); ``assert_interval(start, end, *, at_least_ms,
  golden_ms)`` for stated-value checks (lower = stated - 10 %, upper = 3x).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from _asserts import has_kcalc_binary
from kwin_oracle import KWinOracle

from kwin_mcp.models import Locator, Point, Wait

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import UIElement

E2E_DIR: Final = Path(__file__).resolve().parent
GOLDEN_DIR: Final = E2E_DIR / "golden"
PROBE_SCRIPT: Final = E2E_DIR / "interaction_probe.py"
PROBE_COMMAND: Final = f"python3 {PROBE_SCRIPT}"
PROBE_APP: Final = "interaction_probe.py"
PROBE_ENV: Final[dict[str, str]] = {
    "GDK_BACKEND": "wayland",
    "GTK_MODULES": "gail:atk-bridge",
    "NO_AT_BRIDGE": "0",
    "XDG_SESSION_TYPE": "wayland",
}
EVENT_LOG_ENV: Final = "KWIN_MCP_PROBE_EVENT_LOG"
SCREEN: Final = (1280, 800)
STATE_TIMEOUT_S: Final = 5.0
POLL_S: Final = 0.1
# Longer than GTK's double-click time, so separate gestures never merge.
GESTURE_GAP_S: Final = 1.2
# Pause after a pointer_hold press before the next pointer call. Observed on KWin
# 6.3 (virtual): a motion or button sent within a few ms of a held press over a
# GtkButton reaches the client only with the next release; an agent's turn
# latency always exceeds this.
HOLD_SETTLE_S: Final = 0.3

POINTER_KINDS: Final = frozenset(
    {"motion", "button_press", "button_release", "gtk_double_press", "gtk_triple_press"}
)
BUTTON_KINDS: Final = frozenset({"button_press", "button_release"})
KEY_KINDS: Final = frozenset({"key_press", "key_release"})

# Intervals below this golden minimum are back-to-back sends, not deliberate delays.
_DELIBERATE_MS: Final = 5.0
# Upper-bound basis floor: GTK dispatch jitter in a loaded container.
_JITTER_FLOOR_MS: Final = 10.0
# Lower-bound allowance: an event can reach KWin and the probe up to ~1 ms later than
# the next one did relative to its send (measured: a 10 ms sender sleep observed as
# 9.08 ms dispatch / 9 ms compositor). Removing a deliberate wait still fails by far.
_DELIVERY_LAG_MS: Final = 1.0


Event = dict[str, Any]


# ── status labels ─────────────────────────────────────────────────────────


def field(status: str, name: str) -> str:
    match = re.search(rf"(?:^| ){re.escape(name)}=(\S+)", status)
    assert match is not None, (name, status)
    return match.group(1)


def int_field(status: str, name: str) -> int:
    return int(field(status, name))


def float_field(status: str, name: str) -> float:
    return float(field(status, name))


def point_field(status: str, name: str) -> tuple[int, int]:
    match = re.fullmatch(r"\((-?\d+),(-?\d+)\)", field(status, name))
    assert match is not None, (name, status)
    return int(match.group(1)), int(match.group(2))


def bounds_field(status: str, name: str = "bounds") -> tuple[int, int, int, int]:
    match = re.fullmatch(r"\((-?\d+),(-?\d+),(-?\d+),(-?\d+)\)", field(status, name))
    assert match is not None, (name, status)
    x0, y0, x1, y1 = (int(v) for v in match.groups())
    return x0, y0, x1, y1


def counts(status: str, name: str) -> tuple[int, int]:
    """``left=2/1`` -> ``(2, 1)`` (presses, releases)."""
    match = re.fullmatch(r"(\d+)/(\d+)", field(status, name))
    assert match is not None, (name, status)
    return int(match.group(1)), int(match.group(2))


# ── events ────────────────────────────────────────────────────────────────


def probe_env(log_path: Path) -> dict[str, str]:
    """The ``app_launch`` env for the probe, logging raw events to ``log_path``."""
    return {**PROBE_ENV, EVENT_LOG_ENV: str(log_path)}


def read_events(path: Path) -> list[Event]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    # The probe flushes per line; a torn last line is still being written.
    events: list[Event] = []
    for line in lines:
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            break
    return events


def kinds(events: Iterable[Event]) -> list[str]:
    return [str(e["kind"]) for e in events]


def of_kind(events: Iterable[Event], *wanted: str) -> list[Event]:
    return [e for e in events if e["kind"] in wanted]


def interval_ms(a: Event, b: Event) -> float:
    return (int(b["mono_ns"]) - int(a["mono_ns"])) / 1e6


def assert_interval(
    start: Event, end: Event, *, at_least_ms: float, golden_ms: float | None = None
) -> None:
    """Timing rule A15 between two events: >= stated - 10 %, <= 3x (the golden, else stated).

    As in ``assert_matches_golden``, each bound holds on the probe's dispatch clock
    (``mono``) or on the compositor's ``event_time`` (ms resolution, 1 ms of rounding
    allowed): GTK dispatch jitter alone can compress or stretch a gap. The lower
    bound also allows ``_DELIVERY_LAG_MS`` on either clock.
    """
    actual_ms = interval_ms(start, end)
    compositor_ms = int(end["event_time"]) - int(start["event_time"])
    upper_basis = max(golden_ms if golden_ms is not None else at_least_ms, _JITTER_FLOOR_MS)
    context = (actual_ms, compositor_ms, at_least_ms)
    floor = at_least_ms * 0.9 - _DELIVERY_LAG_MS
    assert actual_ms >= floor or compositor_ms >= floor - 1, context
    assert actual_ms <= 3 * upper_basis or compositor_ms <= 3 * upper_basis + 1, (
        *context,
        upper_basis,
    )


# ── goldens ───────────────────────────────────────────────────────────────


def load_golden(name: str) -> dict[str, Any]:
    stem = name.removesuffix(".json")
    return json.loads((GOLDEN_DIR / f"{stem}.json").read_text(encoding="utf-8"))


def golden_core(events: list[Event], golden: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each event projected onto the fields its golden counterpart records."""
    sequence = golden["sequence"]
    projected: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        keys = sequence[index].keys() if index < len(sequence) else ("kind",)
        projected.append({key: event.get(key) for key in keys})
    return projected


def assert_matches_golden(events: list[Event], golden: Mapping[str, Any]) -> None:
    """Exact event sequence plus the A15 lower/upper interval bounds.

    The lower bound holds on the probe's dispatch clock (``mono``) or, since GTK
    dispatch jitter can compress a gap, on the compositor's ``event_time`` (ms
    resolution, so 1 ms of rounding is allowed), each with ``_DELIVERY_LAG_MS`` of
    delivery lag; the upper bound holds on either clock.
    """
    sequence = golden["sequence"]
    assert golden_core(events, golden) == sequence, (golden["scenario"], events)
    for gap in golden["gaps_ms"]:
        start, end = events[gap["from"]], events[gap["to"]]
        actual = interval_ms(start, end)
        mono = gap["mono"]
        context = (golden["scenario"], gap, actual)
        compositor = int(end["event_time"]) - int(start["event_time"])
        clock = gap["event_clock"]
        if mono["min"] >= _DELIBERATE_MS:
            assert (
                actual >= 0.9 * mono["min"] - _DELIVERY_LAG_MS
                or compositor >= 0.9 * clock["min"] - _DELIVERY_LAG_MS - 1
            ), (*context, compositor)
        assert (
            actual <= 3 * max(mono["max"], _JITTER_FLOOR_MS)
            or compositor <= 3 * max(clock["max"], _JITTER_FLOOR_MS) + 1
        ), (*context, compositor)


# ── probe ─────────────────────────────────────────────────────────────────


@dataclass
class Probe:
    """One running interaction probe and the engine session that drives it."""

    engine: AutomationEngine
    pid: int
    log_path: Path
    dbus_address: str
    window_id: str = ""
    origin: tuple[int, int] = (0, 0)
    _oracle: KWinOracle | None = dc_field(default=None, repr=False)

    @classmethod
    def attach(
        cls, engine: AutomationEngine, *, pid: int, log_path: Path, dbus_address: str
    ) -> Probe:
        """Wait for the probe's window and AT-SPI tree; record its client origin."""
        probe = cls(engine=engine, pid=pid, log_path=log_path, dbus_address=dbus_address)
        windows = probe.oracle().wait_for_windows(
            lambda ws: any(w.pid == pid and w.client is not None and w.active for w in ws),
            timeout_s=20.0,
        )
        window = next(w for w in windows if w.pid == pid)
        probe.window_id = window.id
        probe.refresh_origin()
        found = engine.element_query(
            Locator(app=PROBE_APP, query="raw_events:"), wait=Wait(timeout_ms=15_000)
        )
        assert found.wait is not None and found.wait.satisfied, found.render()
        return probe

    def oracle(self) -> KWinOracle:
        if self._oracle is None:
            self._oracle = KWinOracle(self.dbus_address)
        return self._oracle

    def refresh_origin(self) -> tuple[int, int]:
        window = self.oracle().window(self.window_id)
        assert window is not None and window.client is not None, window
        x, y, _, _ = window.client.as_ints()
        self.origin = (x, y)
        return self.origin

    def to_global(self, lx: int, ly: int) -> Point:
        return Point(x=self.origin[0] + lx, y=self.origin[1] + ly)

    def to_local(self, x: int, y: int) -> tuple[int, int]:
        return x - self.origin[0], y - self.origin[1]

    # events

    def events(self) -> list[Event]:
        return read_events(self.log_path)

    def mark(self) -> int:
        return len(self.events())

    def events_since(self, mark: int) -> list[Event]:
        return self.events()[mark:]

    def wait_events(
        self,
        predicate: Callable[[list[Event]], bool],
        *,
        since: int = 0,
        timeout_s: float = STATE_TIMEOUT_S,
    ) -> list[Event]:
        deadline = time.monotonic() + timeout_s
        current = self.events_since(since)
        while not predicate(current):
            if time.monotonic() >= deadline:
                msg = f"probe events never satisfied the predicate: {current[-40:]}"
                raise AssertionError(msg)
            time.sleep(POLL_S)
            current = self.events_since(since)
        return current

    def settle(self, *, since: int = 0, quiet_s: float = 0.4) -> list[Event]:
        """The events after ``since`` once the log stopped growing for ``quiet_s``."""
        current = self.events_since(since)
        stable_since = time.monotonic()
        deadline = stable_since + STATE_TIMEOUT_S * 2
        while time.monotonic() - stable_since < quiet_s:
            assert time.monotonic() < deadline, "probe log never settled"
            time.sleep(POLL_S / 2)
            latest = self.events_since(since)
            if len(latest) != len(current):
                current = latest
                stable_since = time.monotonic()
        return current

    def park(self, lx: int = 40, ly: int = 40) -> int:
        """Park the pointer at a probe-local point, idle past a multi-click, return mark()."""
        self.engine.pointer_move(self.to_global(lx, ly))
        time.sleep(GESTURE_GAP_S)
        self.settle()
        return self.mark()

    # AT-SPI

    def element(self, name: str, role: str | None = None) -> UIElement:
        found = self.engine.element_query(Locator(app=PROBE_APP, query=name, role=role))
        exact = [e for e in found.elements if e.name == name]
        assert len(exact) == 1, found.render()[:2000]
        return exact[0]

    def center(self, name: str) -> Point:
        rect = self.element(name).rect
        assert rect is not None and rect.width > 0 and rect.height > 0, name
        return Point(x=rect.x + rect.width // 2, y=rect.y + rect.height // 2)

    def status(self, prefix: str) -> str:
        found = self.engine.element_query(Locator(app=PROBE_APP, query=prefix))
        names = [e.name for e in found.elements if e.name.startswith(prefix)]
        assert len(names) == 1, found.render()[:2000]
        return names[0]

    def wait_status(
        self, prefix: str, predicate: Callable[[str], bool], *, timeout_s: float = STATE_TIMEOUT_S
    ) -> str:
        deadline = time.monotonic() + timeout_s
        current = self.status(prefix)
        while not predicate(current):
            if time.monotonic() >= deadline:
                msg = f"{prefix} never satisfied the predicate: {current}"
                raise AssertionError(msg)
            time.sleep(POLL_S)
            current = self.status(prefix)
        return current

    def entry_text(self) -> str:
        return self.element("Keyboard Target").text


def _session_dbus(engine: AutomationEngine) -> str:
    info = engine.session_info()
    assert info.session is not None, info.render()
    return info.session.dbus_address


def launch_probe(
    engine: AutomationEngine, log_dir: Path, *, env: Mapping[str, str] | None = None
) -> Probe:
    """Launch the probe through app_launch in the engine's session."""
    log_path = log_dir / "probe-events.jsonl"
    log_dir.mkdir(parents=True, exist_ok=True)
    launched = engine.app_launch(PROBE_COMMAND, env={**probe_env(log_path), **(env or {})})
    return Probe.attach(
        engine, pid=launched.pid, log_path=log_path, dbus_address=_session_dbus(engine)
    )


def start_probe_session(engine: AutomationEngine, log_dir: Path, **kwargs: Any) -> Probe:
    """A 1280x800 virtual session with EIS input running the probe."""
    info = engine.session_start(screen_width=SCREEN[0], screen_height=SCREEN[1], **kwargs)
    assert info.kind == "virtual" and info.input_backend == "eis", info.render()
    return launch_probe(engine, log_dir)


def spawn_probe(
    *, dbus_address: str, wayland_display: str, log_path: Path
) -> subprocess.Popen[bytes]:
    """Start the probe outside kwin-mcp (session_stop and server exit leave it running)."""
    env = {
        **os.environ,
        **PROBE_ENV,
        "WAYLAND_DISPLAY": wayland_display,
        "DBUS_SESSION_BUS_ADDRESS": dbus_address,
        EVENT_LOG_ENV: str(log_path),
    }
    for name in ("DISPLAY", "AT_SPI_BUS_ADDRESS", "LD_PRELOAD"):
        env.pop(name, None)
    return subprocess.Popen(
        [sys.executable, str(PROBE_SCRIPT)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def stop_process(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


# ── KCalc ────────────────────────────────────────────────────────────────


def kcalc_button(engine: AutomationEngine, name: str) -> UIElement:
    """KCalc's button with this exact name (its '7' key is named "Seven")."""
    found = engine.element_query(Locator(app="kcalc", query=name, role="button"))
    exact = [e for e in found.elements if e.name == name]
    assert len(exact) == 1, found.render()[:1500]
    assert exact[0].rect is not None, exact[0]
    return exact[0]


def center_of(element: UIElement) -> Point:
    rect = element.rect
    assert rect is not None and rect.width > 0 and rect.height > 0, element
    return Point(x=rect.x + rect.width // 2, y=rect.y + rect.height // 2)


def wait_for_kcalc_binary(engine: AutomationEngine, expected: str) -> None:
    """Wait until KCalc's binary display shows ``expected`` (digit grouping tolerated)."""
    deadline = time.monotonic() + STATE_TIMEOUT_S
    found = engine.element_query(Locator(app="kcalc"))
    while not has_kcalc_binary(found, expected):
        if time.monotonic() >= deadline:
            msg = f"KCalc never showed binary {expected}: {found.render()[:800]}"
            raise AssertionError(msg)
        time.sleep(POLL_S)
        found = engine.element_query(Locator(app="kcalc"))
