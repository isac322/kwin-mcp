"""Lifecycle helper for deterministic visual tests against nested KWin."""

from __future__ import annotations

import contextlib
import json
import os
import select
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import BinaryIO

_SCREEN_WIDTH = 1280
_SCREEN_HEIGHT = 800
_X_READY_TIMEOUT = 10.0
_SCALE_READY_TIMEOUT = 15.0
_KWIN_READY_TIMEOUT = 30.0
_SCREENSHOT_BUS_NAME = "org.kde.KWin.ScreenShot2"
_REQUIRED_BUS_NAMES = {
    "org.a11y.Bus",
    "org.kde.KWin",
}


@dataclass(frozen=True)
class NestedVisualKWin:
    """Connection details and retained evidence for a test-owned compositor."""

    dbus_address: str
    wayland_display: str
    artifact_dir: Path
    x_display: str
    socket_path: Path
    xvfb_stdout_path: Path
    xvfb_stderr_path: Path
    kwin_stdout_path: Path
    kwin_stderr_path: Path
    screenshot_service_available: bool | None = None
    scale: float = 1.0
    screen_size: tuple[int, int] = (_SCREEN_WIDTH, _SCREEN_HEIGHT)


class _StartupError(RuntimeError):
    """Internal error annotated with readiness state before log collection."""


_KWIN_SESSION_SCRIPT = r"""
set -eu

KWIN_PID=
AT_SPI_PID=
cleanup() {
    set +e
    if [ -n "$KWIN_PID" ]; then
        kill "$KWIN_PID" 2>/dev/null || true
    fi
    if [ -n "$AT_SPI_PID" ]; then
        kill "$AT_SPI_PID" 2>/dev/null || true
    fi
    if [ -n "$KWIN_PID" ]; then
        wait "$KWIN_PID" 2>/dev/null || true
    fi
    if [ -n "$AT_SPI_PID" ]; then
        wait "$AT_SPI_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 143' HUP INT TERM

printf '%s\n' "$DBUS_SESSION_BUS_ADDRESS" > "$VISUAL_DBUS_ADDRESS_PATH"

"$VISUAL_AT_SPI_LAUNCHER" --launch-immediately &
AT_SPI_PID=$!

"$VISUAL_DBUS_UPDATE_ENVIRONMENT" \
    DISPLAY="$DISPLAY" \
    WAYLAND_DISPLAY="$VISUAL_WAYLAND_DISPLAY" \
    QT_QPA_PLATFORM=wayland \
    XDG_CURRENT_DESKTOP=KDE \
    XDG_SESSION_TYPE=wayland

env \
    -u WAYLAND_DISPLAY \
    -u QT_QPA_PLATFORM \
    KWIN_WAYLAND_NO_PERMISSION_CHECKS=1 \
    KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1 \
    LIBGL_ALWAYS_SOFTWARE=1 \
    "$VISUAL_KWIN_WAYLAND" \
        --x11-display "$DISPLAY" \
        --width "$VISUAL_SCREEN_WIDTH" \
        --height "$VISUAL_SCREEN_HEIGHT" \
        --no-lockscreen \
        --socket "$VISUAL_WAYLAND_DISPLAY" &
KWIN_PID=$!
wait "$KWIN_PID"
"""


def _executable(name: str, *fallbacks: str) -> str:
    path = shutil.which(name)
    if path is not None:
        return path
    for fallback in fallbacks:
        if Path(fallback).is_file() and os.access(fallback, os.X_OK):
            return fallback
    raise _StartupError(f"required executable not found: {name}")


def _group_exists(process: subprocess.Popen[bytes]) -> bool:
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_group(process: subprocess.Popen[bytes] | None) -> None:
    if process is None:
        return

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        process.poll()
        if not _group_exists(process):
            break
        time.sleep(0.05)

    if _group_exists(process):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)

    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=3.0)


def _read_display_number(fd: int, process: subprocess.Popen[bytes]) -> str:
    deadline = time.monotonic() + _X_READY_TIMEOUT
    payload = bytearray()
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise _StartupError(f"Xvfb exited with status {process.returncode}")
        remaining = deadline - time.monotonic()
        readable, _, _ = select.select([fd], [], [], min(remaining, 0.25))
        if not readable:
            continue
        chunk = os.read(fd, 32)
        if not chunk:
            break
        payload.extend(chunk)
        if b"\n" in payload:
            break

    value = bytes(payload).splitlines()[0].decode(errors="replace").strip() if payload else ""
    if not value.isdigit():
        raise _StartupError("Xvfb did not report an allocated display")
    return value


def _wait_for_x_display(display: str, process: subprocess.Popen[bytes], xdpyinfo: str) -> None:
    deadline = time.monotonic() + _X_READY_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise _StartupError(f"Xvfb exited with status {process.returncode}")
        try:
            result = subprocess.run(
                [xdpyinfo, "-display", display],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.0,
                check=False,
            )
        except subprocess.TimeoutExpired:
            result = None
        if result is not None and result.returncode == 0:
            return
        time.sleep(0.1)
    raise _StartupError(f"X display {display} did not become ready")


def _read_dbus_address(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return ""


def _bus_names(dbus_address: str) -> set[str]:
    # Keep dbus-python optional until the Linux-only visual context is entered.
    import dbus
    import dbus.bus

    bus = dbus.bus.BusConnection(dbus_address)
    try:
        return {str(name) for name in bus.list_names()}
    finally:
        bus.close()


def _wait_for_kwin(
    *,
    process: subprocess.Popen[bytes],
    xvfb_process: subprocess.Popen[bytes],
    address_path: Path,
    socket_path: Path,
) -> tuple[str, bool]:
    deadline = time.monotonic() + _KWIN_READY_TIMEOUT
    dbus_address = ""
    socket_ready = False
    observed_names: set[str] = set()
    last_bus_error = ""

    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise _StartupError(f"KWin session exited with status {process.returncode}")
        if xvfb_process.poll() is not None:
            raise _StartupError(f"Xvfb exited with status {xvfb_process.returncode}")

        if not dbus_address:
            dbus_address = _read_dbus_address(address_path)
        socket_ready = socket_path.is_socket()
        if dbus_address:
            try:
                observed_names = _bus_names(dbus_address)
                last_bus_error = ""
            except Exception as error:  # D-Bus may reject connections while starting.
                last_bus_error = f"{type(error).__name__}: {error}"
                dbus_address = ""

        if socket_ready and observed_names >= _REQUIRED_BUS_NAMES:
            return dbus_address, _SCREENSHOT_BUS_NAME in observed_names
        time.sleep(0.1)

    missing_names = sorted(_REQUIRED_BUS_NAMES - observed_names)
    state = (
        f"dbus={bool(dbus_address)}, socket={socket_ready}, "
        f"missing_bus_names={missing_names}, "
        f"screenshot_service={_SCREENSHOT_BUS_NAME in observed_names}"
    )
    if last_bus_error:
        state += f", bus_error={last_bus_error}"
    raise _StartupError(f"nested KWin did not become ready ({state})")


def _kscreen_outputs(kscreen_doctor: str, env: dict[str, str]) -> list[dict[str, object]]:
    result = subprocess.run(
        [kscreen_doctor, "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise _StartupError(f"kscreen-doctor --json failed: {result.stderr.strip()[-500:]}")
    try:
        outputs = json.loads(result.stdout)["outputs"]
    except (KeyError, json.JSONDecodeError) as error:
        raise _StartupError(f"kscreen-doctor --json was unreadable: {error}") from error
    return [o for o in outputs if o.get("enabled", True)]


def _apply_output_scale(
    *,
    dbus_address: str,
    wayland_display: str,
    scale: float,
    log_path: Path,
    isolated_home: dict[str, str],
) -> None:
    """Scale every nested KWin output through KScreen and wait until KWin applies it."""
    kscreen_doctor = _executable("kscreen-doctor")
    env = {
        **os.environ,
        **isolated_home,
        "DBUS_SESSION_BUS_ADDRESS": dbus_address,
        "WAYLAND_DISPLAY": wayland_display,
        "QT_QPA_PLATFORM": "wayland",
    }
    env.pop("DISPLAY", None)
    outputs = _kscreen_outputs(kscreen_doctor, env)
    if not outputs:
        raise _StartupError("kscreen-doctor reported no enabled outputs")
    arguments = [f"output.{output['name']}.scale.{scale:g}" for output in outputs]
    result = subprocess.run(
        [kscreen_doctor, *arguments],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    log_path.write_text(
        f"$ kscreen-doctor {' '.join(arguments)}\nexit {result.returncode}\n"
        f"{result.stdout}{result.stderr}",
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise _StartupError(f"kscreen-doctor could not set scale {scale:g}: {result.stderr}")

    deadline = time.monotonic() + _SCALE_READY_TIMEOUT
    scales: list[object] = []
    while time.monotonic() < deadline:
        scales = [output.get("scale") for output in _kscreen_outputs(kscreen_doctor, env)]
        if all(isinstance(s, int | float) and abs(s - scale) < 1e-3 for s in scales):
            return
        time.sleep(0.2)
    raise _StartupError(f"nested KWin output scale stayed {scales}, expected {scale:g}")


def _log_excerpt(path: Path, limit: int = 2000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return ""
    if len(text) <= limit:
        return text
    return f"...{text[-limit:]}"


def _startup_message(reason: str, paths: tuple[Path, ...]) -> str:
    excerpts: list[str] = []
    for path in paths:
        excerpt = _log_excerpt(path)
        if excerpt:
            excerpts.append(f"{path.name}:\n{excerpt}")
    if not excerpts:
        return f"visual KWin startup failed: {reason}"
    return f"visual KWin startup failed: {reason}\n" + "\n".join(excerpts)


@contextmanager
def nested_visual_kwin(
    *,
    scale: float = 1.0,
    screen_size: tuple[int, int] = (_SCREEN_WIDTH, _SCREEN_HEIGHT),
) -> Iterator[NestedVisualKWin]:
    """Run Xvfb and nested KWin, yielding connection details for ``session_connect``.

    ``screen_size`` is the physical X11 output size; ``scale`` is applied to the
    KWin output through kscreen-doctor before the fixture is yielded, so the
    logical workspace is ``screen_size / scale``.
    """
    screen_width, screen_height = screen_size
    artifact_root_value = os.environ.get("KWIN_MCP_ARTIFACT_DIR")
    if not artifact_root_value:
        raise RuntimeError("KWIN_MCP_ARTIFACT_DIR is required for visual KWin evidence")

    artifact_root = Path(artifact_root_value)
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_dir = Path(tempfile.mkdtemp(prefix="visual-kwin-", dir=artifact_root))
    artifact_dir.chmod(0o755)
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", ""))
    if not runtime_dir.is_dir():
        raise RuntimeError("XDG_RUNTIME_DIR must name the existing runtime directory")
    # Isolate KDE state per fixture: kscreen-doctor persists the applied output
    # scale under $HOME, and the next session's KWin would silently restore it,
    # shrinking a 1280x800 scale-1.0 workspace to 883x552. XDG_RUNTIME_DIR stays
    # shared because it owns the Wayland socket directory.
    home_dir = artifact_dir / "home"
    for subdir in (".config", ".local/share", ".local/state"):
        (home_dir / subdir).mkdir(parents=True, exist_ok=True)
    isolated_home = {
        "HOME": str(home_dir),
        "XDG_CONFIG_HOME": str(home_dir / ".config"),
        "XDG_DATA_HOME": str(home_dir / ".local" / "share"),
        "XDG_STATE_HOME": str(home_dir / ".local" / "state"),
    }

    suffix = f"{os.getpid()}-{uuid4().hex[:12]}"
    wayland_display = f"wayland-visual-{suffix}"
    socket_path = runtime_dir / wayland_display
    address_path = artifact_dir / "dbus-address.txt"
    xvfb_stdout_path = artifact_dir / "xvfb.stdout.log"
    xvfb_stderr_path = artifact_dir / "xvfb.stderr.log"
    kwin_stdout_path = artifact_dir / "kwin.stdout.log"
    kwin_stderr_path = artifact_dir / "kwin.stderr.log"
    log_paths = (
        xvfb_stdout_path,
        xvfb_stderr_path,
        kwin_stdout_path,
        kwin_stderr_path,
    )

    xvfb_process: subprocess.Popen[bytes] | None = None
    kwin_process: subprocess.Popen[bytes] | None = None
    display_read_fd: int | None = None
    display_write_fd: int | None = None
    log_files: list[BinaryIO] = []

    try:
        xvfb = _executable("Xvfb")
        xdpyinfo = _executable("xdpyinfo")
        dbus_run_session = _executable("dbus-run-session")
        dbus_update_environment = _executable("dbus-update-activation-environment")
        kwin_wayland = _executable("kwin_wayland")
        at_spi_launcher = _executable(
            "at-spi-bus-launcher",
            "/usr/libexec/at-spi-bus-launcher",
            "/usr/lib/at-spi2-core/at-spi-bus-launcher",
        )

        xvfb_stdout = xvfb_stdout_path.open("wb")
        xvfb_stderr = xvfb_stderr_path.open("wb")
        kwin_stdout = kwin_stdout_path.open("wb")
        kwin_stderr = kwin_stderr_path.open("wb")
        log_files.extend((xvfb_stdout, xvfb_stderr, kwin_stdout, kwin_stderr))

        display_read_fd, display_write_fd = os.pipe()
        try:
            xvfb_process = subprocess.Popen(
                [
                    xvfb,
                    "-displayfd",
                    str(display_write_fd),
                    "-screen",
                    "0",
                    f"{screen_width}x{screen_height}x24",
                    "-nolisten",
                    "tcp",
                    "-noreset",
                ],
                stdout=xvfb_stdout,
                stderr=xvfb_stderr,
                pass_fds=(display_write_fd,),
                start_new_session=True,
            )
        except OSError as error:
            raise _StartupError(f"could not start Xvfb: {error}") from error
        os.close(display_write_fd)
        display_write_fd = None
        display_number = _read_display_number(display_read_fd, xvfb_process)
        os.close(display_read_fd)
        display_read_fd = None
        x_display = f":{display_number}"
        _wait_for_x_display(x_display, xvfb_process, xdpyinfo)

        environment = {
            **os.environ,
            "ATSPI_DBUS_IMPLEMENTATION": "dbus-daemon",
            "DISPLAY": x_display,
            "GTK_MODULES": "gail:atk-bridge",
            "KWIN_SCREENSHOT_NO_PERMISSION_CHECKS": "1",
            "KWIN_WAYLAND_NO_PERMISSION_CHECKS": "1",
            "LIBGL_ALWAYS_SOFTWARE": "1",
            "NO_AT_BRIDGE": "0",
            "QT_ACCESSIBILITY": "1",
            "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
            "VISUAL_AT_SPI_LAUNCHER": at_spi_launcher,
            "VISUAL_DBUS_UPDATE_ENVIRONMENT": dbus_update_environment,
            "VISUAL_DBUS_ADDRESS_PATH": str(address_path),
            "VISUAL_WAYLAND_DISPLAY": wayland_display,
            "VISUAL_KWIN_WAYLAND": kwin_wayland,
            "VISUAL_SCREEN_HEIGHT": str(screen_height),
            "VISUAL_SCREEN_WIDTH": str(screen_width),
            "XDG_CURRENT_DESKTOP": "KDE",
            "XDG_SESSION_TYPE": "wayland",
        }
        environment.update(isolated_home)
        environment.pop("DBUS_SESSION_BUS_ADDRESS", None)
        environment.pop("WAYLAND_DISPLAY", None)

        try:
            kwin_process = subprocess.Popen(
                [dbus_run_session, "sh", "-c", _KWIN_SESSION_SCRIPT],
                stdout=kwin_stdout,
                stderr=kwin_stderr,
                env=environment,
                start_new_session=True,
            )
        except OSError as error:
            raise _StartupError(f"could not start D-Bus/KWin session: {error}") from error
        dbus_address, screenshot_service_available = _wait_for_kwin(
            process=kwin_process,
            xvfb_process=xvfb_process,
            address_path=address_path,
            socket_path=socket_path,
        )
        if scale != 1.0:
            _apply_output_scale(
                dbus_address=dbus_address,
                wayland_display=wayland_display,
                scale=scale,
                log_path=artifact_dir / "kscreen-doctor.log",
                isolated_home=isolated_home,
            )

        yield NestedVisualKWin(
            dbus_address=dbus_address,
            wayland_display=wayland_display,
            artifact_dir=artifact_dir,
            x_display=x_display,
            socket_path=socket_path,
            xvfb_stdout_path=xvfb_stdout_path,
            xvfb_stderr_path=xvfb_stderr_path,
            kwin_stdout_path=kwin_stdout_path,
            kwin_stderr_path=kwin_stderr_path,
            screenshot_service_available=screenshot_service_available,
            scale=scale,
            screen_size=screen_size,
        )
    except _StartupError as error:
        _terminate_process_group(kwin_process)
        _terminate_process_group(xvfb_process)
        kwin_process = None
        xvfb_process = None
        for log_file in log_files:
            log_file.flush()
        raise RuntimeError(_startup_message(str(error), log_paths)) from error
    finally:
        if display_read_fd is not None:
            os.close(display_read_fd)
        if display_write_fd is not None:
            os.close(display_write_fd)
        _terminate_process_group(kwin_process)
        _terminate_process_group(xvfb_process)
        for log_file in log_files:
            log_file.close()
        socket_path.unlink(missing_ok=True)
        socket_path.with_name(f"{socket_path.name}.lock").unlink(missing_ok=True)
