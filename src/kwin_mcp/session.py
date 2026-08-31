"""KWin Wayland session management.

Manages the lifecycle of KWin Wayland sessions:
- Virtual sessions: isolated via dbus-run-session + kwin_wayland --virtual
- Live sessions: connecting to an existing KWin compositor (real desktop or container)
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


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

        # Build the wrapper script that runs inside dbus-run-session
        wrapper_script = self._build_wrapper_script(config)

        # Start the isolated session in its own process group
        self._process = subprocess.Popen(
            ["dbus-run-session", "bash", "-c", wrapper_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._build_env(config),
            start_new_session=True,
        )

        # Read startup output from the wrapper script.
        # Expected lines: DBUS_SESSION_BUS_ADDRESS=..., READY
        # Any other lines (e.g. from D-Bus activation) are ignored.
        dbus_address, got_ready = self._read_startup_lines(self._process, timeout=25.0)

        # Wait for kwin to be ready (socket file appears)
        socket_path = Path(runtime_dir) / self._socket_name
        if not self._wait_for_socket(socket_path, timeout=10.0):
            self.stop()
            stderr = ""
            if self._process and self._process.stderr:
                stderr = self._process.stderr.read().decode(errors="replace")
            msg = f"KWin failed to start. stderr: {stderr}"
            raise RuntimeError(msg)

        if not got_ready:
            self.stop()
            msg = "Session setup failed: did not receive READY signal"
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

    def stop(self) -> None:
        """Stop the isolated session and clean up all processes."""
        if self._process is None:
            return

        # Send SIGTERM to the entire process group (all children)
        try:
            pgid = os.getpgid(self._process.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Force kill the entire process group
            try:
                pgid = os.getpgid(self._process.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._process.wait(timeout=3)

        # Clean up home directory and/or screenshot directory
        if self._home_dir is not None:
            keep_home = self._config is not None and self._config.keep_home
            keep_screenshots = self._config is not None and self._config.keep_screenshots
            if not keep_home:
                # Remove entire home dir (includes screenshots)
                shutil.rmtree(self._home_dir, ignore_errors=True)
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

        self._process = None
        self._info = None
        self._home_dir = None

    @staticmethod
    def _read_startup_lines(process: subprocess.Popen[bytes], timeout: float) -> tuple[str, bool]:
        """Read wrapper startup lines with an overall deadline.

        Returns ``(dbus_address, got_ready)``. Never blocks longer than
        ``timeout`` seconds: a wrapper that never prints ``READY`` (dead
        KWin, missing binaries) must surface as an error instead of
        hanging the caller forever.
        """
        import queue
        import threading

        lines: queue.Queue[bytes | None] = queue.Queue()

        def reader() -> None:
            try:
                if process.stdout is not None:
                    for line in process.stdout:
                        lines.put(line)
            except Exception:
                pass
            finally:
                lines.put(None)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()

        dbus_address = ""
        got_ready = False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            if line is None:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if text.startswith("DBUS_SESSION_BUS_ADDRESS="):
                dbus_address = text.split("=", 1)[1]
            elif text == "READY":
                got_ready = True
                break
            elif text == "NOSOCKET":
                break
        return dbus_address, got_ready

    def _build_wrapper_script(self, config: SessionConfig) -> str:
        """Build the bash script that runs inside dbus-run-session."""
        return f"""\
echo "DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS"

# Ensure all child processes are cleaned up on exit
cleanup() {{
    kill $KWIN_PID $AT_SPI_PID 2>/dev/null
    wait $KWIN_PID $AT_SPI_PID 2>/dev/null
}}
trap cleanup EXIT TERM INT HUP

# Start the AT-SPI accessibility bus.
# ATSPI_DBUS_IMPLEMENTATION is set in _build_env() to force dbus-daemon
# instead of dbus-broker (which reuses the host's AT-SPI bus).
/usr/lib/at-spi-bus-launcher --launch-immediately &
AT_SPI_PID=$!
sleep 0.2

# Pre-set D-Bus activation environment BEFORE starting KWin.
# When KWin triggers portal auto-activation, portal-kde will get
# WAYLAND_DISPLAY pointing to our isolated compositor socket.
# The socket doesn't exist yet, but portal-kde will be activated
# only after KWin creates it.
dbus-update-activation-environment WAYLAND_DISPLAY={self._socket_name} QT_QPA_PLATFORM=wayland

# Start KWin WITHOUT WAYLAND_DISPLAY to prevent nesting attempt.
# KWin with --virtual creates its own compositor, it must not try
# to connect to another compositor as a client.
# Explicitly pass KWIN_ permission env vars to ensure they reach the
# KWin process (environment inheritance through dbus-run-session can be unreliable).
env -u WAYLAND_DISPLAY -u QT_QPA_PLATFORM \
    KWIN_WAYLAND_NO_PERMISSION_CHECKS=1 \
    KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1 \
    kwin_wayland --virtual --no-lockscreen \
    --width {config.screen_width} --height {config.screen_height} \
    --socket {self._socket_name} &
KWIN_PID=$!

# Wait for KWin socket to appear (bounded: 150 x 0.1s = 15s max)
SOCKET_OK=0
for i in $(seq 1 150); do
    if [ -e "$XDG_RUNTIME_DIR/{self._socket_name}" ]; then
        SOCKET_OK=1
        break
    fi
    sleep 0.1
done
if [ "$SOCKET_OK" != "1" ]; then
    echo "NOSOCKET"
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
