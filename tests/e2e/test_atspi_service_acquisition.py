"""Regression tests for bounded AT-SPI service-file acquisition."""

from __future__ import annotations

import errno
import json
import os
import shlex
import stat
import subprocess
import sys
import tracemalloc
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from test_session_atspi_direct_start import (
    _assert_real_session_state,
    _bus_name_has_owner,
    _deny_first_a11y_activation,
    _install_interpreter_shim,
    _real_launcher_argv,
    _real_service_file,
    _recording_launcher_script,
    _service_data_dir,
)

from kwin_mcp import dbus_service_file
from kwin_mcp.dbus_service_file import _service_argv
from kwin_mcp.session import ATSPI_DIRECT_START_ENV, Session, SessionConfig

if TYPE_CHECKING:
    from collections.abc import Callable

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import SessionInfo

_SERVICE_NAME = "org.a11y.Bus"
_SERVICE_LIMIT = 128 * 1024
_VALID_ARGV = ["/bin/true"]


def _service_path(root: Path) -> Path:
    return root / "dbus-1" / "services" / f"{_SERVICE_NAME}.service"


def _service_bytes() -> bytes:
    return f"[D-BUS Service]\nName={_SERVICE_NAME}\nExec=/bin/true\n".encode()


def _padded_service(size: int) -> bytes:
    data = _service_bytes()
    assert len(data) < size
    return data + b"#" * (size - len(data))


def _write_service(root: Path, data: bytes) -> Path:
    path = _service_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _environment(*roots: Path) -> dict[str, str]:
    return {"XDG_DATA_DIRS": os.pathsep.join(str(root) for root in roots)}


def test_oversized_sparse_service_fails_before_large_python_allocation(tmp_path: Path) -> None:
    path = _service_path(tmp_path)
    path.parent.mkdir(parents=True)
    with path.open("wb") as service:
        service.truncate(32 * 1024 * 1024)

    tracemalloc.start()
    try:
        result = _service_argv("services", _SERVICE_NAME, _environment(tmp_path))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result is None
    assert peak < 1024 * 1024, f"peak Python allocation was {peak} bytes"


def test_service_growth_after_descriptor_stat_is_bounded_and_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_service(tmp_path, _service_bytes())
    identity = path.stat()
    real_fstat = os.fstat
    grew = False

    def grow_after_snapshot(fd: int) -> os.stat_result:
        nonlocal grew
        snapshot = real_fstat(fd)
        if not grew and (snapshot.st_dev, snapshot.st_ino) == (
            identity.st_dev,
            identity.st_ino,
        ):
            with path.open("r+b") as service:
                service.truncate(32 * 1024 * 1024)
            grew = True
        return snapshot

    monkeypatch.setattr(os, "fstat", grow_after_snapshot)
    tracemalloc.start()
    try:
        result = _service_argv("services", _SERVICE_NAME, _environment(tmp_path))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert grew, "the consumer did not reach the descriptor-stat growth injection"
    assert path.stat().st_size == 32 * 1024 * 1024
    assert result is None
    assert peak < 1024 * 1024, f"peak Python allocation was {peak} bytes"


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (_SERVICE_LIMIT, _VALID_ARGV),
        (_SERVICE_LIMIT + 1, None),
    ],
)
def test_service_size_limit_is_exact(tmp_path: Path, size: int, expected: list[str] | None) -> None:
    _write_service(tmp_path, _padded_service(size))

    assert _service_argv("services", _SERVICE_NAME, _environment(tmp_path)) == expected


def test_valid_service_symlink_is_followed(tmp_path: Path) -> None:
    target = tmp_path / "target.service"
    target.write_bytes(_service_bytes())
    path = _service_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)

    assert _service_argv("services", _SERVICE_NAME, _environment(tmp_path)) == _VALID_ARGV


@pytest.mark.parametrize(
    "kind",
    ["missing", "not-directory", "dangling-symlink", "symlink-loop", "directory", "fifo"],
)
def test_nonregular_candidate_is_skipped_for_later_data_root(tmp_path: Path, kind: str) -> None:
    first_root = tmp_path / "first"
    first_path = _service_path(first_root)
    first_path.parent.mkdir(parents=True)
    if kind == "missing":
        pass
    elif kind == "not-directory":
        first_path.parent.rmdir()
        first_path.parent.write_bytes(b"not a directory")
    elif kind == "symlink-loop":
        first_path.symlink_to(first_path)
    elif kind == "dangling-symlink":
        first_path.symlink_to(first_path.with_name("missing.service"))
    elif kind == "directory":
        first_path.mkdir()
    else:
        os.mkfifo(first_path)

    second_root = tmp_path / "second"
    _write_service(second_root, _service_bytes())

    assert (
        _service_argv("services", _SERVICE_NAME, _environment(first_root, second_root))
        == _VALID_ARGV
    )


def test_nul_data_root_is_skipped_for_later_data_root(tmp_path: Path) -> None:
    invalid_root = f"{tmp_path}/invalid\0share"
    second_root = tmp_path / "second"
    _write_service(second_root, _service_bytes())
    environment = {"XDG_DATA_DIRS": f"{invalid_root}{os.pathsep}{second_root}"}

    # Exercise the private lookup's path handling, not public session environment support.
    with pytest.raises(ValueError):
        _service_path(Path(invalid_root)).stat()

    assert _service_argv("services", _SERVICE_NAME, environment) == _VALID_ARGV


def test_bad_descriptor_metadata_error_is_skipped_for_later_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_root = tmp_path / "first"
    first_path = _write_service(first_root, _service_bytes().replace(b"/bin/true", b"/bin/false"))
    second_root = tmp_path / "second"
    second_path = _write_service(second_root, _service_bytes())
    real_stat = Path.stat
    injected = False
    second_stat_called = False

    def stat_with_bad_descriptor(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        nonlocal injected, second_stat_called
        if path == first_path:
            injected = True
            raise OSError(errno.EBADF, os.strerror(errno.EBADF), str(path))
        if path == second_path:
            second_stat_called = True
        return real_stat(path, follow_symlinks=follow_symlinks)

    # Model the metadata I/O boundary; the later candidate still uses the real filesystem.
    monkeypatch.setattr(Path, "stat", stat_with_bad_descriptor)

    assert (
        _service_argv("services", _SERVICE_NAME, _environment(first_root, second_root))
        == _VALID_ARGV
    )
    assert injected, "the first candidate did not reach the metadata error injection"
    assert second_stat_called, "the later candidate did not reach the real metadata lookup"


@pytest.mark.skipif(os.geteuid() == 0, reason="permission failure requires an unprivileged tester")
def test_unreadable_regular_candidate_fails_closed_without_later_root_fallback(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    path = _write_service(first_root, _service_bytes())
    path.chmod(0)
    second_root = tmp_path / "second"
    _write_service(second_root, _service_bytes())

    try:
        result = _service_argv("services", _SERVICE_NAME, _environment(first_root, second_root))
    finally:
        path.chmod(0o600)

    assert result is None


@pytest.mark.skipif(os.geteuid() == 0, reason="permission failure requires an unprivileged tester")
def test_unsearchable_data_root_fails_closed_without_later_root_fallback(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    first_path = _write_service(first_root, _service_bytes())
    second_root = tmp_path / "second"
    _write_service(second_root, _service_bytes())
    original_mode = stat.S_IMODE(first_root.stat().st_mode)
    first_root.chmod(0)

    try:
        with pytest.raises(PermissionError) as denied:
            first_path.stat()
        assert denied.value.errno == errno.EACCES
        result = _service_argv("services", _SERVICE_NAME, _environment(first_root, second_root))
    finally:
        first_root.chmod(original_mode)

    assert result is None


@pytest.mark.skipif(os.geteuid() == 0, reason="permission failure requires an unprivileged tester")
def test_unsearchable_data_root_does_not_prevent_healthy_accessibility_bus_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    screen_size: tuple[int, int],
) -> None:
    first_root = tmp_path / "blocked-share"
    first_path = _write_service(first_root, _service_bytes())
    monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
    monkeypatch.setenv(
        "XDG_DATA_DIRS",
        _environment(first_root, Path("/usr/local/share"), Path("/usr/share"))["XDG_DATA_DIRS"],
    )
    original_mode = stat.S_IMODE(first_root.stat().st_mode)
    session = Session()
    first_root.chmod(0)

    try:
        with pytest.raises(PermissionError) as denied:
            first_path.stat()
        assert denied.value.errno == errno.EACCES
        info = session.start(
            SessionConfig(
                screen_width=screen_size[0],
                screen_height=screen_size[1],
                home="isolated",
            )
        )
        assert info.startup_warnings == []
        assert _bus_name_has_owner(info.dbus_address, _SERVICE_NAME)
    finally:
        first_root.chmod(original_mode)
        # Lookup is child-owned after failed activation; session teardown owns all artifacts.
        session.stop()

    assert info.home_dir is not None
    assert not info.home_dir.exists()
    assert not info.screenshot_dir.exists()


def test_regular_to_fifo_replacement_at_read_boundary_cannot_hang(
    tmp_path: Path,
) -> None:
    root = tmp_path / "race-root"
    candidate = _write_service(root, _service_bytes())
    replacement = candidate.with_name("replacement.service")
    marker = tmp_path / "race-replaced"
    environment = os.environ.copy()
    environment["XDG_DATA_DIRS"] = str(root)
    environment["KWIN_MCP_RACE_TARGET"] = str(candidate)
    environment["KWIN_MCP_RACE_REPLACEMENT"] = str(replacement)
    environment["KWIN_MCP_RACE_MARKER"] = str(marker)
    script = """
import os
import sys
from pathlib import Path

from kwin_mcp.dbus_service_file import _service_argv

target = os.environ["KWIN_MCP_RACE_TARGET"]
replacement = os.environ["KWIN_MCP_RACE_REPLACEMENT"]
marker = os.environ["KWIN_MCP_RACE_MARKER"]


def replace_at_open(event: str, args: tuple[object, ...]) -> None:
    if event != "open" or not args:
        return
    opened = args[0]
    if not isinstance(opened, (str, bytes)) or os.fsdecode(opened) != target:
        return
    os.mkfifo(replacement)
    os.replace(replacement, target)
    Path(marker).write_text("replaced")


sys.addaudithook(replace_at_open)
result = _service_argv(
    "services",
    "org.a11y.Bus",
    {"XDG_DATA_DIRS": os.environ["XDG_DATA_DIRS"]},
)
if result is not None:
    raise SystemExit(2)
print("completed", flush=True)
"""

    process = subprocess.Popen(
        [sys.executable, "-c", script],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    timed_out = False
    try:
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            process.kill()
            stdout, stderr = process.communicate(timeout=3)
            stderr = f"{stderr}\n{exc}"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)

    assert marker.read_text() == "replaced"
    assert not timed_out, f"service acquisition hung during a FIFO race: {stderr}"
    assert process.returncode == 0, stderr
    assert stdout.strip() == "completed"


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="requires Linux procfs")
@pytest.mark.parametrize("kind", ["success", "oversize", "nonregular", "parseerror"])
def test_service_acquisition_closes_descriptors_in_isolated_process(
    tmp_path: Path, kind: str
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    if kind == "nonregular":
        candidate = _service_path(first_root)
        candidate.parent.mkdir(parents=True)
        os.mkfifo(candidate)
    elif kind == "oversize":
        _write_service(first_root, _padded_service(_SERVICE_LIMIT + 1))
    elif kind == "parseerror":
        _write_service(first_root, _service_bytes().replace(b"/bin/true", b"'/bin/true"))
    else:
        _write_service(first_root, _service_bytes())
    _write_service(second_root, _service_bytes())
    script = """
import os
import sys

from kwin_mcp.dbus_service_file import _service_argv


def fd_identities() -> set[tuple[int, int, int, int, str]]:
    with os.scandir("/proc/self/fd") as entries:
        descriptors = [entry.name for entry in entries]
    identities: set[tuple[int, int, int, int, str]] = set()
    for descriptor in descriptors:
        path = f"/proc/self/fd/{descriptor}"
        try:
            info = os.stat(path)
            target = os.readlink(path)
        except FileNotFoundError:
            continue
        identities.add((int(descriptor), info.st_dev, info.st_ino, info.st_mode, target))
    return identities


environment = {"XDG_DATA_DIRS": os.pathsep.join(sys.argv[2:])}
expected = ["/bin/true"] if sys.argv[1] in {"success", "nonregular"} else None
before = fd_identities()
for attempt in range(16):
    result = _service_argv("services", "org.a11y.Bus", environment)
    assert result == expected, (attempt, result, expected)
    after = fd_identities()
    assert after == before, (attempt, before, after)
print("closed", flush=True)
"""

    result = subprocess.run(
        [sys.executable, "-c", script, kind, str(first_root), str(second_root)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "closed"


def _cli_services(
    tmp_path: Path, *, exit_code: int = 0
) -> tuple[Path, dict[str, Path], dict[str, list[str]]]:
    """Provide executable recorders, not bus-name owners, for the private CLI contract."""
    root = tmp_path / "cli-share"
    records: dict[str, Path] = {}
    arguments: dict[str, list[str]] = {}
    for role, subdir, name in (
        ("launcher", "services", _SERVICE_NAME),
        ("registry", "accessibility-services", "org.a11y.atspi.Registry"),
    ):
        recorded = tmp_path / f"{role}-record.json"
        script = tmp_path / f"{role}-recorder"
        script.write_text(
            f"#!{sys.executable} -I\n"
            "import json\n"
            "import os\n"
            "import sys\n"
            f"with open({str(recorded)!r}, 'w') as record:\n"
            "    json.dump({'argv': sys.argv, 'environment': {\n"
            "        name: os.environ.get(name) for name in (\n"
            "            'XDG_DATA_DIRS', 'DBUS_SESSION_BUS_ADDRESS', 'AT_SPI_BUS_ADDRESS',\n"
            "            'KWIN_MCP_CLI_VALUE', 'PYTHONPATH'\n"
            "        )}}, record)\n"
            f"raise SystemExit({exit_code})\n"
        )
        script.chmod(0o755)
        literal = [
            str(script),
            f"{role} value with spaces",
            "",
            "$KWIN_MCP_CLI_VALUE",
            ";",
            "$(touch should-not-exist)",
            "*",
            "quote'and\"double",
        ]
        service = root / "dbus-1" / subdir / f"{name}.service"
        service.parent.mkdir(parents=True, exist_ok=True)
        command = shlex.join(literal).replace("\\", "\\\\")
        service.write_text(f"[D-BUS Service]\nName={name}\nExec={command}\n")
        records[role] = recorded
        arguments[role] = literal
    return root, records, arguments


def _run_internal_cli(
    args: list[str], environment: dict[str, str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Run the actual installed stdlib-only file with the wrapper's isolation flags."""
    helper = dbus_service_file.__file__
    assert helper is not None
    return subprocess.run(
        [sys.executable, "-I", "-S", str(Path(helper).resolve()), *args],
        env=environment,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


@pytest.mark.parametrize(
    "args",
    [
        [],
        [""],
        ["unknown"],
        ["--help"],
        ["launcher", "registry"],
        ["registry", "extra"],
        ["launcher", "extra", "extra"],
    ],
    ids=["zero", "empty", "unknown", "option", "two-roles", "extra", "odd-extra"],
)
def test_internal_atspi_cli_rejects_every_non_role_contract(
    tmp_path: Path, args: list[str]
) -> None:
    root, records, _arguments = _cli_services(tmp_path)
    environment = os.environ | _environment(root)

    result = _run_internal_cli(args, environment, cwd=tmp_path)

    assert result.returncode == 2, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert not any(record.exists() for record in records.values())


@pytest.mark.parametrize(
    ("role", "broken_role"),
    [("launcher", "launcher"), ("launcher", "registry"), ("registry", "registry")],
    ids=["launcher-first", "launcher-preflight-registry", "registry-own"],
)
@pytest.mark.parametrize("kind", ["missing", "invalid"])
def test_internal_atspi_cli_fails_preflight_without_executing_either_service(
    tmp_path: Path, role: str, broken_role: str, kind: str
) -> None:
    root, records, _arguments = _cli_services(tmp_path)
    subdir, name = (
        ("services", _SERVICE_NAME)
        if broken_role == "launcher"
        else ("accessibility-services", "org.a11y.atspi.Registry")
    )
    service = root / "dbus-1" / subdir / f"{name}.service"
    if kind == "missing":
        service.unlink()
    else:
        service.write_text(f"[D-BUS Service]\nName={name}\nExec='unterminated\n")

    result = _run_internal_cli([role], os.environ | _environment(root), cwd=tmp_path)

    assert result.returncode == 1, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert not any(record.exists() for record in records.values())


@pytest.mark.parametrize("role", ["launcher", "registry"])
@pytest.mark.parametrize("exit_code", [0, 23], ids=["success", "target-exit"])
def test_internal_atspi_cli_execs_literal_service_argv_and_environment_in_isolation(
    tmp_path: Path, role: str, exit_code: int
) -> None:
    root, records, arguments = _cli_services(tmp_path, exit_code=exit_code)
    hostile = tmp_path / "hostile-python"
    hostile.mkdir()
    injection_marker = tmp_path / "pythonpath-imported"
    injection = f"open({str(injection_marker)!r}, 'w').write('imported')\nraise SystemExit(79)\n"
    (hostile / "sitecustomize.py").write_text(injection)
    (hostile / "pathlib.py").write_text(injection)
    (hostile / "kwin_mcp").mkdir()
    (hostile / "kwin_mcp" / "__init__.py").write_text(injection)
    environment = (
        os.environ
        | _environment(root)
        | {
            "PYTHONPATH": str(hostile),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/not-a-session-bus",
            "AT_SPI_BUS_ADDRESS": "unix:path=/not-an-accessibility-bus",
            "KWIN_MCP_CLI_VALUE": "literal environment ; $value with spaces",
        }
    )

    result = _run_internal_cli([role], environment, cwd=hostile)

    assert result.returncode == exit_code, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert json.loads(records[role].read_text()) == {
        "argv": arguments[role],
        "environment": {
            name: environment[name]
            for name in (
                "XDG_DATA_DIRS",
                "DBUS_SESSION_BUS_ADDRESS",
                "AT_SPI_BUS_ADDRESS",
                "KWIN_MCP_CLI_VALUE",
                "PYTHONPATH",
            )
        },
    }
    counterpart = "registry" if role == "launcher" else "launcher"
    assert not records[counterpart].exists()
    assert not injection_marker.exists()
    assert not (hostile / "should-not-exist").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="permission failure requires an unprivileged tester")
@pytest.mark.parametrize("kind", ["unreadable-service", "unsearchable-root"])
def test_direct_atspi_child_fails_closed_before_a_later_real_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    kind: str,
) -> None:
    """Real Session startup stays usable, but the child must not bypass a denied first root."""
    first_root = tmp_path / "denied-first"
    first_path = _write_service(first_root, _service_bytes())
    later_record = tmp_path / "later-launcher-argv"
    later_identity = tmp_path / "later-launcher-identity"
    launcher_exec = _recording_launcher_script(
        tmp_path / "later-launcher", later_record, later_identity, _real_launcher_argv()
    )
    later_root = _service_data_dir(tmp_path, monkeypatch, launcher_exec)
    (later_root / "dbus-1/accessibility-services/org.a11y.atspi.Registry.service").write_text(
        _real_service_file("accessibility-services/org.a11y.atspi.Registry.service")
    )
    # App environment belongs to app_launch; operator roots must reach Session._build_env.
    monkeypatch.setenv(
        "XDG_DATA_DIRS",
        _environment(first_root, later_root, Path("/usr/local/share"), Path("/usr/share"))[
            "XDG_DATA_DIRS"
        ],
    )
    monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
    _deny_first_a11y_activation(tmp_path, monkeypatch)
    log = _install_interpreter_shim(tmp_path, monkeypatch, stall_role="none", spoof_stdout=False)
    denied_path = first_path if kind == "unreadable-service" else first_root
    original_mode = stat.S_IMODE(denied_path.stat().st_mode)
    denied_path.chmod(0)
    try:
        with pytest.raises(PermissionError) as denied:
            if kind == "unreadable-service":
                first_path.read_bytes()
            else:
                first_path.stat()
        assert denied.value.errno == errno.EACCES
        output = start_session().render()
        assert "Session started. Wayland socket: " in output, output
        assert "Warning:" in output, output
        assert "started the accessibility bus and registry directly" not in output, output
        assert {line.split()[0] for line in log.read_text().splitlines()} == {"launcher"}
        assert not later_record.exists(), "the denied first root was bypassed"
        assert not later_identity.exists()
        session = engine._session
        assert isinstance(session, Session) and session.info is not None
        assert not _bus_name_has_owner(session.info.dbus_address, _SERVICE_NAME)
        _assert_real_session_state(engine)
    finally:
        denied_path.chmod(original_mode)
        engine.session_stop()


def test_direct_atspi_child_skips_a_fifo_for_a_later_launcher_recorder(tmp_path: Path) -> None:
    """Exercise FIFO fallthrough through the installed lookup/parser/exec child, not D-Bus."""
    # A full Session cannot isolate this fault on dbus-daemon 1.16.2: activation.c's
    # bus_activation_reload -> update_directory loads every *.service before the wrapper runs.
    # desktop-file.c's bus_desktop_file_load calls _dbus_file_get_contents, whose
    # dbus-file-unix.c open(O_RDONLY | O_BINARY) can block on this writerless FIFO before fstat.
    # The Session startup failure therefore never reaches our helper. Run the exact private
    # -I -S launcher role below; the real-Session permission and ownership cases stay above
    # and in test_session_atspi_direct_start.py. Not all D-Bus versions scan identically.
    first_root = tmp_path / "fifo-first"
    first_path = _service_path(first_root)
    first_path.parent.mkdir(parents=True)
    os.mkfifo(first_path)
    later_root, records, arguments = _cli_services(tmp_path)
    launcher = Path(arguments["launcher"][0])
    environment_names = (
        "XDG_DATA_DIRS",
        "DBUS_SESSION_BUS_ADDRESS",
        "AT_SPI_BUS_ADDRESS",
        "KWIN_MCP_CLI_VALUE",
        "PYTHONPATH",
    )
    launcher.write_text(
        f"#!{sys.executable} -I\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "descriptors = {}\n"
        "for descriptor in os.listdir('/proc/self/fd'):\n"
        "    try:\n"
        "        descriptors[descriptor] = os.readlink('/proc/self/fd/' + descriptor)\n"
        "    except FileNotFoundError:\n"
        "        continue\n"
        f"with open({str(records['launcher'])!r}, 'w') as record:\n"
        "    json.dump({'argv': sys.argv, 'environment': {\n"
        f"        name: os.environ.get(name) for name in {environment_names!r}\n"
        "    }, 'descriptors': descriptors}, record)\n"
    )
    launcher.chmod(0o755)
    environment = (
        os.environ
        | _environment(first_root, later_root)
        | {
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/not-a-session-bus",
            "AT_SPI_BUS_ADDRESS": "unix:path=/not-an-accessibility-bus",
            "KWIN_MCP_CLI_VALUE": "literal FIFO fallback ; $value with spaces",
            "PYTHONPATH": str(tmp_path / "not-a-python-import-root"),
        }
    )
    assert stat.S_ISFIFO(first_path.stat().st_mode)
    assert _service_path(later_root).is_file()
    assert (later_root / "dbus-1/accessibility-services/org.a11y.atspi.Registry.service").is_file()

    # subprocess.run kills and reaps the actual helper on timeout; there is no FIFO writer,
    # project-logic replacement, or internal timing assertion that could mask a blocked lookup.
    result = _run_internal_cli(["launcher"], environment, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    observed = json.loads(records["launcher"].read_text())
    assert observed["argv"] == arguments["launcher"]
    assert observed["environment"] == {name: environment[name] for name in environment_names}
    assert not records["registry"].exists(), "registry preflight must not exec the registry"
    assert stat.S_ISFIFO(first_path.stat().st_mode), "the first candidate was not replaced"
    descriptors = observed["descriptors"]
    assert {"0", "1", "2"} <= descriptors.keys(), descriptors
    assert not any(
        target == str(root) or target.startswith(str(root) + os.sep)
        for target in descriptors.values()
        for root in (first_root, later_root)
    ), descriptors
    assert not (tmp_path / "should-not-exist").exists()
