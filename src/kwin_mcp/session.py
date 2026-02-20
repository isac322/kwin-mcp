"""Isolated KWin Wayland session management.

Manages the lifecycle of an isolated KWin Wayland session using
dbus-run-session + kwin_wayland --virtual for complete isolation
from the host desktop.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SessionConfig:
    """Configuration for an isolated KWin session."""

    socket_name: str = ""
    screen_width: int = 1920
    screen_height: int = 1080
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass
class SessionInfo:
    """Runtime information about a running isolated session."""

    dbus_address: str
    wayland_socket: str
    kwin_pid: int
    app_pid: int | None = None
    wrapper_pid: int | None = None


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

    def start(self, config: SessionConfig | None = None) -> SessionInfo:
        """Start an isolated KWin Wayland session.

        Returns SessionInfo with connection details.
        """
        if self.is_running:
            msg = "Session is already running"
            raise RuntimeError(msg)

        if config is None:
            config = SessionConfig()

        self._socket_name = config.socket_name or f"wayland-mcp-{os.getpid()}-{int(time.time())}"

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

        # Read the D-Bus address from the wrapper's stdout (echoed first)
        dbus_address = ""
        if self._process.stdout:
            line = self._process.stdout.readline().decode().strip()
            if line.startswith("DBUS_SESSION_BUS_ADDRESS="):
                dbus_address = line.split("=", 1)[1]

        # Wait for kwin to be ready (socket file appears)
        socket_path = Path(runtime_dir) / self._socket_name
        if not self._wait_for_socket(socket_path, timeout=10.0):
            self.stop()
            stderr = ""
            if self._process and self._process.stderr:
                stderr = self._process.stderr.read().decode(errors="replace")
            msg = f"KWin failed to start. stderr: {stderr}"
            raise RuntimeError(msg)

        # Wait for "READY" signal (D-Bus activation env updated)
        if self._process.stdout:
            ready_line = self._process.stdout.readline().decode().strip()
            if ready_line != "READY":
                self.stop()
                msg = f"Session setup failed. Expected READY, got: {ready_line!r}"
                raise RuntimeError(msg)

        self._info = SessionInfo(
            dbus_address=dbus_address,
            wayland_socket=self._socket_name,
            kwin_pid=self._process.pid,
        )
        return self._info

    def launch_app(self, command: list[str], extra_env: dict[str, str] | None = None) -> int:
        """Launch an application inside the isolated session.

        Returns the app's PID.
        """
        if not self.is_running:
            msg = "Session is not running"
            raise RuntimeError(msg)

        env = {
            **os.environ,
            "WAYLAND_DISPLAY": self._socket_name,
            "QT_QPA_PLATFORM": "wayland",
            "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
            "QT_ACCESSIBILITY": "1",
        }
        if extra_env:
            env.update(extra_env)
        if self._info and self._info.dbus_address:
            env["DBUS_SESSION_BUS_ADDRESS"] = self._info.dbus_address

        proc = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if self._info:
            self._info.app_pid = proc.pid
        return proc.pid

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

        # Clean up socket files
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        for suffix in ("", ".lock"):
            path = Path(runtime_dir) / f"{self._socket_name}{suffix}"
            path.unlink(missing_ok=True)

        self._process = None
        self._info = None

    def _build_wrapper_script(self, _config: SessionConfig) -> str:
        """Build the bash script that runs inside dbus-run-session."""
        return f"""\
echo "DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS"

# Ensure all child processes are cleaned up on exit
cleanup() {{
    kill $KWIN_PID $AT_SPI_PID 2>/dev/null
    wait $KWIN_PID $AT_SPI_PID 2>/dev/null
}}
trap cleanup EXIT TERM INT HUP

# Start AT-SPI accessibility bus
/usr/lib/at-spi-bus-launcher --launch-immediately &
AT_SPI_PID=$!

# Pre-set D-Bus activation environment BEFORE starting KWin.
# When KWin triggers portal auto-activation, portal-kde will get
# WAYLAND_DISPLAY pointing to our isolated compositor socket.
# The socket doesn't exist yet, but portal-kde will be activated
# only after KWin creates it.
dbus-update-activation-environment WAYLAND_DISPLAY={self._socket_name} QT_QPA_PLATFORM=wayland

# Start KWin WITHOUT WAYLAND_DISPLAY to prevent nesting attempt.
# KWin with --virtual creates its own compositor, it must not try
# to connect to another compositor as a client.
env -u WAYLAND_DISPLAY -u QT_QPA_PLATFORM \
    kwin_wayland --virtual --no-lockscreen --socket {self._socket_name} &
KWIN_PID=$!

# Wait for KWin socket to appear
while [ ! -e "$XDG_RUNTIME_DIR/{self._socket_name}" ]; do sleep 0.1; done
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
        }
        # Remove host display references to avoid kwin connecting to host
        env.pop("WAYLAND_DISPLAY", None)
        env.pop("DISPLAY", None)

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
