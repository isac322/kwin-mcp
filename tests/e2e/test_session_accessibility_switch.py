"""The ``org.a11y.Status.IsEnabled`` switch in virtual and live sessions.

Firefox exposes no accessibility tree while the switch is off, and a stock
Plasma session leaves it off. Virtual sessions own a private bus, so they
switch it on; a live connection changes it only on request and switches it
back off at ``session_stop``.

``SessionInfo.accessibility`` has four states (S §2.2): ``on``, ``switched_on``,
``off`` and ``unavailable``. Each non-``on`` state carries its remedy line in
``warnings[]`` and in the text. Live-bus reads use ``dbus-send`` directly on
the compositor's bus, never ``kwin_mcp``.
"""

from __future__ import annotations

import contextlib
import os
import select
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from session_harness import (
    LiveKWin,
    _stderr_excerpt,
    _terminate_process_group,
    live_kwin,
)

from kwin_mcp import core
from kwin_mcp import session as session_module
from kwin_mcp.core import AutomationEngine
from kwin_mcp.models import KeptPaths
from kwin_mcp.session import (
    AccessibilityError,
    LiveSession,
    OwnedProcessRegistry,
    accessibility_enabled,
    set_accessibility_enabled,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from kwin_mcp.models import SessionInfo

ON_LINE = "Accessibility: on (org.a11y.Status.IsEnabled)"
SWITCHED_ON_LINE = (
    "Accessibility: switched on for this connection (org.a11y.Status.IsEnabled); "
    "session_stop switches it back off. Firefox follows it at once; an app that "
    "reads it only at startup must be restarted."
)
OFF_LINE = (
    "Accessibility: off (org.a11y.Status.IsEnabled is false), so apps that check it, "
    "such as Firefox, expose no accessibility tree. Reconnect with "
    "enable_accessibility=true to switch it on until session_stop."
)
UNAVAILABLE_LINE = "Accessibility: org.a11y.Status unavailable"
SWITCHED_ON_REMEDY = (
    "switched on for this connection; an app that reads it only at startup must be restarted"
)
OFF_REMEDY = "Reconnect with enable_accessibility=true"


def _accessibility_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("Accessibility: ")]


def _warning_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("Warning: ")]


# ── Oracle: dbus-send on the bus under test, never kwin_mcp ─────────────


def _a11y_property(dbus_address: str, method: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "dbus-send",
            f"--bus={dbus_address}",
            "--print-reply",
            "--reply-timeout=10000",
            "--dest=org.a11y.Bus",
            "/org/a11y/bus",
            f"org.freedesktop.DBus.Properties.{method}",
            "string:org.a11y.Status",
            "string:IsEnabled",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _oracle_is_enabled(dbus_address: str) -> bool:
    result = _a11y_property(dbus_address, "Get")
    assert result.returncode == 0, result.stderr
    value = result.stdout.split()[-2:]
    assert value in (["boolean", "true"], ["boolean", "false"]), result.stdout
    return value[1] == "true"


def _oracle_set(dbus_address: str, enabled: bool) -> None:
    result = _a11y_property(
        dbus_address, "Set", f"variant:boolean:{'true' if enabled else 'false'}"
    )
    assert result.returncode == 0, result.stderr
    assert _oracle_is_enabled(dbus_address) is enabled


@contextlib.contextmanager
def _switch_restored_off(live: LiveKWin) -> Generator[None]:
    """Leave the switch off however the test ends.

    at-spi stores the switch in GSettings, so it outlives this bus: later live
    buses would start with it on.
    """
    try:
        yield
    finally:
        _a11y_property(live.dbus_address, "Set", "variant:boolean:false")


# A session bus with no service directories: org.a11y.Bus is neither running
# nor activatable on it, as on a desktop without at-spi2-core.
_BUS_WITHOUT_A11Y_CONFIG = """\
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:tmpdir=/tmp</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


@contextlib.contextmanager
def _kwin_without_a11y_bus(tmp_path: Path) -> Generator[LiveKWin]:
    """A real ``kwin_wayland --virtual`` on a session bus that lacks org.a11y.Bus."""
    runtime_dir = Path(os.environ["XDG_RUNTIME_DIR"])
    wayland_display = f"wayland-no-a11y-{os.getpid()}-{uuid4().hex}"
    socket_path = runtime_dir / wayland_display
    config = tmp_path / "bus-without-a11y.conf"
    config.write_text(_BUS_WITHOUT_A11Y_CONFIG)
    script = f"""\
set -eu
printf 'DBUS_SESSION_BUS_ADDRESS=%s\\n' "$DBUS_SESSION_BUS_ADDRESS"
env -u WAYLAND_DISPLAY -u QT_QPA_PLATFORM \\
    KWIN_WAYLAND_NO_PERMISSION_CHECKS=1 \\
    kwin_wayland --virtual --no-lockscreen --width 1280 --height 800 \\
    --socket {wayland_display} &
KWIN_PID=$!
trap 'kill "$KWIN_PID" 2>/dev/null || true; wait "$KWIN_PID" 2>/dev/null || true' \\
    EXIT TERM INT HUP
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
    env = {**os.environ, "XDG_SESSION_TYPE": "wayland"}
    env.pop("WAYLAND_DISPLAY", None)
    env.pop("DISPLAY", None)
    stderr_file = tempfile.TemporaryFile()  # noqa: SIM115
    process = subprocess.Popen(
        ["dbus-run-session", f"--config-file={config}", "--", "bash", "-c", script],
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
            readable, _, _ = select.select(
                [process.stdout], [], [], min(deadline - time.monotonic(), 0.5)
            )
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
            msg = (
                "KWin without an accessibility bus failed to start: "
                f"dbus={bool(dbus_address)}, ready={ready}, "
                f"stderr={_stderr_excerpt(stderr_file)}"
            )
            raise RuntimeError(msg)
        yield LiveKWin(dbus_address, wayland_display, socket_path, process)
    finally:
        _terminate_process_group(process)
        if process.stdout is not None:
            process.stdout.close()
        stderr_file.close()
        socket_path.unlink(missing_ok=True)
        socket_path.with_name(f"{socket_path.name}.lock").unlink(missing_ok=True)


# ── Virtual sessions ─────────────────────────────────────────────────────


def test_virtual_session_switches_accessibility_on(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    info = start_session()
    text = info.render()
    assert info.kind == "virtual"
    assert info.accessibility == "on"
    assert info.warnings == [], text
    assert "Session started. Wayland socket: " in text, text
    assert _accessibility_lines(text) == [ON_LINE], text
    assert _warning_lines(text) == [], text

    reply = engine.dbus_call(
        service="org.a11y.Bus",
        path="/org/a11y/bus",
        interface="org.freedesktop.DBus.Properties",
        method="Get",
        args=["string:org.a11y.Status", "string:IsEnabled"],
    )
    assert reply.reply is True, reply
    assert reply.render().splitlines()[0] == "true", reply.render()


def _fresh_bus_switch(set_to: bool | None = None) -> bool:
    """Read the switch on a new, unrelated session bus, optionally setting it first.

    at-spi stores the switch in GSettings, so a value written through the user's
    dconf database is what any later bus, such as the host desktop's, starts with.
    """
    prefix = "dbus-send --session --print-reply --reply-timeout=10000 --dest=org.a11y.Bus "
    prefix += "/org/a11y/bus org.freedesktop.DBus.Properties."
    script = ""
    if set_to is not None:
        value = "true" if set_to else "false"
        script += f"{prefix}Set string:org.a11y.Status string:IsEnabled variant:boolean:{value}"
        script += " >/dev/null && "
    script += f"{prefix}Get string:org.a11y.Status string:IsEnabled"
    result = subprocess.run(
        ["dbus-run-session", "--", "sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout.split()[-1] == "true"


def test_virtual_session_switch_does_not_reach_the_user_settings(
    start_session: Callable[..., SessionInfo],
) -> None:
    """The virtual session's switch does not reach the user's own settings.

    at-spi stores the switch in GSettings; in a virtual session dconf writes it
    under the per-session ``XDG_CONFIG_HOME``, so a new bus elsewhere, such as the
    host desktop's, still starts with it off.
    """
    assert not _fresh_bus_switch(set_to=False)

    info = start_session()
    assert info.kind == "virtual"
    assert info.accessibility == "on", info.render()

    assert not _fresh_bus_switch(), "the virtual session switched the user's setting on"


def test_virtual_session_reports_a_switch_it_could_not_set(
    start_session: Callable[..., SessionInfo], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only IsEnabled calls are refused; every other call, including the KWin
    # ownership probes, reaches the real dbus-send.
    real_dbus_send = shutil.which("dbus-send")
    assert real_dbus_send is not None, "dbus-send not found in PATH"
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    stub = stub_dir / "dbus-send"
    stub.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "    *string:IsEnabled*)\n"
        "        echo 'Error org.freedesktop.DBus.Error.AccessDenied: stub refuses' >&2\n"
        "        exit 1;;\n"
        "esac\n"
        f'exec {shlex.quote(real_dbus_send)} "$@"\n'
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")

    info = start_session()
    text = info.render()

    assert "Session started. Wayland socket: " in text, text
    switch_warning = (
        "AT-SPI accessibility could not be switched on, apps that check it (such as"
        " Firefox) may expose no accessibility tree: Error"
        " org.freedesktop.DBus.Error.AccessDenied: stub refuses"
    )
    # The read-back is refused by the same stub, so the state is unavailable.
    read_warning = (
        "org.a11y.Status unavailable (org.a11y.Bus call failed: Error"
        " org.freedesktop.DBus.Error.AccessDenied: stub refuses)"
    )
    assert info.accessibility == "unavailable", text
    assert info.warnings == [switch_warning, read_warning], text
    assert _accessibility_lines(text) == [UNAVAILABLE_LINE], text
    assert _warning_lines(text) == [f"Warning: {switch_warning}", f"Warning: {read_warning}"], text


# ── Live connections ─────────────────────────────────────────────────────


def test_live_connect_reports_the_switch_off_and_leaves_it_off(engine: AutomationEngine) -> None:
    with live_kwin() as live, _switch_restored_off(live):
        assert not _oracle_is_enabled(live.dbus_address)

        info = engine.session_connect(
            dbus_address=live.dbus_address, wayland_display=live.wayland_display
        )
        text = info.render()
        assert info.accessibility == "off", text
        assert info.warnings == [OFF_REMEDY], text
        assert _accessibility_lines(text) == [OFF_LINE], text
        assert _warning_lines(text) == [f"Warning: {OFF_REMEDY}"], text
        assert not _oracle_is_enabled(live.dbus_address)

        stopped = engine.session_stop()
        assert stopped.kind == "live"
        assert stopped.render() == "Disconnected from live session."
        assert not _oracle_is_enabled(live.dbus_address)


def test_live_connect_switches_accessibility_on_until_session_stop(
    engine: AutomationEngine,
) -> None:
    with live_kwin() as live, _switch_restored_off(live):
        info = engine.session_connect(
            dbus_address=live.dbus_address,
            wayland_display=live.wayland_display,
            enable_accessibility=True,
        )
        text = info.render()
        assert info.accessibility == "switched_on", text
        assert info.warnings == [SWITCHED_ON_REMEDY], text
        assert _accessibility_lines(text) == [SWITCHED_ON_LINE], text
        assert _oracle_is_enabled(live.dbus_address)

        stopped = engine.session_stop()
        assert stopped.render() == "Disconnected from live session."
        assert not _oracle_is_enabled(live.dbus_address)


def test_live_connect_leaves_a_switch_that_was_already_on(engine: AutomationEngine) -> None:
    with live_kwin() as live, _switch_restored_off(live):
        _oracle_set(live.dbus_address, True)

        info = engine.session_connect(
            dbus_address=live.dbus_address,
            wayland_display=live.wayland_display,
            enable_accessibility=True,
        )
        text = info.render()
        assert info.accessibility == "on", text
        assert info.warnings == [], text
        assert _accessibility_lines(text) == [ON_LINE], text

        assert engine.session_stop().render() == "Disconnected from live session."
        assert _oracle_is_enabled(live.dbus_address)


@pytest.mark.parametrize("enable", [False, True])
def test_live_connect_reports_an_unreadable_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enable: bool
) -> None:
    def unavailable(*_args: object) -> bool:
        raise AccessibilityError("no accessibility bus")

    monkeypatch.setattr(core, "accessibility_enabled", unavailable)
    monkeypatch.setattr(LiveSession, "enable_accessibility", unavailable)
    session = LiveSession("unix:path=/nonexistent", "wayland-x", tmp_path)
    warnings: list[str] = []

    state = AutomationEngine._live_accessibility(
        session, "unix:path=/nonexistent", enable=enable, warnings=warnings
    )

    assert state == "unavailable"
    assert warnings == ["org.a11y.Status unavailable (no accessibility bus)"]


@pytest.mark.qa("QA-SES-012")
def test_accessibility_four_states_with_remedy_lines(
    engine: AutomationEngine, tmp_path: Path
) -> None:
    with live_kwin() as live, _switch_restored_off(live):
        # (a) IsEnabled already true: on, no remedy.
        _oracle_set(live.dbus_address, True)
        info = engine.session_connect(
            dbus_address=live.dbus_address, wayland_display=live.wayland_display
        )
        text = info.render()
        assert info.accessibility == "on", text
        assert info.warnings == [], text
        assert _accessibility_lines(text) == [ON_LINE], text
        assert _warning_lines(text) == [], text
        engine.session_stop()
        assert _oracle_is_enabled(live.dbus_address)

        # (b) false + enable_accessibility=false: off, reconnect remedy.
        _oracle_set(live.dbus_address, False)
        info = engine.session_connect(
            dbus_address=live.dbus_address,
            wayland_display=live.wayland_display,
            enable_accessibility=False,
        )
        text = info.render()
        assert info.accessibility == "off", text
        assert info.warnings == [OFF_REMEDY], text
        assert _accessibility_lines(text) == [OFF_LINE], text
        assert _warning_lines(text) == [f"Warning: {OFF_REMEDY}"], text
        assert not _oracle_is_enabled(live.dbus_address)
        engine.session_stop()

        # (c) false + enable_accessibility=true: switched_on, restart remedy.
        info = engine.session_connect(
            dbus_address=live.dbus_address,
            wayland_display=live.wayland_display,
            enable_accessibility=True,
        )
        text = info.render()
        assert info.accessibility == "switched_on", text
        assert info.warnings == [SWITCHED_ON_REMEDY], text
        assert _accessibility_lines(text) == [SWITCHED_ON_LINE], text
        assert _warning_lines(text) == [f"Warning: {SWITCHED_ON_REMEDY}"], text
        assert _oracle_is_enabled(live.dbus_address)
        engine.session_stop()
        assert not _oracle_is_enabled(live.dbus_address)

    # (d) A bus without org.a11y.Status: unavailable, the bus error as remedy.
    with _kwin_without_a11y_bus(tmp_path) as bare:
        probe = _a11y_property(bare.dbus_address, "Get")
        assert probe.returncode != 0, probe.stdout
        assert "org.freedesktop.DBus.Error.ServiceUnknown" in probe.stderr, probe.stderr

        info = engine.session_connect(
            dbus_address=bare.dbus_address,
            wayland_display=bare.wayland_display,
            enable_accessibility=True,
        )
        text = info.render()
        assert info.accessibility == "unavailable", text
        assert len(info.warnings) == 1, text
        warning = info.warnings[0]
        assert warning.startswith(
            "org.a11y.Status unavailable (org.a11y.Bus call failed: Error "
            "org.freedesktop.DBus.Error.ServiceUnknown: "
        ), text
        assert warning.endswith(")"), text
        assert _accessibility_lines(text) == [UNAVAILABLE_LINE], text
        assert _warning_lines(text) == [f"Warning: {warning}"], text
        assert engine.session_stop().kind == "live"


@pytest.mark.qa("QA-SES-013")
def test_accessibility_restored_after_stop(engine: AutomationEngine) -> None:
    with live_kwin() as live, _switch_restored_off(live):
        _oracle_set(live.dbus_address, False)

        info = engine.session_connect(
            dbus_address=live.dbus_address,
            wayland_display=live.wayland_display,
            enable_accessibility=True,
        )
        assert info.accessibility == "switched_on", info.render()
        assert _oracle_is_enabled(live.dbus_address)

        stopped = engine.session_stop()
        assert stopped.stopped
        assert stopped.kind == "live"
        assert stopped.render() == "Disconnected from live session."
        assert not _oracle_is_enabled(live.dbus_address)


# ── Registry restore paths ───────────────────────────────────────────────


def _record_switches(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    calls: list[tuple[str, bool]] = []

    def record(dbus_address: str, enabled: bool) -> None:
        calls.append((dbus_address, enabled))

    monkeypatch.setattr(session_module, "set_accessibility_enabled", record)
    return calls


def test_stop_survives_a_switch_it_cannot_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bus that went away leaves nothing to restore; stop still completes."""
    screenshots = tmp_path / "screenshots"
    screenshots.mkdir()
    address = "unix:path=/nonexistent/bus"
    registry = OwnedProcessRegistry()
    monkeypatch.setattr(session_module, "process_registry", registry)
    with monkeypatch.context() as patched:
        _record_switches(patched)
        registry.switch_accessibility_on(address)
    session = LiveSession(address, "wayland-x", screenshots)

    kept = session.stop()

    assert kept == KeptPaths()
    assert not session.is_running
    assert not screenshots.exists()
    # The failed restore still drops the record: nothing is left for the exit path.
    calls = _record_switches(monkeypatch)
    registry.terminate_all()
    assert calls == []


def test_terminate_all_restores_a_switch_it_turned_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The busy-exit path turns the switch back off once; a later stop does not repeat it."""
    calls = _record_switches(monkeypatch)
    registry = OwnedProcessRegistry()

    registry.switch_accessibility_on("unix:path=/run/bus-a")
    registry.close()
    registry.terminate_all()
    registry.restore_accessibility("unix:path=/run/bus-a")

    assert calls == [("unix:path=/run/bus-a", True), ("unix:path=/run/bus-a", False)]


def test_exit_waits_for_a_restore_in_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exit that starts while a stop is restoring the switch waits for the call.

    Otherwise the exit would find the record already taken by the stop and could
    exit before the stop's call is sent, leaving the switch on.
    """
    address = "unix:path=/run/bus-a"
    calls: list[tuple[str, bool]] = []
    restoring = threading.Event()
    release = threading.Event()

    def setter(dbus_address: str, enabled: bool) -> None:
        if not enabled:
            restoring.set()
            release.wait(5)
        calls.append((dbus_address, enabled))

    monkeypatch.setattr(session_module, "set_accessibility_enabled", setter)
    registry = OwnedProcessRegistry()
    registry.switch_accessibility_on(address)
    stop = threading.Thread(target=registry.restore_accessibility, args=(address,))
    stop.start()
    assert restoring.wait(5)

    exited = threading.Event()

    def exit_cleanup() -> None:
        registry.close()
        registry.terminate_all()
        exited.set()

    exit_thread = threading.Thread(target=exit_cleanup)
    exit_thread.start()
    try:
        assert not exited.wait(0.3), "the exit did not wait for the restore in progress"
    finally:
        release.set()
        stop.join(5)
        exit_thread.join(5)
    assert exited.is_set()
    assert calls == [(address, True), (address, False)]


def test_restore_leaves_a_switch_it_did_not_turn_on(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_switches(monkeypatch)
    registry = OwnedProcessRegistry()

    registry.restore_accessibility("unix:path=/run/bus-a")
    registry.terminate_all()

    assert calls == []


def test_switch_on_is_refused_after_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the exit path has started, a connect can no longer turn the switch on."""
    calls = _record_switches(monkeypatch)
    registry = OwnedProcessRegistry()
    registry.close()

    with pytest.raises(RuntimeError, match="refusing to switch accessibility on"):
        registry.switch_accessibility_on("unix:path=/run/bus-a")

    registry.terminate_all()
    assert calls == []


@pytest.mark.parametrize("path", ["stop", "exit"])
def test_a_switch_that_may_have_applied_is_restored(
    path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``Set`` whose reply never arrived may still have applied, so the record
    taken before the call is kept when the call fails: the switch is turned back
    off by ``session_stop`` and, when a tool is still running, by the busy-exit
    registry cleanup.
    """
    address = "unix:path=/run/bus-a"
    desktop = {address: False}

    def setter(dbus_address: str, enabled: bool) -> None:
        desktop[dbus_address] = enabled
        if enabled:
            raise AccessibilityError("reply lost")

    monkeypatch.setattr(session_module, "set_accessibility_enabled", setter)
    registry = OwnedProcessRegistry()
    monkeypatch.setattr(session_module, "process_registry", registry)
    with pytest.raises(AccessibilityError, match="reply lost"):
        registry.switch_accessibility_on(address)
    assert desktop[address] is True

    if path == "stop":
        screenshots = tmp_path / "screenshots"
        screenshots.mkdir()
        session = LiveSession(address, "wayland-x", screenshots)
        session.stop()
    else:
        registry.close()
        registry.terminate_all()

    assert desktop[address] is False


# ── The bounded dbus-send helpers ────────────────────────────────────────


def _stalling_dbus_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout_s: float = 0.5
) -> Path:
    """Stand a never-answering ``dbus-send`` first on PATH; return its pid file.

    Each spawned stub appends its own pid and ``exec``s ``sleep``, so the pid
    file's lines are the long-lived processes a bounded call must have killed.
    """
    real_sleep = shutil.which("sleep")
    assert real_sleep is not None, "sleep not found in PATH"
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    pids_file = tmp_path / "dbus-send-pids"
    pids_file.touch()
    stub = stub_dir / "dbus-send"
    stub.write_text(f"#!/bin/sh\necho $$ >> {shlex.quote(str(pids_file))}\nexec {real_sleep} 60\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(session_module, "_A11Y_CALL_TIMEOUT_S", timeout_s)
    return pids_file


def test_a11y_helpers_give_up_on_a_stalled_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bus that never answers fails ``AccessibilityError`` within the bound,
    and the timed-out ``dbus-send`` is killed instead of left running.
    """
    pids_file = _stalling_dbus_send(tmp_path, monkeypatch)
    address = "unix:path=/nonexistent/bus"

    started = time.monotonic()
    with pytest.raises(AccessibilityError, match="timed out"):
        accessibility_enabled(address)
    with pytest.raises(AccessibilityError, match="timed out"):
        set_accessibility_enabled(address, True)
    assert time.monotonic() - started < 5.0

    pids = [int(line) for line in pids_file.read_text().splitlines()]
    assert len(pids) == 2
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_the_registry_cleanup_survives_a_stalled_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``switch_accessibility_on`` under the registry lock stays bounded too, and
    the recorded switch still gets a bounded restore attempt on the exit path.
    """
    pids_file = _stalling_dbus_send(tmp_path, monkeypatch)
    registry = OwnedProcessRegistry()

    started = time.monotonic()
    with pytest.raises(AccessibilityError, match="timed out"):
        registry.switch_accessibility_on("unix:path=/nonexistent/bus")
    registry.close()
    registry.terminate_all()
    assert time.monotonic() - started < 5.0

    for line in pids_file.read_text().splitlines():
        with pytest.raises(ProcessLookupError):
            os.kill(int(line), 0)


def _answering_dbus_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reply: str = "",
    *,
    stderr: str = "",
    exit_code: int = 0,
) -> Path:
    """Stand a ``dbus-send`` stub first on PATH; return the file argv is logged to."""
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    argv_file = tmp_path / "dbus-send-argv"
    (tmp_path / "reply").write_text(reply)
    (tmp_path / "stderr").write_text(stderr)
    stub = stub_dir / "dbus-send"
    stub.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(argv_file))}\n"
        f"cat {shlex.quote(str(tmp_path / 'reply'))}\n"
        f"cat {shlex.quote(str(tmp_path / 'stderr'))} >&2\n"
        f"exit {exit_code}\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    return argv_file


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        # dbus-send --print-reply=literal for Properties.Get's variant<boolean>.
        ("   variant       boolean true\n", True),
        ("   variant       boolean false\n", False),
    ],
)
def test_accessibility_enabled_reads_a_literal_reply(
    reply: str, expected: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv_file = _answering_dbus_send(tmp_path, monkeypatch, reply)

    assert accessibility_enabled("unix:path=/run/bus-a") is expected
    reply_timeout_ms = max(1, int(session_module._A11Y_CALL_TIMEOUT_S * 1000))
    assert argv_file.read_text().splitlines() == [
        "--bus=unix:path=/run/bus-a",
        "--print-reply=literal",
        f"--reply-timeout={reply_timeout_ms}",
        "--dest=org.a11y.Bus",
        "/org/a11y/bus",
        "org.freedesktop.DBus.Properties.Get",
        "string:org.a11y.Status",
        "string:IsEnabled",
    ]


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "method return time=1.000 sender=:1.2 -> destination=:1.3\n",
        '   variant       string "yes"\n',
        "   variant       int32 1\n",
        "garbage\n",
    ],
)
def test_accessibility_enabled_rejects_an_unreadable_reply(
    reply: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _answering_dbus_send(tmp_path, monkeypatch, reply)

    with pytest.raises(AccessibilityError, match="unreadable IsEnabled value"):
        accessibility_enabled("unix:path=/run/bus-a")


def test_set_accessibility_enabled_sends_a_variant_boolean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv_file = _answering_dbus_send(tmp_path, monkeypatch)

    set_accessibility_enabled("unix:path=/run/bus-a", True)

    reply_timeout_ms = max(1, int(session_module._A11Y_CALL_TIMEOUT_S * 1000))
    assert argv_file.read_text().splitlines() == [
        "--bus=unix:path=/run/bus-a",
        "--print-reply=literal",
        f"--reply-timeout={reply_timeout_ms}",
        "--dest=org.a11y.Bus",
        "/org/a11y/bus",
        "org.freedesktop.DBus.Properties.Set",
        "string:org.a11y.Status",
        "string:IsEnabled",
        "variant:boolean:true",
    ]


def test_a11y_helpers_report_a_bus_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dbus-send's own error (nonzero exit) is one ``AccessibilityError`` line."""
    _answering_dbus_send(
        tmp_path,
        monkeypatch,
        stderr="Error org.freedesktop.DBus.Error.ServiceUnknown: The name org.a11y.Bus "
        "was not provided by any .service files\n",
        exit_code=1,
    )

    with pytest.raises(AccessibilityError, match="ServiceUnknown"):
        accessibility_enabled("unix:path=/nonexistent/bus")
    with pytest.raises(AccessibilityError, match="ServiceUnknown"):
        set_accessibility_enabled("unix:path=/nonexistent/bus", True)
