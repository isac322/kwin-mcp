"""Session processes: sockets, process groups, reaping, and bounded startup failures.

``session_stop`` removes the Wayland socket, stops every launched app's whole
process group (SIGTERM, a shared grace, then SIGKILL) while unrelated processes and
a live session's KWin survive, and keeps an app leader unreaped until the last
group signal. ``Session.start`` replaces stale socket/lock files and, when the
compositor fails (PATH stubs for ``kwin_wayland``/``dbus-run-session``), fails
within its handshake deadline and leaves nothing behind.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from session_harness import live_kwin

from kwin_mcp import session as session_module
from kwin_mcp.session import Session, SessionConfig

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import SessionInfo, SessionStopResult

PROCESS_EXIT_TIMEOUT_SECONDS = 5.0


def _wait_for_process_exit(pid: int) -> None:
    process_path = Path("/proc") / str(pid)
    deadline = time.monotonic() + PROCESS_EXIT_TIMEOUT_SECONDS
    while process_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not process_path.exists(), f"process {pid} was not reaped"


def _kwin_pids() -> set[int]:
    pids: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text().strip() == "kwin_wayland":
                pids.add(int(entry.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return pids


def test_session_stop_removes_the_wayland_socket(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    info = start_session()
    socket_name = info.wayland_socket
    socket_path = Path(os.environ["XDG_RUNTIME_DIR"]) / socket_name
    assert socket_path.is_socket()

    assert engine.session_stop().render() == "Session stopped."
    assert not socket_path.exists()


def test_session_start_replaces_stale_socket_and_lock_files(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    runtime_dir = Path(os.environ["XDG_RUNTIME_DIR"])
    now = int(time.time())
    candidate_names = [
        f"wayland-mcp-{os.getpid()}-{timestamp}" for timestamp in range(now - 1, now + 10)
    ]
    stale_paths = [
        runtime_dir / f"{name}{suffix}" for name in candidate_names for suffix in ("", ".lock")
    ]
    for stale_path in stale_paths:
        stale_path.write_text("stale")

    try:
        info = start_session()
        socket_name = info.wayland_socket
        socket_path = runtime_dir / socket_name
        lock_path = runtime_dir / f"{socket_name}.lock"

        assert socket_name in candidate_names
        assert socket_path.is_socket()

        assert engine.session_stop().render() == "Session stopped."
        assert not socket_path.exists()
        assert not lock_path.exists()
    finally:
        engine.session_stop()
        for stale_path in stale_paths:
            stale_path.unlink(missing_ok=True)


def test_session_stop_terminates_and_reaps_launched_process(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    start_session()
    pid = engine.app_launch("sleep 300").pid
    assert (Path("/proc") / str(pid)).exists()

    try:
        assert engine.session_stop().render() == "Session stopped."
        _wait_for_process_exit(pid)
    finally:
        engine.session_stop()


def test_live_session_disconnect_terminates_and_reaps_launched_process(
    engine: AutomationEngine,
) -> None:
    with live_kwin() as live:
        info = engine.session_connect(
            dbus_address=live.dbus_address,
            wayland_display=live.wayland_display,
        )
        assert info.kind == "live", info

        pid = engine.app_launch("sleep 300").pid
        assert (Path("/proc") / str(pid)).exists()

        try:
            assert engine.session_stop().render() == "Disconnected from live session."
            _wait_for_process_exit(pid)
            assert live.process.poll() is None
            assert live.socket_path.is_socket()
        finally:
            engine.session_stop()


# ---------------------------------------------------------------------------
# Launched-app process-group teardown
#
# app_launch puts every app in its own process group so session_stop stops the
# whole tree, not just the direct child. These launch an app that forks a
# background descendant which ignores SIGTERM, then prove the descendant is
# gone after stop() while an unrelated process group — and, in live mode, the
# connected compositor — survive.
#
# The descendant deliberately ignores SIGTERM: a well-behaved one is reaped by
# the surrounding process-group signal the container tears down anyway, so on
# the old direct-child-only teardown it would not survive and the regression
# would be unobservable. A TERM-ignoring descendant survives the old code and
# only the new group-wide SIGKILL escalation removes it.
# ---------------------------------------------------------------------------

# How long stop() waits between SIGTERM and SIGKILL for a launched app's group,
# and after the SIGKILL. The two-phase escalation shares these across all apps.
APP_TERM_GRACE_SECONDS: float = getattr(session_module, "_APP_TERM_GRACE_SECONDS", 3.0)
APP_KILL_GRACE_SECONDS: float = getattr(session_module, "_APP_KILL_GRACE_SECONDS", 2.0)


def _term_holder(tmp_path: Path, tag: str) -> str:
    """A bash helper that ignores SIGTERM and then execs a tagged sleeper."""
    holder = tmp_path / "term-holder.sh"
    holder.write_text(f"#!/bin/bash\ntrap '' TERM\nexec -a {tag} sleep infinity\n")
    holder.chmod(0o755)
    return str(holder)


def _descendant_command(holder: str) -> str:
    """An app whose backgrounded descendant is the TERM-ignoring helper."""
    return f"bash -c '{shlex.quote(holder)} & sleep infinity'"


def test_session_stop_stops_launched_app_descendants(
    tmp_path: Path, engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    """A launched app's SIGTERM-ignoring background descendant must not survive
    session_stop, and only via the group's SIGKILL escalation.

    The descendant ignores SIGTERM, so the TERM grace is fully used up before
    the SIGKILL escalation reaches it; a direct-child-only teardown would leave
    it running and return long before that.
    """
    tag = f"appgroup-desc-{os.getpid()}"
    holder = _term_holder(tmp_path, tag)
    neighbor = _spawn_neighbor(f"neighbor-desc-{os.getpid()}")
    try:
        start_session()
        engine.app_launch(_descendant_command(holder))
        assert _wait_until(lambda: bool(_tagged_pids(tag)), PROCESS_EXIT_TIMEOUT_SECONDS), (
            "background descendant never started"
        )

        started = time.monotonic()
        assert engine.session_stop().render() == "Session stopped."
        elapsed = time.monotonic() - started

        gone = _wait_until(lambda: not _tagged_pids(tag), PROCESS_EXIT_TIMEOUT_SECONDS)
        assert gone, f"app descendant survived session_stop: {_tagged_pids(tag)}"
        assert elapsed >= APP_TERM_GRACE_SECONDS - 0.5, elapsed
        assert neighbor.poll() is None, "unrelated neighbor process was killed"
    finally:
        engine.session_stop()
        _reap(neighbor)


def test_live_session_stop_terminates_launched_app_descendants(
    tmp_path: Path,
    engine: AutomationEngine,
) -> None:
    """In a live session a launched app's descendant must not outlive stop()."""
    tag = f"appgroup-live-{os.getpid()}"
    holder = _term_holder(tmp_path, tag)
    neighbor = _spawn_neighbor(f"neighbor-live-{os.getpid()}")
    with live_kwin() as live:
        try:
            info = engine.session_connect(
                dbus_address=live.dbus_address,
                wayland_display=live.wayland_display,
            )
            assert info.kind == "live", info
            engine.app_launch(_descendant_command(holder))
            assert _wait_until(lambda: bool(_tagged_pids(tag)), PROCESS_EXIT_TIMEOUT_SECONDS), (
                "background descendant never started"
            )

            assert engine.session_stop().render() == "Disconnected from live session."

            gone = _wait_until(lambda: not _tagged_pids(tag), PROCESS_EXIT_TIMEOUT_SECONDS)
            assert gone, f"app descendant survived live stop(): {_tagged_pids(tag)}"
            assert live.process.poll() is None, "connected compositor was killed"
            assert live.socket_path.is_socket()
            assert neighbor.poll() is None, "unrelated neighbor process was killed"
        finally:
            engine.session_stop()
    _reap(neighbor)


def test_session_stop_keeps_app_leader_unreaped_until_last_signal(
    tmp_path: Path, engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    """The launched app's leader stays unreaped (a zombie) until the last group
    signal, so its pid (== its process-group id) is reserved and a killpg on
    that group cannot reach a pid recycled into an unrelated group.

    The leader exits immediately while a SIGTERM-ignoring descendant lingers;
    the descendant must be killed by the group escalation and the leader must
    remain a zombie (unreaped) through the whole escalation, not just be
    reaped early while the group is still being signalled.
    """
    tag = f"appgroup-leader-{os.getpid()}"
    holder = _term_holder(tmp_path, tag)
    neighbor = _spawn_neighbor(f"neighbor-leader-{os.getpid()}")
    try:
        start_session()
        # The leader (bash) exits right after forking the backgrounded,
        # SIGTERM-ignoring descendant, which lingers in the leader's group.
        leader_pid = engine.app_launch(f"bash -c '{shlex.quote(holder)} & exit 0'").pid
        assert _wait_until(lambda: bool(_tagged_pids(tag)), PROCESS_EXIT_TIMEOUT_SECONDS), (
            "background descendant never started"
        )
        # The leader has exited and, being unreaped, is a zombie before stop().
        assert _wait_until(
            lambda: _is_zombie(leader_pid),
            PROCESS_EXIT_TIMEOUT_SECONDS,
        ), "leader did not become a zombie before session_stop"

        # Run session_stop on a worker and poll the leader's state through the
        # escalation; it must stay a zombie (unreaped) until the final reaping.
        stop_result: list[SessionStopResult] = []
        stop_error: list[BaseException] = []

        def _stop() -> None:
            try:
                stop_result.append(engine.session_stop())
            except BaseException as exc:
                stop_error.append(exc)

        worker = threading.Thread(target=_stop)
        stop_started = time.monotonic()
        worker.start()
        observations = 0
        zombie_observations = 0
        stop_budget = APP_TERM_GRACE_SECONDS + APP_KILL_GRACE_SECONDS
        while worker.is_alive() and time.monotonic() - stop_started < stop_budget + 5:
            observations += 1
            if _is_zombie(leader_pid):
                zombie_observations += 1
            time.sleep(0.05)
        worker.join(timeout=5)
        assert not worker.is_alive(), "session_stop did not complete in time"
        if stop_error:
            raise stop_error[0]

        assert stop_result and stop_result[0].render() == "Session stopped."
        gone = _wait_until(lambda: not _tagged_pids(tag), PROCESS_EXIT_TIMEOUT_SECONDS)
        assert gone, f"app descendant survived session_stop: {_tagged_pids(tag)}"
        assert zombie_observations >= observations // 2, (
            f"leader was not held unreaped through the escalation "
            f"(zombie {zombie_observations}/{observations} polls)"
        )
        assert neighbor.poll() is None, "unrelated neighbor process was killed"
    finally:
        engine.session_stop()
        _reap(neighbor)


# ---------------------------------------------------------------------------
# Failing-compositor lifecycle regression tests (issue #48)
#
# These drive the real Session against PATH stubs for its external binaries
# (kwin_wayland or dbus-run-session) so each failure mode is deterministic.
# Startup runs on a worker thread joined against an outer budget: a regression
# to the historical unbounded readline fails an assertion instead of hanging
# the run, and the test's recovery then unblocks and joins the worker.
#
# Cleanup is asserted before any recovery runs. Recovery lives in `finally` and
# always kills every process the test owns, whatever the assertions found.
# ---------------------------------------------------------------------------

# Session.start() is bounded by its handshake deadline plus at most ~23 s of
# teardown (leader waits of 5 s + 5 s, then stop()'s 5 s + 3 s leader and 5 s
# group waits), so startup-failure tests get 90 s; stop() alone gets 30 s.
STUB_START_BUDGET_SECONDS = 90.0
STUB_STOP_BUDGET_SECONDS = 30.0
# How long Session waits for the startup handshake. Older releases had no
# deadline at all; the outer budget above catches that regression.
STARTUP_HANDSHAKE_DEADLINE_SECONDS: float = getattr(session_module, "_STARTUP_READ_TIMEOUT", 60.0)
# How long a stub may take to build the process topology a test waits for.
STUB_TOPOLOGY_TIMEOUT_SECONDS = 15.0


def _proc_stat(pid: int | str) -> tuple[str, int] | None:
    """Return (state, pgrp) of pid from /proc/<pid>/stat, or None once it is gone."""
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None
    # Fields after the parenthesised comm are: state ppid pgrp ...
    fields = stat.rpartition(")")[2].split()
    return fields[0], int(fields[2])


def _is_live(pid: int | str) -> bool:
    """Whether pid still runs; zombie (Z) and dead (X) entries do not count.

    The E2E container runs pytest as PID 1 without an orphan reaper, so
    orphaned session descendants stay zombies forever. Only live processes are
    survivors. A directly owned leader is different: Python must reap it, which
    _leader_reaped checks separately.
    """
    stat = _proc_stat(pid)
    return stat is not None and stat[0] not in ("Z", "X")


def _tagged_pids(tag: str) -> set[int]:
    """Live pids whose argv contains tag (stubs mark themselves with exec -a)."""
    pids: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if tag.encode() in cmdline and _is_live(entry.name):
            pids.add(int(entry.name))
    return pids


def _group_pids(pgid: int) -> set[int]:
    """Live pids belonging to the process group pgid (zombies excluded, see _is_live)."""
    pids: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        stat = _proc_stat(entry.name)
        if stat is not None and stat[1] == pgid and stat[0] not in ("Z", "X"):
            pids.add(int(entry.name))
    return pids


def _live_kwin_pids() -> set[int]:
    return {pid for pid in _kwin_pids() if _is_live(pid)}


def _leader_reaped(pid: int) -> bool:
    """A directly owned leader must be gone from /proc, not left as a zombie."""
    return not (Path("/proc") / str(pid)).exists()


def _is_zombie(pid: int) -> bool:
    """Whether pid is currently an unreaped zombie (state Z) in /proc."""
    stat = _proc_stat(pid)
    return stat is not None and stat[0] == "Z"


def _wait_until(predicate: Callable[[], object], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def _record_pgid(path: Path) -> str:
    """Bash lines that atomically write the script's process group id to path."""
    target = shlex.quote(str(path))
    partial = shlex.quote(f"{path}.partial")
    return (
        "read -r -a _stat < /proc/$$/stat\n"
        f'printf "%s\\n" "${{_stat[4]}}" > {partial}\n'
        f"mv {partial} {target}\n"
    )


def _read_pgid(path: Path) -> int | None:
    try:
        return int(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


def _install_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, script: str) -> None:
    """Put an executable stub for name first on PATH; monkeypatch restores PATH."""
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / name
    stub.write_text(script)
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")


def _spawn_neighbor(tag: str) -> subprocess.Popen[bytes]:
    """An unrelated process in its own group that the session must not kill."""
    return subprocess.Popen(
        ["bash", "-c", f"exec -a {tag} sleep infinity"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _reap(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired, ProcessLookupError):
        process.wait(timeout=3)


class _BackgroundStart:
    """Session.start() on a daemon thread, so an unbounded hang stays observable."""

    def __init__(self, session: Session, config: SessionConfig) -> None:
        self.session = session
        self.error: BaseException | None = None
        self.elapsed: float | None = None
        self._config = config
        self._started = time.monotonic()
        self.thread = threading.Thread(
            target=self._run, name=f"session-start-{config.socket_name}", daemon=True
        )
        self.thread.start()

    def _run(self) -> None:
        try:
            self.session.start(self._config)
        except BaseException as exc:
            self.error = exc
        finally:
            self.elapsed = time.monotonic() - self._started

    def finished_within(self, budget: float) -> bool:
        """Join until budget seconds after the start call; True if it returned."""
        self.thread.join(timeout=max(0.0, self._started + budget - time.monotonic()))
        return not self.thread.is_alive()


@pytest.fixture
def background_start() -> Iterator[Callable[[Session, SessionConfig], _BackgroundStart]]:
    """Run starts in the background and require every worker joined after recovery."""
    runs: list[_BackgroundStart] = []

    def _start(session: Session, config: SessionConfig) -> _BackgroundStart:
        run = _BackgroundStart(session, config)
        runs.append(run)
        return run

    yield _start
    for run in runs:
        run.thread.join(timeout=STUB_STOP_BUDGET_SECONDS)
    leaked = [run.thread.name for run in runs if run.thread.is_alive()]
    assert not leaked, f"startup workers still running after recovery: {leaked}"


@dataclass(frozen=True)
class _FailedStart:
    """Everything a failed start left behind, captured before any recovery."""

    finished: bool
    elapsed: float | None
    error: BaseException | None
    pgid: int | None
    process_cleared: bool
    group_survivors: frozenset[int]
    tagged_survivors: frozenset[int]
    leader_reaped: bool


def _observe_failed_start(run: _BackgroundStart, pgid_file: Path, tag: str) -> _FailedStart:
    finished = run.finished_within(STUB_START_BUDGET_SECONDS)
    pgid = _read_pgid(pgid_file)
    return _FailedStart(
        finished=finished,
        elapsed=run.elapsed if finished else None,
        error=run.error,
        pgid=pgid,
        process_cleared=run.session._process is None,
        group_survivors=frozenset(_group_pids(pgid)) if pgid is not None else frozenset(),
        tagged_survivors=frozenset(_tagged_pids(tag)),
        leader_reaped=pgid is not None and _leader_reaped(pgid),
    )


def _assert_failed_start_cleaned_up(observed: _FailedStart) -> None:
    """start() itself must fail and tear down; the caller never calls stop()."""
    assert observed.finished, f"start() still blocked after its budget: {observed}"
    assert isinstance(observed.error, RuntimeError), observed
    assert observed.pgid is not None, f"stub never recorded its process group: {observed}"
    assert observed.process_cleared, f"failed start kept its process handle: {observed}"
    assert not observed.group_survivors, f"session group outlived start(): {observed}"
    assert not observed.tagged_survivors, f"stub processes outlived start(): {observed}"
    assert observed.leader_reaped, f"session leader was not reaped: {observed}"


def _recover(session: Session, run: _BackgroundStart | None, pgid: int | None, tag: str) -> None:
    """Kill everything the test owns, then join the worker and release the session."""
    if pgid is not None and _group_pids(pgid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
    for pid in _tagged_pids(tag):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    if run is not None:
        # Killing the group closes the stdout pipe, which unblocks even a
        # start() stuck in an unbounded read.
        run.thread.join(timeout=STUB_STOP_BUDGET_SECONDS)
    if run is None or not run.thread.is_alive():
        with contextlib.suppress(Exception):
            session.stop()


def test_session_start_fails_bounded_when_kwin_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    background_start: Callable[[Session, SessionConfig], _BackgroundStart],
) -> None:
    """kwin exits at once; the wrapper reports FAILED and start() tears down."""
    tag = f"stub48-exit-{os.getpid()}"
    pgid_file = tmp_path / "session.pgid"
    _install_stub(
        tmp_path,
        monkeypatch,
        "kwin_wayland",
        f"#!/bin/bash\n{_record_pgid(pgid_file)}echo 'kwin-stub: immediate failure' >&2\nexit 3\n",
    )
    session = Session()
    neighbor = _spawn_neighbor(f"neighbor48-exit-{os.getpid()}")
    run: _BackgroundStart | None = None
    observed: _FailedStart | None = None
    try:
        run = background_start(session, SessionConfig(socket_name=f"mcp48-{os.getpid()}-exit"))
        observed = _observe_failed_start(run, pgid_file, tag)
        neighbor_alive = neighbor.poll() is None

        _assert_failed_start_cleaned_up(observed)
        assert observed.elapsed is not None
        # FAILED ends the handshake; start() must not sit out the deadline.
        assert observed.elapsed < STARTUP_HANDSHAKE_DEADLINE_SECONDS, observed
        assert "immediate failure" in str(observed.error), observed
        assert neighbor_alive, "unrelated neighbor process was killed"
    finally:
        _recover(session, run, observed.pgid if observed else _read_pgid(pgid_file), tag)
        _reap(neighbor)


def test_session_start_cleans_up_when_descendant_holds_pipes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    background_start: Callable[[Session, SessionConfig], _BackgroundStart],
) -> None:
    """kwin dies but a detached descendant keeps stdout/stderr open.

    The leader exits while the holder lives, so the group signal must be sent
    to the pgid directly, not resolved through the reaped leader.
    """
    tag = f"stub48-held-{os.getpid()}"
    pgid_file = tmp_path / "session.pgid"
    _install_stub(
        tmp_path,
        monkeypatch,
        "kwin_wayland",
        f"#!/bin/bash\n{_record_pgid(pgid_file)}"
        f"(exec -a {tag} sleep infinity) &\n"
        "disown\n"
        "echo 'kwin-stub: died, detached child holds pipes' >&2\n"
        "exit 4\n",
    )
    session = Session()
    neighbor = _spawn_neighbor(f"neighbor48-held-{os.getpid()}")
    run: _BackgroundStart | None = None
    observed: _FailedStart | None = None
    try:
        run = background_start(session, SessionConfig(socket_name=f"mcp48-{os.getpid()}-held"))
        observed = _observe_failed_start(run, pgid_file, tag)
        neighbor_alive = neighbor.poll() is None

        _assert_failed_start_cleaned_up(observed)
        assert observed.elapsed is not None
        assert observed.elapsed < STARTUP_HANDSHAKE_DEADLINE_SECONDS, observed
        assert "holds pipes" in str(observed.error), observed
        assert neighbor_alive, "unrelated neighbor process was killed"
    finally:
        _recover(session, run, observed.pgid if observed else _read_pgid(pgid_file), tag)
        _reap(neighbor)


def test_session_start_hits_deadline_with_live_leader_and_partial_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    background_start: Callable[[Session, SessionConfig], _BackgroundStart],
) -> None:
    """The leader stays alive and stdout stays open without a terminal line.

    The stub replaces dbus-run-session: it runs the real one around a
    controlled session that prints the bus address, leaves a partial line and
    then sleeps, so the wrapper's own FAILED report never arrives. Only
    Session's handshake deadline can end startup, and it must then tear the
    group down while a healthy, unrelated KWin keeps running.
    """
    real_dbus_run_session = shutil.which("dbus-run-session")
    assert real_dbus_run_session is not None, "dbus-run-session not found in PATH"
    tag = f"stub48-deadline-{os.getpid()}"
    marker = "kwin-stub-partial-no-newline"
    pgid_file = tmp_path / "session.pgid"
    controlled = tmp_path / "controlled-session.sh"
    controlled.write_text(
        f"{_record_pgid(pgid_file)}"
        "printf 'DBUS_SESSION_BUS_ADDRESS=%s\\n' \"$DBUS_SESSION_BUS_ADDRESS\"\n"
        f"printf '%s' '{marker}'\n"
        f"exec -a {tag} sleep infinity\n"
    )
    session = Session()
    neighbor = _spawn_neighbor(f"neighbor48-deadline-{os.getpid()}")
    run: _BackgroundStart | None = None
    observed: _FailedStart | None = None
    try:
        # The healthy control starts before the stub shadows dbus-run-session.
        with live_kwin() as live:
            _install_stub(
                tmp_path,
                monkeypatch,
                "dbus-run-session",
                "#!/bin/bash\n"
                "# Ignore Session's wrapper script; run a controlled session instead.\n"
                f"exec {shlex.quote(real_dbus_run_session)} -- "
                f"bash {shlex.quote(str(controlled))}\n",
            )
            run = background_start(
                session, SessionConfig(socket_name=f"mcp48-{os.getpid()}-deadline")
            )

            def topology_ready() -> bool:
                pgid = _read_pgid(pgid_file)
                if pgid is None:
                    return False
                members = _group_pids(pgid)
                return pgid in members and bool(_tagged_pids(tag) & members)

            topology_formed = _wait_until(topology_ready, STUB_TOPOLOGY_TIMEOUT_SECONDS)
            still_starting = topology_formed and run.thread.is_alive()
            observed = _observe_failed_start(run, pgid_file, tag)
            neighbor_alive = neighbor.poll() is None
            control_alive = live.process.poll() is None and live.socket_path.is_socket()

            assert topology_formed, "controlled session never started its leader and sleeper"
            assert still_starting, observed
            _assert_failed_start_cleaned_up(observed)
            assert observed.elapsed is not None
            # Nothing but the deadline could end this handshake.
            assert observed.elapsed >= STARTUP_HANDSHAKE_DEADLINE_SECONDS, observed
            assert marker in str(observed.error), observed
            assert neighbor_alive, "unrelated neighbor process was killed"
            assert control_alive, "healthy test-owned KWin did not survive the failed start"
    finally:
        _recover(session, run, observed.pgid if observed else _read_pgid(pgid_file), tag)
        _reap(neighbor)


def test_session_start_stays_bounded_when_leader_is_killed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    background_start: Callable[[Session, SessionConfig], _BackgroundStart],
) -> None:
    """SIGKILL the dbus-run-session leader and wrapper bash mid-startup.

    Models an external kill (OOM/operator) where the surviving kwin stub keeps
    holding the stdout pipe: start() must notice the dead leader and fail well
    before its handshake deadline instead of blocking on the read forever.
    """
    tag = f"stub48-killed-{os.getpid()}"
    pgid_file = tmp_path / "session.pgid"
    _install_stub(
        tmp_path,
        monkeypatch,
        "kwin_wayland",
        f"#!/bin/bash\n{_record_pgid(pgid_file)}exec -a {tag} sleep infinity\n",
    )
    session = Session()
    neighbor = _spawn_neighbor(f"neighbor48-killed-{os.getpid()}")
    run: _BackgroundStart | None = None
    observed: _FailedStart | None = None
    try:
        run = background_start(session, SessionConfig(socket_name=f"mcp48-{os.getpid()}-kill"))

        def stub_running() -> bool:
            pgid = _read_pgid(pgid_file)
            return pgid is not None and bool(_tagged_pids(tag) & _group_pids(pgid))

        # The kwin stub runs only after the wrapper printed the bus address and
        # entered its socket wait, so startup is now blocked in the handshake.
        topology_formed = _wait_until(stub_running, STUB_TOPOLOGY_TIMEOUT_SECONDS)
        still_starting = run.thread.is_alive()
        assert topology_formed, "kwin_wayland stub never ran inside the session group"
        assert still_starting, "start() returned before the leader could be killed"
        pgid = _read_pgid(pgid_file)
        assert pgid is not None

        # Kill the leader plus wrapper bash only, leaving the kwin stub (and
        # dbus-daemon) alive and still holding the inherited stdout pipe.
        with contextlib.suppress(ProcessLookupError):
            os.kill(pgid, signal.SIGKILL)
        for pid in _group_pids(pgid):
            try:
                comm = (Path("/proc") / str(pid) / "comm").read_text().strip()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if comm == "bash":
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)

        observed = _observe_failed_start(run, pgid_file, tag)
        neighbor_alive = neighbor.poll() is None

        _assert_failed_start_cleaned_up(observed)
        assert observed.elapsed is not None
        # The dead leader, not the handshake deadline, must end startup.
        assert observed.elapsed < STARTUP_HANDSHAKE_DEADLINE_SECONDS, observed
        assert neighbor_alive, "unrelated neighbor process was killed"
    finally:
        _recover(session, run, observed.pgid if observed else _read_pgid(pgid_file), tag)
        _reap(neighbor)


def test_session_stop_recovers_after_leader_is_killed(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    """stop() must clean up the group even when the leader was reaped.

    is_running polls (reaps) the leader, after which getpgid(pid) would raise
    ESRCH; the group must still be signalled. An unrelated neighboring process
    group must survive.
    """
    neighbor = _spawn_neighbor(f"neighbor48-stop-{os.getpid()}")
    pre_existing_kwin = _live_kwin_pids()
    pgid: int | None = None
    try:
        info = start_session()
        assert info.kind == "virtual", info
        session = engine._session
        assert isinstance(session, Session)
        leader = session._process
        assert leader is not None
        pgid = leader.pid
        assert _live_kwin_pids() & _group_pids(pgid), "session KWin is not in the session group"

        os.kill(leader.pid, signal.SIGKILL)
        exit_deadline = time.monotonic() + PROCESS_EXIT_TIMEOUT_SECONDS
        while session.is_running and time.monotonic() < exit_deadline:
            time.sleep(0.05)
        assert not session.is_running, "session leader did not exit after SIGKILL"
        _wait_for_process_exit(leader.pid)

        started = time.monotonic()
        result = engine.session_stop()
        elapsed = time.monotonic() - started
        process_cleared = session._process is None
        group_survivors = _group_pids(pgid)
        kwin_survivors = _live_kwin_pids() - pre_existing_kwin
        neighbor_alive = neighbor.poll() is None

        assert result.render() == "Session stopped."
        assert elapsed < STUB_STOP_BUDGET_SECONDS, elapsed
        assert process_cleared
        assert not group_survivors, f"session group {pgid} survived stop(): {group_survivors}"
        assert not kwin_survivors, f"session KWin survived stop(): {kwin_survivors}"
        assert neighbor_alive, "unrelated neighbor process was killed"
    finally:
        engine.session_stop()
        if pgid is not None and _group_pids(pgid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
        _reap(neighbor)


def test_session_stop_kills_term_ignoring_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A group member that ignores SIGTERM must not survive stop().

    The stub wraps the real kwin_wayland so the session starts for real, but
    leaves a TERM-ignoring holder behind; even after the leader exits, the
    holder stays in the group and must be escalated to SIGKILL.
    """
    real_kwin = shutil.which("kwin_wayland")
    assert real_kwin is not None, "kwin_wayland not found in PATH"
    tag = f"stub48-term-{os.getpid()}"
    _install_stub(
        tmp_path,
        monkeypatch,
        "kwin_wayland",
        "#!/bin/bash\n"
        f"bash -c 'trap \"\" TERM; exec -a {tag} sleep infinity' &\n"
        "disown\n"
        f'exec {shlex.quote(real_kwin)} "$@"\n',
    )
    session = Session()
    neighbor = _spawn_neighbor(f"neighbor48-term-{os.getpid()}")
    pgid: int | None = None
    try:
        info = session.start(SessionConfig(socket_name=f"mcp48-{os.getpid()}-term"))
        assert info.kwin_pid > 0
        leader = session._process
        assert leader is not None
        session_pgid = leader.pid
        pgid = session_pgid
        assert _wait_until(
            lambda: bool(_tagged_pids(tag) & _group_pids(session_pgid)),
            PROCESS_EXIT_TIMEOUT_SECONDS,
        ), "TERM-ignoring holder did not start inside the session group"

        started = time.monotonic()
        session.stop()
        elapsed = time.monotonic() - started
        process_cleared = session._process is None
        group_survivors = _group_pids(pgid)
        tagged_survivors = _tagged_pids(tag)
        leader_reaped = _leader_reaped(pgid)
        neighbor_alive = neighbor.poll() is None

        assert elapsed < STUB_STOP_BUDGET_SECONDS, elapsed
        assert process_cleared
        assert not tagged_survivors, "SIGTERM-ignoring descendant survived stop()"
        assert not group_survivors, f"session group {pgid} survived stop(): {group_survivors}"
        assert leader_reaped, "session leader was not reaped"
        assert neighbor_alive, "unrelated neighbor process was killed"
    finally:
        _recover(session, None, pgid, tag)
        _reap(neighbor)


# ---------------------------------------------------------------------------
# KWin bus-name readiness (issue #74)
#
# KWin creates its Wayland socket before it owns org.kde.KWin on the session
# bus, and the EIS input interface lives under that name. These kwin_wayland
# stubs widen the gap at the PATH boundary: they create the socket path first
# and start the real compositor late, or never.
# ---------------------------------------------------------------------------

# How long session_start must keep waiting while KWin is held back. The old
# startup asked for EIS about 1 s after the socket appeared.
KWIN_NAME_HOLD_SECONDS = 5.0
KWIN_BUS_NAME_TIMEOUT_SECONDS: float = getattr(session_module, "_KWIN_BUS_NAME_TIMEOUT", 30.0)


def _early_socket_stub(pgid_file: Path, then: str) -> str:
    """kwin_wayland stub that creates its socket path at once, then runs `then`."""
    return (
        "#!/bin/bash\n"
        f"{_record_pgid(pgid_file)}"
        'args=("$@")\n'
        "for ((i = 0; i < ${#args[@]}; i++)); do\n"
        '    [ "${args[i]}" = --socket ] && socket=${args[i + 1]}\n'
        "done\n"
        ': > "$XDG_RUNTIME_DIR/$socket"\n'
        f"{then}"
    )


def test_session_start_waits_for_kwin_bus_name_before_input_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: AutomationEngine
) -> None:
    """session_start must not request EIS before KWin owns org.kde.KWin."""
    real_kwin = shutil.which("kwin_wayland")
    assert real_kwin is not None, "kwin_wayland not found in PATH"
    gate = tmp_path / "release-kwin"
    pgid_file = tmp_path / "session.pgid"
    _install_stub(
        tmp_path,
        monkeypatch,
        "kwin_wayland",
        _early_socket_stub(
            pgid_file,
            f"until [ -e {shlex.quote(str(gate))} ]; do sleep 0.05; done\n"
            'rm -f "$XDG_RUNTIME_DIR/$socket"\n'
            f'exec {shlex.quote(real_kwin)} "$@"\n',
        ),
    )
    outcome: list[SessionInfo | BaseException] = []

    def run() -> None:
        try:
            outcome.append(engine.session_start())
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run, name="session-start-kwin-name", daemon=True)
    worker.start()
    try:
        stub_ran = _wait_until(
            lambda: _read_pgid(pgid_file) is not None, STUB_TOPOLOGY_TIMEOUT_SECONDS
        )
        worker.join(timeout=KWIN_NAME_HOLD_SECONDS)
        returned_while_held = not worker.is_alive()
    finally:
        gate.touch()
        worker.join(timeout=STUB_START_BUDGET_SECONDS)

    assert stub_ran, "kwin_wayland stub never ran"
    assert not worker.is_alive(), "session_start still blocked after KWin was released"
    assert not returned_while_held, f"returned before KWin owned org.kde.KWin: {outcome}"
    assert len(outcome) == 1, outcome
    assert not isinstance(outcome[0], BaseException), outcome
    assert outcome[0].input_backend == "eis", outcome[0]


def test_session_start_fails_fast_when_kwin_exits_before_owning_its_bus_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    background_start: Callable[[Session, SessionConfig], _BackgroundStart],
) -> None:
    """KWin dies after its socket appeared but before owning org.kde.KWin."""
    tag = f"stub-kwin-name-exit-{os.getpid()}"
    pgid_file = tmp_path / "session.pgid"
    _install_stub(
        tmp_path,
        monkeypatch,
        "kwin_wayland",
        _early_socket_stub(pgid_file, f"exec -a {tag} sleep infinity\n"),
    )
    session = Session()
    run: _BackgroundStart | None = None
    observed: _FailedStart | None = None
    try:
        run = background_start(session, SessionConfig(socket_name=f"mcpname-{os.getpid()}-exit"))
        # The tagged process exists only after the socket path was created.
        stub_running = _wait_until(lambda: bool(_tagged_pids(tag)), STUB_TOPOLOGY_TIMEOUT_SECONDS)
        assert stub_running, "kwin_wayland stub never ran"
        for pid in _tagged_pids(tag):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)

        observed = _observe_failed_start(run, pgid_file, tag)

        _assert_failed_start_cleaned_up(observed)
        assert observed.elapsed is not None
        # The exit, not the bus-name deadline, must end startup.
        assert observed.elapsed < KWIN_BUS_NAME_TIMEOUT_SECONDS, observed
        assert "org.kde.KWin" in str(observed.error), observed
    finally:
        _recover(session, run, observed.pgid if observed else _read_pgid(pgid_file), tag)


def test_session_start_fails_bounded_when_kwin_never_owns_its_bus_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    background_start: Callable[[Session, SessionConfig], _BackgroundStart],
) -> None:
    """A compositor that never takes org.kde.KWin fails start() at the deadline."""
    bus_name_timeout = 2.0
    monkeypatch.setattr(session_module, "_KWIN_BUS_NAME_TIMEOUT", bus_name_timeout, raising=False)
    tag = f"stub-kwin-name-never-{os.getpid()}"
    pgid_file = tmp_path / "session.pgid"
    _install_stub(
        tmp_path,
        monkeypatch,
        "kwin_wayland",
        _early_socket_stub(pgid_file, f"exec -a {tag} sleep infinity\n"),
    )
    session = Session()
    run: _BackgroundStart | None = None
    observed: _FailedStart | None = None
    try:
        run = background_start(session, SessionConfig(socket_name=f"mcpname-{os.getpid()}-never"))
        observed = _observe_failed_start(run, pgid_file, tag)

        _assert_failed_start_cleaned_up(observed)
        assert observed.elapsed is not None
        # The wrapper's deadline is in whole $SECONDS plus a probe round, so
        # it can fire almost two short of a tight patched budget.
        assert observed.elapsed > bus_name_timeout - 2.0, observed
        assert "org.kde.KWin" in str(observed.error), observed
    finally:
        _recover(session, run, observed.pgid if observed else _read_pgid(pgid_file), tag)


def _bus_daemon_pid(leader_pid: int) -> int | None:
    """PID of the dbus-daemon under a dbus-run-session leader, or None."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text().strip() != "dbus-daemon":
                continue
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        # Fields after the comm's ")" are: state ppid pgrp ...
        fields = stat.rpartition(")")[2].split()
        if len(fields) > 2 and int(fields[1]) == leader_pid:
            return int(entry.name)
    return None


def test_session_start_stays_bounded_when_the_bus_stalls_during_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    background_start: Callable[[Session, SessionConfig], _BackgroundStart],
) -> None:
    """A bus that stalls while KWin registers its name fails at the deadline.

    The kwin stub creates the socket then sleeps forever, so the wrapper is
    inside its org.kde.KWin ownership loop. SIGSTOPping the session
    dbus-daemon makes every further dbus-send probe hang; start() must still
    fail through its startup-handshake deadline and tear the group down.
    """
    handshake_timeout = 8.0
    monkeypatch.setattr(session_module, "_STARTUP_READ_TIMEOUT", handshake_timeout, raising=False)
    tag = f"stub-bus-stall-{os.getpid()}"
    pgid_file = tmp_path / "session.pgid"
    _install_stub(
        tmp_path,
        monkeypatch,
        "kwin_wayland",
        _early_socket_stub(pgid_file, f"exec -a {tag} sleep infinity\n"),
    )
    session = Session()
    daemon_pid: int | None = None
    run: _BackgroundStart | None = None
    observed: _FailedStart | None = None
    try:
        run = background_start(session, SessionConfig(socket_name=f"mcpname-{os.getpid()}-stall"))
        # The stub appears after the wrapper entered the name-wait loop.
        stub_running = _wait_until(
            lambda: _read_pgid(pgid_file) is not None, STUB_TOPOLOGY_TIMEOUT_SECONDS
        )
        assert stub_running, "kwin_wayland stub never ran"
        leader = session._process
        assert leader is not None
        daemon_pid = _bus_daemon_pid(leader.pid)
        assert daemon_pid is not None, "session dbus-daemon not found"
        os.kill(daemon_pid, signal.SIGSTOP)

        observed = _observe_failed_start(run, pgid_file, tag)

        _assert_failed_start_cleaned_up(observed)
        assert observed.elapsed is not None
        # The stalled daemon, not the wrapper's name deadline, ended startup.
        assert observed.elapsed >= handshake_timeout, observed
        assert "READY" in str(observed.error), observed
    finally:
        if daemon_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(daemon_pid, signal.SIGCONT)
        _recover(session, run, observed.pgid if observed else _read_pgid(pgid_file), tag)
