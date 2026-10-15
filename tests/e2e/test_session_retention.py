"""What survives a session: ``home`` and ``retention`` on start, connect, stop and exit.

``retention.artifacts`` alone decides the artifacts dir (captured frames and app
logs), ``retention.home`` alone the isolated home (it needs ``home='isolated'``).
``session_stop`` reports the kept dirs in ``SessionStopResult.kept``; every other
dir the session created is removed, and the server's exit path honours the same
retention. Without an isolated home KWin and the apps still get a throwaway
``XDG_CONFIG_HOME`` seeded with a wallet-disabling ``kwalletrc``.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from kwin_oracle import KWinOracle
from probe_session import PROBE_COMMAND, of_kind, probe_env, read_events
from session_harness import live_kwin
from test_exit_cleanup_virtual import (
    SERVER_EXIT_TIMEOUT_SECONDS,
    _assert_session_gone,
    _LineReader,
    _rpc,
    _session_pgid,
    _spawn_with_stderr,
    _start_server_session,
    _stdin,
    _stdout,
    _structured,
)

from kwin_mcp.models import Retention
from kwin_mcp.session import Session

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import AppLaunchResult, SessionInfo

PROCESS_EXIT_TIMEOUT_SECONDS = 5.0
SCREENSHOT_OPT_IN = "KWIN_MCP_E2E_SCREENSHOT"
XDG_HOME_DIRECTORIES = (
    Path(".config"),
    Path(".local/share"),
    Path(".local/state"),
    Path(".cache"),
)
KWALLETRC = "[Wallet]\nEnabled=false\nFirst Use=false\nLaunch Manager=false\n"
HOST_HOME_RETENTION_ERROR = (
    "retention.home=true needs home='isolated'.\nPass home='isolated', or drop retention.home."
)
SERIALIZED_OUTCOME = (
    "kwin-mcp: exit cleanup: serialized session_stop path (completed on the tool thread)"
)


def _capture_enabled() -> bool:
    """Whether frame capture is expected to work (KWin's virtual backend never answers)."""
    return os.environ.get(SCREENSHOT_OPT_IN) == "1"


def _wait_for_app_log(engine: AutomationEngine, pid: int, expected: list[str]) -> list[str]:
    deadline = time.monotonic() + PROCESS_EXIT_TIMEOUT_SECONDS
    lines: list[str] = []
    while time.monotonic() < deadline:
        lines = engine.app_log(pid, last_n_lines=0).lines
        if lines == expected:
            return lines
        time.sleep(0.05)
    pytest.fail(f"app {pid} did not log {expected!r}: {lines!r}")


# ---------------------------------------------------------------------------
# Virtual sessions (engine)
# ---------------------------------------------------------------------------


def test_isolated_home_cleanup_and_keep_home(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    info = start_session(home="isolated")
    assert info.home_dir is not None, info
    temporary_home = Path(info.home_dir)
    assert temporary_home.is_dir()
    assert all((temporary_home / path).is_dir() for path in XDG_HOME_DIRECTORIES)

    stopped = engine.session_stop()
    assert stopped.stopped and stopped.kept.home_dir is None, stopped
    assert not temporary_home.exists()

    kept_info = start_session(home="isolated", retention=Retention(home=True))
    assert kept_info.home_dir is not None, kept_info
    kept_home = Path(kept_info.home_dir)
    try:
        assert kept_home.is_dir()
        assert all((kept_home / path).is_dir() for path in XDG_HOME_DIRECTORIES)
        stopped = engine.session_stop()
        assert stopped.stopped and stopped.kept.home_dir == str(kept_home), stopped
        assert kept_home.is_dir()
        assert all((kept_home / path).is_dir() for path in XDG_HOME_DIRECTORIES)
    finally:
        engine.session_stop()
        shutil.rmtree(kept_home, ignore_errors=True)


@pytest.mark.parametrize("keep_screenshots", [False, True], ids=["drop-shots", "keep-shots"])
@pytest.mark.parametrize("keep_home", [False, True], ids=["drop-home", "keep-home"])
@pytest.mark.parametrize("isolate_home", [False, True], ids=["host-home", "isolated-home"])
def test_virtual_session_artifact_cleanup_respects_retention(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    isolate_home: bool,
    keep_home: bool,
    keep_screenshots: bool,
) -> None:
    """retention.artifacts alone decides the artifacts/app-log dir, retention.home the home.

    ``retention.home`` without an isolated home has nothing to keep: session_start
    refuses it before any process starts (0.11 ignored ``keep_home`` there).
    """
    retention = Retention(artifacts=keep_screenshots, home=keep_home)
    if keep_home and not isolate_home:
        with pytest.raises(ValueError) as excinfo:
            start_session(home="host", retention=retention)
        assert str(excinfo.value) == HOST_HOME_RETENTION_ERROR
        assert engine.session_info().session is None
        return

    screenshot_dir: Path | None = None
    home: Path | None = None
    try:
        info = start_session(home="isolated" if isolate_home else "host", retention=retention)
        assert info.retention == retention, info
        if isolate_home:
            assert info.home_dir is not None, info
            home = Path(info.home_dir)
        else:
            assert info.home_dir is None, info
        launched = launch("printf virtual-retention-payload")
        log_path = Path(launched.log_path)
        screenshot_dir = log_path.parent
        assert screenshot_dir == Path(info.artifacts_dir), (launched, info)
        if home is not None:
            assert not screenshot_dir.is_relative_to(home), (screenshot_dir, home)
        assert _wait_for_app_log(engine, launched.pid, ["virtual-retention-payload"]) == [
            "virtual-retention-payload"
        ]
        assert log_path.is_file()

        stopped = engine.session_stop()
        assert stopped.stopped and stopped.kind == "virtual", stopped
        assert stopped.kept.artifacts_dir == (str(screenshot_dir) if keep_screenshots else None)
        assert stopped.kept.home_dir == (str(home) if keep_home else None), stopped
        assert log_path.exists() is keep_screenshots
        assert screenshot_dir.exists() is keep_screenshots
        if home is not None:
            assert home.exists() is keep_home
    finally:
        engine.session_stop()
        if screenshot_dir is not None:
            shutil.rmtree(screenshot_dir, ignore_errors=True)
        if home is not None:
            shutil.rmtree(home, ignore_errors=True)


def test_isolated_home_retains_app_logs_when_both_retention_flags_are_enabled(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
) -> None:
    info = start_session(home="isolated", retention=Retention(artifacts=True, home=True))
    assert info.home_dir is not None, info
    home = Path(info.home_dir)
    log_path: Path | None = None
    try:
        launched = launch("printf isolated-home-retention-payload")
        log_path = Path(launched.log_path)
        assert not log_path.is_relative_to(home), (log_path, home)
        assert _wait_for_app_log(engine, launched.pid, ["isolated-home-retention-payload"]) == [
            "isolated-home-retention-payload"
        ]
        assert log_path.is_file()

        stopped = engine.session_stop()
        assert stopped.kept.home_dir == str(home), stopped
        assert stopped.kept.artifacts_dir == str(log_path.parent), stopped
        assert home.is_dir()
        assert log_path.is_file()
    finally:
        engine.session_stop()
        shutil.rmtree(home, ignore_errors=True)
        if log_path is not None:
            shutil.rmtree(log_path.parent, ignore_errors=True)


def _env_report(engine: AutomationEngine, launched: AppLaunchResult) -> list[str]:
    """The ``NAME=value`` lines the launched env-reporting shell wrote to its log."""
    deadline = time.monotonic() + PROCESS_EXIT_TIMEOUT_SECONDS
    lines: list[str] = []
    while time.monotonic() < deadline:
        lines = engine.app_log(launched.pid, last_n_lines=0).lines
        if lines and lines[-1] == "END":
            return lines[:-1]
        time.sleep(0.05)
    pytest.fail(f"app {launched.pid} never finished its env report: {lines!r}")


_ENV_REPORT_COMMAND = "sh -c " + shlex.quote(
    'echo "HOME=$HOME"; echo "XDG_CONFIG_HOME=$XDG_CONFIG_HOME"; '
    'echo "XDG_DATA_HOME=$XDG_DATA_HOME"; echo "XDG_CACHE_HOME=$XDG_CACHE_HOME"; '
    "echo END"
)


@pytest.mark.qa("QA-SES-002")
def test_isolated_home_xdg_dirs_and_kwalletrc(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
) -> None:
    """home='isolated': a temp HOME with its XDG dirs and a wallet-disabling kwalletrc."""
    info = start_session(home="isolated")
    assert info.home_dir is not None, info
    home = Path(info.home_dir)
    assert home.is_dir() and home.name.startswith("kwin-mcp-home-"), home
    assert not Path.home().resolve().is_relative_to(home.resolve()), home

    launched = launch(_ENV_REPORT_COMMAND)
    report = dict(line.split("=", 1) for line in _env_report(engine, launched))
    # The log file read from disk says the same as app_log.
    assert Path(launched.log_path).read_text().splitlines()[:-1] == [
        f"{name}={value}" for name, value in report.items()
    ]
    assert report["HOME"] == str(home), report
    assert report["XDG_CONFIG_HOME"] == str(home / ".config"), report
    assert report["XDG_DATA_HOME"] == str(home / ".local" / "share"), report
    assert report["XDG_CACHE_HOME"] == str(home / ".cache"), report
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        assert Path(report[name]).is_dir(), (name, report[name])
    assert (home / ".config" / "kwalletrc").read_text() == KWALLETRC


@pytest.mark.qa("QA-SES-003")
def test_host_home_still_temp_xdg_config_with_kwalletrc(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
) -> None:
    """home='host': HOME stays the host's; KWin gets a temp XDG_CONFIG_HOME with kwalletrc.

    S §5.2 #119 keeps 0.11 unchanged: the throwaway config dir is the compositor's
    (``kwin_wayland`` reads kwalletrc from it), while launched apps keep the host's
    HOME and XDG_CONFIG_HOME. The dir is removed on stop.
    """
    info = start_session()
    assert info.home_dir is None, info

    launched = launch(_ENV_REPORT_COMMAND)
    report = dict(line.split("=", 1) for line in _env_report(engine, launched))
    assert report["HOME"] == os.environ["HOME"], report
    assert report["XDG_CONFIG_HOME"] == os.environ.get("XDG_CONFIG_HOME", ""), report

    kwin_env = _environ(_session_kwin_pid(engine))
    assert kwin_env.get("HOME") == os.environ["HOME"], kwin_env.get("HOME")
    config_home = Path(kwin_env.get("XDG_CONFIG_HOME", ""))
    assert config_home.name.startswith("kwin-mcp-config-"), config_home
    assert not config_home.is_relative_to(Path(os.environ["HOME"])), config_home
    assert (config_home / "kwalletrc").read_text() == KWALLETRC

    stopped = engine.session_stop()
    assert stopped.kept.home_dir is None, stopped
    assert not config_home.exists(), f"the temp XDG_CONFIG_HOME survived the stop: {config_home}"


def _environ(pid: int) -> dict[str, str]:
    raw = (Path("/proc") / str(pid) / "environ").read_bytes()
    pairs = (item.decode(errors="replace").partition("=") for item in raw.split(b"\0") if item)
    return {name: value for name, _, value in pairs}


def _session_kwin_pid(engine: AutomationEngine) -> int:
    """The ``kwin_wayland`` in the virtual session's process group."""
    session = engine._session
    assert isinstance(session, Session) and session._process is not None, session
    pgid = session._process.pid
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip()
            fields = (entry / "stat").read_text().rpartition(")")[2].split()
        except OSError:
            continue
        if comm == "kwin_wayland" and int(fields[2]) == pgid:
            pids.append(int(entry.name))
    assert len(pids) == 1, f"expected one kwin_wayland in group {pgid}, got {pids}"
    return pids[0]


@pytest.mark.qa("QA-SES-017")
@pytest.mark.parametrize(
    ("home", "retention"),
    [
        ("host", Retention()),
        ("host", Retention(artifacts=True)),
        ("isolated", Retention(home=True)),
        ("isolated", Retention(artifacts=True, home=True)),
    ],
    ids=["nothing", "artifacts", "home", "both"],
)
def test_retention_matrix_on_stop(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    home: str,
    retention: Retention,
) -> None:
    """``kept`` names a dir exactly when it is retained; the rest is deleted on stop.

    The artifacts dir holds the app log (and, where capture works, the frame).
    """
    info = start_session(home=home, retention=retention)
    artifacts_dir = Path(info.artifacts_dir)
    home_dir = Path(info.home_dir) if info.home_dir is not None else None
    assert (home_dir is not None) is (home == "isolated"), info
    try:
        launched = launch("printf matrix-payload")
        assert _wait_for_app_log(engine, launched.pid, ["matrix-payload"]) == ["matrix-payload"]
        artifacts = [Path(launched.log_path)]
        if _capture_enabled():
            frame = engine.screen_capture()
            assert Path(frame.path).parent == artifacts_dir, frame
            artifacts.append(Path(frame.path))
        assert all(path.is_file() for path in artifacts), artifacts

        stopped = engine.session_stop()
        assert stopped.stopped, stopped
        assert stopped.kept.artifacts_dir == (
            str(artifacts_dir) if retention.artifacts else None
        ), stopped
        assert stopped.kept.home_dir == (
            str(home_dir) if retention.home and home_dir is not None else None
        ), stopped
        assert artifacts_dir.exists() is retention.artifacts
        assert all(path.exists() is retention.artifacts for path in artifacts), artifacts
        if home_dir is not None:
            assert home_dir.exists() is retention.home
    finally:
        engine.session_stop()
        shutil.rmtree(artifacts_dir, ignore_errors=True)
        if home_dir is not None:
            shutil.rmtree(home_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Live sessions (engine)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("keep_screenshots", [False, True], ids=["remove", "retain"])
def test_live_session_artifact_cleanup_respects_retention(
    engine: AutomationEngine,
    keep_screenshots: bool,
) -> None:
    screenshot_dir: Path | None = None
    with live_kwin() as live:
        try:
            info = engine.session_connect(
                dbus_address=live.dbus_address,
                wayland_display=live.wayland_display,
                retention=Retention(artifacts=keep_screenshots),
            )
            assert info.kind == "live" and info.home_dir is None, info

            launched = engine.app_launch("sleep 300")
            log_path = Path(launched.log_path)
            screenshot_dir = log_path.parent
            assert screenshot_dir == Path(info.artifacts_dir), (launched, info)
            assert log_path.is_file()

            stopped = engine.session_stop()
            assert stopped.stopped and stopped.kind == "live", stopped
            assert stopped.render().startswith("Disconnected from live session."), stopped
            assert stopped.kept.artifacts_dir == (
                str(screenshot_dir) if keep_screenshots else None
            ), stopped
            assert log_path.exists() is keep_screenshots
            assert screenshot_dir.exists() is keep_screenshots
            assert live.process.poll() is None
            assert live.socket_path.is_socket()
        finally:
            engine.session_stop()
            if screenshot_dir is not None:
                shutil.rmtree(screenshot_dir, ignore_errors=True)


@pytest.mark.qa("QA-SES-015")
def test_live_artifacts_retention(engine: AutomationEngine) -> None:
    """session_connect(retention={artifacts:true}) keeps the artifacts dir after stop.

    ``kept.artifacts_dir`` names the dir and its files survive: the app log always,
    the captured frame where capture works.
    """
    artifacts_dir: Path | None = None
    with live_kwin() as live:
        try:
            info = engine.session_connect(
                dbus_address=live.dbus_address,
                wayland_display=live.wayland_display,
                retention=Retention(artifacts=True),
            )
            assert info.retention == Retention(artifacts=True), info
            artifacts_dir = Path(info.artifacts_dir)
            launched = engine.app_launch("printf live-retained")
            assert _wait_for_app_log(engine, launched.pid, ["live-retained"]) == ["live-retained"]
            artifacts = [Path(launched.log_path)]
            if _capture_enabled():
                frame = engine.screen_capture()
                frame_path = Path(frame.path)
                assert frame_path.parent == artifacts_dir, frame
                assert frame_path.read_bytes().startswith(b"\x89PNG"), frame_path
                artifacts.append(frame_path)

            stopped = engine.session_stop()
            assert stopped.kept.artifacts_dir == str(artifacts_dir), stopped
            assert stopped.kept.home_dir is None, stopped
            assert f"Artifacts kept: {artifacts_dir}" in stopped.render(), stopped.render()
            assert artifacts_dir.is_dir()
            assert all(path.is_file() for path in artifacts), artifacts
            assert live.process.poll() is None
        finally:
            engine.session_stop()
            if artifacts_dir is not None:
                shutil.rmtree(artifacts_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Server exit (stdio)
# ---------------------------------------------------------------------------

# The probe ignores SIGTERM (an ignored disposition survives exec), so session_stop's
# group escalation leaves it the TERM grace to dispatch the releases it was sent
# just before; a default GTK app dies on SIGTERM before reading them.
_TERM_IGNORING_PROBE = "sh -c " + shlex.quote(f"trap '' TERM; exec {PROBE_COMMAND}")


def _call(
    server: subprocess.Popen[str], reader: _LineReader, request_id: int, name: str, args: dict
) -> dict:
    """Call one tool over the raw stdio client; return its structured result."""
    response = _rpc(reader, server, request_id, "tools/call", {"name": name, "arguments": args})
    return _structured(response)


@pytest.mark.qa("QA-SES-020")
@pytest.mark.parametrize("ending", ["eof", "sigterm"])
@pytest.mark.parametrize(
    ("home", "retention"),
    [
        ("host", {}),
        ("host", {"artifacts": True}),
        ("isolated", {"artifacts": True, "home": True}),
    ],
    ids=["nothing", "artifacts", "artifacts-home"],
)
def test_server_exit_honours_retention(
    tmp_path: Path, ending: str, home: str, retention: dict[str, bool]
) -> None:
    """Server exit runs the serialized session_stop: retention honoured, holds released.

    Over stdio the session holds left and shift on the interaction probe when stdin
    closes (exit 0) or SIGTERM lands (exit 143). The stderr outcome line names the
    serialized session_stop path, the probe logs the release of both, retained dirs
    survive and unretained ones are removed.
    """
    log_path = tmp_path / "probe-events.jsonl"
    server = _spawn_with_stderr([sys.executable, "-m", "kwin_mcp"])
    reader = _LineReader(_stdout(server))
    dirs: list[Path] = []
    try:
        info = _start_server_session(server, reader, {"home": home, "retention": retention})
        artifacts_dir = Path(info["artifacts_dir"])
        dirs.append(artifacts_dir)
        home_dir = Path(info["home_dir"]) if info["home_dir"] is not None else None
        if home_dir is not None:
            dirs.append(home_dir)
        pgid = _session_pgid(server.pid)
        assert pgid is not None, "session started but the wrapper process group was not found"

        response = _rpc(
            reader,
            server,
            3,
            "tools/call",
            {
                "name": "app_launch",
                "arguments": {"command": _TERM_IGNORING_PROBE, "env": probe_env(log_path)},
            },
        )
        launched = _structured(response)
        app_log = Path(launched["log_path"])
        oracle = KWinOracle(info["dbus_address"])
        windows = oracle.wait_for_windows(
            lambda ws: any(
                w.pid == launched["pid"] and w.client is not None and w.active for w in ws
            ),
            timeout_s=20.0,
        )
        window = next(w for w in windows if w.pid == launched["pid"])
        assert window.client is not None, window
        x, y, _, _ = window.client.as_ints()
        target = {"x": x + 40, "y": y + 40}

        held = _call(server, reader, 4, "pointer_hold", {"phase": "press", "target": target})
        assert held["held"]["buttons"] == ["left"], held
        held = _call(server, reader, 5, "keyboard_hold", {"phase": "press", "keys": "shift"})
        assert held["held"]["keys"] == ["shift"], held
        _wait_for_presses(log_path)

        if ending == "eof":
            _stdin(server).close()
            expected = 0
        else:
            os.kill(server.pid, signal.SIGTERM)
            expected = 128 + signal.SIGTERM
        exit_code = server.wait(timeout=SERVER_EXIT_TIMEOUT_SECONDS)
        assert exit_code == expected, f"expected exit {expected} on {ending}, got {exit_code}"
        stderr = server.stderr.read() if server.stderr is not None else ""
        assert SERIALIZED_OUTCOME in stderr, stderr
        _assert_session_gone(pgid, launched["pid"])

        events = read_events(log_path)
        releases = of_kind(events, "button_release", "key_release")
        assert any(e["kind"] == "button_release" and e["button"] == "left" for e in releases), (
            events[-20:]
        )
        assert any(
            e["kind"] == "key_release" and str(e["key"]).startswith("Shift") for e in releases
        ), events[-20:]

        artifacts_kept = retention.get("artifacts", False)
        assert artifacts_dir.exists() is artifacts_kept, (retention, artifacts_dir)
        assert app_log.exists() is artifacts_kept, app_log
        if home_dir is not None:
            assert home_dir.exists() is retention.get("home", False), (retention, home_dir)
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()
        for path in dirs:
            shutil.rmtree(path, ignore_errors=True)


def _wait_for_presses(log_path: Path) -> None:
    """Wait until the probe logged the left press and a shift press."""
    deadline = time.monotonic() + PROCESS_EXIT_TIMEOUT_SECONDS
    events: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        events = read_events(log_path)
        left = any(e["kind"] == "button_press" and e["button"] == "left" for e in events)
        shift = any(e["kind"] == "key_press" and str(e["key"]).startswith("Shift") for e in events)
        if left and shift:
            return
        time.sleep(0.1)
    pytest.fail(f"the probe never logged the held presses: {json.dumps(events[-20:])}")
