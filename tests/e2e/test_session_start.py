"""End-to-end coverage for ``session_start`` (virtual KWin sessions).

Oracles are independent of the tool under test: the process table (``/proc``
argv, environ and states of ``kwin_wayland``, ``dbus-daemon`` and the AT-SPI
launcher), the session bus itself (``NameHasOwner``), raw ``wayland-info``
lines, and the temp/runtime directories on disk.
"""

from __future__ import annotations

import inspect
import itertools
import os
import re
import shlex
import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from _asserts import frame_png
from session_harness import live_kwin

from kwin_mcp.core import SESSION_RUNNING_TEXT, AutomationEngine
from kwin_mcp.models import NO_RETENTION, Retention, ScreenSize, SessionInfo

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

A11Y_BUS_NAME = "org.a11y.Bus"
PROCESS_TIMEOUT_S = 10.0
SCREENSHOT_OPT_IN = "KWIN_MCP_E2E_SCREENSHOT"
# Every process a virtual session's startup can spawn (matched on argv[0]'s basename,
# since /proc/<pid>/comm truncates names such as dbus-run-session to 15 bytes).
SESSION_PROCESS_NAMES = frozenset(
    {
        "dbus-run-session",
        "dbus-daemon",
        "kwin_wayland",
        "at-spi-bus-launcher",
        "at-spi2-registryd",
    }
)
# Temp-dir prefixes a virtual session creates under tempfile.gettempdir().
SESSION_TEMP_PREFIXES = ("kwin-mcp-config-", "kwin-mcp-screenshots-", "kwin-mcp-home-")

# Golden environment of a launched app, from 0.11.0 ``Session.launch_app``
# (src/kwin_mcp/session.py:818-834): host environ plus these values, minus DISPLAY,
# with DBUS_SESSION_BUS_ADDRESS replaced by the session bus.
GOLDEN_APP_ENV_0_11 = {
    "QT_QPA_PLATFORM": "wayland",
    "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
    "QT_ACCESSIBILITY": "1",
}
GOLDEN_APP_ENV_REMOVED_0_11 = ("DISPLAY",)
# Golden environment of the compositor, from 0.11.0 ``Session._build_env``
# (src/kwin_mcp/session.py:1242-1295) and the wrapper's ``env -u ...`` line (1178-1180).
GOLDEN_KWIN_ENV_0_11 = {
    "XDG_SESSION_TYPE": "wayland",
    "XDG_CURRENT_DESKTOP": "KDE",
    "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
    "QT_ACCESSIBILITY": "1",
    "ATSPI_DBUS_IMPLEMENTATION": "dbus-daemon",
    "KWIN_SCREENSHOT_NO_PERMISSION_CHECKS": "1",
    "KWIN_WAYLAND_NO_PERMISSION_CHECKS": "1",
    "KWIN_XKB_DEFAULT_KEYMAP": "1",
    "XKB_DEFAULT_LAYOUT": "us",
}
GOLDEN_KWIN_ENV_REMOVED_0_11 = (
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "QT_QPA_PLATFORM",
    "KDE_FULL_SESSION",
    "KDE_SESSION_VERSION",
    "XKB_DEFAULT_VARIANT",
    "XKB_DEFAULT_OPTIONS",
    "XKB_DEFAULT_RULES",
    "XKB_DEFAULT_MODEL",
)

RETENTION_HOME_NEEDS_ISOLATED = (
    "retention.home=true needs home='isolated'.\nPass home='isolated', or drop retention.home."
)
RETENTION_HOME_ON_CONNECT = (
    "retention.home is not available on session_connect: live sessions use "
    "the host HOME.\nDrop retention.home."
)
A11Y_ACTIVATION_WARNING = (
    "AT-SPI bus activation failed, accessibility tools may be unavailable in this session: "
)


# ── /proc oracles ─────────────────────────────────────────────────────────


def _proc_state(pid: int) -> str | None:
    """Process state letter from /proc/<pid>/stat, or None once the pid is gone."""
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    return stat.rpartition(")")[2].split()[0]


def _is_live(pid: int) -> bool:
    """Whether pid still runs; zombies do not count (the container has no reaper)."""
    return _proc_state(pid) not in (None, "Z", "X")


def _cmdline(pid: int) -> list[str]:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return []
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _environ(pid: int) -> dict[str, str]:
    raw = (Path("/proc") / str(pid) / "environ").read_bytes()
    env: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        key, _, value = entry.decode(errors="replace").partition("=")
        env[key] = value
    return env


def _live_pids(names: Iterable[str]) -> dict[int, str]:
    """Live pids whose argv[0] basename is one of names, mapped to that name."""
    wanted = frozenset(names)
    found: dict[int, str] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        argv = _cmdline(pid)
        if argv and Path(argv[0]).name in wanted and _is_live(pid):
            found[pid] = Path(argv[0]).name
    return found


def _session_kwin_pid(dbus_address: str) -> int:
    """The live kwin_wayland whose session bus is dbus_address."""
    matches = [
        pid
        for pid in _live_pids({"kwin_wayland"})
        if _environ(pid).get("DBUS_SESSION_BUS_ADDRESS") == dbus_address
    ]
    assert len(matches) == 1, f"kwin_wayland on {dbus_address}: {matches}"
    return matches[0]


def _assert_kwin_argv_size(dbus_address: str, width: int, height: int) -> None:
    argv = _cmdline(_session_kwin_pid(dbus_address))
    pairs = list(itertools.pairwise(argv))
    assert ("--width", str(width)) in pairs, argv
    assert ("--height", str(height)) in pairs, argv


def _wait_until(predicate: Callable[[], bool], timeout: float = PROCESS_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _temp_entries() -> set[str]:
    return {
        entry.name
        for entry in Path(tempfile.gettempdir()).iterdir()
        if entry.name.startswith(SESSION_TEMP_PREFIXES)
    }


def _runtime_entries() -> set[str]:
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    return {entry.name for entry in runtime_dir.iterdir() if entry.name.startswith("wayland-mcp-")}


def _socket_path(info: SessionInfo) -> Path:
    return Path(os.environ["XDG_RUNTIME_DIR"]) / info.wayland_socket


def _install_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, script: str) -> None:
    """Put an executable stub for name first on PATH; monkeypatch restores PATH."""
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / name
    stub.write_text(script)
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")


def _a11y_bus_has_owner(engine: AutomationEngine) -> bool:
    reply = engine.dbus_call(
        service="org.freedesktop.DBus",
        path="/org/freedesktop/DBus",
        interface="org.freedesktop.DBus",
        method="NameHasOwner",
        args=[f"string:{A11Y_BUS_NAME}"],
    )
    assert reply.signature == "b", reply
    assert isinstance(reply.reply, bool), reply
    return reply.reply


def _assert_frame_size(engine: AutomationEngine, max_edge: int | None, size: ScreenSize) -> None:
    """A full-workspace frame at the session size, or the container's known ScreenShot2 limit.

    KWin's virtual backend never answers ScreenShot2 in a headless container, so
    without the GPU-backed opt-in the capture must fail as a clean error and leave
    the session running (the established behaviour of the 0.11 suite).
    """
    if os.environ.get(SCREENSHOT_OPT_IN) == "1":
        frame = engine.screen_capture(max_edge=max_edge)
        assert (frame.width, frame.height) == (size.width, size.height), frame
        assert frame.coverage == "full", frame
        frame_png(frame)
        return
    with pytest.raises(RuntimeError) as excinfo:
        engine.screen_capture(max_edge=max_edge)
    assert "ScreenShot2" in str(excinfo.value), excinfo.value
    assert engine.session_info().session is not None


# ── Tests ─────────────────────────────────────────────────────────────────


def test_session_start_has_no_app_command_parameter() -> None:
    parameters = inspect.signature(AutomationEngine.session_start).parameters
    assert "app_command" not in parameters
    assert list(parameters) == [
        "self",
        "screen_width",
        "screen_height",
        "enable_clipboard",
        "home",
        "retention",
    ]


@pytest.mark.qa("QA-GATE-003")
def test_double_start_is_require_no_session_error_and_keeps_compositor(
    engine: AutomationEngine,
) -> None:
    assert SESSION_RUNNING_TEXT == "Session already running.\nCall session_stop first."

    # session_start -> session_start, then session_start -> session_connect.
    info = engine.session_start(screen_width=1280, screen_height=800)
    kwin_pid = _session_kwin_pid(info.dbus_address)
    kwin_pids = set(_live_pids({"kwin_wayland"}))
    before = engine.session_info()
    assert before.session == info

    with pytest.raises(RuntimeError) as excinfo:
        engine.session_start(screen_width=640, screen_height=480)
    assert str(excinfo.value) == SESSION_RUNNING_TEXT
    with pytest.raises(RuntimeError) as excinfo:
        engine.session_connect(dbus_address=info.dbus_address, wayland_display=info.wayland_socket)
    assert str(excinfo.value) == SESSION_RUNNING_TEXT

    assert _is_live(kwin_pid)
    assert set(_live_pids({"kwin_wayland"})) == kwin_pids
    assert _socket_path(info).is_socket()
    assert engine.session_info() == before
    assert engine.session_stop().stopped

    # session_connect -> session_start.
    with live_kwin() as live:
        connected = engine.session_connect(
            dbus_address=live.dbus_address, wayland_display=live.wayland_display
        )
        try:
            assert connected.kind == "live", connected
            kwin_pids = set(_live_pids({"kwin_wayland"}))
            before = engine.session_info()
            with pytest.raises(RuntimeError) as excinfo:
                engine.session_start(screen_width=640, screen_height=480)
            assert str(excinfo.value) == SESSION_RUNNING_TEXT
            assert live.process.poll() is None
            assert set(_live_pids({"kwin_wayland"})) == kwin_pids
            assert live.socket_path.is_socket()
            assert engine.session_info() == before
        finally:
            engine.session_stop()


@pytest.mark.qa("QA-GATE-006")
def test_retention_home_without_isolated_home_is_a_same_call_teaching_error(
    engine: AutomationEngine,
) -> None:
    keep_home = Retention(home=True)
    calls: list[tuple[Callable[[], object], str]] = [
        (lambda: engine.session_start(retention=keep_home), RETENTION_HOME_NEEDS_ISOLATED),
        (
            lambda: engine.session_start(home="host", retention=keep_home),
            RETENTION_HOME_NEEDS_ISOLATED,
        ),
        (lambda: engine.session_connect(retention=keep_home), RETENTION_HOME_ON_CONNECT),
    ]
    for call, expected in calls:
        processes = _live_pids(SESSION_PROCESS_NAMES)
        temp_entries = _temp_entries()
        with pytest.raises(ValueError) as excinfo:
            call()
        assert str(excinfo.value) == expected
        # Rejected before any process or directory exists.
        assert _live_pids(SESSION_PROCESS_NAMES).keys() <= processes.keys()
        assert _temp_entries() == temp_entries
        assert engine.session_info().session is None

    info = engine.session_start(
        screen_width=1280, screen_height=800, home="isolated", retention=keep_home
    )
    assert info.retention == Retention(artifacts=False, home=True), info
    assert info.home_dir is not None, info
    home_dir = Path(info.home_dir)
    assert home_dir.is_dir()
    assert f"Isolated home: {home_dir}" in info.render().splitlines()
    assert "Retention: artifacts=false, home=true" in info.render().splitlines()
    try:
        stopped = engine.session_stop()
        assert stopped.kept.home_dir == str(home_dir), stopped
        assert home_dir.is_dir()
    finally:
        shutil.rmtree(home_dir, ignore_errors=True)


@pytest.mark.qa("QA-SES-001")
def test_session_info_reports_virtual_kind_sockets_screen_and_backend(
    engine: AutomationEngine,
) -> None:
    info = engine.session_start()
    assert info.kind == "virtual"
    assert info.screen == ScreenSize(width=1920, height=1080)
    assert info.clipboard is False
    assert info.retention == NO_RETENTION
    assert info.retention.model_dump() == {"artifacts": False, "home": False}
    assert info.home_dir is None
    assert info.input_backend == "eis"
    assert info.wayland_socket
    assert _socket_path(info).is_socket()
    assert info.dbus_address.startswith("unix:"), info.dbus_address
    text = info.render()
    assert text.startswith(f"Session started. Wayland socket: {info.wayland_socket}\n"), text
    assert "Input backend: KWin EIS" in text.splitlines(), text
    assert "Screen: 1920x1080" in text.splitlines(), text
    assert engine.session_info().session == info
    _assert_kwin_argv_size(info.dbus_address, 1920, 1080)
    _assert_frame_size(engine, 0, ScreenSize(width=1920, height=1080))
    assert engine.session_stop().stopped

    info = engine.session_start(screen_width=1280, screen_height=800)
    assert info.screen == ScreenSize(width=1280, height=800)
    assert "Screen: 1280x800" in info.render().splitlines()
    _assert_kwin_argv_size(info.dbus_address, 1280, 800)
    _assert_frame_size(engine, None, ScreenSize(width=1280, height=800))


def test_custom_screen_geometry_is_reported_by_wayland(engine: AutomationEngine) -> None:
    expected_width = 937
    expected_height = 613
    info = engine.session_start(screen_width=expected_width, screen_height=expected_height)
    assert info.screen == ScreenSize(width=expected_width, height=expected_height)
    assert info.render().startswith("Session started."), info.render()

    wayland_output = "\n".join(engine.wayland_list().lines)
    expected_mode = (
        rf"(?ms)^interface: 'wl_output'.*?^\s+mode:\s*$"
        rf".*?^\s+width: {expected_width} px, height: {expected_height} px,"
        rf".*?^\s+flags:.*\bcurrent\b"
    )
    assert re.search(expected_mode, wayland_output), wayland_output[:1000]


def test_accessibility_bus_is_owned_when_session_start_returns(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    # Qt apps join the accessibility bus only when org.a11y.Bus already has an
    # owner; otherwise the first element_query finds no apps. Asking the bus
    # daemon (no app, no AT-SPI2 query) cannot activate the name itself, so
    # this observes exactly what session_start left behind.
    info = start_session()
    assert info.warnings == [], info.warnings
    assert info.accessibility == "on", info
    assert "Warning:" not in info.render(), info.render()

    assert _a11y_bus_has_owner(engine) is True


def test_session_start_reports_a_failed_accessibility_bus_activation(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The stub stands in for a distro without an org.a11y.Bus service file. The
    # org.kde.KWin ownership probe must pass through to the real dbus-send or
    # start() could never see KWin register.
    real_dbus_send = shutil.which("dbus-send")
    assert real_dbus_send is not None, "dbus-send not found in PATH"
    stub_error = "Error org.freedesktop.DBus.Error.ServiceUnknown: stub has no a11y bus"
    _install_stub(
        tmp_path,
        monkeypatch,
        "dbus-send",
        "#!/bin/sh\n"
        'case "$*" in\n'
        "    *NameHasOwner*)\n"
        f'        exec {shlex.quote(real_dbus_send)} "$@";;\n'
        "esac\n"
        f"echo '{stub_error}' >&2\n"
        "echo 'second stderr line' >&2\n"
        "exit 1\n",
    )

    info = start_session()

    # The session stays usable for everything but accessibility, and each
    # failure reaches the caller as one line carrying the D-Bus error.
    assert info.kind == "virtual"
    assert info.input_backend == "eis"
    assert info.accessibility == "unavailable", info
    assert info.warnings == [
        f"{A11Y_ACTIVATION_WARNING}{stub_error}; set KWIN_MCP_ATSPI_DIRECT_START=1 "
        "to start it directly from its service files",
    ], info.warnings
    text = info.render()
    assert [line for line in text.splitlines() if line.startswith("Warning: ")] == [
        f"Warning: {warning}" for warning in info.warnings
    ], text

    monkeypatch.undo()
    assert _a11y_bus_has_owner(engine) is False


@pytest.mark.qa("QA-SES-004")
def test_startup_warnings_in_session_info(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mask at-spi-bus-launcher through the environment only: the session bus reads
    # $XDG_DATA_HOME/dbus-1/services before the system service dirs, so this
    # org.a11y.Bus service file shadows the distro's and its launcher always fails.
    data_home = tmp_path / "data-home"
    services = data_home / "dbus-1" / "services"
    services.mkdir(parents=True)
    false_bin = shutil.which("false")
    assert false_bin is not None
    (services / "org.a11y.Bus.service").write_text(
        f"[D-BUS Service]\nName={A11Y_BUS_NAME}\nExec={false_bin}\n"
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    launchers_before = _live_pids({"at-spi-bus-launcher"})

    info = start_session()

    assert info.kind == "virtual"
    assert info.input_backend == "eis"
    activation = [w for w in info.warnings if w.startswith(A11Y_ACTIVATION_WARNING)]
    assert len(activation) == 1, info.warnings
    assert "org.freedesktop.DBus.Error.Spawn" in activation[0], activation
    assert info.accessibility == "unavailable", info

    status = engine.session_info()
    assert status.session == info
    status_lines = status.render().splitlines()
    for warning in info.warnings:
        assert f"Warning: {warning}" in info.render().splitlines()
        assert f"Warning: {warning}" in status_lines

    # Process table: no accessibility bus launcher came up for this session.
    assert _live_pids({"at-spi-bus-launcher"}).keys() <= launchers_before.keys()
    assert _a11y_bus_has_owner(engine) is False


@pytest.mark.qa("QA-SES-007")
def test_virtual_session_env_matches_0_11(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Host values the session must strip or override, so the golden rules are observable.
    monkeypatch.setenv("DISPLAY", ":4242")
    monkeypatch.setenv("WAYLAND_DISPLAY", "host-wayland-0")
    monkeypatch.setenv("KDE_FULL_SESSION", "host-value")
    monkeypatch.setenv("KDE_SESSION_VERSION", "5")
    monkeypatch.setenv("XKB_DEFAULT_VARIANT", "dvorak")
    monkeypatch.setenv("QT_ACCESSIBILITY", "0")

    info = start_session()

    # The compositor: _build_env plus the wrapper's env -u line.
    kwin_env = _environ(_session_kwin_pid(info.dbus_address))
    for key, value in GOLDEN_KWIN_ENV_0_11.items():
        assert kwin_env.get(key) == value, (key, kwin_env.get(key))
    for key in GOLDEN_KWIN_ENV_REMOVED_0_11:
        assert key not in kwin_env, (key, kwin_env[key])
    assert kwin_env["DBUS_SESSION_BUS_ADDRESS"] == info.dbus_address
    config_home = Path(kwin_env["XDG_CONFIG_HOME"])
    assert config_home.parent == Path(tempfile.gettempdir()), config_home
    assert config_home.name.startswith("kwin-mcp-config-"), config_home

    # A launched app: launch_app's environment, byte for byte as 0.11 built it.
    host_env = dict(os.environ)
    launched = engine.app_launch("env")
    assert _wait_until(
        lambda: any(
            not app.running and app.pid == launched.pid for app in engine.session_info().apps
        )
    )
    expected = {**host_env, "WAYLAND_DISPLAY": info.wayland_socket, **GOLDEN_APP_ENV_0_11}
    for key in GOLDEN_APP_ENV_REMOVED_0_11:
        expected.pop(key, None)
    expected["DBUS_SESSION_BUS_ADDRESS"] = info.dbus_address
    lines = Path(launched.log_path).read_text().splitlines()
    actual = dict(line.partition("=")[::2] for line in lines)
    assert actual == expected
    assert "DISPLAY" not in actual
    assert actual["WAYLAND_DISPLAY"] == info.wayland_socket
    assert actual["DBUS_SESSION_BUS_ADDRESS"] == info.dbus_address
    assert actual["KDE_FULL_SESSION"] == "host-value"
    assert actual["QT_ACCESSIBILITY"] == "1"


@pytest.mark.qa("QA-SES-008")
def test_failed_startup_rolls_back(
    engine: AutomationEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The product has no compositor-binary hook, so a PATH shim stands in for a
    # kwin_wayland that exits at once.
    _install_stub(tmp_path, monkeypatch, "kwin_wayland", "#!/bin/sh\nexit 1\n")
    processes = _live_pids(SESSION_PROCESS_NAMES)
    temp_entries = _temp_entries()
    runtime_entries = _runtime_entries()

    with pytest.raises(RuntimeError) as excinfo:
        engine.session_start(screen_width=1280, screen_height=800)
    message = str(excinfo.value)
    assert message.startswith("KWin failed to start"), message
    assert "kwin_wayland exited before creating socket" in message, message

    status = engine.session_info()
    assert status.session is None
    assert status.apps == []
    assert status.render() == "No session running."

    # No orphan dbus-daemon / KWin / AT-SPI processes, no leftover directories or sockets.
    assert _wait_until(lambda: _live_pids(SESSION_PROCESS_NAMES).keys() <= processes.keys()), {
        pid: name for pid, name in _live_pids(SESSION_PROCESS_NAMES).items() if pid not in processes
    }
    assert _temp_entries() == temp_entries
    assert _runtime_entries() == runtime_entries
    assert engine.session_stop().stopped is False
