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
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import IO

# Upper bound for the startup handshake. The wrapper itself is bounded: the
# AT-SPI bus activation call gives up after 10 s (--reply-timeout) and the
# socket wait after ~30 s, about 40 s in the worst case. This bound only covers
# cases the wrapper cannot report: a leader killed while descendants keep
# stdout open, or a partial line that never terminates. 60 s leaves the wrapper
# room to report first so its FAILED diagnostics reach the caller.
_STARTUP_READ_TIMEOUT = 60.0


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
    """Pre-seed an isolated config dir with settings that make sessions deterministic.

    KWin builds its XKB keymap from ``$XDG_CONFIG_HOME/kxkbrc`` and falls back
    to the environment (``XKB_DEFAULT_LAYOUT``), which on hosts configured with
    a non-US primary layout (e.g. ``ru,us``) makes evdev keycodes typed by the
    EIS keyboard produce the wrong characters (``hello`` → ``руддщ``).
    Leaving ``kxkbrc`` absent from the isolated config dir keeps the default
    US layout.

    The same directory silences the kwallet popup (ksecretd/kwalletd) that
    steals compositor focus at session start, which otherwise breaks
    focus-dependent automation flows such as ``focus_window`` + keyboard
    verification.

    Only writes files that do not exist yet so explicit pre-seeding wins.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    kwalletrc = config_dir / "kwalletrc"
    if not kwalletrc.exists():
        kwalletrc.write_text("[Wallet]\nEnabled=false\nFirst Use=false\nLaunch Manager=false\n")


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
        return self._process.poll() is None

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

    def start(self, config: SessionConfig | None = None) -> SessionInfo:
        """Start an isolated KWin Wayland session.

        Returns SessionInfo with connection details.
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

        # Clean up any stale socket files
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        for suffix in ("", ".lock"):
            path = Path(runtime_dir) / f"{self._socket_name}{suffix}"
            path.unlink(missing_ok=True)

        # Deterministic per-session config dir: without an isolated
        # XDG_CONFIG_HOME, KWin inherits the host's kxkbrc and the virtual
        # session runs with the host's XKB layout list (e.g. ru,us — the
        # evdev keycodes then type Cyrillic instead of ASCII), and the
        # kwallet popup steals focus at session start.
        self._session_config_dir = Path(tempfile.mkdtemp(prefix="kwin-mcp-config-"))
        _write_deterministic_session_config(self._session_config_dir)
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
            self._process = subprocess.Popen(
                ["dbus-run-session", "bash", "-c", wrapper_script],
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
            self._cleanup_isolated_home()
            raise

        # Read startup output from the wrapper script.
        # Expected lines: DBUS_SESSION_BUS_ADDRESS=..., READY or FAILED.
        # Any other lines (e.g. from D-Bus activation) are kept as diagnostics;
        # "WARN: " lines additionally reach the caller of a successful start.
        dbus_address, got_ready, stdout_tail = self._read_startup_output()

        # Wait for kwin to be ready (socket file appears). Without READY the
        # start has already failed, so only probe the socket to pick the error
        # reason instead of spending another full wait on a dead handshake.
        socket_path = Path(runtime_dir) / self._socket_name
        if got_ready:
            socket_ready = self._wait_for_socket(socket_path, timeout=10.0)
        else:
            socket_ready = socket_path.exists()
        if not socket_ready or not got_ready:
            reason = (
                "KWin failed to start"
                if not socket_ready
                else "Session setup failed: did not receive READY signal"
            )
            try:
                # The session is failed but may still hold children; terminate
                # the whole group before reading diagnostics or giving up.
                self._signal_process_group(signal.SIGTERM)
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._signal_process_group(signal.SIGKILL)
                    try:
                        self._process.wait(timeout=5)
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

        if self._home_dir is not None:
            screenshot_dir = self._home_dir / ".screenshots"
        else:
            screenshot_dir = Path(tempfile.mkdtemp(prefix="kwin-mcp-screenshots-"))

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
        if extra_env:
            env.update(extra_env)
        if self._info.dbus_address:
            env["DBUS_SESSION_BUS_ADDRESS"] = self._info.dbus_address

        # Create log file for stdout/stderr capture
        app_name = Path(command[0]).stem if command else "unknown"
        self._app_counter += 1
        log_path = self._info.screenshot_dir / f"app_{app_name}_{self._app_counter}.log"
        log_file = log_path.open("ab")

        proc = subprocess.Popen(
            command,
            env=env,
            stdout=log_file,
            stderr=log_file,
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
        """Stop applications started through launch_app and reap them."""
        if self._info is None:
            return
        for app in list(self._info.apps.values()):
            if app.process.poll() is not None:
                continue
            with contextlib.suppress(ProcessLookupError):
                app.process.terminate()
        for app in list(self._info.apps.values()):
            try:
                app.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    app.process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    app.process.wait(timeout=2)

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

        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._process.wait(timeout=3)

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
            self._session_config_dir = None

        self._process = None
        self._info = None
        self._home_dir = None

    def _cleanup_isolated_home(self) -> None:
        """Release the isolated home created by start() when no session exists.

        Only used on the Popen-failure path, where _process is None and stop()
        cannot run; keep_home/keep_screenshots semantics mirror stop().
        """
        if self._home_dir is None:
            return
        keep_home = self._config is not None and self._config.keep_home
        keep_screenshots = self._config is not None and self._config.keep_screenshots
        with contextlib.suppress(OSError):
            if not keep_home:
                _remove_tree(self._home_dir)
            elif not keep_screenshots:
                shutil.rmtree(self._home_dir / ".screenshots", ignore_errors=True)
        self._home_dir = None

    def _signal_process_group(self, sig: int) -> None:
        """Signal the whole session process group, ignoring races.

        start_new_session=True makes the leader's pid the process group id, so
        the pid doubles as the pgid and stays valid even after the leader was
        reaped — os.getpgid(pid) would instead fail with ESRCH on a reaped
        leader and silently skip the signal. This is bounded by the owned
        lifecycle only: nothing prevents the kernel from recycling the pid
        once every group member is gone, so callers must signal promptly.
        """
        if self._process is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._process.pid, sig)

    def _group_alive(self) -> bool:
        """Return True while any live (non-zombie) member of the session group exists.

        killpg(pgid, 0) also succeeds for unreaped zombies, which persist when
        orphaned members are reparented to a PID 1 that never reaps (e.g. a
        container whose entrypoint execs pytest). Treating those as alive would
        make every teardown wait out its full timeout, so on Linux the group's
        members are confirmed through /proc/<pid>/stat and zombies are ignored.
        """
        if self._process is None:
            return False
        pgid = self._process.pid
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

    def _wait_for_group_exit(self, timeout: float) -> None:
        """Block until the session process group is empty or timeout elapses."""
        deadline = time.monotonic() + timeout
        while self._group_alive() and time.monotonic() < deadline:
            time.sleep(0.05)

    def _read_startup_output(self) -> tuple[str, bool, str]:
        """Read the wrapper's stdout handshake with a hard deadline.

        Returns (dbus_address, got_ready, tail) where tail contains a decoded
        prefix of any unrecognized stdout output (a partial line counts). A
        blocking readline() cannot be used: a reaped or killed leader with
        surviving descendants keeps the pipe open without writing a newline,
        which once wedged start() forever. Reads are chunk-based on a
        non-blocking fd so a newline-free write also terminates the loop.
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
        deadline = time.monotonic() + _STARTUP_READ_TIMEOUT

        def remember(text: str) -> None:
            # Keep only a bounded excerpt; stdout is diagnostics, not a stream.
            tail.extend((text + "\n").encode(errors="replace")[: 4096 - min(len(tail), 4096)])

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            readable, _, _ = select.select([fd], [], [], min(remaining, 0.5))
            if not readable:
                # No complete line can arrive any more once the leader is gone;
                # whatever bytes remain pending are a partial line. Breaking
                # here is what keeps a SIGKILLed wrapper with pipe-holding
                # descendants bounded.
                if process.poll() is not None:
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
sleep 0.3

# Signal parent that setup is complete
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
        # Per-session deterministic config dir (US keymap via absent kxkbrc,
        # kwallet popup disabled). Set after os.environ so it always wins.
        if self._session_config_dir is not None:
            env["XDG_CONFIG_HOME"] = str(self._session_config_dir)
            # Strip host XKB defaults: on hosts with a non-US primary layout
            # (e.g. ru,us via locale1) KWin compiles that keymap even with an
            # empty kxkbrc, and evdev keycodes from the EIS keyboard then
            # produce the host layout's characters instead of ASCII.
            # An empty XKB_DEFAULT_LAYOUT resets libxkbcommon to plain "us".
            for var in (
                "XKB_DEFAULT_LAYOUT",
                "XKB_DEFAULT_VARIANT",
                "XKB_DEFAULT_OPTIONS",
                "XKB_DEFAULT_RULES",
                "XKB_DEFAULT_MODEL",
            ):
                env.pop(var, None)
        # Remove host display references to avoid kwin connecting to host
        env.pop("WAYLAND_DISPLAY", None)
        env.pop("DISPLAY", None)

        env.update(self._xdg_isolation_env())
        env.update(config.extra_env)
        return env

    def _wait_for_socket(self, socket_path: Path, timeout: float) -> bool:
        """Wait for the Wayland socket file to appear."""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if socket_path.exists():
                return True
            # Check if process died
            if self._process and self._process.poll() is not None:
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

        proc = subprocess.Popen(
            command,
            env=env,
            stdout=log_file,
            stderr=log_file,
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

        # Terminate apps launched by us
        for app in self._info.apps.values():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                app.process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                app.process.wait(timeout=3)

        if not keep_screenshots and self._info.screenshot_dir.exists():
            shutil.rmtree(self._info.screenshot_dir, ignore_errors=True)
