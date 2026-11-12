"""Unit tests for session homes, retention and the launched-app registry.

``Session.start`` runs against a stand-in wrapper: the registry's spawn is
wrapped so ``dbus-run-session`` is replaced by a shell that prints the startup
handshake and sleeps, and the Wayland socket file is created for it. Nothing
starts KWin or a D-Bus daemon. Every temp dir lands under ``tmp_path`` and a
fresh ``OwnedProcessRegistry`` replaces the module's, so the registered dirs
(the server exit path's view) can be compared with what ``stop`` keeps.
"""

from __future__ import annotations

import sys
import tempfile
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import IO, TYPE_CHECKING

import pytest

from kwin_mcp import server as server_module
from kwin_mcp import session as session_module
from kwin_mcp.models import KeptPaths, Retention
from kwin_mcp.session import (
    LiveSession,
    OwnedProcessRegistry,
    Session,
    SessionConfig,
    _remove_tree,
)

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

    from kwin_mcp.models import AppEntry, HomeMode

SOCKET_NAME = "wayland-kwin-mcp-retention-unit"
FAKE_DBUS_ADDRESS = "unix:path=/nonexistent/kwin-mcp-unit-test-bus"
# The startup handshake the real wrapper prints once KWin is up; exec keeps
# the sleep as the group leader so session stop's SIGTERM ends it at once.
FAKE_WRAPPER = f"printf 'DBUS_SESSION_BUS_ADDRESS={FAKE_DBUS_ADDRESS}\\nREADY\\n'; exec sleep 60"

RETENTION_MATRIX = [
    pytest.param("host", Retention(), id="host-none"),
    pytest.param("host", Retention(artifacts=True), id="host-artifacts"),
    pytest.param("isolated", Retention(), id="isolated-none"),
    pytest.param("isolated", Retention(artifacts=True), id="isolated-artifacts"),
    pytest.param("isolated", Retention(home=True), id="isolated-home"),
    pytest.param("isolated", Retention(artifacts=True, home=True), id="isolated-both"),
]


@dataclass
class _Harness:
    """A fresh registry whose spawn runs the stand-in wrapper, plus what it observed."""

    registry: OwnedProcessRegistry
    temp_root: Path
    runtime_dir: Path
    spawn_env: list[dict[str, str]] = field(default_factory=list)
    registered_at_spawn: list[list[Path]] = field(default_factory=list)
    fail_spawn: bool = False

    def registered(self) -> set[Path]:
        return set(self.registry._temp_dirs)

    def temp_dirs(self, prefix: str) -> list[Path]:
        return sorted(self.temp_root.glob(f"{prefix}*"))


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Harness:
    temp_root = tmp_path / "tmp"
    runtime_dir = tmp_path / "run"
    temp_root.mkdir()
    runtime_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    registry = OwnedProcessRegistry()
    monkeypatch.setattr(session_module, "process_registry", registry)
    observed = _Harness(registry=registry, temp_root=temp_root, runtime_dir=runtime_dir)
    real_spawn = registry.spawn

    def spawn(
        args: list[str],
        *,
        stdin: int | IO[bytes] | None = None,
        stdout: int | IO[bytes] | None = None,
        stderr: int | IO[bytes] | None = None,
        env: dict[str, str] | None = None,
        start_new_session: bool = False,
    ) -> subprocess.Popen[bytes]:
        if args[0] != "dbus-run-session":
            return real_spawn(
                args,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                env=env,
                start_new_session=start_new_session,
            )
        observed.spawn_env.append(dict(env or {}))
        observed.registered_at_spawn.append(list(registry._temp_dirs))
        if observed.fail_spawn:
            msg = "dbus-run-session: not found"
            raise FileNotFoundError(msg)
        (runtime_dir / SOCKET_NAME).touch()
        return real_spawn(
            ["sh", "-c", FAKE_WRAPPER],
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=env,
            start_new_session=start_new_session,
        )

    monkeypatch.setattr(registry, "spawn", spawn)
    return observed


def _start(home: HomeMode, retention: Retention) -> Session:
    session = Session()
    session.start(SessionConfig(socket_name=SOCKET_NAME, home=home, retention=retention))
    return session


def _expected_registered(
    home_dir: Path | None, screenshot_dir: Path, config_dirs: list[Path], retention: Retention
) -> set[Path]:
    expected = set(config_dirs)
    if not retention.artifacts:
        expected.add(screenshot_dir)
    if home_dir is not None and not retention.home:
        expected.add(home_dir)
    return expected


@pytest.mark.qa("QA-SES-017")
@pytest.mark.parametrize(("home", "retention"), RETENTION_MATRIX)
def test_stop_keeps_exactly_the_retained_dirs(
    harness: _Harness, home: HomeMode, retention: Retention
) -> None:
    session = _start(home, retention)
    info = session.info
    assert info is not None
    screenshot_dir, home_dir = info.screenshot_dir, info.home_dir
    assert (home_dir is not None) == (home == "isolated")
    config_dirs = harness.temp_dirs("kwin-mcp-config-")
    assert len(config_dirs) == (1 if home == "host" else 0)
    # Decided at start: the exit path's registry holds exactly what stop removes.
    assert harness.registered() == _expected_registered(
        home_dir, screenshot_dir, config_dirs, retention
    )
    (screenshot_dir / "frame.png").write_bytes(b"png")

    kept = session.stop()

    assert kept == KeptPaths(
        artifacts_dir=str(screenshot_dir) if retention.artifacts else None,
        home_dir=str(home_dir) if retention.home and home_dir is not None else None,
    )
    assert screenshot_dir.is_dir() == retention.artifacts
    if retention.artifacts:
        assert (screenshot_dir / "frame.png").read_bytes() == b"png"
    if home_dir is not None:
        assert home_dir.is_dir() == retention.home
    assert all(not path.exists() for path in config_dirs)
    assert harness.registered() == set()
    assert session.stop() == KeptPaths()


@pytest.mark.qa("QA-SES-020")
@pytest.mark.qa("QA-SES-021")
@pytest.mark.parametrize(("home", "retention"), RETENTION_MATRIX)
def test_exit_path_keeps_the_same_dirs_as_stop(
    harness: _Harness, home: HomeMode, retention: Retention
) -> None:
    session = _start(home, retention)
    info = session.info
    assert info is not None
    leader = session._process
    assert leader is not None
    screenshot_dir, home_dir = info.screenshot_dir, info.home_dir
    config_dirs = harness.temp_dirs("kwin-mcp-config-")

    # The server exit path (stdin EOF or SIGTERM while a tool is busy) cannot
    # run session_stop: it closes the registry and sweeps what is registered.
    harness.registry.close()
    harness.registry.terminate_all()

    try:
        assert screenshot_dir.is_dir() == retention.artifacts
        if home_dir is not None:
            assert home_dir.is_dir() == retention.home
        assert all(not path.exists() for path in config_dirs)
    finally:
        leader.wait(timeout=10)
        if leader.stdout is not None:
            leader.stdout.close()
        if session._stderr_file is not None:
            session._stderr_file.close()


@pytest.mark.qa("QA-SES-003")
def test_host_home_gets_a_private_config_dir_with_kwalletrc(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/home/host-user")
    session = _start("host", Retention())
    try:
        info = session.info
        assert info is not None
        assert info.home_dir is None
        [config_dir] = harness.temp_dirs("kwin-mcp-config-")
        [env] = harness.spawn_env
        assert env["HOME"] == "/home/host-user"
        assert env["XDG_CONFIG_HOME"] == str(config_dir)
        assert "Enabled=false" in (config_dir / "kwalletrc").read_text()
        assert harness.registered_at_spawn == [[config_dir]]
    finally:
        session.stop()
    assert not config_dir.exists()


@pytest.mark.qa("QA-SES-002")
def test_isolated_home_has_xdg_dirs_and_kwalletrc(harness: _Harness) -> None:
    session = _start("isolated", Retention())
    try:
        info = session.info
        assert info is not None
        home_dir = info.home_dir
        assert home_dir is not None
        assert home_dir.name.startswith("kwin-mcp-home-")
        for subdir in (".config", ".local/share", ".local/state", ".cache"):
            assert (home_dir / subdir).is_dir(), subdir
        assert "Enabled=false" in (home_dir / ".config" / "kwalletrc").read_text()
        [env] = harness.spawn_env
        assert env["HOME"] == str(home_dir)
        assert env["XDG_CONFIG_HOME"] == str(home_dir / ".config")
        assert harness.temp_dirs("kwin-mcp-config-") == []
        assert harness.registered_at_spawn == [[home_dir]]
    finally:
        session.stop()
    assert not home_dir.exists()


@pytest.mark.qa("QA-SES-017")
@pytest.mark.parametrize("keep_home", [False, True])
def test_failed_spawn_follows_retention_home(harness: _Harness, keep_home: bool) -> None:
    harness.fail_spawn = True
    session = Session()
    config = SessionConfig(
        socket_name=SOCKET_NAME, home="isolated", retention=Retention(home=keep_home)
    )

    with pytest.raises(FileNotFoundError, match="dbus-run-session"):
        session.start(config)

    homes = harness.temp_dirs("kwin-mcp-home-")
    assert len(homes) == (1 if keep_home else 0)
    assert harness.registered() == set()
    assert session.info is None


@pytest.mark.qa("QA-SES-015")
@pytest.mark.parametrize("artifacts", [False, True])
def test_live_stop_honours_retention_from_connect(
    harness: _Harness, tmp_path: Path, artifacts: bool
) -> None:
    screenshot_dir = tmp_path / "live-artifacts"
    screenshot_dir.mkdir()
    if not artifacts:
        harness.registry.register_temp_dir(screenshot_dir)
    live = LiveSession(
        FAKE_DBUS_ADDRESS, "wayland-x", screenshot_dir, retention=Retention(artifacts=artifacts)
    )

    kept = live.stop()

    assert kept == KeptPaths(artifacts_dir=str(screenshot_dir) if artifacts else None)
    assert screenshot_dir.is_dir() == artifacts
    assert harness.registered() == set()
    assert live.stop() == KeptPaths()


@pytest.mark.qa("QA-SES-015")
def test_live_stop_retention_argument_overrides_connect(harness: _Harness, tmp_path: Path) -> None:
    screenshot_dir = tmp_path / "live-artifacts"
    screenshot_dir.mkdir()
    live = LiveSession(FAKE_DBUS_ADDRESS, "wayland-x", screenshot_dir)

    kept = live.stop(retention=Retention(artifacts=True))

    assert kept == KeptPaths(artifacts_dir=str(screenshot_dir))
    assert screenshot_dir.is_dir()
    assert harness.registered() == set()


def _wait_until_exited(live: LiveSession, pid: int) -> list[AppEntry]:
    deadline = time.monotonic() + 10
    while True:
        entries = live.apps()
        if not next(entry for entry in entries if entry.pid == pid).running:
            return entries
        if time.monotonic() > deadline:
            pytest.fail(f"app {pid} did not exit")
        time.sleep(0.05)


@pytest.mark.qa("QA-SES-024")
@pytest.mark.qa("QA-APP-009")
@pytest.mark.qa("QA-APP-010")
def test_app_registry_lists_launches_and_tails_logs(harness: _Harness, tmp_path: Path) -> None:
    screenshot_dir = tmp_path / "live-artifacts"
    screenshot_dir.mkdir()
    live = LiveSession(FAKE_DBUS_ADDRESS, "wayland-x", screenshot_dir)
    assert live.apps() == []
    try:
        printer = live.launch_app(
            [sys.executable, "-c", "print('a'); print('b'); print('c'); raise SystemExit(3)"]
        )
        sleeper = live.launch_app(["sleep", "60"])
        silent = live.launch_app([sys.executable, "-c", "pass"])

        _wait_until_exited(live, printer.pid)
        entries = _wait_until_exited(live, silent.pid)

        assert [entry.pid for entry in entries] == [printer.pid, sleeper.pid, silent.pid]
        by_pid = {entry.pid: entry for entry in entries}
        assert by_pid[printer.pid].running is False
        assert by_pid[printer.pid].exit_code == 3
        assert by_pid[printer.pid].log_path == str(printer.log_path)
        assert by_pid[printer.pid].command == printer.command
        assert by_pid[sleeper.pid].running is True
        assert by_pid[sleeper.pid].exit_code is None
        assert by_pid[silent.pid].exit_code == 0

        tail = live.read_app_log(printer.pid, last_n_lines=2)
        assert (tail.lines, tail.truncated) == (["b", "c"], True)
        assert tail.log_path == str(printer.log_path)
        default = live.read_app_log(printer.pid)
        assert (default.lines, default.truncated) == (["a", "b", "c"], False)
        whole = live.read_app_log(printer.pid, last_n_lines=0)
        assert (whole.lines, whole.truncated) == (["a", "b", "c"], False)
        exact = live.read_app_log(printer.pid, last_n_lines=3)
        assert (exact.lines, exact.truncated) == (["a", "b", "c"], False)
        empty = live.read_app_log(silent.pid)
        assert (empty.lines, empty.truncated) == ([], False)

        known = [printer.pid, sleeper.pid, silent.pid]
        with pytest.raises(ValueError) as excinfo:
            live.read_app_log(1)
        assert str(excinfo.value) == f"No app with PID 1. Available PIDs: {known}"
    finally:
        live.stop()
    assert all(entry.running is False for entry in live.apps())
    assert not screenshot_dir.exists()


def test_failed_home_removal_skips_no_other_cleanup(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _start("isolated", Retention())
    info = session.info
    assert info is not None and info.home_dir is not None

    def stuck(path: Path, attempts: int = 3) -> None:
        raise PermissionError(f"cannot remove {path}")

    monkeypatch.setattr(session_module, "_remove_tree", stuck)

    with pytest.raises(PermissionError):
        session.stop()

    assert not info.screenshot_dir.exists()
    assert not (harness.runtime_dir / SOCKET_NAME).exists()
    # The home stays registered, so the registry's exit sweep can retry it.
    assert harness.registered() == {info.home_dir}
    assert session.stop() == KeptPaths()


def test_server_exit_sweeps_a_home_the_stop_could_not_remove(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _start("isolated", Retention())
    info = session.info
    assert info is not None and info.home_dir is not None
    real_remove_tree = session_module._remove_tree
    calls = []

    def stuck_once(path: Path, attempts: int = 3) -> None:
        calls.append(path)
        if len(calls) == 1:
            raise PermissionError(f"cannot remove {path}")
        real_remove_tree(path, attempts)

    monkeypatch.setattr(session_module, "_remove_tree", stuck_once)
    monkeypatch.setattr(server_module, "process_registry", harness.registry)
    monkeypatch.setattr(server_module, "abort_waits", lambda: None)
    monkeypatch.setattr(
        server_module, "_server", SimpleNamespace(engine=SimpleNamespace(session_stop=session.stop))
    )

    outcome = server_module._exit_cleanup()

    assert "raised: PermissionError" in outcome
    assert not info.home_dir.exists()


def test_remove_tree_opens_dirs_apps_left_read_only(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").touch()
    outside.chmod(0o555)
    home = tmp_path / "home"
    mod = home / "go" / "pkg" / "mod" / "x"  # Go's module cache is 0555
    mod.mkdir(parents=True)
    (mod / "f").touch()
    locked = home / "locked" / "inner"  # unreadable, so rmtree cannot enter it
    locked.mkdir(parents=True)
    (locked / "f").touch()
    (mod / "link").symlink_to(outside)  # inside a 0555 dir, so it survives the first rmtree
    for path in (mod, mod.parent, locked.parent):
        path.chmod(0o555)
    locked.parent.chmod(0)

    _remove_tree(home)

    assert not home.exists()
    # The symlink was not followed out of the tree.
    assert (outside / "keep").exists()
    assert outside.stat().st_mode & 0o777 == 0o555
    outside.chmod(0o755)


def test_remove_tree_leaves_the_target_of_a_symlinked_root_alone(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "inner").mkdir(parents=True)
    for path in (outside / "inner", outside):
        path.chmod(0o555)
    home = tmp_path / "home"
    home.symlink_to(outside)  # an app replaced its home with a symlink

    with pytest.raises(OSError):
        _remove_tree(home)

    assert outside.stat().st_mode & 0o777 == 0o555
    assert (outside / "inner").stat().st_mode & 0o777 == 0o555
    outside.chmod(0o755)
