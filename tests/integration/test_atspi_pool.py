"""Tests for the AT-SPI multiprocessing.Pool worker integration in core.py."""

from __future__ import annotations

import contextlib
import multiprocessing
import multiprocessing.pool
import os
import signal
import subprocess
import threading
import time
import uuid
from multiprocessing import get_context
from typing import TYPE_CHECKING

import pytest

from kwin_mcp.accessibility_worker import _init_atspi_worker

if TYPE_CHECKING:
    from kwin_mcp.core import AutomationEngine

pytestmark = pytest.mark.kwin


def _pid_alive(pid: int) -> bool:
    """Return True if ``pid`` still answers signal 0 (running or zombie not yet reaped).

    ProcessLookupError = pid is gone. Anything else (PermissionError) is unexpected
    for our own children and is allowed to propagate.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _pgrep_has_worker_pid(pid: int) -> bool:
    result = subprocess.run(
        ["pgrep", "-f", "_init_atspi_worker"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return str(pid) in result.stdout.split()


def _join_worker_processes(pool: multiprocessing.pool.Pool) -> None:
    for proc in getattr(pool, "_pool", []):
        proc.join(timeout=0.2)


def _worker_pids(pool: multiprocessing.pool.Pool) -> list[int]:
    procs = getattr(pool, "_pool", [])
    return [p.pid for p in procs if p.pid is not None]


def _ensure_session(engine: AutomationEngine) -> None:
    if engine._session is None or not engine._session.is_running:
        engine.session_start(screen_width=1920, screen_height=1080)


def test_pool_cold_start_under_1500ms(engine: AutomationEngine) -> None:
    """Cold first accessibility_tree call (spawn worker + bind AT-SPI) finishes under 1.5s."""
    _ensure_session(engine)
    if engine._atspi_pool is not None:
        engine._teardown_atspi_pool()

    t0 = time.perf_counter()
    out = engine.accessibility_tree(app_name="")
    cold_ms = (time.perf_counter() - t0) * 1000.0

    assert isinstance(out, str)
    assert cold_ms < 1500.0, f"cold start {cold_ms:.1f}ms >= 1500ms threshold"


def test_pool_warm_call_under_200ms(engine: AutomationEngine) -> None:
    """Warm reuse of an already-spawned worker keeps each tree call under 200ms."""
    _ensure_session(engine)
    t0 = time.perf_counter()
    engine.accessibility_tree(app_name="")
    cold_ms = (time.perf_counter() - t0) * 1000.0

    warm_timings: list[float] = []
    for _ in range(2):
        ts = time.perf_counter()
        out = engine.accessibility_tree(app_name="")
        warm_timings.append((time.perf_counter() - ts) * 1000.0)
        assert isinstance(out, str)

    for idx, ms in enumerate(warm_timings, start=1):
        assert ms < 200.0, f"warm call #{idx} took {ms:.1f}ms (>= 200ms threshold)"
    assert min(warm_timings) <= cold_ms, (
        f"warm calls {warm_timings} not faster than cold {cold_ms:.1f}ms — pool not warming?"
    )


def test_pool_recovers_from_external_kill(engine: AutomationEngine) -> None:
    """SIGKILL on the worker is caught by ``_run_atspi`` retry, which respawns the pool."""
    _ensure_session(engine)
    out = engine.accessibility_tree(app_name="")
    assert isinstance(out, str)
    pool_before = engine._atspi_pool
    assert pool_before is not None
    pids = _worker_pids(pool_before)
    assert pids, "expected at least one live worker pid"
    worker_pid = pids[0]

    os.kill(worker_pid, signal.SIGKILL)
    deadline = time.perf_counter() + 0.5
    while _pid_alive(worker_pid) and time.perf_counter() < deadline:
        time.sleep(0.01)

    out = engine.accessibility_tree(app_name="")
    assert isinstance(out, str)
    pool_after = engine._atspi_pool
    assert pool_after is not None
    assert pool_after is not pool_before, "pool was not rebuilt after worker death"


def test_pool_init_failure_raises_with_bus_address() -> None:
    """Spawn-context Pool with an unreachable DBUS bus address fails — never silently succeeds."""
    ctx = get_context("spawn")
    bad_addr = f"unix:path=/tmp/nonexistent-bus-for-test-{uuid.uuid4().hex}"
    pool = ctx.Pool(processes=1, initializer=_init_atspi_worker, initargs=(bad_addr,))
    try:
        with pytest.raises(BaseException) as excinfo:
            pool.apply_async(os.getpid).get(timeout=5)
        # On Python 3.12, ``Atspi.get_desktop`` aborts the worker via dbind-ERROR before
        # our RuntimeError is raised; the Pool respawns workers that all die, so .get()
        # raises ``multiprocessing.TimeoutError`` after the 5s budget. On Python 3.13+
        # the original RuntimeError propagates instead. Accept either form so the test
        # remains valid across versions, and at minimum verify that .get() did NOT
        # silently return a pid (which would mean init succeeded).
        msg = str(excinfo.value)
        is_timeout = isinstance(excinfo.value, multiprocessing.TimeoutError)
        is_init_err = "AT-SPI worker init failed" in msg and bad_addr in msg
        assert is_timeout or is_init_err, (
            f"expected TimeoutError or AT-SPI init failure containing {bad_addr!r}; "
            f"got {type(excinfo.value).__name__}: {msg[:200]!r}"
        )
    finally:
        pool.terminate()
        pool.join()


def test_pool_terminate_within_7s_even_if_hung(engine: AutomationEngine) -> None:
    """SIGSTOP'd worker can't shut down gracefully; session_stop must escalate within 7.5s."""
    _ensure_session(engine)
    engine.accessibility_tree(app_name="")
    pool = engine._atspi_pool
    assert pool is not None
    pids = _worker_pids(pool)
    assert pids
    worker_pid = pids[0]

    os.kill(worker_pid, signal.SIGSTOP)
    cont_timer = threading.Timer(5.2, os.kill, args=(worker_pid, signal.SIGCONT))
    cont_timer.start()
    try:
        t0 = time.perf_counter()
        engine.session_stop()
        elapsed = time.perf_counter() - t0

        assert elapsed < 7.5, f"session_stop took {elapsed:.2f}s (>= 7.5s threshold)"
        assert not _pid_alive(worker_pid), (
            f"worker pid {worker_pid} still alive after session_stop "
            f"(expected SIGKILL escalation to reap it)"
        )
    finally:
        cont_timer.cancel()
        # Best-effort SIGCONT in case the test failed mid-flight before SIGKILL landed.
        with contextlib.suppress(ProcessLookupError):
            os.kill(worker_pid, signal.SIGCONT)


def test_no_zombie_after_session_stop(engine: AutomationEngine) -> None:
    """``session_stop`` reaps the AT-SPI worker — no orphan PID may survive."""
    _ensure_session(engine)
    engine.accessibility_tree(app_name="")
    pool = engine._atspi_pool
    assert pool is not None
    pids = _worker_pids(pool)
    assert pids
    worker_pid = pids[0]

    engine.session_stop()
    try:
        assert not _pid_alive(worker_pid), (
            f"worker pid {worker_pid} still present after session_stop"
        )
        assert not _pgrep_has_worker_pid(worker_pid), (
            f"pgrep still reports AT-SPI worker pid {worker_pid} after session_stop"
        )
    finally:
        # Restart the session so the session-scoped ``engine`` fixture can be reused
        # by any test files that pytest collects after this one.
        _ensure_session(engine)
