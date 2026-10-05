"""KWin Wayland session management.

Manages the lifecycle of KWin Wayland sessions:
- Virtual sessions: isolated via dbus-run-session + kwin_wayland --virtual
- Live sessions: connecting to an existing KWin compositor (real desktop or container)
"""

from __future__ import annotations

import contextlib
import os
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, cast

from kwin_mcp import progress

if TYPE_CHECKING:
    from typing import IO

# Upper bound for the startup handshake. The wrapper itself is bounded: the
# AT-SPI bus activation call gives up after 10 s (--reply-timeout) and the
# socket wait after ~30 s, about 40 s in the worst case. This bound only covers
# cases the wrapper cannot report: a leader killed while descendants keep
# stdout open, or a partial line that never terminates. 60 s leaves the wrapper
# room to report first so its FAILED diagnostics reach the caller.
_STARTUP_READ_TIMEOUT = 60.0

# KWin creates its Wayland socket early in startup but owns org.kde.KWin on
# the session bus only once its workspace exists, which took over 2 s on a
# loaded host. Every D-Bus consumer (EIS input, screenshots, scripting)
# needs that name, so the wrapper waits for it before printing READY; this
# bounds that wait.
_KWIN_BUS_NAME = "org.kde.KWin"
_KWIN_BUS_NAME_TIMEOUT = 30


class SessionType(Enum):
    """Type of KWin session."""

    VIRTUAL = "virtual"
    LIVE = "live"


@dataclass
class SessionConfig:
    """Configuration for an isolated KWin session."""

    socket_name: str = ""
    screen_width: int = 1920
    screen_height: int = 1080
    enable_clipboard: bool = False
    keep_screenshots: bool = False
    isolate_home: bool = False
    keep_home: bool = False
    extra_env: dict[str, str] = field(default_factory=dict)


def _write_deterministic_session_config(config_dir: Path) -> None:
    """Pre-seed the compositor's config dir with settings that keep sessions deterministic.

    A wallet-enabled ``kwalletrc`` makes kwalletd open its popup when a client
    requests the wallet, and that window takes compositor focus away from the
    app under automation, breaking flows such as ``focus_window`` followed by
    keyboard input. Seeding ``Enabled=false`` keeps the wallet closed.

    The keyboard layout is not configured here: ``_build_env`` pins the
    compositor keymap through the environment, so no ``kxkbrc`` is needed.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "kwalletrc").write_text(
        "[Wallet]\nEnabled=false\nFirst Use=false\nLaunch Manager=false\n"
    )


@dataclass
class AppInfo:
    """Tracking info for a launched application."""

    pid: int
    command: str
    log_path: Path
    process: subprocess.Popen[bytes]


@dataclass
class SessionInfo:
    """Runtime information about a running session."""

    dbus_address: str
    wayland_socket: str
    kwin_pid: int
    screenshot_dir: Path = field(default_factory=lambda: Path("/tmp"))
    home_dir: Path | None = None
    app_pid: int | None = None
    wrapper_pid: int | None = None
    apps: dict[int, AppInfo] = field(default_factory=dict)
    session_type: SessionType = SessionType.VIRTUAL
    # Degraded-but-running conditions reported by the startup wrapper, such as
    # a failed AT-SPI bus activation. The session is usable; callers surface these.
    startup_warnings: list[str] = field(default_factory=list)


def _remove_tree(path: Path, attempts: int = 3) -> None:
    """Remove a directory tree, retrying while stragglers finish writing.

    Silently ignoring errors here once hid a leaked isolated home for a whole
    session, so the last attempt reports what is left behind.
    """
    for attempt in range(attempts):
        shutil.rmtree(path, ignore_errors=attempt < attempts - 1)
        if not path.exists():
            return
        time.sleep(0.3)


# Grace periods for launched-app process-group teardown: how long to wait
# between SIGTERM and SIGKILL, and after SIGKILL. The escalation is shared
# across all launched apps (SIGTERM every group, wait, SIGKILL every surviving
# group, wait), so the total bound is the TERM grace plus the KILL grace (~5 s)
# regardless of how many apps were launched — the same TERM-all-then-wait shape
# the old direct-child-only teardown used.
_APP_TERM_GRACE_SECONDS = 3.0
_APP_KILL_GRACE_SECONDS = 2.0


def _signal_process_group(pgid: int, sig: int) -> None:
    """Signal every member of a process group, ignoring races.

    The pgid is allocated while any member lives, so it stays valid even after
    the group's leader was reaped — unlike os.getpgid(pid), which raises ESRCH
    on a reaped leader. Nothing stops the kernel from recycling the pid once
    the last member is gone, so callers attribute the pgid to a process they
    own and signal promptly.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def _group_alive(pgid: int) -> bool:
    """Return True while any live (non-zombie) member of the group exists.

    killpg(pgid, 0) also succeeds for unreaped zombies, which persist when
    orphaned members are reparented to a PID 1 that never reaps (e.g. a
    container whose entrypoint execs pytest). Treating those as alive would
    make every teardown wait out its full timeout, so on Linux the group's
    members are confirmed through /proc/<pid>/stat and zombies are ignored.
    """
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    proc = Path("/proc")
    if not (proc / "self" / "stat").exists():
        # No procfs: killpg cannot tell zombies apart, so assume alive.
        return True
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            # The process exited (or is inaccessible) between listing and read.
            continue
        # Fields after the comm's closing ")" are: state ppid pgrp ...
        fields = stat.rpartition(")")[2].split()
        if len(fields) > 2 and fields[2] == str(pgid) and fields[0] not in ("Z", "X"):
            return True
    return False


def _wait_for_group_exit(pgid: int, timeout: float) -> None:
    """Block until the process group is empty or timeout elapses."""
    deadline = time.monotonic() + timeout
    while _group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.05)


def _leader_reaped(pid: int) -> bool:
    """Whether our direct child pid has been reaped (is no longer a child).

    Probes with waitid(WEXITED | WNOHANG | WNOWAIT), which reports the child's
    status without reaping it: a still-running or zombie child is ours, a
    reaped one is not. While the leader is ours, its pid — which is its
    process-group id — stays reserved, so killpg on that group cannot reach a
    pid recycled into an unrelated group.
    """
    try:
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return True
    return False


def _wait_for_leader_groups_exit(leaders: list[subprocess.Popen[bytes]], timeout: float) -> None:
    """Block until every leader's process group is empty or timeout elapses."""
    deadline = time.monotonic() + timeout
    while any(_group_alive(leader.pid) for leader in leaders) and time.monotonic() < deadline:
        time.sleep(0.05)


def _terminate_app_groups(apps: list[AppInfo]) -> None:
    """Stop every launched app and its descendants, reaping the leaders."""
    process_registry.terminate_leaders([app.process for app in apps], reap=True)


class OwnedProcessRegistry:
    """Thread-safe registry of the process groups and temp dirs kwin-mcp owns.

    The server's exit path cannot always run ``session_stop`` (the engine is not
    thread-safe and the tool thread may be busy with a long tool), so on that
    path the main thread cleans up the owned resources directly. This registry
    records, at spawn time, the process groups kwin-mcp owns — the virtual
    session's ``dbus-run-session`` wrapper group and every launched app group,
    virtual or live — plus the temp dirs a virtual session creates that
    ``session_stop`` would remove (isolated home, per-session config dir,
    screenshot dir, and a keep-home session's ``.screenshots`` subdirectory).
    KWin of a live session and any pre-existing process are never recorded, so
    they are never signalled.

    An entry exists if and only if its leader is an unreaped child of this
    process: the registry holds the owned ``subprocess.Popen`` (the leader), not
    a bare pgid, and every operation that can reap a registered leader —
    ``poll``, ``wait``, ``kill``, and the final ``reap`` — goes through a helper
    that holds the lock and unregisters the leader in the same critical section.
    The exit path (``terminate_all``) therefore signals only the entries whose
    leaders are still unreaped children, so it never signals a group whose
    leader has been reaped and whose pgid the kernel may have recycled into an
    unrelated group.

    Spawn and registration are atomic with respect to ``close()``: the lock is
    held across ``subprocess.Popen`` and the registration, so ``close()``
    observes a spawn as either fully complete or not started. After ``close()``
    a spawn is refused with a clear error instead of starting a group that
    ``close()`` and ``terminate_all`` have already passed over (leaking it).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._closed = False
        self._processes: dict[int, subprocess.Popen[bytes]] = {}
        self._temp_dirs: list[Path] = []

    def close(self) -> None:
        """Mark the registry closed; no spawn is allowed from here on.

        Takes the same lock ``spawn`` holds, so a spawn in progress finishes
        registering its group before ``close`` proceeds.
        """
        with self._lock:
            self._closed = True

    def spawn(self, args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Atomically spawn a process and register it as an owned leader.

        Holds the lock across ``subprocess.Popen`` and the registration. The
        caller passes the same arguments it would give ``subprocess.Popen``.
        After ``close()`` this raises instead of spawning: a group started now
        would not be signalled by the already-run ``terminate_all`` and would
        leak, so it must be refused rather than started.
        """
        with self._lock:
            if self._closed:
                msg = "the process registry is closed; refusing to spawn a new process"
                raise RuntimeError(msg)
            raw = subprocess.Popen(args, **kwargs)  # ty: ignore[no-matching-overload]
            proc = cast("subprocess.Popen[bytes]", raw)
            self._processes[proc.pid] = proc
        return proc

    def register_temp_dir(self, path: Path) -> None:
        """Record a temp dir a session created, for ``terminate_all`` to remove."""
        with self._lock:
            if path not in self._temp_dirs:
                self._temp_dirs.append(path)

    def unregister_temp_dir(self, path: Path) -> None:
        """Drop a temp dir a stop already removed."""
        with self._lock, contextlib.suppress(ValueError):
            self._temp_dirs.remove(path)

    def poll(self, proc: subprocess.Popen[bytes]) -> int | None:
        """Poll a registered leader; if it has exited, unregister it.

        ``Popen.poll`` reaps a finished child, so once it reports a status the
        leader is no longer an owned child and must leave the registry in the
        same critical section.
        """
        with self._lock:
            rc = proc.poll()
            if rc is not None:
                self._processes.pop(proc.pid, None)
            return rc

    def wait(self, proc: subprocess.Popen[bytes], timeout: float) -> int:
        """Wait for a registered leader, unregistering on reap.

        Polls in short steps so the lock is not held across the whole wait
        (``Popen.wait`` with a timeout would block the lock for the full bound).
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                rc = proc.poll()
                if rc is not None:
                    self._processes.pop(proc.pid, None)
                    return rc
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(proc.args, timeout)
            time.sleep(0.05)

    def kill(self, proc: subprocess.Popen[bytes]) -> None:
        """Send SIGKILL to a registered leader; CPython polls first, so this
        can reap a finished child and must unregister it in the same section."""
        with self._lock:
            proc.kill()
            if proc.returncode is not None:
                self._processes.pop(proc.pid, None)

    def reap(self, proc: subprocess.Popen[bytes]) -> None:
        """Reap a registered leader after its group's last signal, unregistering it.

        Called only by the owner side (``session_stop``), once no further killpg
        can follow. Polls in short steps so the lock is not held across the whole
        wait; a leader that is not gone by the bound is left for the process to
        orphan (its descendants, if any, are the residual documented below).
        """
        deadline = time.monotonic() + _APP_KILL_GRACE_SECONDS
        while True:
            with self._lock:
                rc = proc.poll()
                if rc is not None:
                    self._processes.pop(proc.pid, None)
                    return
            if time.monotonic() >= deadline:
                return
            time.sleep(0.05)

    def _signal_owned(self, leaders: list[subprocess.Popen[bytes]], sig: int) -> None:
        """Signal every leader that is still an unreaped child, under the lock.

        The ownership check and the signal run in one critical section, so no
        other thread can reap the leader between them. Signals are fast; the
        grace waits in the escalation run outside the lock.
        """
        with self._lock:
            for leader in leaders:
                if not _leader_reaped(leader.pid):
                    _signal_process_group(leader.pid, sig)

    def _kill_owned(self, leaders: list[subprocess.Popen[bytes]]) -> None:
        """SIGKILL every still-owned leader whose group has live members."""
        with self._lock:
            for leader in leaders:
                if not _leader_reaped(leader.pid) and _group_alive(leader.pid):
                    _signal_process_group(leader.pid, signal.SIGKILL)

    def terminate_leaders(self, leaders: list[subprocess.Popen[bytes]], *, reap: bool) -> None:
        """Stop every leader's process group in one bounded escalation.

        SIGTERM every group whose leader is still an unreaped child, wait once for
        the groups to exit, SIGKILL every group that still has live members, wait
        once more. The bound is the TERM grace plus the KILL grace, shared across
        all groups rather than paid per group.

        The ownership check (``_leader_reaped``) and the signal run under this
        registry's lock, so no other thread can reap the leader between them; the
        grace waits run outside the lock. While a leader is an unreaped child its
        pid (== its process-group id) stays reserved, so killpg can never reach a
        pid recycled into an unrelated group.

        Leaders are reaped (and unregistered from the registry) only after the last
        signal, and only when ``reap`` is set — the owner side (``session_stop``),
        where this thread owns the ``Popen`` objects. On the exit path (``reap=False``)
        the process exits right after and the tool thread may still own the objects,
        so the leaders are left for the process to exit. If a leader was already
        reaped while its descendants still hold the group, that group is left
        unsignalled: ownership can no longer be proven (the same floor PR2 accepted).
        """
        self._signal_owned(leaders, signal.SIGTERM)
        _wait_for_leader_groups_exit(leaders, _APP_TERM_GRACE_SECONDS)
        self._kill_owned(leaders)
        _wait_for_leader_groups_exit(leaders, _APP_KILL_GRACE_SECONDS)
        if reap:
            for leader in leaders:
                self.reap(leader)

    def terminate_all(self) -> None:
        """Signal every still-registered group (no reap) and remove owned temp dirs.

        Runs on the main thread after ``close()`` without touching the engine,
        through the same shared escalation ``session_stop`` uses (``reap=False``):
        SIGTERM to every group whose leader is still an unreaped child, one
        shared wait, SIGKILL to the survivors, one shared wait, then best-effort
        removal of the recorded temp dirs. The leaders are not reaped here: the
        tool thread may still own the ``Popen`` objects, and the process exits
        immediately afterwards.

        If a leader was already reaped while its descendants still hold the
        group, that group is left unsignalled — ownership can no longer be
        proven, so signalling it could reach a recycled pgid (the same floor
        PR2 accepted; reachable only if the wrapper exits on its own first).
        """
        with self._lock:
            leaders = list(self._processes.values())
        self.terminate_leaders(leaders, reap=False)
        with self._lock:
            dirs = list(self._temp_dirs)
        for path in dirs:
            # One dir that cannot be removed (an app may leave an inaccessible
            # subdirectory) must not stop the removal of the others.
            with contextlib.suppress(OSError):
                _remove_tree(path)


process_registry = OwnedProcessRegistry()


class Session:
    """An isolated KWin Wayland session.

    Uses dbus-run-session to create an isolated D-Bus session bus,
    then starts kwin_wayland --virtual inside it. Apps launched in
    this session are completely isolated from the host desktop.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._info: SessionInfo | None = None
        self._socket_name: str = ""
        self._app_counter: int = 0
        self._config: SessionConfig | None = None
        self._home_dir: Path | None = None
        # The session's stderr is redirected to this file instead of a pipe so
        # the compositor can never block on a full stderr buffer. Closed in
        # stop() after the diagnostics it holds were consumed.
        self._stderr_file: IO[bytes] | None = None
        self._session_config_dir: Path | None = None

    @property
    def is_running(self) -> bool:
        if self._process is None:
            return False
        # Poll through the registry so a finished wrapper is reaped and
        # unregistered here, not left as a stale entry for a later stop or
        # session replacement (core.py replaces a dead session without stop).
        return process_registry.poll(self._process) is None

    @property
    def info(self) -> SessionInfo | None:
        return self._info

    @property
    def wayland_socket(self) -> str:
        return self._socket_name

    def _xdg_isolation_env(self) -> dict[str, str]:
        """Build XDG environment overrides for home directory isolation."""
        if self._home_dir is None:
            return {}
        home = str(self._home_dir)
        return {
            "HOME": home,
            "XDG_CONFIG_HOME": str(self._home_dir / ".config"),
            "XDG_DATA_HOME": str(self._home_dir / ".local" / "share"),
            "XDG_CACHE_HOME": str(self._home_dir / ".cache"),
            "XDG_STATE_HOME": str(self._home_dir / ".local" / "state"),
        }

    def start(
        self, config: SessionConfig | None = None, *, progress_total: int | None = None
    ) -> SessionInfo:
        """Start an isolated KWin Wayland session.

        Returns SessionInfo with connection details. Startup is reported as
        progress steps 1-3 (KWin started, KWin ready, Wayland socket ready) out of
        ``progress_total``, so a caller can append its own steps after them.
        """
        if self.is_running:
            msg = "Session is already running"
            raise RuntimeError(msg)

        if config is None:
            config = SessionConfig()
        self._config = config

        self._socket_name = config.socket_name or f"wayland-mcp-{os.getpid()}-{int(time.time())}"

        # Create isolated home directory if requested
        if config.isolate_home:
            self._home_dir = Path(tempfile.mkdtemp(prefix="kwin-mcp-home-"))
            for subdir in (
                ".config",
                Path(".local") / "share",
                Path(".local") / "state",
                ".cache",
                ".screenshots",
            ):
                (self._home_dir / subdir).mkdir(parents=True, exist_ok=True)
            # Record the removable path(s) session_stop would remove, so the exit
            # path (when it cannot run session_stop) mirrors session_stop's
            # retention semantics: keep_home=False removes the whole home;
            # keep_home=True with keep_screenshots=False removes only
            # home/.screenshots. A keep_home + keep_screenshots home is the
            # caller's to retain, so nothing is recorded.
            if not config.keep_home:
                process_registry.register_temp_dir(self._home_dir)
            elif not config.keep_screenshots:
                process_registry.register_temp_dir(self._home_dir / ".screenshots")

        # Clean up any stale socket files
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        for suffix in ("", ".lock"):
            path = Path(runtime_dir) / f"{self._socket_name}{suffix}"
            path.unlink(missing_ok=True)

        # The compositor must not read the host's config dir: a wallet-enabled
        # kwalletrc there lets the kwallet popup steal focus from the app under
        # automation. An isolated home already gives it a private .config; seed
        # that one, otherwise create a throwaway dir that _build_env points
        # XDG_CONFIG_HOME at. The keymap is pinned separately in _build_env.
        if self._home_dir is not None:
            _write_deterministic_session_config(self._home_dir / ".config")
        else:
            self._session_config_dir = Path(tempfile.mkdtemp(prefix="kwin-mcp-config-"))
            _write_deterministic_session_config(self._session_config_dir)
            process_registry.register_temp_dir(self._session_config_dir)
        # Deliberately NOT isolating XDG_DATA_HOME / XDG_CACHE_HOME: qtbase
        # crash-logs a fatal qFatal when a nonexistent standard data dir is
        # set (reproduced on qt6-base 6.10), and existing dirs already
        # contain everything kwin/konsole need to start.

        # Build the wrapper script that runs inside dbus-run-session
        wrapper_script = self._build_wrapper_script(config)

        # Start the isolated session in its own process group. stderr goes to a
        # file, not a pipe: a pipe would let a descendant block forever once
        # the parent stops draining it, and it re-opens the "process wedged on
        # a full stderr buffer" failure this call is supposed to survive. The
        # file is also the diagnostic sink read when startup fails.
        self._stderr_file = tempfile.TemporaryFile()  # noqa: SIM115
        try:
            # stdin is the MCP server's JSON-RPC transport; no descendant of
            # the session (compositor, AT-SPI bus, apps) may read from it.
            # Spawn through the registry so the wrapper's group is registered
            # atomically with the spawn (close() cannot slip in between).
            self._process = process_registry.spawn(
                ["dbus-run-session", "bash", "-c", wrapper_script],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=self._stderr_file,
                env=self._build_env(config),
                start_new_session=True,
            )
        except BaseException:
            self._stderr_file.close()
            self._stderr_file = None
            # _process is still None, so stop() would return early; undo the
            # resource acquisition this method already performed.
            self._cleanup_start_dirs()
            raise
        progress.report(1, progress_total, "Starting KWin")

        # Read startup output from the wrapper script.
        # Expected lines: DBUS_SESSION_BUS_ADDRESS=..., READY or FAILED.
        # Any other lines (e.g. from D-Bus activation) are kept as diagnostics;
        # "WARN: " lines additionally reach the caller of a successful start.
        dbus_address, got_ready, stdout_tail = self._read_startup_output(progress_total)
        if got_ready:
            progress.report(2, progress_total, "KWin ready")

        # Wait for kwin to be ready (socket file appears). Without READY the
        # start has already failed, so only probe the socket to pick the error
        # reason instead of spending another full wait on a dead handshake.
        socket_path = Path(runtime_dir) / self._socket_name
        if got_ready:
            socket_ready = self._wait_for_socket(socket_path, timeout=10.0)
        else:
            socket_ready = socket_path.exists()
        reason = None
        if not socket_ready:
            reason = "KWin failed to start"
        elif not got_ready:
            reason = "Session setup failed: did not receive READY signal"
        if reason is not None:
            try:
                # The session is failed but may still hold children; terminate
                # the whole group before reading diagnostics or giving up.
                self._signal_process_group(signal.SIGTERM)
                try:
                    process_registry.wait(self._process, timeout=5)
                except subprocess.TimeoutExpired:
                    self._signal_process_group(signal.SIGKILL)
                    try:
                        process_registry.wait(self._process, timeout=5)
                    except subprocess.TimeoutExpired as exc:
                        stderr = self._read_stderr_log()
                        detail = self._format_startup_diagnostics(stderr, stdout_tail)
                        msg = (
                            f"{reason}; session teardown timed out"
                            f"{detail}. Causal exception: {exc!r}"
                        )
                        raise RuntimeError(msg) from exc
                stderr = self._read_stderr_log()
            finally:
                # Runs even when the re-raise above fires or wait() throws:
                # group teardown, fd closure, and directory cleanup must not be
                # skipped just because diagnostics collection failed — and a
                # cleanup hiccup must not mask the real startup error.
                with contextlib.suppress(Exception):
                    self.stop()
            detail = self._format_startup_diagnostics(stderr, stdout_tail)
            msg = f"{reason}.{detail}"
            raise RuntimeError(msg)

        progress.report(3, progress_total, "Wayland socket ready")
        if self._home_dir is not None:
            screenshot_dir = self._home_dir / ".screenshots"
        else:
            screenshot_dir = Path(tempfile.mkdtemp(prefix="kwin-mcp-screenshots-"))
            # A keep_screenshots session is the caller's to retain, so it is not
            # recorded for the exit path.
            if not config.keep_screenshots:
                process_registry.register_temp_dir(screenshot_dir)

        self._info = SessionInfo(
            dbus_address=dbus_address,
            wayland_socket=self._socket_name,
            kwin_pid=self._process.pid,
            screenshot_dir=screenshot_dir,
            home_dir=self._home_dir,
            startup_warnings=[
                line.removeprefix("WARN: ")
                for line in stdout_tail.splitlines()
                if line.startswith("WARN: ")
            ],
        )
        return self._info

    def launch_app(self, command: list[str], extra_env: dict[str, str] | None = None) -> AppInfo:
        """Launch an application inside the isolated session.

        Returns AppInfo with pid, command, and log_path.
        """
        if not self.is_running or self._info is None:
            msg = "Session is not running"
            raise RuntimeError(msg)

        env = {
            **os.environ,
            "WAYLAND_DISPLAY": self._socket_name,
            "QT_QPA_PLATFORM": "wayland",
            "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
            "QT_ACCESSIBILITY": "1",
        }
        env.update(self._xdg_isolation_env())
        # Never leak the host DISPLAY into the isolated session: X11 apps would
        # silently open on the user's real desktop instead of failing. The
        # virtual compositor starts no Xwayland, so there is no session-local
        # value to substitute. An explicit extra_env DISPLAY still applies.
        env.pop("DISPLAY", None)
        if extra_env:
            env.update(extra_env)
        if self._info.dbus_address:
            env["DBUS_SESSION_BUS_ADDRESS"] = self._info.dbus_address

        # Create log file for stdout/stderr capture
        app_name = Path(command[0]).stem if command else "unknown"
        self._app_counter += 1
        log_path = self._info.screenshot_dir / f"app_{app_name}_{self._app_counter}.log"
        log_file = log_path.open("ab")

        # stdin=DEVNULL: an inherited stdin would be the MCP server's JSON-RPC
        # transport, and a launched program that reads it steals requests.
        # start_new_session=True puts the app and its descendants in their own
        # process group so session_stop can stop the whole tree, not just the
        # direct child (a shell script's background jobs, a browser's helpers).
        # Spawn through the registry so the app's group is recorded atomically
        # with the spawn.
        proc = process_registry.spawn(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
        # Close the fd in the parent; child has inherited it
        log_file.close()

        app_info = AppInfo(
            pid=proc.pid,
            command=" ".join(command),
            log_path=log_path,
            process=proc,
        )
        self._info.app_pid = proc.pid
        self._info.apps[proc.pid] = app_info
        return app_info

    def read_app_log(self, pid: int, last_n_lines: int = 50) -> str:
        """Read the log output of a launched app.

        Args:
            pid: PID of the app (from launch_app).
            last_n_lines: Number of trailing lines to return (0 = all).

        Returns:
            The app's stdout/stderr output.
        """
        if self._info is None:
            msg = "Session is not running"
            raise RuntimeError(msg)

        app = self._info.apps.get(pid)
        if app is None:
            available = list(self._info.apps.keys())
            msg = f"No app with PID {pid}. Available PIDs: {available}"
            raise ValueError(msg)

        if not app.log_path.exists():
            return "(no log output yet)"

        text = app.log_path.read_text(errors="replace")
        if last_n_lines > 0:
            lines = text.splitlines()
            text = "\n".join(lines[-last_n_lines:])
        return text or "(no log output yet)"

    def _terminate_apps(self) -> None:
        """Stop every launched app with its whole process group and reap the leaders."""
        if self._info is None:
            return
        _terminate_app_groups(list(self._info.apps.values()))

    def stop(self) -> None:
        """Stop the isolated session and clean up all processes."""
        if self._process is None:
            return

        # Apps started by launch_app are children of this process, not of the
        # session's process group, so the signal below never reaches them. A
        # surviving app keeps writing into the isolated home and defeats its
        # removal, which shows up as a leaked directory after session_stop.
        self._terminate_apps()

        # Send SIGTERM to the entire process group (all children). This still
        # reaches descendants when the leader was already reaped, because the
        # group id is the leader's pid and stays allocated while any group
        # member lives.
        self._signal_process_group(signal.SIGTERM)

        # Reap through the registry so the wrapper is unregistered as it exits.
        try:
            process_registry.wait(self._process, timeout=5)
        except subprocess.TimeoutExpired:
            process_registry.kill(self._process)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process_registry.wait(self._process, timeout=3)

        # A returned leader wait only proves the leader exited. Descendants
        # that ignored SIGTERM (or briefly outlive their parent) keep the
        # session group alive, so check the group itself and escalate.
        if self._group_alive():
            self._signal_process_group(signal.SIGKILL)
            self._wait_for_group_exit(timeout=5)

        # The parent holds two handles created for the child: the stdout pipe
        # and the stderr file. Close them so a failed start cannot leak fds.
        if self._process.stdout is not None:
            self._process.stdout.close()
        if self._stderr_file is not None:
            self._stderr_file.close()
            self._stderr_file = None

        # Clean up home directory and/or screenshot directory
        if self._home_dir is not None:
            keep_home = self._config is not None and self._config.keep_home
            keep_screenshots = self._config is not None and self._config.keep_screenshots
            if not keep_home:
                _remove_tree(self._home_dir)
            elif not keep_screenshots:
                # Keep home but remove screenshots subdirectory
                screenshots = self._home_dir / ".screenshots"
                if screenshots.exists():
                    shutil.rmtree(screenshots, ignore_errors=True)
        else:
            # No isolated home — use original screenshot cleanup logic
            keep = self._config is not None and self._config.keep_screenshots
            if not keep and self._info and self._info.screenshot_dir.exists():
                shutil.rmtree(self._info.screenshot_dir, ignore_errors=True)

        # Clean up socket files
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        for suffix in ("", ".lock"):
            path = Path(runtime_dir) / f"{self._socket_name}{suffix}"
            path.unlink(missing_ok=True)

        # Remove the per-session config dir
        if self._session_config_dir is not None:
            shutil.rmtree(self._session_config_dir, ignore_errors=True)
            process_registry.unregister_temp_dir(self._session_config_dir)
            self._session_config_dir = None

        # Unregister the temp dirs this stop removed, so the registry stays
        # accurate across start/stop cycles and the server's exit path never
        # re-removes a dir the caller asked to keep. The owned process groups
        # (the wrapper and the launched apps) are unregistered as they are
        # reaped above, so they need no explicit drop here.
        config = self._config
        keep_home = config is not None and config.keep_home
        keep_screenshots = config is not None and config.keep_screenshots
        if (
            self._info is not None
            and not keep_screenshots
            and self._home_dir is None
            and self._info.screenshot_dir is not None
        ):
            process_registry.unregister_temp_dir(self._info.screenshot_dir)
        if self._home_dir is not None:
            if not keep_home:
                process_registry.unregister_temp_dir(self._home_dir)
            elif not keep_screenshots:
                process_registry.unregister_temp_dir(self._home_dir / ".screenshots")

        self._process = None
        self._info = None
        self._home_dir = None

    def _cleanup_start_dirs(self) -> None:
        """Release the directories created by start() when no session exists.

        Only used on the Popen-failure path, where _process is None and stop()
        cannot run; keep_home/keep_screenshots semantics mirror stop().
        """
        if self._session_config_dir is not None:
            shutil.rmtree(self._session_config_dir, ignore_errors=True)
            process_registry.unregister_temp_dir(self._session_config_dir)
            self._session_config_dir = None
        if self._home_dir is None:
            return
        keep_home = self._config is not None and self._config.keep_home
        keep_screenshots = self._config is not None and self._config.keep_screenshots
        with contextlib.suppress(OSError):
            if not keep_home:
                _remove_tree(self._home_dir)
                process_registry.unregister_temp_dir(self._home_dir)
            elif not keep_screenshots:
                shutil.rmtree(self._home_dir / ".screenshots", ignore_errors=True)
                process_registry.unregister_temp_dir(self._home_dir / ".screenshots")
        self._home_dir = None

    def _signal_process_group(self, sig: int) -> None:
        """Signal the whole session process group, ignoring races."""
        if self._process is None:
            return
        _signal_process_group(self._process.pid, sig)

    def _group_alive(self) -> bool:
        """Return True while any live (non-zombie) member of the session group exists."""
        if self._process is None:
            return False
        return _group_alive(self._process.pid)

    def _wait_for_group_exit(self, timeout: float) -> None:
        """Block until the session process group is empty or timeout elapses."""
        if self._process is None:
            return
        _wait_for_group_exit(self._process.pid, timeout)

    def _read_startup_output(self, progress_total: int | None = None) -> tuple[str, bool, str]:
        """Read the wrapper's stdout handshake with a hard deadline.

        Returns (dbus_address, got_ready, tail) where tail contains a decoded
        prefix of any unrecognized stdout output (a partial line counts). A
        blocking readline() cannot be used: a reaped or killed leader with
        surviving descendants keeps the pipe open without writing a newline,
        which once wedged start() forever. Reads are chunk-based on a
        non-blocking fd so a newline-free write also terminates the loop.

        While waiting, reports fractional progress between steps 1 and 2 of
        ``progress_total`` as a heartbeat.
        """
        process = self._process
        if process is None or process.stdout is None:
            return "", False, ""
        fd = process.stdout.fileno()
        os.set_blocking(fd, False)
        dbus_address = ""
        got_ready = False
        pending = bytearray()
        tail = bytearray()
        started = time.monotonic()
        deadline = started + _STARTUP_READ_TIMEOUT

        def remember(text: str) -> None:
            # Keep only a bounded excerpt; stdout is diagnostics, not a stream.
            tail.extend((text + "\n").encode(errors="replace")[: 4096 - min(len(tail), 4096)])

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            readable, _, _ = select.select([fd], [], [], min(remaining, 0.5))
            elapsed = time.monotonic() - started
            progress.report(
                1 + min(elapsed / _STARTUP_READ_TIMEOUT, 0.99),
                progress_total,
                f"Waiting for KWin ({elapsed:.0f}s)",
            )
            if not readable:
                # No complete line can arrive any more once the leader is gone;
                # whatever bytes remain pending are a partial line. Breaking
                # here is what keeps a SIGKILLed wrapper with pipe-holding
                # descendants bounded.
                if process_registry.poll(process) is not None:
                    break
                continue
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                break
            pending.extend(chunk)
            while True:
                line, sep, rest = pending.partition(b"\n")
                if not sep:
                    # No newline yet: everything stays buffered in pending.
                    break
                pending = rest
                text = line.decode(errors="replace").strip()
                if text.startswith("DBUS_SESSION_BUS_ADDRESS="):
                    dbus_address = text.split("=", 1)[1]
                elif text == "READY":
                    got_ready = True
                elif text:
                    # FAILED, warnings, and stray compositor output all become
                    # diagnostics; FAILED additionally ends the handshake.
                    remember(text)
                    if text == "FAILED":
                        return dbus_address, False, tail.decode(errors="replace").strip()
            if got_ready:
                break

        if pending:
            remember(pending.decode(errors="replace"))
        return dbus_address, got_ready, tail.decode(errors="replace").strip()

    def _read_stderr_log(self) -> str:
        """Return what the session wrote to its file-backed stderr."""
        if self._stderr_file is None:
            return ""
        self._stderr_file.flush()
        self._stderr_file.seek(0)
        return self._stderr_file.read().decode(errors="replace").strip()

    @staticmethod
    def _format_startup_diagnostics(stderr: str, stdout_tail: str) -> str:
        """Combine captured stderr and stray stdout into an error suffix."""
        parts = []
        if stderr:
            parts.append(f"stderr: {stderr}")
        if stdout_tail:
            parts.append(f"stdout: {stdout_tail}")
        return f" {'; '.join(parts)}" if parts else ""

    def _build_wrapper_script(self, config: SessionConfig) -> str:
        """Build the bash script that runs inside dbus-run-session."""
        return f"""\
echo "DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS"

# Ensure all child processes are cleaned up on exit.
# The AT-SPI bus launcher and registryd are started via D-Bus
# auto-activation below; they are terminated automatically when our
# isolated session bus exits (dbus-run-session tears the bus down on
# parent exit), so we only need to track KWin explicitly here.
cleanup() {{
    kill $KWIN_PID 2>/dev/null
    wait $KWIN_PID 2>/dev/null
}}
trap cleanup EXIT TERM INT HUP

# Bring up the AT-SPI accessibility bus via synchronous D-Bus activation.
# Calling org.a11y.Bus.GetAddress makes dbus-daemon resolve the distro-provided
# service file (/usr/share/dbus-1/services/org.a11y.Bus.service) and exec the
# launcher at whatever path the current distro uses — /usr/libexec on
# Fedora/Debian/Ubuntu, /usr/libexec/at-spi2 on openSUSE, /app/libexec on
# Flatpak, and so on. A hardcoded candidate list silently misses every layout
# it does not name, and a backgrounded launcher hides the exec failure.
# dbus-send ships in the same package as the dbus-update-activation-environment
# the wrapper already requires, so no extra dependency is introduced.
# ATSPI_DBUS_IMPLEMENTATION=dbus-daemon (set in _build_env) prevents
# dbus-broker from sharing the host's a11y bus.
# The registry daemon comes up on its own when apps first touch the a11y
# bus, so no manual bootstrap is needed here. Activation failure is reported
# to the caller instead of being hidden: without the bus the first AT-SPI2
# query itself activates the launcher and always returns an empty result.
if ! ATSPI_ERR=$(dbus-send --session --print-reply --reply-timeout=10000 \\
    --dest=org.a11y.Bus /org/a11y/bus \\
    org.a11y.Bus.GetAddress 2>&1 >/dev/null); then
    # Keep the warning on one line: multi-line stderr would read as several
    # unrelated diagnostics in the startup handshake.
    ATSPI_ERR=${{ATSPI_ERR%%$'\\n'*}}
    echo "WARN: AT-SPI bus activation failed, accessibility tools may be unavailable" \\
        "in this session: ${{ATSPI_ERR:-unknown error}}"
fi

# Pre-set D-Bus activation environment BEFORE starting KWin.
# When KWin triggers portal auto-activation, portal-kde will get
# WAYLAND_DISPLAY pointing to our isolated compositor socket.
# The socket doesn't exist yet, but portal-kde will be activated
# only after KWin creates it.
dbus-update-activation-environment WAYLAND_DISPLAY={self._socket_name} QT_QPA_PLATFORM=wayland

# Start KWin WITHOUT WAYLAND_DISPLAY to prevent nesting attempt.
# KWin with --virtual creates its own compositor, it must not try
# to connect to another compositor as a client.
# KDE_FULL_SESSION / KDE_SESSION_VERSION are also stripped: they make KWin take
# the full Plasma session startup path (ksmserver, kded, plasma-workspace),
# which is absent in minimal environments such as CI containers and makes the
# compositor crash on startup. Apps launched into the session still see them.
# Explicitly pass KWIN_ permission env vars to ensure they reach the
# KWin process (environment inheritance through dbus-run-session can be unreliable).
env -u WAYLAND_DISPLAY -u QT_QPA_PLATFORM -u KDE_FULL_SESSION -u KDE_SESSION_VERSION \
    KWIN_WAYLAND_NO_PERMISSION_CHECKS=1 \
    KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1 \
    kwin_wayland --virtual --no-lockscreen \
    --width {config.screen_width} --height {config.screen_height} \
    --socket {self._socket_name} &
KWIN_PID=$!

# Wait for the KWin socket to appear, but never block forever: give up as soon
# as KWin dies, or after 30s. Exiting here lets the parent report the failure
# (including KWin's stderr) instead of hanging on the READY handshake.
for _ in $(seq 1 300); do
    [ -e "$XDG_RUNTIME_DIR/{self._socket_name}" ] && break
    kill -0 $KWIN_PID 2>/dev/null || break
    sleep 0.1
done
if [ ! -e "$XDG_RUNTIME_DIR/{self._socket_name}" ]; then
    echo "FAILED"
    echo "kwin_wayland exited before creating socket {self._socket_name}" >&2
    exit 1
fi


# The socket appears before KWin owns org.kde.KWin (registered when the
# workspace is built, followed by the EIS plugin). Every D-Bus consumer of
# the session needs that name, so the wrapper waits for it here; READY only
# means callers may talk to KWin immediately. The wait never hangs: stop as
# soon as KWin dies, and give up after {_KWIN_BUS_NAME_TIMEOUT} s so a hung
# bus name claim still reaches FAILED before the parent's handshake
# deadline. dbus-send --reply-timeout bounds each probe reply; a bus that
# stalls authentication leaves the parent deadline as the outer bound.
KWIN_NAME_DEADLINE=$((SECONDS + {int(_KWIN_BUS_NAME_TIMEOUT)}))
while true; do
    kill -0 $KWIN_PID 2>/dev/null || break
    if dbus-send --session --print-reply=literal --reply-timeout=500 \\
        --dest=org.freedesktop.DBus /org/freedesktop/DBus \\
        org.freedesktop.DBus.NameHasOwner string:{_KWIN_BUS_NAME} 2>/dev/null \\
        | grep -q true; then
        break
    fi
    [ "$SECONDS" -lt "$KWIN_NAME_DEADLINE" ] || break
    sleep 0.1
done
if ! dbus-send --session --print-reply=literal --reply-timeout=500 \\
    --dest=org.freedesktop.DBus /org/freedesktop/DBus \\
    org.freedesktop.DBus.NameHasOwner string:{_KWIN_BUS_NAME} 2>/dev/null \\
    | grep -q true; then
    echo "FAILED"
    if ! kill -0 $KWIN_PID 2>/dev/null; then
        echo "kwin_wayland exited before registering {_KWIN_BUS_NAME} on the session bus" >&2
    else
        echo "kwin_wayland did not register {_KWIN_BUS_NAME} on the session bus" \\
            "within {int(_KWIN_BUS_NAME_TIMEOUT)}s of creating its socket" >&2
    fi
    exit 1
fi

# Signal parent that KWin owns its bus name and the session is usable.
echo "READY"

# Block until kwin exits
wait $KWIN_PID
"""

    def _build_env(self, config: SessionConfig) -> dict[str, str]:
        """Build the environment for the isolated session."""
        env = {
            **os.environ,
            "KDE_FULL_SESSION": "true",
            "KDE_SESSION_VERSION": "6",
            "XDG_SESSION_TYPE": "wayland",
            "XDG_CURRENT_DESKTOP": "KDE",
            "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
            "QT_ACCESSIBILITY": "1",
            # Force dbus-daemon for the AT-SPI bus instead of dbus-broker.
            # dbus-broker with --scope=user reuses the host's existing AT-SPI bus,
            # breaking accessibility isolation. Verified as REQUIRED.
            "ATSPI_DBUS_IMPLEMENTATION": "dbus-daemon",
            # Allow direct D-Bus screenshot capture without portal authorization.
            # Safe in isolated virtual sessions where there is no user desktop to protect.
            "KWIN_SCREENSHOT_NO_PERMISSION_CHECKS": "1",
            # Allow clients to bind restricted Wayland protocols (e.g. plasma_window_management).
            # Safe in isolated virtual sessions where there is no user desktop to protect.
            "KWIN_WAYLAND_NO_PERMISSION_CHECKS": "1",
        }
        # Remove host display references to avoid kwin connecting to host
        env.pop("WAYLAND_DISPLAY", None)
        env.pop("DISPLAY", None)

        env.update(self._xdg_isolation_env())
        # Without an isolated home, point the compositor at the per-session
        # config dir from start() instead of the host's (see there).
        if self._session_config_dir is not None:
            env["XDG_CONFIG_HOME"] = str(self._session_config_dir)

        # Pin the compositor keymap to plain US. The EIS keyboard sends evdev
        # keycodes computed from a US QWERTY table, and KWin translates them
        # through its own keymap, so a host layout such as ru,us would type
        # "руддщ" for "hello". KWIN_XKB_DEFAULT_KEYMAP makes Xkb::reconfigure()
        # skip both kxkbrc and locale1 and build the keymap from XKB_DEFAULT_*
        # alone (KWin 6.3 xkb.cpp, loadDefaultKeymap/applyEnvironmentRules),
        # so every XKB_DEFAULT_* the host may set is replaced here: an unset
        # rules/model/variant/options falls back to libxkbcommon's defaults.
        env["KWIN_XKB_DEFAULT_KEYMAP"] = "1"
        env["XKB_DEFAULT_LAYOUT"] = "us"
        for var in (
            "XKB_DEFAULT_VARIANT",
            "XKB_DEFAULT_OPTIONS",
            "XKB_DEFAULT_RULES",
            "XKB_DEFAULT_MODEL",
        ):
            env.pop(var, None)

        # Last, so SessionConfig.extra_env can replace values such as
        # XKB_DEFAULT_LAYOUT. It cannot unset KWIN_XKB_DEFAULT_KEYMAP (KWin only
        # checks that it is set), so kxkbrc and locale1 stay out of the keymap.
        env.update(config.extra_env)
        return env

    def _wait_for_socket(self, socket_path: Path, timeout: float) -> bool:
        """Wait for the Wayland socket file to appear."""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if socket_path.exists():
                return True
            # Check if process died
            if self._process is not None and process_registry.poll(self._process) is not None:
                return False
            time.sleep(0.2)
        return False

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


class LiveSession:
    """Connection to an existing (non-virtual) KWin session.

    Attaches to a KWin compositor that is already running, such as
    the user's real desktop or a KWin instance inside a container.
    Does NOT manage the compositor lifecycle — stop() only disconnects.
    """

    def __init__(
        self,
        dbus_address: str,
        wayland_socket: str,
        screenshot_dir: Path,
    ) -> None:
        self._info = SessionInfo(
            dbus_address=dbus_address,
            wayland_socket=wayland_socket,
            kwin_pid=0,
            screenshot_dir=screenshot_dir,
            session_type=SessionType.LIVE,
        )
        self._running = True
        self._app_counter: int = 0
        self._keep_screenshots: bool = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def info(self) -> SessionInfo | None:
        return self._info if self._running else None

    @property
    def wayland_socket(self) -> str:
        return self._info.wayland_socket

    def launch_app(self, command: list[str], extra_env: dict[str, str] | None = None) -> AppInfo:
        """Launch an application in the live session.

        Returns AppInfo with pid, command, and log_path.
        """
        if not self._running:
            msg = "Session is not running"
            raise RuntimeError(msg)

        env = {
            **os.environ,
            "WAYLAND_DISPLAY": self._info.wayland_socket,
            "QT_QPA_PLATFORM": "wayland",
            "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
            "QT_ACCESSIBILITY": "1",
        }
        if self._info.dbus_address:
            env["DBUS_SESSION_BUS_ADDRESS"] = self._info.dbus_address
        if extra_env:
            env.update(extra_env)

        app_name = Path(command[0]).stem if command else "unknown"
        self._app_counter += 1
        log_path = self._info.screenshot_dir / f"app_{app_name}_{self._app_counter}.log"
        log_file = log_path.open("ab")

        # stdin=DEVNULL: see Session.launch_app. start_new_session=True gives
        # the app its own process group so stop() can signal the whole tree.
        # Spawn through the registry so the app's group is recorded atomically
        # with the spawn (the live session's KWin itself is never recorded).
        proc = process_registry.spawn(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
        log_file.close()

        app_info = AppInfo(
            pid=proc.pid,
            command=" ".join(command),
            log_path=log_path,
            process=proc,
        )
        self._info.app_pid = proc.pid
        self._info.apps[proc.pid] = app_info
        return app_info

    def read_app_log(self, pid: int, last_n_lines: int = 50) -> str:
        """Read the log output of a launched app."""
        app = self._info.apps.get(pid)
        if app is None:
            available = list(self._info.apps.keys())
            msg = f"No app with PID {pid}. Available PIDs: {available}"
            raise ValueError(msg)

        if not app.log_path.exists():
            return "(no log output yet)"

        text = app.log_path.read_text(errors="replace")
        if last_n_lines > 0:
            lines = text.splitlines()
            text = "\n".join(lines[-last_n_lines:])
        return text or "(no log output yet)"

    def stop(self, *, keep_screenshots: bool = False) -> None:
        """Disconnect from the live session.

        Only cleans up screenshot directory. Does NOT kill KWin or any apps
        that were already running before the connection.
        """
        if not self._running:
            return
        self._running = False

        # Stop every app we launched, with its whole process group, escalating
        # SIGTERM to SIGKILL. KWin and any apps that predate the connection are
        # never signalled. The launched app leaders are unregistered as they are
        # reaped by _terminate_app_groups, so no explicit drop is needed here.
        _terminate_app_groups(list(self._info.apps.values()))

        # Remove the live session's screenshot dir (registered at connect when
        # keep_screenshots is False), mirroring the registry's retention
        # semantics. KWin of the live session was never recorded, so it is
        # never touched here.
        if not keep_screenshots:
            shutil.rmtree(self._info.screenshot_dir, ignore_errors=True)
            process_registry.unregister_temp_dir(self._info.screenshot_dir)
