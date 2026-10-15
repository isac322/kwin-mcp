"""End-to-end coverage for ``session_stop``: teardown, held-input release and live disconnect.

Oracles: a ``/proc`` process census (pids, process groups, zombie state), the wall
clock, ``kwin_oracle`` on the live compositor's bus, ``dbus-send`` for the
accessibility switch, and the interaction probe's raw event log (PEL).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from probe_session import (
    BUTTON_KINDS,
    KEY_KINDS,
    Probe,
    of_kind,
    read_events,
    spawn_probe,
    start_probe_session,
    stop_process,
)
from session_harness import live_kwin

from kwin_mcp.models import HeldInput, SessionStopResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from probe_session import Event

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import AppLaunchResult, ElementList, SessionInfo

KCALC_CLASS = "org.kde.kcalc"
# session.py escalates launched-app groups: SIGTERM, a 3 s grace, then SIGKILL.
APP_TERM_GRACE_S = 3.0
CENSUS_TIMEOUT_S = 10.0
SIGTERM_BIT = 1 << (15 - 1)
NO_SESSION_STOP = {
    "stopped": False,
    "kind": None,
    "kept": {"artifacts_dir": None, "home_dir": None},
    "released": {"buttons": [], "keys": []},
    "warnings": [],
}


# ── process census ─────────────────────────────────────────────────────────


def _proc_stat(pid: int) -> tuple[str, int] | None:
    """(state, pgid) of ``pid`` from /proc/<pid>/stat, or None once it is gone."""
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    fields = stat.rsplit(")", 1)[1].split()
    return fields[0], int(fields[2])


def _is_live(pid: int) -> bool:
    """Whether ``pid`` still runs. Zombies do not count: pytest is PID 1 in the
    container and reaps orphans only when it waits, so a dead orphan stays ``Z``.
    """
    stat = _proc_stat(pid)
    return stat is not None and stat[0] not in {"Z", "X"}


def _pids() -> list[int]:
    return [int(entry.name) for entry in Path("/proc").iterdir() if entry.name.isdigit()]


def _comm(pid: int) -> str:
    try:
        return (Path("/proc") / str(pid) / "comm").read_text().strip()
    except (FileNotFoundError, ProcessLookupError):
        return ""


def _cmdline(pid: int) -> list[str]:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return []
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _group_pids(pgid: int) -> set[int]:
    """Live members of process group ``pgid``."""
    members: set[int] = set()
    for pid in _pids():
        stat = _proc_stat(pid)
        if stat is not None and stat[1] == pgid and stat[0] not in {"Z", "X"}:
            members.add(pid)
    return members


def _kwin_for_socket(socket_name: str) -> int:
    """The kwin_wayland process serving ``socket_name``."""
    matches = [
        pid
        for pid in _pids()
        if _comm(pid) == "kwin_wayland" and socket_name in _cmdline(pid) and _is_live(pid)
    ]
    assert len(matches) == 1, (socket_name, matches)
    return matches[0]


def _ignores_sigterm(pid: int) -> bool:
    try:
        status = (Path("/proc") / str(pid) / "status").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    for line in status.splitlines():
        if line.startswith("SigIgn:"):
            return bool(int(line.split()[1], 16) & SIGTERM_BIT)
    return False


def _wait_until(
    predicate: Callable[[], bool], what: str, timeout_s: float = CENSUS_TIMEOUT_S
) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


class _ExitWatcher(threading.Thread):
    """Records the monotonic time at which ``pid`` stops running."""

    def __init__(self, pid: int) -> None:
        super().__init__(daemon=True)
        self.pid = pid
        self.gone_at: float | None = None
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            if not _is_live(self.pid):
                self.gone_at = time.monotonic()
                return
            time.sleep(0.01)

    def finish(self) -> None:
        self._stop_event.set()
        self.join(timeout=5)


# ── accessibility switch oracle ───────────────────────────────────────────


def _a11y_switch(dbus_address: str, set_to: bool | None = None) -> bool:
    """Read ``org.a11y.Status.IsEnabled`` on ``dbus_address``, optionally setting it first."""
    dbus_send = shutil.which("dbus-send")
    assert dbus_send is not None, "dbus-send not found in PATH"
    env = {**os.environ, "DBUS_SESSION_BUS_ADDRESS": dbus_address}
    base = [
        dbus_send,
        "--session",
        "--print-reply",
        "--reply-timeout=10000",
        "--dest=org.a11y.Bus",
        "/org/a11y/bus",
    ]
    if set_to is not None:
        value = "true" if set_to else "false"
        subprocess.run(
            [
                *base,
                "org.freedesktop.DBus.Properties.Set",
                "string:org.a11y.Status",
                "string:IsEnabled",
                f"variant:boolean:{value}",
            ],
            env=env,
            capture_output=True,
            timeout=30,
            check=True,
        )
    reply = subprocess.run(
        [
            *base,
            "org.freedesktop.DBus.Properties.Get",
            "string:org.a11y.Status",
            "string:IsEnabled",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return reply.stdout.split()[-1] == "true"


# ── probe events ──────────────────────────────────────────────────────────


def _input_steps(events: list[Event]) -> list[tuple[str, str]]:
    """Button and key events as ``(kind, button-or-key)``, in log order."""
    return [
        (str(e["kind"]), str(e.get("button", e.get("key"))))
        for e in of_kind(events, *BUTTON_KINDS, *KEY_KINDS)
    ]


def _wait_log(
    log_path: Path, since: int, predicate: Callable[[list[Event]], bool], timeout_s: float = 5.0
) -> list[Event]:
    deadline = time.monotonic() + timeout_s
    current = read_events(log_path)[since:]
    while not predicate(current):
        assert time.monotonic() < deadline, f"probe log never satisfied the predicate: {current}"
        time.sleep(0.05)
        current = read_events(log_path)[since:]
    return current


PRESSED = [("button_press", "left"), ("key_press", "Shift_L")]
RELEASED = [("key_release", "Shift_L"), ("button_release", "left")]
HELD = HeldInput(buttons=["left"], keys=["shift"])


def _hold_left_and_shift(engine: AutomationEngine, probe: Probe) -> int:
    """Press left on the click target, then shift; return the log mark after both presses."""
    mark = probe.park()
    target = probe.center("Click Target")
    pressed = engine.pointer_hold("press", target, "left")
    assert pressed.changed is True, pressed.render()
    held_key = engine.keyboard_hold("press", "shift")
    assert held_key.changed is True, held_key.render()
    assert held_key.held == HELD, held_key.render()

    events = probe.wait_events(lambda es: _input_steps(es) == PRESSED, since=mark)
    info = engine.session_info()
    assert info.held == HELD, info.render()
    return mark + len(events)


def _assert_stop_released(result: SessionStopResult) -> None:
    assert result.stopped is True, result.render()
    assert result.released == HELD, result.render()
    assert "Released held input: left, shift" in result.render().splitlines()


# ── tests ─────────────────────────────────────────────────────────────────


@pytest.mark.qa("QA-GATE-004")
def test_stop_without_session_is_stopped_false_not_error(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    for _ in range(2):
        result = engine.session_stop()
        assert result == SessionStopResult(stopped=False)
        assert result.model_dump(mode="json") == NO_SESSION_STOP
        assert result.render() == "No session running."

    info = engine.session_info()
    assert info.session is None
    assert info.apps == []
    assert info.render() == "No session running."

    start_session()
    stopped = engine.session_stop()
    assert stopped.stopped is True
    assert stopped.kind == "virtual"
    assert stopped.render() == "Session stopped."

    again = engine.session_stop()
    assert again.model_dump(mode="json") == NO_SESSION_STOP
    assert again.render() == "No session running."


@pytest.mark.qa("QA-SES-016")
def test_virtual_stop_terminates_apps_kwin_and_bus(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
) -> None:
    info = start_session()
    kwin_pid = _kwin_for_socket(info.wayland_socket)
    session_stat = _proc_stat(kwin_pid)
    assert session_stat is not None
    session_pgid = session_stat[1]
    session_group = _group_pids(session_pgid)
    bus_daemons = {pid for pid in session_group if _comm(pid) == "dbus-daemon"}
    assert bus_daemons, (session_group, {pid: _comm(pid) for pid in session_group})

    kcalc = launch("kcalc")
    wait_for_app("kcalc")
    # The trailing ':' keeps the shell alive: bash (Fedora's /bin/sh) would otherwise
    # exec the last command, leaving one process where dash (Debian's) leaves two.
    ignorer = launch("sh -c \"trap '' TERM; sleep 100; :\"")
    # The shell has installed its trap and forked the sleep (which inherits it).
    _wait_until(
        lambda: (
            len(_group_pids(ignorer.pid)) == 2
            and all(_ignores_sigterm(pid) for pid in _group_pids(ignorer.pid))
        ),
        "the TERM-ignoring app to start",
    )
    assert _group_pids(kcalc.pid), "kcalc is not running"

    watcher = _ExitWatcher(kcalc.pid)
    started = time.monotonic()
    watcher.start()
    result = engine.session_stop()
    elapsed = time.monotonic() - started
    watcher.finish()

    assert result.stopped is True
    assert result.kind == "virtual"
    assert result.released == HeldInput()
    assert result.render() == "Session stopped."

    # The TERM-ignoring group forces the full grace before its SIGKILL (A15:
    # lower bound = the grace, upper bound = 3x).
    assert elapsed >= APP_TERM_GRACE_S, elapsed
    assert elapsed <= 3 * APP_TERM_GRACE_S, elapsed
    # kcalc honours SIGTERM, so it is gone before the SIGKILL step.
    assert watcher.gone_at is not None, "kcalc survived session_stop"
    assert watcher.gone_at - started < APP_TERM_GRACE_S, watcher.gone_at - started

    assert _group_pids(kcalc.pid) == set()
    assert _group_pids(ignorer.pid) == set()
    assert not _is_live(kwin_pid)
    assert all(not _is_live(pid) for pid in bus_daemons), bus_daemons
    assert _group_pids(session_pgid) == set()
    assert not (Path(os.environ["XDG_RUNTIME_DIR"]) / info.wayland_socket).exists()
    assert engine.session_info().session is None


@pytest.mark.qa("QA-SES-018")
def test_live_stop_kills_only_launched_apps_and_restores_a11y(
    engine: AutomationEngine, tmp_path: Path
) -> None:
    with live_kwin() as live:
        assert _a11y_switch(live.dbus_address, set_to=False) is False
        # A Wayland client the test starts itself: kwin-mcp never launched it.
        neighbour = spawn_probe(
            dbus_address=live.dbus_address,
            wayland_display=live.wayland_display,
            log_path=tmp_path / "neighbour-events.jsonl",
        )
        try:
            live.wait_for_windows(lambda ws: any(w.pid == neighbour.pid for w in ws))

            info = engine.session_connect(
                dbus_address=live.dbus_address,
                wayland_display=live.wayland_display,
                enable_accessibility=True,
            )
            assert info.kind == "live", info.render()
            assert info.accessibility == "switched_on", info.render()
            assert _a11y_switch(live.dbus_address) is True

            kcalc = engine.app_launch("kcalc")
            live.wait_for_windows(
                lambda ws: any(w.pid == kcalc.pid and w.resource_class == KCALC_CLASS for w in ws)
            )
            assert _group_pids(kcalc.pid), "kcalc is not running"

            result = engine.session_stop()
            assert result.stopped is True
            assert result.kind == "live"
            assert result.render() == "Disconnected from live session."

            # Only the launched app's group is gone.
            assert _group_pids(kcalc.pid) == set()
            assert neighbour.poll() is None
            assert _is_live(neighbour.pid)
            assert live.process.poll() is None
            assert live.socket_path.is_socket()
            windows = live.wait_for_windows(lambda ws: all(w.pid != kcalc.pid for w in ws))
            assert any(w.pid == neighbour.pid for w in windows), windows
            # The switch the connection turned on is back off.
            assert _a11y_switch(live.dbus_address) is False
        finally:
            stop_process(neighbour)


@pytest.mark.qa("QA-SES-019")
def test_stop_releases_held_buttons_and_keys_and_reports_them(
    engine: AutomationEngine, tmp_path: Path
) -> None:
    # Virtual session: the probe is an app_launch child, killed after the release.
    virtual_dir = tmp_path / "virtual"
    virtual_dir.mkdir()
    probe = start_probe_session(engine, virtual_dir)
    since = _hold_left_and_shift(engine, probe)

    result = engine.session_stop()
    _assert_stop_released(result)
    assert result.kind == "virtual"
    released = _wait_log(probe.log_path, since, lambda es: len(_input_steps(es)) >= 2)
    assert _input_steps(released) == RELEASED

    # Live session: the test owns the probe, so it survives the disconnect.
    live_dir = tmp_path / "live"
    live_dir.mkdir()
    log_path = live_dir / "probe-events.jsonl"
    with live_kwin() as live:
        process = spawn_probe(
            dbus_address=live.dbus_address,
            wayland_display=live.wayland_display,
            log_path=log_path,
        )
        try:
            info = engine.session_connect(
                dbus_address=live.dbus_address, wayland_display=live.wayland_display
            )
            assert info.input_backend == "eis", info.render()
            live_probe = Probe.attach(
                engine, pid=process.pid, log_path=log_path, dbus_address=live.dbus_address
            )
            since = _hold_left_and_shift(engine, live_probe)

            result = engine.session_stop()
            _assert_stop_released(result)
            assert result.kind == "live"
            assert result.render().splitlines()[0] == "Disconnected from live session."

            released = _wait_log(log_path, since, lambda es: len(_input_steps(es)) >= 2)
            assert _input_steps(released) == RELEASED
            # Nothing else arrives after the releases, and the probe is still running.
            time.sleep(0.5)
            assert _input_steps(read_events(log_path)[since:]) == RELEASED
            assert process.poll() is None
        finally:
            stop_process(process)
