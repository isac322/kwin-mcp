"""End-to-end coverage for virtual and live session lifecycle semantics."""

from __future__ import annotations

import contextlib
import errno
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from _asserts import element_count
from session_harness import live_kwin

from kwin_mcp import session as session_module
from kwin_mcp.core import AutomationEngine
from kwin_mcp.session import Session, SessionConfig

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

BAD_BINARY = "definitely-not-a-real-binary"
SESSION_ALREADY_RUNNING = "Session already running. Call session_stop first."
LAUNCHED_PID = re.compile(r"\(PID=(\d+)\)")
PROCESS_EXIT_TIMEOUT_SECONDS = 5.0
A11Y_BUS_NAME = "org.a11y.Bus"
XDG_HOME_DIRECTORIES = (
    Path(".config"),
    Path(".local/share"),
    Path(".local/state"),
    Path(".cache"),
    Path(".screenshots"),
)


def _output_value(output: str, prefix: str) -> str:
    for line in output.splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix)
    pytest.fail(f"missing {prefix!r} in output: {output[:500]}")


def _launched_pid(output: str) -> int:
    match = LAUNCHED_PID.search(output)
    assert match is not None, output[:500]
    return int(match.group(1))


def _app_log_path(output: str) -> Path:
    return Path(_output_value(output, "App log: "))


def _wait_for_app_log(engine: AutomationEngine, pid: int, expected: str) -> str:
    deadline = time.monotonic() + PROCESS_EXIT_TIMEOUT_SECONDS
    output = ""
    while time.monotonic() < deadline:
        output = engine.read_app_log(pid, last_n_lines=0)
        if expected in output:
            return output
        time.sleep(0.05)
    pytest.fail(f"app {pid} did not log {expected!r}: {output[:500]}")


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


def test_double_start_keeps_the_original_compositor(
    engine: AutomationEngine, start_session: Callable[..., str]
) -> None:
    first_output = start_session()
    first_socket = _output_value(first_output, "Session started. Wayland socket: ")
    pids_after_first_start = _kwin_pids()

    second_output = engine.session_start(screen_width=640, screen_height=480)

    assert second_output == SESSION_ALREADY_RUNNING
    assert _kwin_pids() == pids_after_first_start
    assert (Path(os.environ["XDG_RUNTIME_DIR"]) / first_socket).is_socket()


def test_stop_without_a_session_and_repeated_stop_are_safe(
    engine: AutomationEngine, start_session: Callable[..., str]
) -> None:
    assert engine.session_stop() == "No session running."
    start_session()
    assert engine.session_stop() == "Session stopped."
    assert engine.session_stop() == "No session running."


def test_bad_app_command_raises_a_clear_error(
    engine: AutomationEngine, start_session: Callable[..., str]
) -> None:
    with pytest.raises(FileNotFoundError) as exc_info:
        start_session(BAD_BINARY)

    error = exc_info.value
    assert error.errno == errno.ENOENT
    assert error.filename == BAD_BINARY
    assert "No such file or directory" in str(error)
    assert BAD_BINARY in str(error)
    assert engine.session_stop() == "Session stopped."


def test_session_start_propagates_environment_to_the_initial_app(
    engine: AutomationEngine, start_session: Callable[..., str]
) -> None:
    variable = "KWIN_MCP_E2E_SESSION_ENV"
    value = "session-start-env-propagated"
    command = f"""sh -c 'printf "env:%s" "${variable}"'"""

    output = start_session(command, env={variable: value})
    pid = _launched_pid(output)

    assert _wait_for_app_log(engine, pid, f"env:{value}") == f"env:{value}"


def test_custom_screen_geometry_is_reported_by_wayland(engine: AutomationEngine) -> None:
    expected_width = 937
    expected_height = 613
    try:
        output = engine.session_start(
            screen_width=expected_width,
            screen_height=expected_height,
        )
        assert "Session started." in output, output

        wayland_output = engine.wayland_info()
        expected_mode = re.compile(
            rf"(?ms)^interface: 'wl_output'.*?^\s+mode:\s*$"
            rf".*?^\s+width: {expected_width} px, height: {expected_height} px,"
            rf".*?^\s+flags:.*\bcurrent\b"
        )
        assert expected_mode.search(wayland_output), wayland_output[:1000]
    finally:
        engine.session_stop()


def test_isolated_home_cleanup_and_keep_home(
    engine: AutomationEngine, start_session: Callable[..., str]
) -> None:
    output = start_session(isolate_home=True)
    temporary_home = Path(_output_value(output, "Isolated home: "))
    assert temporary_home.is_dir()
    assert all((temporary_home / path).is_dir() for path in XDG_HOME_DIRECTORIES)

    assert engine.session_stop() == "Session stopped."
    assert not temporary_home.exists()

    kept_output = start_session(isolate_home=True, keep_home=True)
    kept_home = Path(_output_value(kept_output, "Isolated home: "))
    try:
        assert kept_home.is_dir()
        assert all((kept_home / path).is_dir() for path in XDG_HOME_DIRECTORIES)
        assert engine.session_stop() == "Session stopped."
        assert kept_home.is_dir()
        assert all((kept_home / path).is_dir() for path in XDG_HOME_DIRECTORIES[:-1])
    finally:
        engine.session_stop()
        shutil.rmtree(kept_home, ignore_errors=True)


@pytest.mark.parametrize("keep_screenshots", [False, True], ids=["remove", "retain"])
def test_virtual_session_artifact_cleanup_respects_retention(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    keep_screenshots: bool,
) -> None:
    screenshot_dir: Path | None = None
    try:
        output = start_session(
            "printf virtual-retention-payload",
            keep_screenshots=keep_screenshots,
        )
        pid = _launched_pid(output)
        log_path = _app_log_path(output)
        screenshot_dir = log_path.parent
        assert _wait_for_app_log(engine, pid, "virtual-retention-payload") == (
            "virtual-retention-payload"
        )
        assert log_path.is_file()

        assert engine.session_stop() == "Session stopped."
        assert log_path.exists() is keep_screenshots
        assert screenshot_dir.exists() is keep_screenshots
    finally:
        engine.session_stop()
        if screenshot_dir is not None:
            shutil.rmtree(screenshot_dir, ignore_errors=True)


def test_isolated_home_retains_app_logs_when_both_retention_flags_are_enabled(
    engine: AutomationEngine, start_session: Callable[..., str]
) -> None:
    output = start_session(
        "printf isolated-home-retention-payload",
        isolate_home=True,
        keep_home=True,
        keep_screenshots=True,
    )
    home = Path(_output_value(output, "Isolated home: "))
    try:
        pid = _launched_pid(output)
        log_path = _app_log_path(output)
        assert log_path.parent == home / ".screenshots"
        assert _wait_for_app_log(engine, pid, "isolated-home-retention-payload") == (
            "isolated-home-retention-payload"
        )
        assert log_path.is_file()

        assert engine.session_stop() == "Session stopped."
        assert home.is_dir()
        assert log_path.is_file()
    finally:
        engine.session_stop()
        shutil.rmtree(home, ignore_errors=True)


def test_session_stop_removes_the_wayland_socket(
    engine: AutomationEngine, start_session: Callable[..., str]
) -> None:
    output = start_session()
    socket_name = _output_value(output, "Session started. Wayland socket: ")
    socket_path = Path(os.environ["XDG_RUNTIME_DIR"]) / socket_name
    assert socket_path.is_socket()

    assert engine.session_stop() == "Session stopped."
    assert not socket_path.exists()


def test_session_start_replaces_stale_socket_and_lock_files(
    engine: AutomationEngine, start_session: Callable[..., str]
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
        output = start_session()
        socket_name = _output_value(output, "Session started. Wayland socket: ")
        socket_path = runtime_dir / socket_name
        lock_path = runtime_dir / f"{socket_name}.lock"

        assert socket_name in candidate_names
        assert socket_path.is_socket()

        assert engine.session_stop() == "Session stopped."
        assert not socket_path.exists()
        assert not lock_path.exists()
    finally:
        engine.session_stop()
        for stale_path in stale_paths:
            stale_path.unlink(missing_ok=True)


def test_session_stop_terminates_and_reaps_launched_process(
    engine: AutomationEngine, start_session: Callable[..., str]
) -> None:
    output = start_session("sleep 300")
    pid = _launched_pid(output)
    assert (Path("/proc") / str(pid)).exists()

    try:
        assert engine.session_stop() == "Session stopped."
        _wait_for_process_exit(pid)
    finally:
        engine.session_stop()


def _a11y_bus_has_owner(engine: AutomationEngine) -> str:
    return engine.dbus_call(
        service="org.freedesktop.DBus",
        path="/org/freedesktop/DBus",
        interface="org.freedesktop.DBus",
        method="NameHasOwner",
        args=[f"string:{A11Y_BUS_NAME}"],
    )


def test_accessibility_bus_is_owned_when_session_start_returns(
    engine: AutomationEngine, start_session: Callable[..., str]
) -> None:
    # Qt apps join the accessibility bus only when org.a11y.Bus already has an
    # owner; otherwise the first accessibility_tree() finds no apps. Asking the
    # bus daemon (no app, no AT-SPI2 query) cannot activate the name itself, so
    # this observes exactly what session_start left behind.
    output = start_session()
    assert "Warning:" not in output, output

    reply = _a11y_bus_has_owner(engine)
    assert reply.split()[-1:] == ["true"], reply


def test_session_start_reports_a_failed_accessibility_bus_activation(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only the wrapper resolves dbus-send through the patched PATH; the stub
    # stands in for a distro without an org.a11y.Bus service file.
    _install_stub(
        tmp_path,
        monkeypatch,
        "dbus-send",
        "#!/bin/sh\n"
        "echo 'Error org.freedesktop.DBus.Error.ServiceUnknown: stub has no a11y bus' >&2\n"
        "echo 'second stderr line' >&2\n"
        "exit 1\n",
    )

    output = start_session()

    # The session stays usable for everything but accessibility, and the
    # failure reaches the caller as one line carrying the D-Bus error.
    assert "Session started. Wayland socket: " in output, output
    warnings = [line for line in output.splitlines() if line.startswith("Warning: ")]
    assert warnings == [
        "Warning: AT-SPI bus activation failed, accessibility tools may be unavailable in this"
        " session: Error org.freedesktop.DBus.Error.ServiceUnknown: stub has no a11y bus"
    ], output

    monkeypatch.undo()
    assert _a11y_bus_has_owner(engine).split()[-1:] == ["false"]


def test_connects_to_the_second_compositor_without_owning_it(
    engine: AutomationEngine, wait_for_app: Callable[[str], str]
) -> None:
    first_engine = AutomationEngine()
    first_output = first_engine.session_start(screen_width=1280, screen_height=800)
    first_display = _output_value(first_output, "Session started. Wayland socket: ")
    first_socket = Path(os.environ["XDG_RUNTIME_DIR"]) / first_display

    try:
        with live_kwin() as live:
            assert live.wayland_display != first_display
            assert first_socket.is_socket()
            assert live.process.poll() is None
            assert live.socket_path.is_socket()

            output = engine.session_connect(
                dbus_address=live.dbus_address,
                wayland_display=live.wayland_display,
            )
            first_line = output.splitlines()[0]
            assert first_line == (
                "Connected to live KWin session. "
                f"D-Bus: {live.dbus_address}, Wayland: {live.wayland_display}"
            )

            launch_output = engine.launch_app("kcalc")
            assert launch_output.startswith("App launched: kcalc (PID=")
            assert element_count(wait_for_app("kcalc")) > 0

            assert engine.session_stop() == "Disconnected from live session."
            assert live.process.poll() is None
            assert live.socket_path.is_socket()
            assert first_socket.is_socket()

        assert live.process.poll() is not None
        assert not live.socket_path.exists()
        assert first_socket.is_socket()
    finally:
        first_engine.session_stop()

    assert not first_socket.exists()


def test_session_connect_rejects_an_explicit_unreachable_dbus_address(
    engine: AutomationEngine, tmp_path: Path
) -> None:
    dbus_address = f"unix:path={tmp_path / 'missing-session-bus'}"

    output = engine.session_connect(
        dbus_address=dbus_address,
        wayland_display="wayland-missing",
    )

    assert output.startswith(f"Cannot reach KWin on D-Bus ({dbus_address}):"), output
    assert engine.session_stop() == "No session running."


def test_session_connect_rejects_an_explicit_missing_wayland_socket(
    engine: AutomationEngine,
) -> None:
    with live_kwin() as live:
        missing_display = f"wayland-missing-{uuid4().hex}"
        try:
            output = engine.session_connect(
                dbus_address=live.dbus_address,
                wayland_display=missing_display,
            )

            assert output.startswith(f"Cannot reach Wayland display ({missing_display}):"), output
            assert engine.session_stop() == "No session running."
            assert live.process.poll() is None
            assert live.socket_path.is_socket()
        finally:
            engine.session_stop()


def test_session_connect_auto_discovery_reports_each_missing_environment_variable(
    engine: AutomationEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    assert engine.session_connect() == (
        "No D-Bus address available. Provide dbus_address parameter "
        "or ensure $DBUS_SESSION_BUS_ADDRESS is set."
    )

    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/not-consulted")
    assert engine.session_connect() == (
        "No Wayland display available. Provide wayland_display parameter "
        "or ensure $WAYLAND_DISPLAY is set."
    )
    assert engine.session_stop() == "No session running."


def test_session_connect_rejects_replacing_an_active_virtual_session(
    engine: AutomationEngine, start_session: Callable[..., str]
) -> None:
    output = start_session()
    socket_name = _output_value(output, "Session started. Wayland socket: ")
    socket_path = Path(os.environ["XDG_RUNTIME_DIR"]) / socket_name

    with live_kwin() as live:
        assert (
            engine.session_connect(
                dbus_address=live.dbus_address,
                wayland_display=live.wayland_display,
            )
            == SESSION_ALREADY_RUNNING
        )
        assert socket_path.is_socket()
        assert live.process.poll() is None
        assert live.socket_path.is_socket()


@pytest.mark.parametrize("keep_screenshots", [False, True], ids=["remove", "retain"])
def test_live_session_artifact_cleanup_respects_retention(
    engine: AutomationEngine,
    keep_screenshots: bool,
) -> None:
    screenshot_dir: Path | None = None
    with live_kwin() as live:
        try:
            output = engine.session_connect(
                dbus_address=live.dbus_address,
                wayland_display=live.wayland_display,
                keep_screenshots=keep_screenshots,
            )
            assert output.startswith("Connected to live KWin session."), output

            launch_output = engine.launch_app("sleep 300")
            log_path = _app_log_path(launch_output)
            screenshot_dir = log_path.parent
            assert log_path.is_file()

            assert engine.session_stop() == "Disconnected from live session."
            assert log_path.exists() is keep_screenshots
            assert screenshot_dir.exists() is keep_screenshots
            assert live.process.poll() is None
            assert live.socket_path.is_socket()
        finally:
            engine.session_stop()
            if screenshot_dir is not None:
                shutil.rmtree(screenshot_dir, ignore_errors=True)


def test_live_session_disconnect_terminates_and_reaps_launched_process(
    engine: AutomationEngine,
) -> None:
    with live_kwin() as live:
        output = engine.session_connect(
            dbus_address=live.dbus_address,
            wayland_display=live.wayland_display,
        )
        assert output.startswith("Connected to live KWin session."), output

        launch_output = engine.launch_app("sleep 300")
        pid = _launched_pid(launch_output)
        assert (Path("/proc") / str(pid)).exists()

        try:
            assert engine.session_stop() == "Disconnected from live session."
            _wait_for_process_exit(pid)
            assert live.process.poll() is None
            assert live.socket_path.is_socket()
        finally:
            engine.session_stop()


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
    engine: AutomationEngine, start_session: Callable[..., str]
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
        output = start_session()
        assert "Session started" in output, output
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

        assert result == "Session stopped."
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
