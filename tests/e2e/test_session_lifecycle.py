"""End-to-end coverage for virtual and live session lifecycle semantics."""

from __future__ import annotations

import contextlib
import errno
import os
import select
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from _asserts import element_count

from kwin_mcp.core import AutomationEngine

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

BAD_BINARY = "definitely-not-a-real-binary"
SESSION_ALREADY_RUNNING = "Session already running. Call session_stop first."
XDG_HOME_DIRECTORIES = (
    Path(".config"),
    Path(".local/share"),
    Path(".local/state"),
    Path(".cache"),
    Path(".screenshots"),
)


@dataclass(frozen=True)
class LiveKWin:
    """Connection details and process handle for a test-owned compositor."""

    dbus_address: str
    wayland_display: str
    socket_path: Path
    process: subprocess.Popen[bytes]


def _output_value(output: str, prefix: str) -> str:
    for line in output.splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix)
    pytest.fail(f"missing {prefix!r} in output: {output[:500]}")


def _kwin_pids() -> set[int]:
    pids: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text().strip() == "kwin_wayland":
                pids.add(int(entry.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return pids


def _stderr_excerpt(stderr_file: object) -> str:
    stderr_file.flush()  # type: ignore[attr-defined]
    stderr_file.seek(0)  # type: ignore[attr-defined]
    return stderr_file.read().decode(errors="replace")[:500]  # type: ignore[attr-defined]


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
def _live_kwin() -> Iterator[LiveKWin]:
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


def test_double_start_keeps_the_original_compositor(
    engine: AutomationEngine, start_session: Callable[..., str]
) -> None:
    first_output = start_session()
    first_socket = _output_value(first_output, "Session started. Wayland socket: ")
    pids_after_first_start = _kwin_pids()

    second_output = engine.session_start(screen_width=640, screen_height=480)

    assert second_output == SESSION_ALREADY_RUNNING
    assert _kwin_pids() == pids_after_first_start
    assert (Path(os.environ["XDG_RUNTIME_DIR"]) / first_socket).is_socket()


def test_stop_without_a_session_and_repeated_stop_are_safe(
    engine: AutomationEngine, start_session: Callable[..., str]
) -> None:
    assert engine.session_stop() == "No session running."
    start_session()
    assert engine.session_stop() == "Session stopped."
    assert engine.session_stop() == "No session running."


def test_bad_app_command_raises_a_clear_error(
    engine: AutomationEngine, start_session: Callable[..., str]
) -> None:
    with pytest.raises(FileNotFoundError) as exc_info:
        start_session(BAD_BINARY)

    error = exc_info.value
    assert error.errno == errno.ENOENT
    assert error.filename == BAD_BINARY
    assert "No such file or directory" in str(error)
    assert BAD_BINARY in str(error)
    assert engine.session_stop() == "Session stopped."


def test_isolated_home_cleanup_and_keep_home(
    engine: AutomationEngine, start_session: Callable[..., str]
) -> None:
    output = start_session(isolate_home=True)
    temporary_home = Path(_output_value(output, "Isolated home: "))
    assert temporary_home.is_dir()
    assert all((temporary_home / path).is_dir() for path in XDG_HOME_DIRECTORIES)

    assert engine.session_stop() == "Session stopped."
    assert not temporary_home.exists()

    kept_output = start_session(isolate_home=True, keep_home=True)
    kept_home = Path(_output_value(kept_output, "Isolated home: "))
    try:
        assert kept_home.is_dir()
        assert all((kept_home / path).is_dir() for path in XDG_HOME_DIRECTORIES)
        assert engine.session_stop() == "Session stopped."
        assert kept_home.is_dir()
        assert all((kept_home / path).is_dir() for path in XDG_HOME_DIRECTORIES[:-1])
    finally:
        engine.session_stop()
        shutil.rmtree(kept_home, ignore_errors=True)


def test_session_stop_removes_the_wayland_socket(
    engine: AutomationEngine, start_session: Callable[..., str]
) -> None:
    output = start_session()
    socket_name = _output_value(output, "Session started. Wayland socket: ")
    socket_path = Path(os.environ["XDG_RUNTIME_DIR"]) / socket_name
    assert socket_path.is_socket()

    assert engine.session_stop() == "Session stopped."
    assert not socket_path.exists()


def test_connects_to_the_second_compositor_without_owning_it(
    engine: AutomationEngine, wait_for_app: Callable[[str], str]
) -> None:
    first_engine = AutomationEngine()
    first_output = first_engine.session_start(screen_width=1280, screen_height=800)
    first_display = _output_value(first_output, "Session started. Wayland socket: ")
    first_socket = Path(os.environ["XDG_RUNTIME_DIR"]) / first_display

    try:
        with _live_kwin() as live:
            assert live.wayland_display != first_display
            assert first_socket.is_socket()
            assert live.process.poll() is None
            assert live.socket_path.is_socket()

            output = engine.session_connect(
                dbus_address=live.dbus_address,
                wayland_display=live.wayland_display,
            )
            first_line = output.splitlines()[0]
            assert first_line == (
                "Connected to live KWin session. "
                f"D-Bus: {live.dbus_address}, Wayland: {live.wayland_display}"
            )

            launch_output = engine.launch_app("kcalc")
            assert launch_output.startswith("App launched: kcalc (PID=")
            assert element_count(wait_for_app("kcalc")) > 0

            assert engine.session_stop() == "Disconnected from live session."
            assert live.process.poll() is None
            assert live.socket_path.is_socket()
            assert first_socket.is_socket()

        assert live.process.poll() is not None
        assert not live.socket_path.exists()
        assert first_socket.is_socket()
    finally:
        first_engine.session_stop()

    assert not first_socket.exists()
