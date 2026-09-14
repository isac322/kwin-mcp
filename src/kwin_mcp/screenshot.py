"""Screenshot capture via KWin ScreenShot2, Spectacle, or opt-in X11 fallback."""

from __future__ import annotations

import os
import select
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import dbus
import dbus.bus

if TYPE_CHECKING:
    from PIL.Image import Image

# Upper bound for a single ScreenShot2 capture. Generous for a real capture
# (tens of milliseconds) yet short enough that the spectacle fallback stays
# responsive when KWin never answers.
_CAPTURE_TIMEOUT_S = 5.0
_READER_POLL_INTERVAL_S = 0.1
_READER_JOIN_TIMEOUT_S = 1.0

_SUBPROCESS_CAPTURE_TIMEOUT_S = 10.0


def capture_screenshot_to_file(
    dbus_address: str = "",
    wayland_socket: str = "",
    *,
    include_cursor: bool = False,
    output_dir: Path | None = None,
) -> Path:
    """Capture a screenshot and save to a file.

    Args:
        dbus_address: D-Bus session bus address for the isolated session.
        wayland_socket: Wayland socket name for the isolated session.
        include_cursor: Whether to include the mouse cursor.
        output_dir: Directory to save the screenshot. Uses /tmp if not specified.

    Returns:
        Absolute path of the saved PNG file.
    """
    if output_dir is None:
        output_dir = Path("/tmp")
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"screenshot_{timestamp}.png"

    x11_enabled = os.environ.get("KWIN_MCP_X11_SCREENSHOT") == "1"
    x11_selected = x11_enabled and bool(os.environ.get("DISPLAY"))
    x11_error: RuntimeError | None = None
    if x11_selected:
        try:
            _capture_via_scrot(output_path, include_cursor=include_cursor)
            return output_path
        except RuntimeError as exc:
            x11_error = exc

    # Default path: KWin ScreenShot2 D-Bus, then Spectacle.
    try:
        return capture_screenshot_dbus(
            dbus_address,
            output_path,
            include_cursor=include_cursor,
        )
    except (dbus.DBusException, RuntimeError) as dbus_exc:
        try:
            _capture_via_spectacle(
                dbus_address,
                wayland_socket,
                output_path=output_path,
                include_cursor=include_cursor,
            )
            return output_path
        except RuntimeError as spectacle_exc:
            if not x11_enabled:
                msg = (
                    "Screenshot capture failed: "
                    f"ScreenShot2 ({dbus_exc}); "
                    f"Spectacle ({spectacle_exc})"
                )
                raise RuntimeError(msg) from spectacle_exc

            if x11_error is None:
                try:
                    _capture_via_scrot(output_path, include_cursor=include_cursor)
                    return output_path
                except RuntimeError as exc:
                    x11_error = exc

            msg = (
                "Screenshot capture failed: "
                f"X11/scrot ({x11_error}); "
                f"ScreenShot2 ({dbus_exc}); "
                f"Spectacle ({spectacle_exc})"
            )
            raise RuntimeError(msg) from x11_error


def capture_screenshot_dbus(
    dbus_address: str,
    output_path: Path,
    *,
    include_cursor: bool = False,
) -> Path:
    """Capture screenshot directly via KWin ScreenShot2 D-Bus interface.

    Much faster than spectacle CLI (~15-30ms vs ~200-300ms per frame)
    because it avoids process spawn overhead. Suitable for rapid
    frame capture at short intervals (e.g., every 50ms).

    Requires KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1 to be set in the
    KWin process environment (automatically set for isolated sessions).

    Args:
        dbus_address: D-Bus session bus address for the isolated session.
        output_path: Path to save the PNG file.
        include_cursor: Whether to include the mouse cursor.

    Returns:
        The output_path with the saved PNG file.
    """

    bus = dbus.bus.BusConnection(dbus_address)
    screenshot_obj = bus.get_object("org.kde.KWin", "/org/kde/KWin/ScreenShot2")
    iface = dbus.Interface(screenshot_obj, "org.kde.KWin.ScreenShot2")

    options = {"include-cursor": dbus.Boolean(include_cursor)}
    data, width, height, stride = _capture_raw_frame(iface, options)
    img = _image_from_raw_frame(data, width, height, stride)
    img.save(output_path, "PNG")
    return output_path


def _capture_raw_frame(
    iface: dbus.Interface,
    options: dict[str, dbus.Boolean],
) -> tuple[bytes, int, int, int]:
    """Capture one raw frame over ScreenShot2, returning (data, w, h, stride).

    KWin streams the pixels into the pipe before it answers the D-Bus call, and
    a frame is far larger than the pipe buffer, so the pipe is drained by a
    reader thread while the call is in flight.

    The call carries an explicit timeout: a compositor that never answers (KWin
    on a headless virtual backend does exactly that) would otherwise burn
    dbus-python's 25s default before callers can fall back to spectacle.
    """
    read_fd, write_fd = os.pipe()
    chunks: list[bytes] = []
    reader_errors: list[OSError] = []
    stop_reader = threading.Event()

    def drain() -> None:
        # The reader owns read_fd: closing it here, rather than in the caller,
        # keeps a still-running thread from reading a recycled fd number.
        try:
            while not stop_reader.is_set():
                try:
                    readable, _, _ = select.select(
                        [read_fd],
                        [],
                        [],
                        _READER_POLL_INTERVAL_S,
                    )
                except InterruptedError:
                    continue
                if not readable:
                    continue

                chunk = os.read(read_fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError as exc:
            reader_errors.append(exc)
        finally:
            os.close(read_fd)

    reader = threading.Thread(
        target=drain,
        name="kwin-mcp-screenshot-reader",
        daemon=True,
    )
    reader.start()
    capture_succeeded = False
    try:
        results = iface.CaptureActiveScreen(
            options,
            dbus.types.UnixFd(write_fd),
            timeout=_CAPTURE_TIMEOUT_S,
        )
        capture_succeeded = True
    finally:
        os.close(write_fd)
        if not capture_succeeded:
            stop_reader.set()
        reader.join(timeout=_READER_JOIN_TIMEOUT_S)
        if reader.is_alive():
            stop_reader.set()
            reader.join(timeout=_READER_JOIN_TIMEOUT_S)
        if reader.is_alive():
            msg = "KWin ScreenShot2 pipe reader did not terminate"
            raise RuntimeError(msg)

    if reader_errors:
        msg = f"KWin ScreenShot2 pipe read failed: {reader_errors[0]}"
        raise RuntimeError(msg) from reader_errors[0]

    data = b"".join(chunks)
    if not data:
        msg = "KWin ScreenShot2 returned no data"
        raise RuntimeError(msg)
    return data, int(results["width"]), int(results["height"]), int(results["stride"])


def _image_from_raw_frame(data: bytes, width: int, height: int, stride: int) -> Image:
    """Convert KWin's raw frame and normalize malformed payloads as backend failures."""
    from PIL import Image

    try:
        return Image.frombytes("RGBA", (width, height), data, "raw", "BGRA", stride)
    except (OSError, ValueError) as exc:
        msg = f"KWin ScreenShot2 returned malformed image data: {exc}"
        raise RuntimeError(msg) from exc


def capture_frame_burst(
    dbus_address: str,
    output_dir: Path,
    delays_ms: list[int],
    *,
    include_cursor: bool = False,
    wayland_socket: str = "",
) -> list[Path]:
    """Capture multiple screenshots at specified delays after an action.

    Takes screenshots at each delay (in milliseconds). An explicitly enabled
    X11 display uses scrot first; otherwise ScreenShot2 remains the fast path,
    with Spectacle as its fallback.

    Args:
        dbus_address: D-Bus session bus address for the session.
        output_dir: Directory to save the frame PNG files.
        delays_ms: List of delays in milliseconds (e.g., [0, 50, 100, 200, 500]).
        include_cursor: Whether to include the mouse cursor.
        wayland_socket: Wayland socket name (needed for spectacle fallback).

    Returns:
        List of paths to the captured PNG files, ordered by delay.
    """
    burst_start = time.monotonic()
    output_dir.mkdir(parents=True, exist_ok=True)
    sorted_delays = sorted(delays_ms)

    x11_enabled = os.environ.get("KWIN_MCP_X11_SCREENSHOT") == "1"
    x11_selected = x11_enabled and bool(os.environ.get("DISPLAY"))
    x11_error: RuntimeError | None = None
    if x11_selected:
        try:
            return _capture_frame_burst_x11(
                output_dir,
                sorted_delays,
                include_cursor=include_cursor,
                start_time=burst_start,
            )
        except RuntimeError as exc:
            x11_error = exc

    try:
        return _capture_frame_burst_dbus(
            dbus_address, output_dir, sorted_delays, include_cursor=include_cursor
        )
    except (dbus.DBusException, RuntimeError) as dbus_exc:
        try:
            return _capture_frame_burst_spectacle(
                dbus_address,
                wayland_socket,
                output_dir,
                sorted_delays,
                include_cursor=include_cursor,
            )
        except RuntimeError as spectacle_exc:
            if not x11_enabled:
                msg = (
                    "Frame burst capture failed: "
                    f"ScreenShot2 ({dbus_exc}); "
                    f"Spectacle ({spectacle_exc})"
                )
                raise RuntimeError(msg) from spectacle_exc

            if x11_error is None:
                try:
                    return _capture_frame_burst_x11(
                        output_dir,
                        sorted_delays,
                        include_cursor=include_cursor,
                    )
                except RuntimeError as exc:
                    x11_error = exc

            msg = (
                "Frame burst capture failed: "
                f"X11/scrot ({x11_error}); "
                f"ScreenShot2 ({dbus_exc}); "
                f"Spectacle ({spectacle_exc})"
            )
            raise RuntimeError(msg) from x11_error


def _capture_frame_burst_dbus(
    dbus_address: str,
    output_dir: Path,
    sorted_delays: list[int],
    *,
    include_cursor: bool = False,
) -> list[Path]:
    """Capture frames using fast KWin ScreenShot2 D-Bus interface."""

    # Reuse a single D-Bus connection for all captures
    bus = dbus.bus.BusConnection(dbus_address)
    screenshot_obj = bus.get_object("org.kde.KWin", "/org/kde/KWin/ScreenShot2")
    iface = dbus.Interface(screenshot_obj, "org.kde.KWin.ScreenShot2")
    options = {"include-cursor": dbus.Boolean(include_cursor)}

    # Phase 1: Capture all raw frames with accurate timing
    raw_frames: list[tuple[bytes, int, int, int]] = []  # (data, width, height, stride)
    start = time.monotonic()
    for delay_ms in sorted_delays:
        target_time = start + delay_ms / 1000.0
        now = time.monotonic()
        if now < target_time:
            time.sleep(target_time - now)

        raw_frames.append(_capture_raw_frame(iface, options))

    # Phase 2: Convert raw frames to PNG (timing-insensitive)
    frame_paths: list[Path] = []
    for i, (delay_ms, (data, width, height, stride)) in enumerate(
        zip(sorted_delays, raw_frames, strict=True)
    ):
        if not data:
            continue
        frame_path = output_dir / f"frame_{i:03d}_{delay_ms}ms.png"
        img = _image_from_raw_frame(data, width, height, stride)
        img.save(frame_path, "PNG")
        frame_paths.append(frame_path)

    return frame_paths


def _capture_frame_burst_spectacle(
    dbus_address: str,
    wayland_socket: str,
    output_dir: Path,
    sorted_delays: list[int],
    *,
    include_cursor: bool = False,
) -> list[Path]:
    """Capture frames using spectacle CLI (slower but always authorized)."""
    frame_paths: list[Path] = []
    start = time.monotonic()
    for i, delay_ms in enumerate(sorted_delays):
        target_time = start + delay_ms / 1000.0
        now = time.monotonic()
        if now < target_time:
            time.sleep(target_time - now)

        frame_path = output_dir / f"frame_{i:03d}_{delay_ms}ms.png"
        _capture_via_spectacle(
            dbus_address,
            wayland_socket,
            output_path=frame_path,
            include_cursor=include_cursor,
        )
        frame_paths.append(frame_path)
    return frame_paths


def _capture_frame_burst_x11(
    output_dir: Path,
    sorted_delays: list[int],
    *,
    include_cursor: bool = False,
    start_time: float | None = None,
) -> list[Path]:
    """Capture frames from the explicitly enabled X11 display."""
    frame_paths: list[Path] = []
    start = time.monotonic() if start_time is None else start_time
    for i, delay_ms in enumerate(sorted_delays):
        target_time = start + delay_ms / 1000.0
        now = time.monotonic()
        if now < target_time:
            time.sleep(target_time - now)

        frame_path = output_dir / f"frame_{i:03d}_{delay_ms}ms.png"
        _capture_via_scrot(frame_path, include_cursor=include_cursor)
        frame_paths.append(frame_path)
    return frame_paths


def _capture_via_scrot(output_path: Path, *, include_cursor: bool = False) -> None:
    """Capture the explicitly selected X11 root window with scrot."""
    display = os.environ.get("DISPLAY", "")
    if os.environ.get("KWIN_MCP_X11_SCREENSHOT") != "1":
        msg = "X11/scrot fallback is disabled; set KWIN_MCP_X11_SCREENSHOT=1 to enable it"
        raise RuntimeError(msg)
    if not display:
        msg = "X11/scrot fallback requires DISPLAY"
        raise RuntimeError(msg)

    output_path.unlink(missing_ok=True)
    cmd = ["scrot", "--overwrite", "--display", display]
    if include_cursor:
        cmd.append("--pointer")
    cmd.append(str(output_path))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=_SUBPROCESS_CAPTURE_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        msg = "scrot not found. Install scrot (e.g. 'sudo apt install scrot')."
        raise RuntimeError(msg) from None
    except subprocess.TimeoutExpired as exc:
        stderr = (
            exc.stderr.decode(errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        detail = f": {stderr.strip()}" if stderr.strip() else ""
        msg = f"scrot timed out after {_SUBPROCESS_CAPTURE_TIMEOUT_S:g}s{detail}"
        raise RuntimeError(msg) from None

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        detail = f": {stderr}" if stderr else ""
        msg = f"scrot failed (exit {result.returncode}){detail}"
        raise RuntimeError(msg)

    if not output_path.is_file() or output_path.stat().st_size == 0:
        msg = "scrot produced no output"
        raise RuntimeError(msg)


def _capture_via_spectacle(
    dbus_address: str,
    wayland_socket: str,
    *,
    output_path: Path,
    include_cursor: bool = False,
) -> None:
    """Capture screenshot using spectacle CLI in background mode."""
    cmd = ["spectacle", "-b", "-f", "-n", "-o", str(output_path)]
    if include_cursor:
        cmd.append("-p")

    env = {**os.environ}
    if dbus_address:
        env["DBUS_SESSION_BUS_ADDRESS"] = dbus_address
    if wayland_socket:
        env["WAYLAND_DISPLAY"] = wayland_socket
        env["QT_QPA_PLATFORM"] = "wayland"
    # Remove host display refs
    env.pop("DISPLAY", None)

    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            timeout=_SUBPROCESS_CAPTURE_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        msg = (
            "spectacle not found. Install spectacle "
            "(e.g. 'sudo pacman -S spectacle' or 'sudo apt install kde-spectacle')."
        )
        raise RuntimeError(msg) from None
    except subprocess.TimeoutExpired as exc:
        stderr = (
            exc.stderr.decode(errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        detail = f": {stderr.strip()}" if stderr.strip() else ""
        msg = f"spectacle timed out after {_SUBPROCESS_CAPTURE_TIMEOUT_S:g}s{detail}"
        raise RuntimeError(msg) from None

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")
        msg = f"spectacle failed (exit {result.returncode}): {stderr}"
        raise RuntimeError(msg)

    if not output_path.exists() or output_path.stat().st_size == 0:
        msg = "spectacle produced no output"
        raise RuntimeError(msg)
