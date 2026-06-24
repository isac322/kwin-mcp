"""Screenshot capture via KWin ScreenShot2 D-Bus or spectacle CLI fallback."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import dbus
import dbus.bus


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

    # Try fast direct D-Bus capture first; fall back to spectacle CLI on
    # D-Bus authorization / interface errors. spectacle 6 routes through
    # xdg-desktop-portal which we disable in headless CI containers (it
    # crashes KWin on activation), so spectacle hangs forever there.
    # capture_frame_burst already uses this same pattern.
    if dbus_address:
        try:
            return capture_screenshot_dbus(
                dbus_address,
                output_path,
                include_cursor=include_cursor,
            )
        except dbus.DBusException as exc:
            # Surface the exact KWin error to stderr so future failures are
            # diagnosable without code changes (CaptureWorkspace returning
            # InvalidScreen, ServiceUnknown, etc.) before we silently fall back.
            sys.stderr.write(
                f"kwin-mcp: ScreenShot2 D-Bus failed, falling back to spectacle: "
                f"{exc.get_dbus_name()}: {exc.get_dbus_message()}\n"
            )

    _capture_via_spectacle(
        dbus_address,
        wayland_socket,
        output_path=output_path,
        include_cursor=include_cursor,
    )
    return output_path


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
    from PIL import Image

    bus = dbus.bus.BusConnection(dbus_address)
    screenshot_obj = bus.get_object("org.kde.KWin", "/org/kde/KWin/ScreenShot2")
    iface = dbus.Interface(screenshot_obj, "org.kde.KWin.ScreenShot2")

    read_fd, write_fd = os.pipe()
    try:
        options = {"include-cursor": dbus.Boolean(include_cursor)}
        results = iface.CaptureWorkspace(options, dbus.types.UnixFd(write_fd))
    finally:
        os.close(write_fd)

    try:
        chunks = []
        while True:
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(read_fd)

    data = b"".join(chunks)
    if not data:
        msg = "KWin ScreenShot2 returned no data"
        raise RuntimeError(msg)

    width = int(results["width"])
    height = int(results["height"])
    stride = int(results["stride"])

    # KWin returns raw ARGB32_Premultiplied (Qt format 6) in native byte order.
    # On little-endian systems, bytes are stored as BGRA.
    img = Image.frombytes("RGBA", (width, height), data, "raw", "BGRA", stride)
    img.save(output_path, "PNG")
    return output_path


def capture_frame_burst(
    dbus_address: str,
    output_dir: Path,
    delays_ms: list[int],
    *,
    include_cursor: bool = False,
    wayland_socket: str = "",
) -> list[Path]:
    """Capture multiple screenshots at specified delays after an action.

    Takes screenshots at each delay (in milliseconds) using the fast
    D-Bus capture method. Falls back to spectacle CLI if D-Bus authorization
    fails (e.g. in live sessions without KWIN_SCREENSHOT_NO_PERMISSION_CHECKS).

    Args:
        dbus_address: D-Bus session bus address for the session.
        output_dir: Directory to save the frame PNG files.
        delays_ms: List of delays in milliseconds (e.g., [0, 50, 100, 200, 500]).
        include_cursor: Whether to include the mouse cursor.
        wayland_socket: Wayland socket name (needed for spectacle fallback).

    Returns:
        List of paths to the captured PNG files, ordered by delay.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    sorted_delays = sorted(delays_ms)

    # Try fast D-Bus capture first
    try:
        return _capture_frame_burst_dbus(
            dbus_address, output_dir, sorted_delays, include_cursor=include_cursor
        )
    except dbus.DBusException:
        # D-Bus authorization failed (e.g. live session without permission bypass).
        # Fall back to spectacle CLI (slower but always authorized).
        return _capture_frame_burst_spectacle(
            dbus_address,
            wayland_socket,
            output_dir,
            sorted_delays,
            include_cursor=include_cursor,
        )


def _capture_frame_burst_dbus(
    dbus_address: str,
    output_dir: Path,
    sorted_delays: list[int],
    *,
    include_cursor: bool = False,
) -> list[Path]:
    """Capture frames using fast KWin ScreenShot2 D-Bus interface."""
    from PIL import Image

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

        read_fd, write_fd = os.pipe()
        try:
            results = iface.CaptureWorkspace(options, dbus.types.UnixFd(write_fd))
        finally:
            os.close(write_fd)
        try:
            chunks = []
            while True:
                chunk = os.read(read_fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(read_fd)

        raw_frames.append(
            (
                b"".join(chunks),
                int(results["width"]),
                int(results["height"]),
                int(results["stride"]),
            )
        )

    # Phase 2: Convert raw frames to PNG (timing-insensitive)
    frame_paths: list[Path] = []
    for i, (delay_ms, (data, width, height, stride)) in enumerate(
        zip(sorted_delays, raw_frames, strict=True)
    ):
        if not data:
            continue
        frame_path = output_dir / f"frame_{i:03d}_{delay_ms}ms.png"
        img = Image.frombytes("RGBA", (width, height), data, "raw", "BGRA", stride)
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
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        msg = (
            "spectacle not found. Install spectacle "
            "(e.g. 'sudo pacman -S spectacle' or 'sudo apt install kde-spectacle')."
        )
        raise RuntimeError(msg) from None

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")
        msg = f"spectacle failed (exit {result.returncode}): {stderr}"
        raise RuntimeError(msg)

    if not output_path.exists() or output_path.stat().st_size == 0:
        msg = "spectacle produced no output"
        raise RuntimeError(msg)
