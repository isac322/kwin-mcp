"""Screenshot capture via KWin ScreenShot2 D-Bus or spectacle CLI fallback."""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
from pathlib import Path


def capture_screenshot(
    dbus_address: str = "",
    wayland_socket: str = "",
    *,
    include_cursor: bool = False,
) -> bytes:
    """Capture a screenshot of the isolated session.

    Tries spectacle CLI first (most reliable), falls back to
    KWin ScreenShot2 D-Bus if needed.

    Returns PNG image data as bytes.
    """
    # Use spectacle CLI which works reliably in isolated sessions
    return _capture_via_spectacle(dbus_address, wayland_socket, include_cursor=include_cursor)


def capture_screenshot_base64(
    dbus_address: str = "",
    wayland_socket: str = "",
    *,
    include_cursor: bool = False,
) -> str:
    """Capture a screenshot and return as base64-encoded PNG string."""
    png_data = capture_screenshot(dbus_address, wayland_socket, include_cursor=include_cursor)
    return base64.b64encode(png_data).decode("ascii")


def _capture_via_spectacle(
    dbus_address: str,
    wayland_socket: str,
    *,
    include_cursor: bool = False,
) -> bytes:
    """Capture screenshot using spectacle CLI in background mode."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        output_path = f.name

    try:
        cmd = ["spectacle", "-b", "-f", "-n", "-o", output_path]
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

        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            timeout=10,
            check=False,
        )

        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")
            msg = f"spectacle failed (exit {result.returncode}): {stderr}"
            raise RuntimeError(msg)

        path = Path(output_path)
        if not path.exists() or path.stat().st_size == 0:
            msg = "spectacle produced no output"
            raise RuntimeError(msg)

        return path.read_bytes()
    finally:
        Path(output_path).unlink(missing_ok=True)
