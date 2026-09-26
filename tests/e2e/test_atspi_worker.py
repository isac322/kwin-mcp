"""End-to-end contracts for the long-lived AT-SPI2 worker.

pin the lifecycle guarantees that reuse must not break: answers always come
from the current session (even when the previous one crashed or was never
stopped, or its accessibility bus restarted under an unchanged session bus),
a killed worker is replaced transparently, and ``session_stop`` leaves no
helper process behind.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from kwin_mcp.core import AutomationEngine
from kwin_mcp.session import Session

if TYPE_CHECKING:
    from collections.abc import Callable

_TREE_APPLICATION = re.compile(r'^- \[application] "([^"]+)"', re.MULTILINE)
PROCESS_EXIT_TIMEOUT_SECONDS = 5.0
STOP_BUDGET_SECONDS = 10.0


def _tree_apps(engine: AutomationEngine) -> set[str]:
    return {name.lower() for name in _TREE_APPLICATION.findall(engine.accessibility_tree())}


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
    session = engine._session
    assert session is not None
    assert session.info is not None
    return session.info.dbus_address


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


def test_accessibility_recovers_when_the_a11y_bus_restarts(
    kcalc_session: AutomationEngine,
    wait_for_app: Callable[[str], str],
) -> None:
    """A restarted a11y bus (same socket path, new guid) must not keep stale answers."""
    kcalc_session.list_windows()
    first_pid = _worker_pid(kcalc_session)

    old_addr = _a11y_address(kcalc_session)
    assert old_addr, "org.a11y.Bus.GetAddress returned nothing"
    _kill_accessibility_bus()
    new_addr = _wait_for_new_a11y_bus(kcalc_session, old_addr)
    assert new_addr != old_addr, "the a11y bus did not restart within 15s"

    # An app launched before the restart bound to the dead bus and will not
    # return; one launched after the restart must be reachable on the new bus.
    kcalc_session.launch_app("kwrite")
    wait_for_app("kwrite")
    windows = kcalc_session.list_windows()
    assert "kwrite" in windows.lower(), windows
    if first_pid is not None:
        assert _worker_pid(kcalc_session) != first_pid


def _worker_pid(engine: AutomationEngine) -> int | None:
    """The AT-SPI worker pid, or ``None`` on builds without a persistent worker."""
    proc = getattr(engine, "_atspi_proc", None)
    if proc is None:
        return None
    assert proc.poll() is None, "AT-SPI worker exited"
    return proc.pid


def test_accessibility_follows_restarted_session_without_session_stop(
    engine: AutomationEngine,
    kcalc_session: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
) -> None:
    """A session that crashed and was replaced must not keep answering AT-SPI queries."""
    assert any("kcalc" in name for name in _tree_apps(engine))
    old_bus = _bus_address(engine)

    _crash_session(engine)
    output = start_session("kwrite")
    assert "Session started" in output, output
    assert _bus_address(engine) != old_bus

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
        output = other.session_start("kwrite", screen_width=width, screen_height=height)
        assert "Session started" in output, output
        other_session = other._session
        assert other_session is not None
        info = other_session.info
        assert info is not None

        _crash_session(engine)
        connected = engine.session_connect(
            dbus_address=info.dbus_address, wayland_display=info.wayland_socket
        )
        assert "Connected to live KWin session" in connected, connected

        found = engine.wait_for_element(query="", app_name="kwrite", timeout_ms=15000)
        assert found.startswith("Found"), found[:500]
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
    if first is None:  # no persistent worker (e.g. main): recovery is per-call
        assert any("kcalc" in name for name in _tree_apps(kcalc_session))
        return

    os.kill(first, signal.SIGKILL)
    assert _wait_until_gone(first, zombie_ok=True), f"worker {first} survived SIGKILL"

    assert any("kcalc" in name for name in _tree_apps(kcalc_session))
    assert _worker_pid(kcalc_session) != first


def test_session_stop_reaps_a_stopped_atspi_worker(
    engine: AutomationEngine,
    kcalc_session: AutomationEngine,
) -> None:
    """A SIGSTOPped worker cannot exit on EOF; session_stop must still reap it promptly."""
    kcalc_session.accessibility_tree()
    pid = _worker_pid(kcalc_session)
    assert pid is not None, "no AT-SPI worker after an accessibility call"
    os.kill(pid, signal.SIGSTOP)
    try:
        started = time.monotonic()
        result = engine.session_stop()
        elapsed = time.monotonic() - started
        assert result == "Session stopped."
        assert elapsed < STOP_BUDGET_SECONDS, elapsed
        assert _wait_until_gone(pid), f"AT-SPI worker {pid} survived session_stop"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGCONT)


def test_session_stop_leaves_no_atspi_helper_processes(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
) -> None:
    """Accessibility calls must not leave a running child of the engine's process after stop."""
    before = _child_pids()
    output = start_session("kcalc")
    assert "Session started" in output, output
    wait_for_app("kcalc")
    engine.accessibility_tree()
    engine.list_windows()

    assert engine.session_stop() == "Session stopped."
    new_children = _child_pids() - before
    leftovers = {pid for pid in new_children if not _wait_until_gone(pid, zombie_ok=True)}
    assert not leftovers, {pid: _cmdline(pid) for pid in leftovers}
