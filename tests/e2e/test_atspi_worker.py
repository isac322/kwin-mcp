"""End-to-end contracts for the long-lived AT-SPI2 worker.

pin the lifecycle guarantees that reuse must not break: answers always come
from the current session (even when the previous one crashed or was never
stopped, or its accessibility bus restarted under an unchanged session bus),
a killed worker is replaced transparently, a hung worker is abandoned at its
deadline, and ``session_stop`` leaves no helper process behind. Every worker
replacement starts a new ref generation, so refs of the old worker go stale.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from _asserts import has_kcalc_binary

from kwin_mcp import core
from kwin_mcp.core import AutomationEngine
from kwin_mcp.models import Locator, RefAddr, Wait
from kwin_mcp.session import Session

if TYPE_CHECKING:
    from collections.abc import Callable

    from kwin_mcp.models import AppLaunchResult, ElementList, SessionInfo

PROCESS_EXIT_TIMEOUT_SECONDS = 5.0
STOP_BUDGET_SECONDS = 10.0
_REF = re.compile(r"e(\d+)\.(\d+)")


def _tree_apps(engine: AutomationEngine) -> set[str]:
    found = engine.element_query(max_depth=0)
    return {element.name.lower() for element in found.elements if element.depth == 0}


def _generation(ref: str) -> int:
    match = _REF.fullmatch(ref)
    assert match is not None, ref
    return int(match.group(1))


def _child_pids() -> set[int]:
    """PIDs whose parent is this pytest process."""
    children: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for line in status.splitlines():
            if line.startswith("PPid:"):
                if int(line.split(":", 1)[1]) == os.getpid():
                    children.add(int(entry.name))
                break
    return children


def _cmdline(pid: int) -> str:
    try:
        return (Path("/proc") / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode()
    except (FileNotFoundError, ProcessLookupError):
        return "(exited)"


def _is_zombie(pid: int) -> bool:
    try:
        status = (Path("/proc") / str(pid) / "status").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return False
    return "\nState:\tZ" in status


def _wait_until_gone(pid: int, *, zombie_ok: bool = False) -> bool:
    """Wait for ``pid`` to disappear (or, with ``zombie_ok``, merely to exit)."""
    deadline = time.monotonic() + PROCESS_EXIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not (Path("/proc") / str(pid)).exists() or (zombie_ok and _is_zombie(pid)):
            return True
        time.sleep(0.05)
    return False


def _bus_address(engine: AutomationEngine) -> str:
    info = engine.session_info().session
    assert info is not None
    return info.dbus_address


def _crash_session(engine: AutomationEngine) -> None:
    """SIGKILL the whole virtual session (compositor and bus) without session_stop."""
    session = engine._session
    assert isinstance(session, Session)
    leader = session._process
    assert leader is not None
    with contextlib.suppress(ProcessLookupError):
        os.killpg(leader.pid, signal.SIGKILL)
    deadline = time.monotonic() + PROCESS_EXIT_TIMEOUT_SECONDS
    while session.is_running and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not session.is_running, "crashed session still reports running"


def _a11y_address(engine: AutomationEngine) -> str:
    """Current ``org.a11y.Bus.GetAddress`` reply (same socket, new guid per restart)."""
    out = subprocess.run(
        [
            "dbus-send",
            "--session",
            "--print-reply=literal",
            "--dest=org.a11y.Bus",
            "/org/a11y/bus",
            "org.a11y.Bus.GetAddress",
        ],
        env=engine._session_env(),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return out.stdout.strip()


def _kill_accessibility_bus() -> None:
    """Kill the AT-SPI bus trio so the next GetAddress relaunches it with a new guid."""
    # comm is truncated to 15 chars; matching the full name with -f would also
    # hit the dbus-run-session wrapper whose script text mentions the launcher.
    subprocess.run(["pkill", "-KILL", "-x", "at-spi2-registr"], check=False, capture_output=True)
    subprocess.run(["pkill", "-KILL", "-x", "at-spi-bus-laun"], check=False, capture_output=True)
    # The a11y dbus-daemon is distinguished from the session daemon by its config.
    listing = subprocess.run(
        ["pgrep", "-a", "-x", "dbus-daemon"], check=False, capture_output=True, text=True
    )
    for line in listing.stdout.splitlines():
        if "at-spi2" in line:
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(line.split()[0]), signal.SIGKILL)


def _wait_for_new_a11y_bus(engine: AutomationEngine, previous: str) -> str:
    """Poll GetAddress until the a11y bus restarts (reply changes) or 15 s pass."""
    deadline = time.monotonic() + 15.0
    current = previous
    while current == previous and time.monotonic() < deadline:
        time.sleep(0.2)
        current = _a11y_address(engine)
    return current


def _worker_pid(engine: AutomationEngine) -> int:
    """The running AT-SPI worker's pid."""
    proc = engine._atspi_proc
    assert proc is not None, "no AT-SPI worker after an accessibility call"
    assert proc.poll() is None, "AT-SPI worker exited"
    return proc.pid


def _seven(engine: AutomationEngine, app: str = "kcalc") -> str:
    """Ref of a KCalc "Seven" button (its 7 key)."""
    found = engine.element_query(Locator(app=app, query="Seven", role="button"))
    exact = [element for element in found.elements if element.name == "Seven"]
    assert len(exact) == 1, found.render()[:2000]
    return exact[0].ref


def _binary_display(found: ElementList) -> list[str]:
    """Names of KCalc's labels (its displays), in tree order."""
    return [element.name for element in found.elements if element.role == "label"]


def _wait_for_display(engine: AutomationEngine, expected: str) -> None:
    """Poll until a KCalc binary display shows ``expected`` (5 s)."""
    deadline = time.monotonic() + 5.0
    found = engine.element_query(Locator(app="kcalc"))
    while not has_kcalc_binary(found, expected):
        assert time.monotonic() < deadline, _binary_display(found)
        time.sleep(0.2)
        found = engine.element_query(Locator(app="kcalc"))


@pytest.mark.qa("QA-ELEM-022")
def test_accessibility_recovers_when_the_a11y_bus_restarts(
    kcalc_session: AutomationEngine,
    launch: Callable[..., AppLaunchResult],
) -> None:
    """A restarted a11y bus (same socket path, new guid) must not keep stale answers.

    The respawned worker starts a new ref generation: a ref minted before the
    restart is refused as stale (nothing is pressed), one minted after works.
    """
    ref_a = _seven(kcalc_session)
    first_pid = _worker_pid(kcalc_session)

    old_addr = _a11y_address(kcalc_session)
    assert old_addr, "org.a11y.Bus.GetAddress returned nothing"
    _kill_accessibility_bus()
    new_addr = _wait_for_new_a11y_bus(kcalc_session, old_addr)
    assert new_addr != old_addr, "the a11y bus did not restart within 15s"

    # An app launched before the restart bound to the dead bus and will not
    # return; one launched after the restart must be reachable on the new bus.
    launch("kcalc")
    found = kcalc_session.element_query(
        Locator(app="kcalc", query="Seven", role="button"), wait=Wait(timeout_ms=15000)
    )
    assert found.wait is not None and found.wait.satisfied, found.render()[:2000]
    assert _worker_pid(kcalc_session) != first_pid
    sevens = [element for element in found.elements if element.name == "Seven"]
    assert sevens, found.render()[:2000]
    ref_b = sevens[0].ref
    assert _generation(ref_b) == _generation(ref_a) + 1, (ref_a, ref_b)
    windows = kcalc_session.window_list().windows
    assert any("kcalc" in window.app_id.lower() for window in windows), windows

    before = _binary_display(kcalc_session.element_query(Locator(app="kcalc")))
    assert not has_kcalc_binary(kcalc_session.element_query(Locator(app="kcalc")), "111")
    with pytest.raises(RuntimeError) as stale:
        kcalc_session.element_act(RefAddr(ref=ref_a), "Press")
    assert str(stale.value) == (
        f"Stale ref {ref_a} (AT-SPI worker restarted); re-run element_query."
    )
    assert _binary_display(kcalc_session.element_query(Locator(app="kcalc"))) == before

    pressed = kcalc_session.element_act(RefAddr(ref=ref_b), "Press")
    assert pressed.dispatch == "confirmed", pressed.render()
    assert pressed.element is not None and pressed.element.ref == ref_b, pressed.render()
    _wait_for_display(kcalc_session, "111")


def test_accessibility_follows_restarted_session_without_session_stop(
    engine: AutomationEngine,
    kcalc_session: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
) -> None:
    """A session that crashed and was replaced must not keep answering AT-SPI queries."""
    assert any("kcalc" in name for name in _tree_apps(engine))
    old_bus = _bus_address(engine)

    _crash_session(engine)
    info = start_session()
    assert info.render().startswith("Session started"), info.render()
    assert _bus_address(engine) != old_bus
    launch("kwrite")

    wait_for_app("kwrite")
    apps = _tree_apps(engine)
    assert any("kwrite" in name for name in apps), apps
    assert not any("kcalc" in name for name in apps), apps


def test_accessibility_follows_session_connect_to_another_bus(
    engine: AutomationEngine,
    kcalc_session: AutomationEngine,
    screen_size: tuple[int, int],
) -> None:
    """After a crash, session_connect to another KWin must query that KWin's bus."""
    assert any("kcalc" in name for name in _tree_apps(engine))
    other = AutomationEngine()
    try:
        width, height = screen_size
        started = other.session_start(screen_width=width, screen_height=height)
        assert started.render().startswith("Session started"), started.render()
        other.app_launch("kwrite")

        _crash_session(engine)
        connected = engine.session_connect(
            dbus_address=started.dbus_address, wayland_display=started.wayland_socket
        )
        assert connected.render().startswith("Connected to live KWin session"), connected.render()

        found = engine.element_query(Locator(app="kwrite"), wait=Wait(timeout_ms=15000))
        assert found.wait is not None and found.wait.satisfied, found.render()[:500]
        assert found.render().startswith("Found"), found.render()[:500]
        apps = _tree_apps(engine)
        assert any("kwrite" in name for name in apps), apps
        assert not any("kcalc" in name for name in apps), apps
    finally:
        engine.session_stop()
        other.session_stop()


def test_killed_atspi_worker_is_replaced_on_next_call(
    kcalc_session: AutomationEngine,
) -> None:
    """A worker killed between calls is respawned and the next query still answers."""
    assert any("kcalc" in name for name in _tree_apps(kcalc_session))
    first = _worker_pid(kcalc_session)
    generation = kcalc_session._atspi_generation

    os.kill(first, signal.SIGKILL)
    assert _wait_until_gone(first, zombie_ok=True), f"worker {first} survived SIGKILL"

    assert any("kcalc" in name for name in _tree_apps(kcalc_session))
    assert _worker_pid(kcalc_session) != first
    assert kcalc_session._atspi_generation == generation + 1


def test_session_stop_reaps_a_stopped_atspi_worker(
    engine: AutomationEngine,
    kcalc_session: AutomationEngine,
) -> None:
    """A SIGSTOPped worker cannot exit on EOF; session_stop must still reap it promptly."""
    kcalc_session.element_query()
    pid = _worker_pid(kcalc_session)
    os.kill(pid, signal.SIGSTOP)
    try:
        started = time.monotonic()
        result = engine.session_stop()
        elapsed = time.monotonic() - started
        assert result.render() == "Session stopped.", result.render()
        assert elapsed < STOP_BUDGET_SECONDS, elapsed
        assert _wait_until_gone(pid), f"AT-SPI worker {pid} survived session_stop"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGCONT)


def test_session_stop_leaves_no_atspi_helper_processes(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
) -> None:
    """Accessibility calls must not leave a running child of the engine's process after stop."""
    before = _child_pids()
    info = start_session()
    assert info.render().startswith("Session started"), info.render()
    launch("kcalc")
    wait_for_app("kcalc")
    engine.element_query()
    engine.window_list()

    assert engine.session_stop().render() == "Session stopped."
    new_children = _child_pids() - before
    leftovers = {pid for pid in new_children if not _wait_until_gone(pid, zombie_ok=True)}
    assert not leftovers, {pid: _cmdline(pid) for pid in leftovers}


def test_atspi_worker_runs_in_its_own_session(kcalc_session: AutomationEngine) -> None:
    """The worker leads its own session, outside the caller's process group.

    Regression: the worker shared the caller's process group, so a terminal Ctrl-C
    aimed at kwin-mcp-cli (SIGINT to the foreground group) also killed it.
    """
    assert any("kcalc" in name for name in _tree_apps(kcalc_session))
    pid = _worker_pid(kcalc_session)
    assert os.getpgid(pid) != os.getpgrp()
    assert os.getsid(pid) == pid


class _StopEveryWorker:
    """SIGSTOP the engine's current worker and every replacement, recording each spawn."""

    def __init__(self, engine: AutomationEngine) -> None:
        self._engine = engine
        self.stopped: list[tuple[int, float]] = []
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> _StopEveryWorker:
        self._stop_current()
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._done.set()
        self._thread.join(timeout=5)
        for pid, _ in self.stopped:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGCONT)

    def _stop_current(self) -> None:
        proc = self._engine._atspi_proc
        if proc is None or any(pid == proc.pid for pid, _ in self.stopped):
            return
        with contextlib.suppress(ProcessLookupError):
            os.kill(proc.pid, signal.SIGSTOP)
            self.stopped.append((proc.pid, time.monotonic()))

    def _run(self) -> None:
        while not self._done.wait(0.01):
            self._stop_current()


@pytest.mark.qa("QA-ELEM-019")
@pytest.mark.timeout(300)
def test_stopped_worker_deadline_bound(kcalc_session: AutomationEngine) -> None:
    """A hung worker is abandoned at timeout_ms + 30 s and replaced; when the
    replacement hangs too, the call ends with the retry-failed error, bounded by
    two per-attempt deadlines plus the retry pause."""
    kcalc_session.element_query(max_depth=0)
    first = _worker_pid(kcalc_session)
    deadline_s = 1.0 + core._ATSPI_TIMEOUT_S

    started = time.monotonic()
    with _StopEveryWorker(kcalc_session) as stopper, pytest.raises(RuntimeError) as failure:
        kcalc_session.element_query(Locator(app="kcalc"), wait=Wait(timeout_ms=1000))
    elapsed = time.monotonic() - started

    assert str(failure.value) == (
        f"AT-SPI2 query timed out after {deadline_s:g}s (op=wait). "
        "Retried once but still failed — the AT-SPI2 bus may be unstable."
    )
    pids = [pid for pid, _ in stopper.stopped]
    assert pids[0] == first and len(pids) == 2, stopper.stopped
    # The first worker was abandoned at its deadline and a new one spawned 0.5 s later.
    respawned_after = stopper.stopped[1][1] - started
    assert respawned_after >= deadline_s + 0.5, respawned_after
    assert respawned_after <= 3 * (deadline_s + 0.5), respawned_after
    total = 2 * deadline_s + 0.5
    assert elapsed >= total, elapsed
    assert elapsed <= total + 10.0, elapsed
    # Both hung workers are gone; the next call gets a fresh one.
    for pid in pids:
        assert _wait_until_gone(pid, zombie_ok=True), f"hung worker {pid} survived"
    assert any("kcalc" in name for name in _tree_apps(kcalc_session))
    assert _worker_pid(kcalc_session) not in pids
