"""Direct AT-SPI startup, operator opt-in, isolation, and rollback regressions."""

from __future__ import annotations

import builtins
import contextlib
import io
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import pytest
from _asserts import element_count
from test_session_processes import (
    STUB_START_BUDGET_SECONDS,
    STUB_STOP_BUDGET_SECONDS,
    _group_pids,
    _install_stub,
    _reap,
    _spawn_neighbor,
    _tagged_pids,
    _wait_until,
)

from kwin_mcp import session as session_module
from kwin_mcp.dbus_service_file import service_file_argv
from kwin_mcp.models import Point
from kwin_mcp.session import ATSPI_DIRECT_START_ENV, Session, SessionConfig

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import ElementList, SessionInfo

PROCESS_EXIT_TIMEOUT_SECONDS = 5.0
A11Y_BUS_NAME = "org.a11y.Bus"


def _wait_for_app_log(engine: AutomationEngine, pid: int, expected: str) -> str:
    deadline = time.monotonic() + PROCESS_EXIT_TIMEOUT_SECONDS
    output = ""
    while time.monotonic() < deadline:
        lines = engine.app_log(pid, last_n_lines=0).lines
        output = "\n".join(lines) + ("\n" if lines else "")
        if expected in output:
            return output
        time.sleep(0.05)
    pytest.fail(f"app {pid} did not log {expected!r}: {output}")


def _a11y_bus_has_owner(engine: AutomationEngine) -> str:
    reply = engine.dbus_call(
        service="org.freedesktop.DBus",
        path="/org/freedesktop/DBus",
        interface="org.freedesktop.DBus",
        method="NameHasOwner",
        args=[f"string:{A11Y_BUS_NAME}"],
    )
    assert isinstance(reply.reply, bool), reply
    return "true" if reply.reply else "false"


def _session_dbus_call(
    address: str,
    *,
    service: str,
    path: str,
    interface: str,
    method: str,
    args: list[str] | None = None,
) -> str:
    """Call D-Bus on a test session without relying on the engine wrapper."""
    completed = subprocess.run(
        [
            "dbus-send",
            f"--bus={address}",
            "--print-reply=literal",
            "--reply-timeout=1000",
            f"--dest={service}",
            path,
            f"{interface}.{method}",
            *(args or []),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def _bus_name_owner_pid(address: str, name: str) -> int:
    """Return the live PID owning a well-known name on a session bus."""
    owner = _session_dbus_call(
        address,
        service="org.freedesktop.DBus",
        path="/org/freedesktop/DBus",
        interface="org.freedesktop.DBus",
        method="GetNameOwner",
        args=[f"string:{name}"],
    ).split()[-1]
    return int(
        _session_dbus_call(
            address,
            service="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            method="GetConnectionUnixProcessID",
            args=[f"string:{owner}"],
        ).split()[-1]
    )


def _bus_name_has_owner(address: str, name: str) -> bool:
    return _session_dbus_call(
        address,
        service="org.freedesktop.DBus",
        path="/org/freedesktop/DBus",
        interface="org.freedesktop.DBus",
        method="NameHasOwner",
        args=[f"string:{name}"],
    ).split()[-1:] == ["true"]


def _a11y_registry_address(session_bus: str) -> str:
    """Resolve the private AT-SPI bus address from the session bus."""
    return _session_dbus_call(
        session_bus,
        service=A11Y_BUS_NAME,
        path="/org/a11y/bus",
        interface="org.a11y.Bus",
        method="GetAddress",
    ).split()[-1]


def _process_identity(pid: int) -> tuple[int, int] | None:
    """Return ``(process-group, start-time)`` for a live PID."""
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text().rpartition(")")[2].split()
        # The suffix starts at field 3 (state), so field 22 (starttime) is index 19.
        if len(fields) <= 19 or fields[0] in ("Z", "X"):
            return None
        return int(fields[2]), int(fields[19])
    except (OSError, ValueError):
        return None


def _read_process_identity(path: Path) -> tuple[int, int, int]:
    """Read ``pid pgid start-time`` recorded by a test-owned launcher."""
    pid, pgid, start_time = (int(value) for value in path.read_text().split())
    return pid, pgid, start_time


def _assert_process_identity(path: Path) -> tuple[int, int, int]:
    pid, pgid, start_time = _read_process_identity(path)
    assert _process_identity(pid) == (pgid, start_time), (path, pid, pgid, start_time)
    return pid, pgid, start_time


def _assert_process_identity_exited(identity: tuple[int, int, int]) -> None:
    pid, pgid, start_time = identity

    def exited() -> bool:
        current = _process_identity(pid)
        return current is None or current != (pgid, start_time)

    assert _wait_until(exited, PROCESS_EXIT_TIMEOUT_SECONDS), identity


def _record_identity(path: Path) -> str:
    target = shlex.quote(str(path))
    partial = shlex.quote(f"{path}.partial")
    return (
        "read -r _stat < /proc/$$/stat\n"
        "_stat=${_stat##*)}\n"
        'read -r -a _stat_fields <<< "$_stat"\n'
        f'printf "%s %s %s\\n" "$$" "${{_stat_fields[2]}}" "${{_stat_fields[19]}}" > {partial}\n'
        f"mv {partial} {target}\n"
    )


@pytest.mark.parametrize("comm", ["bash", "space name", "bracket ) name", "(nested )"])
def test_process_identity_recording_handles_spaces_and_parentheses(
    comm: str, tmp_path: Path
) -> None:
    identity_path = tmp_path / "process-identity"
    process = subprocess.Popen(
        [
            "bash",
            "-c",
            f"printf '%s' {shlex.quote(comm)} > /proc/$$/comm\n"
            f"{_record_identity(identity_path)}"
            "read -r _hold\n",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert _wait_until(identity_path.is_file, PROCESS_EXIT_TIMEOUT_SECONDS)
        assert (Path("/proc") / str(process.pid) / "comm").read_text().strip() == comm
        identity = _assert_process_identity(identity_path)
        assert identity[:2] == (process.pid, process.pid)
    finally:
        _reap(process)
        if process.stdin is not None:
            process.stdin.close()
    _assert_process_identity_exited(identity)


def _recording_launcher_script(
    script: Path,
    recorded: Path,
    identity: Path,
    real_launcher: list[str],
    *,
    fd_log: Path | None = None,
) -> str:
    """Create a launcher that records literal argv and then execs dbus-daemon."""
    record_fds = (
        "for _fd in /proc/$$/fd/*; do\n"
        f'    readlink "$_fd" >> {shlex.quote(str(fd_log))} || :\n'
        "done\n"
        if fd_log is not None
        else ""
    )
    script.write_text(
        "#!/bin/bash\n"
        f"{_record_identity(identity)}"
        f"{record_fds}"
        f"printf '%s\\0' \"$@\" > {shlex.quote(str(recorded))}\n"
        "shift\n"
        'exec "$@"\n'
    )
    script.chmod(0o755)
    return f"{shlex.quote(str(script))} --record {shlex.join(real_launcher)}"


def _real_registry_argv() -> list[str]:
    return service_file_argv(
        _real_service_file("accessibility-services/org.a11y.atspi.Registry.service").encode(),
        "org.a11y.atspi.Registry",
    )


def _assert_real_session_state(engine: AutomationEngine) -> None:
    """Exercise the real private bus, compositor, EIS connection, and app environment."""
    session = engine._session
    assert isinstance(session, Session) and session.info is not None
    info = session.info
    assert info.dbus_address != "unix:path=/definitely-not-the-session-bus"
    assert not any("injected-" in warning for warning in info.startup_warnings)
    assert (Path(os.environ["XDG_RUNTIME_DIR"]) / info.wayland_socket).is_socket()
    kwin_pid = _bus_name_owner_pid(info.dbus_address, "org.kde.KWin")
    kwin_identity = _process_identity(kwin_pid)
    assert kwin_identity is not None and kwin_identity[0] == info.kwin_pid
    assert _bus_name_has_owner(info.dbus_address, "org.freedesktop.DBus")
    engine.pointer_move(Point(x=13, y=17))
    pid = engine.app_launch(
        'sh -c \'printf "bus:%s\\nwayland:%s\\n" '
        '"$DBUS_SESSION_BUS_ADDRESS" "$WAYLAND_DISPLAY"; exec sleep 60\''
    ).pid
    assert _process_identity(pid) is not None
    output = _wait_for_app_log(engine, pid, "wayland:")
    assert output == f"bus:{info.dbus_address}\nwayland:{info.wayland_socket}\n"


def _install_interpreter_shim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stall_role: Literal["none", "launcher", "registry"],
    spoof_stdout: bool,
    spoof_source_marker: Path | None = None,
) -> Path:
    """Record role/PID/group/start-time lines and role-specific NUL-delimited argv."""
    real_interpreter = sys.executable
    assert real_interpreter
    log = tmp_path / "interpreter-log"
    shim = tmp_path / "interpreter-shim"
    injected_warning = f"injected-{uuid4().hex}"
    spoof = (
        f'printf "READY\\nFAILED\\nWARN: {injected_warning}\\n'
        f"Warning: {injected_warning}\\n"
        'DBUS_SESSION_BUS_ADDRESS=unix:path=/definitely-not-the-session-bus\\n"\n'
        if spoof_stdout
        else ""
    )
    if spoof_source_marker is not None:
        command = f"touch {shlex.quote(str(spoof_source_marker))}"
        source = f"{command}\nATSPI_LAUNCHER=(/bin/sh -c {shlex.quote(command)})\n"
        spoof += f"printf '%s' {shlex.quote(source)}\n"
    shim.write_text(
        "#!/bin/bash\n"
        "_role=none\n"
        'for _arg in "$@"; do\n'
        '    case "$_arg" in launcher|registry) _role=$_arg;; esac\n'
        "done\n"
        "read -r _stat < /proc/$$/stat\n"
        "_stat=${_stat##*)}\n"
        'read -r -a _fields <<< "$_stat"\n'
        f'printf "%s %s %s %s\\n" "$_role" "$$" "${{_fields[2]}}" "${{_fields[19]}}"'
        f" >> {shlex.quote(str(log))}\n"
        f'printf "%s\\0" "$@" > {shlex.quote(str(log))}.argv."$_role"\n'
        f"{spoof}"
        f'if [ "$_role" = {shlex.quote(stall_role)} ] && [ "$_role" != none ]; then\n'
        "    trap '' TERM\n"
        "    exec /bin/sleep infinity\n"
        "fi\n"
        f'exec {shlex.quote(real_interpreter)} "$@"\n'
    )
    shim.chmod(0o755)
    monkeypatch.setattr(session_module.sys, "executable", str(shim))
    return log


def _record_parent_data_root_io(
    monkeypatch: pytest.MonkeyPatch, data_root: Path
) -> list[tuple[str, str]]:
    """Observe filesystem APIs without changing their results or service lookup logic."""
    accesses: list[tuple[str, str]] = []
    prefix = str(data_root)

    def instrument[**P, R](label: str, original: Callable[P, R]) -> Callable[P, R]:
        @wraps(original)
        def recorded(*args: P.args, **kwargs: P.kwargs) -> R:
            target = args[0] if args else kwargs.get("path", kwargs.get("file"))
            if isinstance(target, (str, bytes, os.PathLike)):
                path = os.fsdecode(target)
                if path == prefix or path.startswith(prefix + os.sep):
                    accesses.append((label, path))
            return original(*args, **kwargs)

        return recorded

    for name in ("stat", "lstat", "open", "scandir", "listdir", "access"):
        monkeypatch.setattr(os, name, instrument(f"os.{name}", getattr(os, name)))
    monkeypatch.setattr(builtins, "open", instrument("builtins.open", builtins.open))
    monkeypatch.setattr(io, "open", instrument("io.open", io.open))
    return accesses


@pytest.mark.parametrize("activation", ["healthy", "denied"])
def test_atspi_service_lookup_never_reads_data_roots_in_the_parent(
    activation: str,
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the activation daemon or owned lookup child may touch the data roots."""
    data_dir = _service_data_dir(
        tmp_path, monkeypatch, launcher_exec=shlex.join(_real_launcher_argv())
    )
    for service in (
        "services/org.a11y.Bus.service",
        "accessibility-services/org.a11y.atspi.Registry.service",
    ):
        (data_dir / "dbus-1" / service).write_text(_real_service_file(service))
    # Keep the system data roots installed by _service_data_dir: the native
    # launcher also needs GLib's compiled GSettings schemas from those roots.
    monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
    if activation == "denied":
        _deny_first_a11y_activation(tmp_path, monkeypatch)
    log = _install_interpreter_shim(tmp_path, monkeypatch, stall_role="none", spoof_stdout=False)
    accesses = _record_parent_data_root_io(monkeypatch, data_dir)
    output = start_session().render()
    assert not accesses, f"server performed service-file I/O: {accesses}"
    assert ("Warning:" in output) == (activation == "denied"), output
    _assert_real_session_state(engine)
    session = engine._session
    assert isinstance(session, Session) and session.info is not None
    assert _bus_name_has_owner(session.info.dbus_address, A11Y_BUS_NAME)
    if activation == "healthy":
        assert not log.exists(), "healthy activation unnecessarily ran a lookup child"
    else:
        assert {line.split()[0] for line in log.read_text().splitlines()} == {
            "launcher",
            "registry",
        }
        registry_bus = _a11y_registry_address(session.info.dbus_address)
        assert _bus_name_has_owner(registry_bus, "org.a11y.atspi.Registry")


@pytest.mark.parametrize("stall_role", ["launcher", "registry"])
@pytest.mark.parametrize("spoof_stdout", [False, True], ids=["silent", "spoofed-handshake"])
def test_atspi_direct_start_rollback_on_stalled_or_failed_helper(
    stall_role: Literal["launcher", "registry"],
    spoof_stdout: bool,
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    background_start: Callable[..., _BackgroundStart],
) -> None:
    """A TERM-resistant lookup child is rolled back before real KWin readiness."""
    data_dir = _service_data_dir(
        tmp_path, monkeypatch, launcher_exec=shlex.join(_real_launcher_argv())
    )
    (data_dir / "dbus-1/accessibility-services/org.a11y.atspi.Registry.service").write_text(
        _real_service_file("accessibility-services/org.a11y.atspi.Registry.service")
    )
    monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
    _deny_first_a11y_activation(tmp_path, monkeypatch)
    log = _install_interpreter_shim(
        tmp_path, monkeypatch, stall_role=stall_role, spoof_stdout=spoof_stdout
    )
    daemons_before = _accessibility_daemons()
    neighbor = _spawn_neighbor(f"kwin-mcp-lookup-neighbor-{uuid4().hex}")
    neighbor_identity = _process_identity(neighbor.pid)
    assert neighbor_identity is not None
    placeholder = Session()
    run: _BackgroundStart | None = None
    try:
        run = background_start(placeholder, SessionConfig(), start_call=start_session)
        assert run.finished_within(STUB_START_BUDGET_SECONDS), "session_start hung"
        assert run.error is None, run.error
        output = run.output
        assert isinstance(output, str) and "Warning:" in output, output
        records = [line.split() for line in log.read_text().splitlines()]
        assert any(record[0] == stall_role for record in records), records
        session = engine._session
        assert isinstance(session, Session) and session.info is not None
        for _, pid, pgid, start_time in records:
            identity = (int(pid), int(pgid), int(start_time))
            assert identity[1] == session.info.kwin_pid
            assert _process_identity(identity[0]) != identity[1:], identity
        assert _accessibility_daemons() == daemons_before
        assert not _bus_name_has_owner(session.info.dbus_address, A11Y_BUS_NAME)
        assert "Input backend: KWin EIS" in output, output
        _assert_real_session_state(engine)
        assert _process_identity(neighbor.pid) == neighbor_identity
        pgid = session.info.kwin_pid
        assert engine.session_stop().stopped
        assert not _group_pids(pgid)
        assert _process_identity(neighbor.pid) == neighbor_identity
    finally:
        active = engine._session
        session = active if isinstance(active, Session) else placeholder
        _recover(
            session,
            run,
            session._process.pid if session._process is not None else None,
            f"atspi-lookup-recovery-{uuid4().hex}",
        )
        engine.session_stop()
        _reap(neighbor)


def test_direct_atspi_registry_is_revalidated_after_launcher_preflight(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing the registry descriptor before its lookup must fail closed."""
    launcher_identity_path = tmp_path / "launcher-identity"
    registry_marker = tmp_path / "registry-executed"
    launcher = tmp_path / "invalidating-launcher"
    registry = tmp_path / "recording-registry"
    registry.write_text(
        "#!/bin/sh\n"
        f": > {shlex.quote(str(registry_marker))}\n"
        f"exec {shlex.join(_real_registry_argv())}\n"
    )
    registry.chmod(0o755)
    data_dir = _service_data_dir(tmp_path, monkeypatch, launcher_exec=str(launcher))
    registry_file = data_dir / "dbus-1/accessibility-services/org.a11y.atspi.Registry.service"
    registry_file.write_text(f"[D-BUS Service]\nName=org.a11y.atspi.Registry\nExec={registry}\n")
    invalid = "[D-BUS Service]\nName=org.a11y.atspi.Registry\nExec=/bin/x unclosed'\n"
    launcher.write_text(
        "#!/bin/bash\n"
        f"{_record_identity(launcher_identity_path)}"
        f"printf '%s' {shlex.quote(invalid)} > {shlex.quote(str(registry_file))}\n"
        f"exec {shlex.join(_real_launcher_argv())}\n"
    )
    launcher.chmod(0o755)
    monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
    _deny_first_a11y_activation(tmp_path, monkeypatch)
    daemons_before = _accessibility_daemons()
    output = start_session().render()
    assert "Warning:" in output, output
    assert not registry_marker.exists()
    identity = _read_process_identity(launcher_identity_path)
    assert _process_identity(identity[0]) != identity[1:], identity
    assert _accessibility_daemons() == daemons_before
    assert _a11y_bus_has_owner(engine).split()[-1:] == ["false"]
    _assert_real_session_state(engine)


def test_atspi_direct_start_respects_xdg_data_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SessionConfig roots, not app-only env, select argv without inherited service FDs."""
    launcher = tmp_path / "recording-launcher"
    launcher_identity_path = tmp_path / "launcher-identity"
    fd_log = tmp_path / "launcher-fds"
    launcher_exec = _recording_launcher_script(
        launcher,
        tmp_path / "launcher-argv",
        launcher_identity_path,
        _real_launcher_argv(),
        fd_log=fd_log,
    )
    system_data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    data_dir = _service_data_dir(tmp_path, monkeypatch, launcher_exec=launcher_exec)
    (data_dir / "dbus-1/accessibility-services/org.a11y.atspi.Registry.service").write_text(
        _real_service_file("accessibility-services/org.a11y.atspi.Registry.service")
    )
    session_data_dirs = os.environ["XDG_DATA_DIRS"]
    server_data_dir = tmp_path / "server-share"
    server_data_dir.mkdir()
    server_data_dirs = f"{server_data_dir}:{system_data_dirs}"
    monkeypatch.setenv("XDG_DATA_DIRS", server_data_dirs)
    monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
    _deny_first_a11y_activation(tmp_path, monkeypatch)
    session = Session()
    neighbor = _spawn_neighbor(f"kwin-mcp-data-roots-neighbor-{uuid4().hex}")
    neighbor_identity = _process_identity(neighbor.pid)
    identity: tuple[int, int, int] | None = None
    screenshot_dir: Path | None = None
    try:
        assert neighbor_identity is not None
        # App-specific environment belongs to app_launch, not session_start.
        # Service lookup uses the real SessionConfig environment instead.
        info = session.start(SessionConfig(extra_env={"XDG_DATA_DIRS": session_data_dirs}))
        screenshot_dir = info.screenshot_dir
        assert info.startup_warnings
        assert os.environ["XDG_DATA_DIRS"] == server_data_dirs
        identity = _assert_process_identity(launcher_identity_path)
        assert identity[1] == info.kwin_pid
        assert _bus_name_owner_pid(info.dbus_address, A11Y_BUS_NAME) == identity[0]
        launcher_env = (Path("/proc") / str(identity[0]) / "environ").read_bytes().split(b"\0")
        assert f"XDG_DATA_DIRS={session_data_dirs}".encode() in launcher_env
        assert f"{ATSPI_DIRECT_START_ENV}=1".encode() in launcher_env
        registry_bus = _a11y_registry_address(info.dbus_address)
        assert _bus_name_has_owner(registry_bus, "org.a11y.atspi.Registry")
        targets = fd_log.read_text().splitlines()
        assert targets, "launcher did not record its file descriptors"
        assert not any(
            target == str(data_dir) or target.startswith(str(data_dir) + os.sep)
            for target in targets
        ), targets
        assert info.dbus_address != os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        assert (Path(os.environ["XDG_RUNTIME_DIR"]) / info.wayland_socket).is_socket()
        kwin_identity = _process_identity(_bus_name_owner_pid(info.dbus_address, "org.kde.KWin"))
        assert kwin_identity is not None and kwin_identity[0] == info.kwin_pid
        assert _bus_name_has_owner(info.dbus_address, "org.freedesktop.DBus")
        assert _process_identity(neighbor.pid) == neighbor_identity
        session.stop()
        _assert_process_identity_exited(identity)
        assert not _group_pids(info.kwin_pid)
        assert not screenshot_dir.exists()
        assert _process_identity(neighbor.pid) == neighbor_identity
    finally:
        try:
            session.stop()
        finally:
            _reap(neighbor)


def test_accessibility_bus_is_owned_when_session_start_returns(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    # Qt apps join the accessibility bus only when org.a11y.Bus already has an
    # owner; otherwise the first element_query() finds no apps. Asking the
    # bus daemon (no app, no AT-SPI2 query) cannot activate the name itself, so
    # this observes exactly what session_start left behind.
    output = start_session().render()
    assert "Warning:" not in output, output

    reply = _a11y_bus_has_owner(engine)
    assert reply.split()[-1:] == ["true"], reply


def test_session_start_reports_a_failed_accessibility_bus_activation(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only the wrapper resolves dbus-send through the patched PATH. The stub
    # fails the activation and every later GetAddress, so the opted-in direct
    # start of the launcher cannot complete either and must leave no bus behind.
    # The NameHasOwner probes (org.kde.KWin, and the direct start's own wait)
    # must pass through to the real dbus-send or start() could never see KWin
    # register.
    monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
    real_dbus_send = shutil.which("dbus-send")
    assert real_dbus_send is not None, "dbus-send not found in PATH"
    _install_stub(
        tmp_path,
        monkeypatch,
        "dbus-send",
        "#!/bin/sh\n"
        'case "$*" in\n'
        "    *NameHasOwner*)\n"
        f'        exec {shlex.quote(real_dbus_send)} "$@";;\n'
        "esac\n"
        "echo 'Error org.freedesktop.DBus.Error.ServiceUnknown: stub has no a11y bus' >&2\n"
        "echo 'second stderr line' >&2\n"
        "exit 1\n",
    )

    output = start_session().render()

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


@pytest.mark.parametrize(
    "inherited_monitor", [False, True], ids=["default-shell", "inherited-monitor"]
)
def test_session_start_starts_the_accessibility_bus_directly_when_activation_is_denied(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    wait_for_app: Callable[[str], ElementList],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inherited_monitor: bool,
) -> None:
    # Fedora's SELinux policy denies the launcher's activation from a private
    # session bus with exactly this error; a direct start works.
    monkeypatch.setenv("SHELLOPTS", "monitor" if inherited_monitor else "")
    real_launcher = _real_launcher_argv()
    real_registry = _real_registry_argv()
    launcher_record = tmp_path / "launcher-argv"
    launcher_identity_path = tmp_path / "launcher-identity"
    launcher_fd_log = tmp_path / "launcher-fds"
    launcher = tmp_path / "recording-launcher"
    launcher_exec = _recording_launcher_script(
        launcher,
        launcher_record,
        launcher_identity_path,
        real_launcher,
        fd_log=launcher_fd_log,
    )
    data_dir = _service_data_dir(tmp_path, monkeypatch, launcher_exec=launcher_exec)
    registry_record = tmp_path / "registry-argv"
    registry_identity_path = tmp_path / "registry-identity"
    registry_fd_log = tmp_path / "registry-fds"
    registry_exec = _recording_launcher_script(
        tmp_path / "recording-registry",
        registry_record,
        registry_identity_path,
        real_registry,
        fd_log=registry_fd_log,
    )
    (data_dir / "dbus-1/accessibility-services/org.a11y.atspi.Registry.service").write_text(
        f"[D-BUS Service]\nName=org.a11y.atspi.Registry\nExec={registry_exec}\n"
    )
    monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
    _deny_first_a11y_activation(tmp_path, monkeypatch)
    neighbor = _spawn_neighbor(f"kwin-mcp-a11y-neighbor-{uuid4().hex}")
    neighbor_identity = _process_identity(neighbor.pid)
    assert neighbor_identity is not None

    try:
        output = start_session().render()

        assert "Session started. Wayland socket: " in output, output
        assert (tmp_path / "activation-denied").is_file()
        assert "Input backend: KWin EIS" in output, output
        warnings = [line for line in output.splitlines() if line.startswith("Warning: ")]
        assert any(
            "started the accessibility bus and registry directly" in line for line in warnings
        ), output

        session = engine._session
        assert isinstance(session, Session) and session.info is not None
        session_bus = session.info.dbus_address
        launcher_identity = _assert_process_identity(launcher_identity_path)
        assert _bus_name_owner_pid(session_bus, A11Y_BUS_NAME) == launcher_identity[0]
        registry_bus = _a11y_registry_address(session_bus)
        registry_identity = _assert_process_identity(registry_identity_path)
        assert _bus_name_owner_pid(registry_bus, "org.a11y.atspi.Registry") == registry_identity[0]
        pgid = session.info.kwin_pid
        assert launcher_identity[1] == registry_identity[1] == pgid
        for record, real_argv, fd_log in (
            (launcher_record, real_launcher, launcher_fd_log),
            (registry_record, real_registry, registry_fd_log),
        ):
            assert record.read_bytes().split(b"\0")[:-1] == [
                argument.encode() for argument in ["--record", *real_argv]
            ]
            targets = fd_log.read_text().splitlines()
            assert targets, "service did not record its file descriptors"
            assert not any(
                target == str(data_dir) or target.startswith(str(data_dir) + os.sep)
                for target in targets
            ), targets
        _assert_real_session_state(engine)

        reply = engine.dbus_call(
            service="org.a11y.Bus",
            path="/org/a11y/bus",
            interface="org.freedesktop.DBus.Properties",
            method="Get",
            args=["string:org.a11y.Status", "string:IsEnabled"],
        )
        assert reply.reply is True, reply
        assert engine.app_launch("kcalc").pid > 0
        assert element_count(wait_for_app("kcalc")) > 0

        monkeypatch.undo()
        assert _process_identity(neighbor.pid) == neighbor_identity
        assert engine.session_stop().stopped
        _assert_process_identity_exited(launcher_identity)
        _assert_process_identity_exited(registry_identity)
        assert session._process is None
        assert not _group_pids(pgid)
        assert _process_identity(neighbor.pid) == neighbor_identity
    finally:
        try:
            engine.session_stop()
        finally:
            # Before the regression is fixed, monitor can give each owned role
            # its own group. Recover only a recorder's still-matching identity.
            for path in (launcher_identity_path, registry_identity_path):
                if path.is_file():
                    pid, pgid, start_time = _read_process_identity(path)
                    if pid == pgid and _process_identity(pid) == (pgid, start_time):
                        with contextlib.suppress(ProcessLookupError, PermissionError):
                            os.killpg(pgid, signal.SIGKILL)
            _reap(neighbor)


@pytest.mark.qa("QA-SES-026")
def test_atspi_direct_start_warning_surfaces_when_denied_without_opt_in(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A direct start would work here, as in the test above, but it bypasses
    # the SELinux domain activation gives the launcher, so it waits for the
    # opt-in, which the warning names.
    monkeypatch.delenv(ATSPI_DIRECT_START_ENV, raising=False)
    _deny_first_a11y_activation(tmp_path, monkeypatch)
    log = _install_interpreter_shim(tmp_path, monkeypatch, stall_role="none", spoof_stdout=False)
    daemons_before = _accessibility_daemons()

    output = start_session().render()

    assert "Session started. Wayland socket: " in output, output
    warnings = [line for line in output.splitlines() if line.startswith("Warning: ")]
    assert any(f"{ATSPI_DIRECT_START_ENV}=1" in line for line in warnings), output
    monkeypatch.undo()
    assert _a11y_bus_has_owner(engine).split()[-1:] == ["false"]
    assert _accessibility_daemons() == daemons_before
    assert not log.exists(), "default-off startup unexpectedly ran a lookup child"


@pytest.mark.parametrize(
    ("server_value", "extra_value", "direct"),
    [
        pytest.param(None, "1", False, id="extra-env-only-does-not-opt-in"),
        pytest.param("true", None, False, id="server-true-is-not-exact-one"),
        pytest.param("0", None, False, id="server-zero-is-not-exact-one"),
        pytest.param("", None, False, id="server-empty-is-not-exact-one"),
        pytest.param(" 1", None, False, id="server-spaced-one-is-not-exact-one"),
        pytest.param("1", "0", True, id="server-one-wins-over-extra-env"),
    ],
)
@pytest.mark.qa("QA-SES-026")
def test_atspi_direct_start_uses_only_the_exact_server_opt_in(
    server_value: str | None,
    extra_value: str | None,
    direct: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direct-start switch is server-only and must be observed at runtime."""
    if server_value is None:
        monkeypatch.delenv(ATSPI_DIRECT_START_ENV, raising=False)
    else:
        monkeypatch.setenv(ATSPI_DIRECT_START_ENV, server_value)
    launcher_identity_path = tmp_path / "launcher-identity"
    launcher_mode_path = tmp_path / "launcher-mode"
    launcher_env_path = tmp_path / "launcher-env"
    launcher_argv_path = tmp_path / "launcher-argv"
    real_launcher = _real_launcher_argv()
    launcher = tmp_path / "recording-launcher"
    launcher.write_text(
        "#!/bin/bash\n"
        f"{_record_identity(launcher_identity_path)}"
        'if [[ -n "${DBUS_STARTER_ADDRESS:-}" || -n "${DBUS_STARTER_BUS_TYPE:-}" ]]; then\n'
        f"    printf 'activated\\n' > {shlex.quote(str(launcher_mode_path))}\n"
        "else\n"
        f"    printf 'direct\\n' > {shlex.quote(str(launcher_mode_path))}\n"
        "fi\n"
        f'printf "%s\\0" "$@" > {shlex.quote(str(launcher_argv_path))}\n'
        f'printf "%s" "${{{ATSPI_DIRECT_START_ENV}-unset}}"'
        f" > {shlex.quote(str(launcher_env_path))}\n"
        'exec "$@"\n'
    )
    launcher.chmod(0o755)
    launcher_exec = f"{shlex.quote(str(launcher))} {shlex.join(real_launcher)}"
    data_dir = _service_data_dir(tmp_path, monkeypatch, launcher_exec=launcher_exec)
    (data_dir / "dbus-1/accessibility-services/org.a11y.atspi.Registry.service").write_text(
        _real_service_file("accessibility-services/org.a11y.atspi.Registry.service")
    )
    _deny_first_a11y_activation(tmp_path, monkeypatch)
    config = SessionConfig(
        extra_env={} if extra_value is None else {ATSPI_DIRECT_START_ENV: extra_value}
    )
    session = Session()
    identity: tuple[int, int, int] | None = None
    try:
        info = session.start(config)
        assert _bus_name_has_owner(info.dbus_address, A11Y_BUS_NAME) is direct
        if direct:
            identity = _assert_process_identity(launcher_identity_path)
            assert _bus_name_owner_pid(info.dbus_address, A11Y_BUS_NAME) == identity[0]
            assert identity[1] == info.kwin_pid
            assert launcher_mode_path.read_text().strip() == "direct"
            assert launcher_env_path.read_text() == (
                server_value if extra_value is None else extra_value
            )
            assert launcher_argv_path.read_bytes().split(b"\0")[:-1] == [
                argument.encode() for argument in real_launcher
            ]
            assert any(
                "started the accessibility bus and registry directly" in line
                for line in info.startup_warnings
            )
        else:
            assert not launcher_identity_path.exists()
            assert any(
                "accessibility tools may be unavailable" in line for line in info.startup_warnings
            )
    finally:
        session.stop()
        if identity is not None:
            _assert_process_identity_exited(identity)


@pytest.mark.parametrize("broken_role", ["launcher", "registry"])
@pytest.mark.parametrize("service_kind", ["missing", "invalid"])
def test_atspi_direct_start_fails_closed_for_missing_or_invalid_services(
    broken_role: str,
    service_kind: str,
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid counterpart must never execute when either descriptor is unusable."""
    monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
    launcher_marker = tmp_path / "launcher-executed"
    registry_marker = tmp_path / "registry-executed"
    launcher = tmp_path / "recording-launcher"
    launcher_exec = _recording_launcher_script(
        launcher, tmp_path / "launcher-argv", launcher_marker, _real_launcher_argv()
    )
    registry = tmp_path / "recording-registry"
    registry.write_text(
        "#!/bin/sh\n"
        f": > {shlex.quote(str(registry_marker))}\n"
        f"exec {shlex.join(_real_registry_argv())}\n"
    )
    registry.chmod(0o755)
    data_dir = _service_data_dir(tmp_path, monkeypatch, launcher_exec=launcher_exec)
    registry_file = data_dir / "dbus-1/accessibility-services/org.a11y.atspi.Registry.service"
    registry_file.write_text(f"[D-BUS Service]\nName=org.a11y.atspi.Registry\nExec={registry}\n")
    broken_file = (
        data_dir / "dbus-1/services/org.a11y.Bus.service"
        if broken_role == "launcher"
        else registry_file
    )
    if service_kind == "missing":
        broken_file.unlink()
    else:
        name = A11Y_BUS_NAME if broken_role == "launcher" else "org.a11y.atspi.Registry"
        broken_file.write_text(f"[D-BUS Service]\nName={name}\nExec=/bin/x unclosed'\n")
    monkeypatch.setenv("XDG_DATA_DIRS", str(data_dir))
    _deny_first_a11y_activation(tmp_path, monkeypatch)
    daemons_before = _accessibility_daemons()
    output = start_session().render()
    assert "Warning:" in output, output
    assert not launcher_marker.exists()
    assert not registry_marker.exists()
    assert _accessibility_daemons() == daemons_before
    _assert_real_session_state(engine)
    assert _a11y_bus_has_owner(engine).split()[-1:] == ["false"]


@pytest.mark.parametrize(
    ("inherited_monitor", "direct_opt_in"),
    [
        pytest.param(False, True, id="default-shell-enabled"),
        pytest.param(True, True, id="inherited-monitor-enabled"),
        pytest.param(True, False, id="inherited-monitor-default-off"),
    ],
)
def test_healthy_activation_does_not_fall_back_to_the_direct_launcher(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inherited_monitor: bool,
    direct_opt_in: bool,
) -> None:
    """Healthy activation retains owned KWin teardown regardless of shell options."""
    monkeypatch.setenv("SHELLOPTS", "monitor" if inherited_monitor else "")
    if direct_opt_in:
        monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
    else:
        monkeypatch.delenv(ATSPI_DIRECT_START_ENV, raising=False)
    real_launcher = _real_launcher_argv()
    launcher = tmp_path / "activation-observer"
    recorded = tmp_path / "activation-modes"
    launcher.write_text(
        "#!/bin/sh\n"
        'if [ -n "${DBUS_STARTER_ADDRESS:-}" ] || [ -n "${DBUS_STARTER_BUS_TYPE:-}" ]; then\n'
        f"    printf 'activated\\n' >> {shlex.quote(str(recorded))}\n"
        "else\n"
        f"    printf 'direct\\n' >> {shlex.quote(str(recorded))}\n"
        "fi\n"
        'shift\nexec "$@"\n'
    )
    launcher.chmod(0o755)
    data_dir = _service_data_dir(
        tmp_path,
        monkeypatch,
        launcher_exec=f"{shlex.quote(str(launcher))} --record {shlex.join(real_launcher)}",
    )
    (data_dir / "dbus-1/accessibility-services/org.a11y.atspi.Registry.service").write_text(
        _real_service_file("accessibility-services/org.a11y.atspi.Registry.service")
    )

    neighbor = _spawn_neighbor(f"kwin-mcp-activation-neighbor-{uuid4().hex}")
    neighbor_identity = _process_identity(neighbor.pid)
    assert neighbor_identity is not None
    kwin_identity: tuple[int, int, int] | None = None
    try:
        output = start_session().render()

        assert "Session started. Wayland socket: " in output, output
        assert "Input backend: KWin EIS" in output, output
        assert all(
            "started the accessibility bus and registry directly" not in line
            for line in output.splitlines()
        )
        assert _a11y_bus_has_owner(engine).split()[-1:] == ["true"]
        assert recorded.is_file()
        assert recorded.read_text().splitlines() == ["activated"]
        session = engine._session
        assert isinstance(session, Session) and session.info is not None
        pgid = session.info.kwin_pid
        kwin_pid = _bus_name_owner_pid(session.info.dbus_address, "org.kde.KWin")
        kwin_process = _process_identity(kwin_pid)
        assert kwin_process is not None
        kwin_identity = (kwin_pid, *kwin_process)
        assert kwin_process[0] == pgid
        _assert_real_session_state(engine)
        assert _process_identity(neighbor.pid) == neighbor_identity
        assert engine.session_stop().stopped
        assert session._process is None
        _assert_process_identity_exited(kwin_identity)
        assert not _group_pids(pgid)
        assert _process_identity(neighbor.pid) == neighbor_identity
    finally:
        escaped_group_members: dict[int, tuple[int, int] | None] = {}
        if kwin_identity is not None:
            pid, pgid, start_time = kwin_identity
            if pid == pgid and _process_identity(pid) == (pgid, start_time):
                escaped_group_members = {
                    member: _process_identity(member) for member in _group_pids(pgid)
                }
        try:
            engine.session_stop()
        finally:
            # A matching member proves this remains the test's escaped KWin
            # group, even if stopping the wrapper already reaped its leader.
            for member, identity in escaped_group_members.items():
                if identity is not None and _process_identity(member) == identity:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(identity[0], signal.SIGKILL)
                    break
            _reap(neighbor)


def test_session_start_runs_the_service_file_command_as_dbus_would(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The direct start must preserve the service parser's literal argv. In
    # particular, shell metacharacters are data, not a second interpretation.
    real_launcher = _real_launcher_argv()
    launcher_dir = tmp_path / "dir with space"
    launcher_dir.mkdir()
    launcher = launcher_dir / "a11y launcher"
    recorded = tmp_path / "launcher-argv"
    inert = tmp_path / "must-not-exist"
    literal_args = [
        f"$(touch {inert})",
        f"`touch {inert}`",
        f"; touch {inert}",
        f"*{inert}",
        f'"{inert}"',
        "",
        "line1\nline2",
        f"back\\slash {inert}",
    ]
    # Quote the argv first, then encode the desktop-file value that wraps it.
    service_exec = shlex.join([str(launcher), "--label=two words", *literal_args, *real_launcher])
    service_exec = (
        service_exec.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    launcher.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\0' \"$@\" > {shlex.quote(str(recorded))}\n"
        f"shift {1 + len(literal_args)}\n"
        'exec "$@"\n'
    )
    launcher.chmod(0o755)
    data_dir = _service_data_dir(
        tmp_path,
        monkeypatch,
        launcher_exec=service_exec,
    )
    (data_dir / "dbus-1/accessibility-services/org.a11y.atspi.Registry.service").write_text(
        _real_service_file("accessibility-services/org.a11y.atspi.Registry.service")
    )
    monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
    _deny_first_a11y_activation(tmp_path, monkeypatch)

    output = start_session().render()

    warnings = [line for line in output.splitlines() if line.startswith("Warning: ")]
    assert any(
        "started the accessibility bus and registry directly" in line for line in warnings
    ), output
    actual = recorded.read_bytes().split(b"\0")[:-1]
    assert [item.decode() for item in actual] == [
        "--label=two words",
        *literal_args,
        *real_launcher,
    ]
    assert not inert.exists()
    monkeypatch.undo()
    assert _a11y_bus_has_owner(engine).split()[-1:] == ["true"]


def _accessibility_daemons() -> int:
    """Running accessibility bus daemons (the launcher's dbus-daemon or broker)."""
    count = 0
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            if b"at-spi2/accessibility.conf" in cmdline.read_bytes():
                count += 1
        except OSError:
            continue
    return count


@pytest.mark.parametrize(
    ("registry_mode", "inherited_monitor"),
    [
        pytest.param("exits", False, id="exits"),
        pytest.param("ignores-term", False, id="ignores-term"),
        pytest.param("ignores-term", True, id="inherited-monitor-ignores-term"),
    ],
)
def test_session_start_stops_the_direct_launcher_when_the_registry_fails(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    background_start: Callable[..., _BackgroundStart],
    registry_mode: str,
    inherited_monitor: bool,
) -> None:
    # Both an immediate exit and a TERM-resistant registry must leave no
    # owned accessibility children before the usable session is published.
    monkeypatch.setenv("SHELLOPTS", "monitor" if inherited_monitor else "")
    launcher_identity_path = tmp_path / "launcher-identity"
    launcher = tmp_path / "recording-launcher"
    launcher_exec = _recording_launcher_script(
        launcher,
        tmp_path / "launcher-argv",
        launcher_identity_path,
        _real_launcher_argv(),
    )
    data_dir = _service_data_dir(tmp_path, monkeypatch, launcher_exec=launcher_exec)
    registry = tmp_path / "failing-registry"
    registry_identity_path = tmp_path / "registry-identity"
    registry_body = (
        "exit 1\n" if registry_mode == "exits" else "trap '' TERM\nexec /bin/sleep infinity\n"
    )
    registry.write_text(f"#!/bin/bash\n{_record_identity(registry_identity_path)}{registry_body}")
    registry.chmod(0o755)
    (data_dir / "dbus-1/accessibility-services/org.a11y.atspi.Registry.service").write_text(
        f"[D-BUS Service]\nName=org.a11y.atspi.Registry\nExec={registry}\n"
    )
    monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
    _deny_first_a11y_activation(tmp_path, monkeypatch)
    neighbor = _spawn_neighbor(f"kwin-mcp-failure-neighbor-{uuid4().hex}")
    neighbor_identity = _process_identity(neighbor.pid)
    assert neighbor_identity is not None

    placeholder = Session()
    run: _BackgroundStart | None = None
    daemons_before = _accessibility_daemons()
    try:
        run = background_start(placeholder, SessionConfig(), start_call=start_session)
        assert run.finished_within(STUB_START_BUDGET_SECONDS), "session_start hung"
        assert run.error is None, run.error
        output = run.output
        assert isinstance(output, str), output

        assert "Session started. Wayland socket: " in output, output
        assert (tmp_path / "activation-denied").is_file()
        warnings = [line for line in output.splitlines() if line.startswith("Warning: ")]
        assert any("accessibility tools may be unavailable" in line for line in warnings), output
        assert launcher_identity_path.is_file()
        assert registry_identity_path.is_file()
        launcher_identity = _read_process_identity(launcher_identity_path)
        registry_identity = _read_process_identity(registry_identity_path)
        session = engine._session
        assert isinstance(session, Session) and session.info is not None
        assert launcher_identity[1] == registry_identity[1] == session.info.kwin_pid
        assert _process_identity(launcher_identity[0]) != launcher_identity[1:]
        assert _process_identity(registry_identity[0]) != registry_identity[1:]
        assert _accessibility_daemons() == daemons_before
        assert "Input backend: KWin EIS" in output, output
        _assert_real_session_state(engine)

        monkeypatch.undo()
        assert _a11y_bus_has_owner(engine).split()[-1:] == ["false"]
        assert _process_identity(neighbor.pid) == neighbor_identity
        pgid = session.info.kwin_pid
        assert engine.session_stop().stopped
        assert session._process is None
        assert not _group_pids(pgid)
        assert _process_identity(neighbor.pid) == neighbor_identity
    finally:
        active = engine._session
        session = active if isinstance(active, Session) else placeholder
        _recover(
            session,
            run,
            session._process.pid if session._process is not None else None,
            f"atspi-recovery-{uuid4().hex}",
        )
        try:
            engine.session_stop()
        finally:
            for path in (launcher_identity_path, registry_identity_path):
                if path.is_file():
                    pid, pgid, start_time = _read_process_identity(path)
                    if pid == pgid and _process_identity(pid) == (pgid, start_time):
                        with contextlib.suppress(ProcessLookupError, PermissionError):
                            os.killpg(pgid, signal.SIGKILL)
            _reap(neighbor)


@pytest.mark.parametrize("ignore_term", [False, True], ids=["honors-term", "ignores-term"])
@pytest.mark.parametrize("spoof_stdout", [False, True], ids=["silent", "spoofed-handshake"])
def test_session_start_cleans_up_a_direct_launcher_that_never_claims_its_bus_name(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    background_start: Callable[..., _BackgroundStart],
    ignore_term: bool,
    spoof_stdout: bool,
) -> None:
    launcher_identity_path = tmp_path / "stalled-launcher-identity"
    launcher = tmp_path / "stalled-launcher"
    launcher.write_text(
        "#!/bin/bash\n"
        f"{_record_identity(launcher_identity_path)}"
        + ("trap '' TERM\n" if ignore_term else "")
        + "exec /bin/sleep infinity\n"
    )
    launcher.chmod(0o755)
    data_dir = _service_data_dir(tmp_path, monkeypatch, launcher_exec=str(launcher))
    (data_dir / "dbus-1/accessibility-services/org.a11y.atspi.Registry.service").write_text(
        _real_service_file("accessibility-services/org.a11y.atspi.Registry.service")
    )
    monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
    _deny_first_a11y_activation(tmp_path, monkeypatch)
    _install_interpreter_shim(tmp_path, monkeypatch, stall_role="none", spoof_stdout=spoof_stdout)
    neighbor = _spawn_neighbor(f"kwin-mcp-stalled-neighbor-{uuid4().hex}")
    neighbor_identity = _process_identity(neighbor.pid)
    assert neighbor_identity is not None
    placeholder = Session()
    run: _BackgroundStart | None = None

    try:
        run = background_start(placeholder, SessionConfig(), start_call=start_session)
        assert run.finished_within(STUB_START_BUDGET_SECONDS), "session_start hung"
        assert run.error is None, run.error
        output = run.output
        assert isinstance(output, str), output

        assert "Session started. Wayland socket: " in output, output
        warnings = [line for line in output.splitlines() if line.startswith("Warning: ")]
        assert any("accessibility tools may be unavailable" in line for line in warnings), output
        assert launcher_identity_path.is_file()
        stalled_identity = _read_process_identity(launcher_identity_path)
        session = engine._session
        assert isinstance(session, Session) and session.info is not None
        assert stalled_identity[1] == session.info.kwin_pid
        assert _process_identity(stalled_identity[0]) != stalled_identity[1:]
        assert "Input backend: KWin EIS" in output, output
        _assert_real_session_state(engine)
        monkeypatch.undo()
        assert _a11y_bus_has_owner(engine).split()[-1:] == ["false"]
        assert _process_identity(neighbor.pid) == neighbor_identity
        pgid = session.info.kwin_pid
        assert engine.session_stop().stopped
        assert not _group_pids(pgid)
        assert _process_identity(neighbor.pid) == neighbor_identity
    finally:
        active = engine._session
        session = active if isinstance(active, Session) else placeholder
        _recover(
            session,
            run,
            session._process.pid if session._process is not None else None,
            f"atspi-recovery-{uuid4().hex}",
        )
        engine.session_stop()
        _reap(neighbor)


_DENIED_ACTIVATION_ERROR = (
    "Error org.freedesktop.DBus.Error.Spawn.ExecFailed: Failed to execute program org.a11y.Bus:"
    " Permission denied"
)


def _deny_first_a11y_activation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail only the first org.a11y.Bus.GetAddress, the activation, as SELinux does on Fedora.

    Only the session wrapper resolves dbus-send through the patched PATH; every
    other call passes through to the real dbus-send.
    """
    real_dbus_send = shutil.which("dbus-send")
    assert real_dbus_send is not None, "dbus-send not found in PATH"
    marker = tmp_path / "activation-denied"
    _install_stub(
        tmp_path,
        monkeypatch,
        "dbus-send",
        "#!/bin/sh\n"
        'case "$*" in\n'
        "    *org.a11y.Bus.GetAddress*)\n"
        f"        if [ ! -e {shlex.quote(str(marker))} ]; then\n"
        f"            : > {shlex.quote(str(marker))}\n"
        f"            echo {shlex.quote(_DENIED_ACTIVATION_ERROR)} >&2\n"
        "            exit 1\n"
        "        fi;;\n"
        "esac\n"
        f'exec {shlex.quote(real_dbus_send)} "$@"\n',
    )


def _real_service_file(service: str) -> str:
    """Text of the distro's dbus-1/<service> file."""
    for root in ("/usr/local/share", "/usr/share"):
        path = Path(root) / "dbus-1" / service
        if path.is_file():
            return path.read_text()
    pytest.fail(f"no {service} file")


def _real_launcher_argv() -> list[str]:
    """The argv dbus-daemon runs to activate the distro's org.a11y.Bus."""
    return service_file_argv(
        _real_service_file("services/org.a11y.Bus.service").encode(), "org.a11y.Bus"
    )


def _service_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher_exec: str) -> Path:
    """A data dir first on XDG_DATA_DIRS whose org.a11y.Bus service runs launcher_exec.

    The caller writes the registry's service file into the returned dir.
    """
    data_dir = tmp_path / "share"
    (data_dir / "dbus-1/services").mkdir(parents=True)
    (data_dir / "dbus-1/accessibility-services").mkdir(parents=True)
    (data_dir / "dbus-1/services/org.a11y.Bus.service").write_text(
        f"[D-BUS Service]\nName=org.a11y.Bus\nExec={launcher_exec}\n"
    )
    monkeypatch.setenv(
        "XDG_DATA_DIRS",
        f"{data_dir}:{os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share')}",
    )
    return data_dir


class _BackgroundStart:
    """Session.start() on a daemon thread, so an unbounded hang stays observable."""

    def __init__(
        self,
        session: Session,
        config: SessionConfig,
        *,
        start_call: Callable[[], SessionInfo] | None = None,
    ) -> None:
        self.session = session
        self.error: BaseException | None = None
        self.elapsed: float | None = None
        self.output: object = None
        self._start_call = start_call
        self._config = config
        self._started = time.monotonic()
        self.thread = threading.Thread(
            target=self._run, name=f"session-start-{config.socket_name}", daemon=True
        )
        self.thread.start()

    def _run(self) -> None:
        try:
            self.output = (
                self.session.start(self._config)
                if self._start_call is None
                else self._start_call().render()
            )
        except BaseException as exc:
            self.error = exc
        finally:
            self.elapsed = time.monotonic() - self._started

    def finished_within(self, budget: float) -> bool:
        """Join until budget seconds after the start call; True if it returned."""
        self.thread.join(timeout=max(0.0, self._started + budget - time.monotonic()))
        return not self.thread.is_alive()


@pytest.fixture
def background_start() -> Iterator[Callable[..., _BackgroundStart]]:
    """Run starts in the background and require every worker joined after recovery."""
    runs: list[_BackgroundStart] = []

    def _start(
        session: Session,
        config: SessionConfig,
        *,
        start_call: Callable[[], SessionInfo] | None = None,
    ) -> _BackgroundStart:
        run = _BackgroundStart(session, config, start_call=start_call)
        runs.append(run)
        return run

    yield _start
    for run in runs:
        run.thread.join(timeout=STUB_STOP_BUDGET_SECONDS)
    leaked = [run.thread.name for run in runs if run.thread.is_alive()]
    assert not leaked, f"startup workers still running after recovery: {leaked}"


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
