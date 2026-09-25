"""Test-owned live KWin compositor shared by the session_connect end-to-end tests."""

from __future__ import annotations

import contextlib
import os
import select
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import IO


@dataclass(frozen=True)
class LiveKWin:
    """Connection details and process handle for a test-owned compositor."""

    dbus_address: str
    wayland_display: str
    socket_path: Path
    process: subprocess.Popen[bytes]


def _stderr_excerpt(stderr_file: IO[bytes]) -> str:
    stderr_file.flush()
    stderr_file.seek(0)
    return stderr_file.read().decode(errors="replace")[:500]


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


@contextlib.contextmanager
def live_kwin() -> Iterator[LiveKWin]:
    runtime_dir = Path(os.environ["XDG_RUNTIME_DIR"])
    wayland_display = f"wayland-live-test-{os.getpid()}-{uuid4().hex}"
    socket_path = runtime_dir / wayland_display
    script = f"""\
set -eu
printf 'DBUS_SESSION_BUS_ADDRESS=%s\\n' "$DBUS_SESSION_BUS_ADDRESS"
cleanup() {{
    kill "$KWIN_PID" "$AT_SPI_PID" 2>/dev/null || true
    wait "$KWIN_PID" "$AT_SPI_PID" 2>/dev/null || true
}}
trap cleanup EXIT TERM INT HUP
/usr/libexec/at-spi-bus-launcher --launch-immediately &
AT_SPI_PID=$!
sleep 0.2
dbus-update-activation-environment \\
    WAYLAND_DISPLAY={wayland_display} QT_QPA_PLATFORM=wayland
env -u WAYLAND_DISPLAY -u QT_QPA_PLATFORM -u KDE_FULL_SESSION -u KDE_SESSION_VERSION \\
    KWIN_WAYLAND_NO_PERMISSION_CHECKS=1 \\
    KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1 \\
    kwin_wayland --virtual --no-lockscreen --width 1280 --height 800 \\
    --socket {wayland_display} &
KWIN_PID=$!
for _ in $(seq 1 300); do
    [ -S "$XDG_RUNTIME_DIR/{wayland_display}" ] && break
    kill -0 "$KWIN_PID" 2>/dev/null || break
    sleep 0.1
done
if [ ! -S "$XDG_RUNTIME_DIR/{wayland_display}" ]; then
    echo FAILED
    exit 1
fi
sleep 0.3
echo READY
wait "$KWIN_PID"
"""
    env = {
        **os.environ,
        "ATSPI_DBUS_IMPLEMENTATION": "dbus-daemon",
        "KWIN_WAYLAND_NO_PERMISSION_CHECKS": "1",
        "KWIN_SCREENSHOT_NO_PERMISSION_CHECKS": "1",
        "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
        "QT_ACCESSIBILITY": "1",
        "XDG_CURRENT_DESKTOP": "KDE",
        "XDG_SESSION_TYPE": "wayland",
    }
    env.pop("WAYLAND_DISPLAY", None)
    env.pop("DISPLAY", None)

    # Held open across the yield below and closed in the finally; a with-block
    # would shut it before the helper compositor has finished writing.
    stderr_file = tempfile.TemporaryFile()  # noqa: SIM115
    process = subprocess.Popen(
        ["dbus-run-session", "bash", "-c", script],
        stdout=subprocess.PIPE,
        stderr=stderr_file,
        env=env,
        start_new_session=True,
    )
    dbus_address = ""
    ready = False
    try:
        assert process.stdout is not None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not ready:
            remaining = deadline - time.monotonic()
            readable, _, _ = select.select([process.stdout], [], [], min(remaining, 0.5))
            if not readable:
                if process.poll() is not None:
                    break
                continue
            line = process.stdout.readline().decode(errors="replace").strip()
            if line.startswith("DBUS_SESSION_BUS_ADDRESS="):
                dbus_address = line.split("=", 1)[1]
            elif line == "READY":
                ready = True
            elif line == "FAILED":
                break

        if not dbus_address or not ready or not socket_path.is_socket():
            error = _stderr_excerpt(stderr_file)
            raise RuntimeError(
                "test-owned KWin failed to start: "
                f"dbus={bool(dbus_address)}, ready={ready}, stderr={error}"
            )

        yield LiveKWin(dbus_address, wayland_display, socket_path, process)
    finally:
        _terminate_process_group(process)
        if process.stdout is not None:
            process.stdout.close()
        stderr_file.close()
        socket_path.unlink(missing_ok=True)
        socket_path.with_name(f"{socket_path.name}.lock").unlink(missing_ok=True)
