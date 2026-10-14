"""End-to-end coverage for virtual and live session lifecycle semantics."""

from __future__ import annotations

import builtins
import contextlib
import errno
import io
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import pytest
from _asserts import element_count
from session_harness import live_kwin

from kwin_mcp import session as session_module
from kwin_mcp.core import AutomationEngine
from kwin_mcp.dbus_service_file import service_file_argv
from kwin_mcp.session import ATSPI_DIRECT_START_ENV, Session, SessionConfig

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

BAD_BINARY = "definitely-not-a-real-binary"
SESSION_ALREADY_RUNNING = "Session already running"
LAUNCHED_PID = re.compile(r"\(PID=(\d+)\)")
PROCESS_EXIT_TIMEOUT_SECONDS = 5.0
A11Y_BUS_NAME = "org.a11y.Bus"
XDG_HOME_DIRECTORIES = (
    Path(".config"),
    Path(".local/share"),
    Path(".local/state"),
    Path(".cache"),
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
    pids_after_first_start = _live_kwin_pids()

    with pytest.raises(RuntimeError, match=SESSION_ALREADY_RUNNING):
        engine.session_start(screen_width=640, screen_height=480)

    assert _live_kwin_pids() == pids_after_first_start
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
    kwin_before = _live_kwin_pids()

    with pytest.raises(FileNotFoundError) as exc_info:
        start_session(BAD_BINARY)

    error = exc_info.value
    assert error.errno == errno.ENOENT
    assert error.filename == BAD_BINARY
    assert "No such file or directory" in str(error)
    assert BAD_BINARY in str(error)
    # The failed start rolled back atomically: no compositor and no session remain.
    # Only live compositors count: the rolled-back KWin is orphaned to the container's
    # reaperless PID 1 and stays a zombie there (see _is_live).
    assert _live_kwin_pids() == kwin_before
    assert engine.session_stop() == "No session running."
    # A following start succeeds instead of reporting a running session.
    assert "Session started" in start_session()


def test_session_start_propagates_environment_to_the_initial_app(
    engine: AutomationEngine, start_session: Callable[..., str]
) -> None:
    variable = "KWIN_MCP_E2E_SESSION_ENV"
    value = "session-start-env-propagated"
    command = f"""sh -c 'printf "env:%s" "${variable}"'"""

    output = start_session(command, env={variable: value})
    pid = _launched_pid(output)

    assert _wait_for_app_log(engine, pid, f"env:{value}") == f"env:{value}"


def test_virtual_session_apps_never_inherit_the_host_display(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A host X server the server process can see must stay out of the virtual
    # session; X11 apps would otherwise open on the user's real desktop.
    monkeypatch.setenv("DISPLAY", ":4242")
    command = """sh -c 'printf "display:%s" "${DISPLAY-unset}"'"""

    initial_pid = _launched_pid(start_session(command))
    launched_pid = _launched_pid(engine.launch_app(command))
    explicit_pid = _launched_pid(engine.launch_app(command, env={"DISPLAY": ":7"}))

    assert _wait_for_app_log(engine, initial_pid, "display:") == "display:unset"
    assert _wait_for_app_log(engine, launched_pid, "display:") == "display:unset"
    # A caller that asks for a display on purpose still gets it.
    assert _wait_for_app_log(engine, explicit_pid, "display:") == "display::7"


def test_live_session_apps_keep_the_inherited_display(
    engine: AutomationEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only virtual sessions drop DISPLAY: launch_app after session_connect
    # keeps the inherited value unchanged.
    monkeypatch.setenv("DISPLAY", ":4242")
    with live_kwin() as live:
        engine.session_connect(dbus_address=live.dbus_address, wayland_display=live.wayland_display)
        try:
            pid = _launched_pid(
                engine.launch_app("""sh -c 'printf "display:%s" "${DISPLAY-unset}"'""")
            )
            assert _wait_for_app_log(engine, pid, "display:") == "display::4242"
        finally:
            engine.session_stop()


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
        assert all((kept_home / path).is_dir() for path in XDG_HOME_DIRECTORIES)
    finally:
        engine.session_stop()
        shutil.rmtree(kept_home, ignore_errors=True)


@pytest.mark.parametrize("keep_screenshots", [False, True], ids=["drop-shots", "keep-shots"])
@pytest.mark.parametrize("keep_home", [False, True], ids=["drop-home", "keep-home"])
@pytest.mark.parametrize("isolate_home", [False, True], ids=["host-home", "isolated-home"])
def test_virtual_session_artifact_cleanup_respects_retention(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    isolate_home: bool,
    keep_home: bool,
    keep_screenshots: bool,
) -> None:
    """keep_screenshots alone decides the screenshot/app-log dir, keep_home alone the home."""
    screenshot_dir: Path | None = None
    home: Path | None = None
    try:
        output = start_session(
            "printf virtual-retention-payload",
            isolate_home=isolate_home,
            keep_home=keep_home,
            keep_screenshots=keep_screenshots,
        )
        if isolate_home:
            home = Path(_output_value(output, "Isolated home: "))
        pid = _launched_pid(output)
        log_path = _app_log_path(output)
        screenshot_dir = log_path.parent
        if home is not None:
            assert not screenshot_dir.is_relative_to(home), (screenshot_dir, home)
        assert _wait_for_app_log(engine, pid, "virtual-retention-payload") == (
            "virtual-retention-payload"
        )
        assert log_path.is_file()

        assert engine.session_stop() == "Session stopped."
        assert log_path.exists() is keep_screenshots
        assert screenshot_dir.exists() is keep_screenshots
        if home is not None:
            assert home.exists() is keep_home
    finally:
        engine.session_stop()
        if screenshot_dir is not None:
            shutil.rmtree(screenshot_dir, ignore_errors=True)
        if home is not None:
            shutil.rmtree(home, ignore_errors=True)


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
    log_path: Path | None = None
    try:
        pid = _launched_pid(output)
        log_path = _app_log_path(output)
        assert not log_path.is_relative_to(home), (log_path, home)
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
        if log_path is not None:
            shutil.rmtree(log_path.parent, ignore_errors=True)


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
    engine.mouse_move(13, 17)
    pid = _launched_pid(
        engine.launch_app(
            'sh -c \'printf "bus:%s\\nwayland:%s\\n" '
            '"$DBUS_SESSION_BUS_ADDRESS" "$WAYLAND_DISPLAY"; exec sleep 60\''
        )
    )
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
    start_session: Callable[..., str],
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
    output = start_session()
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
def test_direct_atspi_lookup_helper_cannot_stall_or_spoof_startup(
    stall_role: Literal["launcher", "registry"],
    spoof_stdout: bool,
    engine: AutomationEngine,
    start_session: Callable[..., str],
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
        assert engine.session_stop() == "Session stopped."
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
    start_session: Callable[..., str],
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
    output = start_session()
    assert "Warning:" in output, output
    assert not registry_marker.exists()
    identity = _read_process_identity(launcher_identity_path)
    assert _process_identity(identity[0]) != identity[1:], identity
    assert _accessibility_daemons() == daemons_before
    assert _a11y_bus_has_owner(engine).split()[-1:] == ["false"]
    _assert_real_session_state(engine)


def test_direct_atspi_lookup_uses_session_data_roots_and_closes_service_fds(
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
        # AutomationEngine.session_start(env=...) applies only to its initial app.
        # This contract belongs to the real session environment used for service lookup.
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


@pytest.mark.parametrize(
    "inherited_monitor", [False, True], ids=["default-shell", "inherited-monitor"]
)
def test_session_start_starts_the_accessibility_bus_directly_when_activation_is_denied(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
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
        output = start_session()

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
        assert reply.split()[-1:] == ["true"], reply
        assert engine.launch_app("kcalc").startswith("App launched: kcalc (PID=")
        assert element_count(wait_for_app("kcalc")) > 0

        monkeypatch.undo()
        assert _process_identity(neighbor.pid) == neighbor_identity
        assert engine.session_stop() == "Session stopped."
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


def test_session_start_does_not_start_the_accessibility_bus_directly_without_opt_in(
    engine: AutomationEngine,
    start_session: Callable[..., str],
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

    output = start_session()

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
    start_session: Callable[..., str],
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
    output = start_session()
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
    start_session: Callable[..., str],
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
        output = start_session()

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
        assert engine.session_stop() == "Session stopped."
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
    start_session: Callable[..., str],
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

    output = start_session()

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
    start_session: Callable[..., str],
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
        assert engine.session_stop() == "Session stopped."
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
    start_session: Callable[..., str],
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
        assert engine.session_stop() == "Session stopped."
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

    with pytest.raises(RuntimeError) as exc_info:
        engine.session_connect(
            dbus_address=dbus_address,
            wayland_display="wayland-missing",
        )

    message = str(exc_info.value)
    assert message.startswith(f"Cannot reach KWin on D-Bus ({dbus_address}):"), message
    assert engine.session_stop() == "No session running."


def test_session_connect_rejects_an_explicit_missing_wayland_socket(
    engine: AutomationEngine,
) -> None:
    with live_kwin() as live:
        missing_display = f"wayland-missing-{uuid4().hex}"
        try:
            with pytest.raises(RuntimeError) as exc_info:
                engine.session_connect(
                    dbus_address=live.dbus_address,
                    wayland_display=missing_display,
                )

            message = str(exc_info.value)
            assert message.startswith(f"Cannot reach Wayland display ({missing_display}):"), message
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

    with pytest.raises(RuntimeError, match=r"^No D-Bus address available\. ") as exc_info:
        engine.session_connect()
    assert str(exc_info.value) == (
        "No D-Bus address available. Provide dbus_address parameter "
        "or ensure $DBUS_SESSION_BUS_ADDRESS is set."
    )

    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/not-consulted")
    with pytest.raises(RuntimeError, match=r"^No Wayland display available\. ") as exc_info:
        engine.session_connect()
    assert str(exc_info.value) == (
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
        with pytest.raises(RuntimeError, match=SESSION_ALREADY_RUNNING):
            engine.session_connect(
                dbus_address=live.dbus_address,
                wayland_display=live.wayland_display,
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
# Launched-app process-group teardown
#
# launch_app puts every app in its own process group so session_stop stops the
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
    tmp_path: Path, engine: AutomationEngine, start_session: Callable[..., str]
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
        launch_output = engine.launch_app(_descendant_command(holder))
        _launched_pid(launch_output)
        assert _wait_until(lambda: bool(_tagged_pids(tag)), PROCESS_EXIT_TIMEOUT_SECONDS), (
            "background descendant never started"
        )

        started = time.monotonic()
        assert engine.session_stop() == "Session stopped."
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
            output = engine.session_connect(
                dbus_address=live.dbus_address,
                wayland_display=live.wayland_display,
            )
            assert output.startswith("Connected to live KWin session."), output
            launch_output = engine.launch_app(_descendant_command(holder))
            _launched_pid(launch_output)
            assert _wait_until(lambda: bool(_tagged_pids(tag)), PROCESS_EXIT_TIMEOUT_SECONDS), (
                "background descendant never started"
            )

            assert engine.session_stop() == "Disconnected from live session."

            gone = _wait_until(lambda: not _tagged_pids(tag), PROCESS_EXIT_TIMEOUT_SECONDS)
            assert gone, f"app descendant survived live stop(): {_tagged_pids(tag)}"
            assert live.process.poll() is None, "connected compositor was killed"
            assert live.socket_path.is_socket()
            assert neighbor.poll() is None, "unrelated neighbor process was killed"
        finally:
            engine.session_stop()
    _reap(neighbor)


def test_session_stop_keeps_app_leader_unreaped_until_last_signal(
    tmp_path: Path, engine: AutomationEngine, start_session: Callable[..., str]
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
        launch_output = engine.launch_app(f"bash -c '{shlex.quote(holder)} & exit 0'")
        leader_pid = _launched_pid(launch_output)
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
        stop_result: list[str] = []
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

        assert stop_result and stop_result[0] == "Session stopped."
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

    def __init__(
        self,
        session: Session,
        config: SessionConfig,
        *,
        start_call: Callable[[], object] | None = None,
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
                self.session.start(self._config) if self._start_call is None else self._start_call()
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
        start_call: Callable[[], object] | None = None,
    ) -> _BackgroundStart:
        run = _BackgroundStart(session, config, start_call=start_call)
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
    outcome: list[str | BaseException] = []

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
    assert isinstance(outcome[0], str), outcome
    assert "Input backend: KWin EIS" in outcome[0], outcome[0]


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
