"""Regression tests for bounded AT-SPI service-file acquisition."""

from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
import tracemalloc
from pathlib import Path

import pytest
from test_session_lifecycle import _bus_name_has_owner

from kwin_mcp.session import ATSPI_DIRECT_START_ENV, Session, SessionConfig, _atspi_service_argv

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
        result = _atspi_service_argv("services", _SERVICE_NAME, _environment(tmp_path))
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
        result = _atspi_service_argv("services", _SERVICE_NAME, _environment(tmp_path))
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

    assert _atspi_service_argv("services", _SERVICE_NAME, _environment(tmp_path)) == expected


def test_valid_service_symlink_is_followed(tmp_path: Path) -> None:
    target = tmp_path / "target.service"
    target.write_bytes(_service_bytes())
    path = _service_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)

    assert _atspi_service_argv("services", _SERVICE_NAME, _environment(tmp_path)) == _VALID_ARGV


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
        _atspi_service_argv("services", _SERVICE_NAME, _environment(first_root, second_root))
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

    assert _atspi_service_argv("services", _SERVICE_NAME, environment) == _VALID_ARGV


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
        _atspi_service_argv("services", _SERVICE_NAME, _environment(first_root, second_root))
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
        result = _atspi_service_argv(
            "services", _SERVICE_NAME, _environment(first_root, second_root)
        )
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
        result = _atspi_service_argv(
            "services", _SERVICE_NAME, _environment(first_root, second_root)
        )
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
                isolate_home=True,
            )
        )
        assert info.startup_warnings == []
        assert _bus_name_has_owner(info.dbus_address, _SERVICE_NAME)
    finally:
        first_root.chmod(original_mode)
        try:
            session.stop()
        finally:
            # Metadata lookup can fail before spawning, when stop() cannot clean up.
            if session._process is None:
                session._cleanup_start_dirs()

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

from kwin_mcp.session import _atspi_service_argv

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
result = _atspi_service_argv(
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

from kwin_mcp.session import _atspi_service_argv


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
    result = _atspi_service_argv("services", "org.a11y.Bus", environment)
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
