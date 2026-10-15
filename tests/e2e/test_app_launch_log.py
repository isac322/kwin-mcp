"""End-to-end coverage for ``app_launch`` and ``app_log``.

``app_launch`` shlex-splits the command (no shell), runs it in the session with
stdin on ``/dev/null`` and its own process group, and captures stdout+stderr in a
log file that ``app_log`` tails. A failed launch is a tool error that keeps the
session running and names ``session_stop`` as the rollback. The oracles are the
log file on disk, ``/proc/<pid>`` and the process table, never the tool's text.
"""

from __future__ import annotations

import ast
import errno
import os
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from _asserts import assert_render_roundtrip
from conftest import SCREEN_HEIGHT, SCREEN_WIDTH
from mcp_harness import running_mcp_server
from session_harness import live_kwin

from kwin_mcp.core import AutomationEngine
from kwin_mcp.models import (
    AppLaunchResult,
    AppLogResult,
    SessionInfoResult,
    SessionStopResult,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

EXIT_TIMEOUT_S = 10.0
POLL_INTERVAL_S = 0.05
BAD_BINARY = "definitely-not-a-real-binary"
ROLLBACK_LINE = "The session is still running; call session_stop to roll back."
NO_LOG_OUTPUT = "(no log output yet)"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
def session() -> Iterator[AutomationEngine]:
    """One virtual session shared by the launch/log tests that keep it running."""
    engine = AutomationEngine()
    try:
        info = engine.session_start(screen_width=SCREEN_WIDTH, screen_height=SCREEN_HEIGHT)
        assert info.kind == "virtual", info
        yield engine
    finally:
        engine.session_stop()


# ── Helpers ─────────────────────────────────────────────────────────────────


def _proc_stat(pid: int | str) -> tuple[str, int] | None:
    """Return (state, pgrp) of pid from /proc/<pid>/stat, or None once it is gone."""
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None
    # Fields after the parenthesised comm are: state ppid pgrp ...
    fields = stat.rpartition(")")[2].split()
    return fields[0], int(fields[2])


def _is_live(pid: int | str) -> bool:
    """Whether pid still runs; zombie (Z) and dead (X) entries do not count.

    The E2E container runs pytest as PID 1 without an orphan reaper, so orphaned
    session descendants stay zombies forever; only live processes count.
    """
    stat = _proc_stat(pid)
    return stat is not None and stat[0] not in ("Z", "X")


def _live_kwin_pids() -> set[int]:
    pids: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text().strip() != "kwin_wayland":
                continue
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if _is_live(entry.name):
            pids.add(int(entry.name))
    return pids


def _wait_until(predicate: object, what: str, timeout_s: float = EXIT_TIMEOUT_S) -> None:
    assert callable(predicate)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(f"timed out waiting for {what}")


def _wait_exit(engine: AutomationEngine, pid: int) -> int | None:
    """Wait until session_info reports the app exited; return its exit code."""
    exit_code: list[int | None] = []

    def exited() -> bool:
        for app in engine.session_info().apps:
            if app.pid == pid and not app.running:
                exit_code.append(app.exit_code)
                return True
        return False

    _wait_until(exited, f"app {pid} to exit")
    return exit_code[0]


def _log_after_exit(engine: AutomationEngine, pid: int) -> list[str]:
    """The whole log of an app once it exited (``last_n_lines=0``)."""
    _wait_exit(engine, pid)
    return engine.app_log(pid, last_n_lines=0).lines


def _env_of(lines: list[str]) -> dict[str, str]:
    """Parse ``env`` output lines into a mapping."""
    return dict(line.split("=", 1) for line in lines if "=" in line)


def _app_pids(engine: AutomationEngine) -> set[int]:
    return {app.pid for app in engine.session_info().apps}


# ── app_launch ──────────────────────────────────────────────────────────────


@pytest.mark.qa("QA-APP-001")
def test_launch_returns_pid_command_log_path_uri(session: AutomationEngine) -> None:
    result = session.app_launch("kcalc")
    cmdline = (Path("/proc") / str(result.pid) / "cmdline").read_bytes().split(b"\0")
    assert Path(cmdline[0].decode()).name == "kcalc", (
        cmdline,
        _proc_stat(result.pid),
        Path(result.log_path).read_text(errors="replace")[-2000:],
    )

    assert result.command == "kcalc"
    assert result.pid > 0
    assert result.log_uri == f"kwin://apps/{result.pid}/log"
    log_path = Path(result.log_path)
    assert log_path.is_file(), log_path
    info = session.session_info()
    assert info.session is not None
    assert log_path.parent == Path(info.session.artifacts_dir), (log_path, info.session)
    assert result.pid in _app_pids(session)
    text = result.render()
    assert text.splitlines()[:2] == [
        f"App launched: kcalc (PID={result.pid})",
        f"App log: {result.log_path}",
    ], text
    assert_render_roundtrip(AppLaunchResult, result.model_dump(mode="json"), text)


@pytest.mark.qa("QA-APP-002")
def test_launch_app_rejects_empty_and_unbalanced_command(session: AutomationEngine) -> None:
    apps_before = _app_pids(session)

    for command in ("", "   "):
        with pytest.raises(ValueError) as empty_info:
            session.app_launch(command)
        assert str(empty_info.value) == "command must contain a command to launch"
    with pytest.raises(ValueError) as unbalanced_info:
        session.app_launch("kcalc 'unterminated")
    assert str(unbalanced_info.value) == "Invalid command: No closing quotation"

    assert session.session_info().session is not None
    assert _app_pids(session) == apps_before

    # Quoting is honoured: sh receives 'echo "a b"' as one argument.
    quoted = session.app_launch("sh -c 'echo \"a b\"'")
    assert _log_after_exit(session, quoted.pid) == ["a b"]


@pytest.mark.qa("QA-APP-003")
def test_env_reaches_only_this_process(session: AutomationEngine) -> None:
    """``env`` is applied after DISPLAY handling and before the bus is forced."""
    info = session.session_info().session
    assert info is not None
    env = {
        "DISPLAY": ":9",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/evil",
        "WAYLAND_DISPLAY": "wayland-evil",
        "FOO": "bar",
    }
    first = session.app_launch("env", env=env)
    second = session.app_launch("env")

    first_env = _env_of(_log_after_exit(session, first.pid))
    assert first_env["DISPLAY"] == ":9"
    assert first_env["FOO"] == "bar"
    # The session bus cannot be redirected; WAYLAND_DISPLAY can (0.11 order).
    assert first_env["DBUS_SESSION_BUS_ADDRESS"] == info.dbus_address
    assert first_env["WAYLAND_DISPLAY"] == "wayland-evil"

    second_env = _env_of(_log_after_exit(session, second.pid))
    assert "FOO" not in second_env, second_env
    assert "DISPLAY" not in second_env, second_env
    assert second_env["WAYLAND_DISPLAY"] == info.wayland_socket
    assert second_env["DBUS_SESSION_BUS_ADDRESS"] == info.dbus_address
    assert "FOO" not in os.environ


@pytest.mark.qa("QA-APP-004")
def test_stdin_devnull_own_pgid_log_captures_both(session: AutomationEngine) -> None:
    script = "read x; echo rc=$?; echo err >&2; exec sleep 30"
    result = session.app_launch(f"sh -c {shlex.quote(script)}")

    log_path = Path(result.log_path)
    _wait_until(
        lambda: {"rc=1", "err"} <= set(log_path.read_text().splitlines()),
        "the app to log rc and err",
    )
    assert log_path.read_text().splitlines() == ["rc=1", "err"]
    assert session.app_log(result.pid).lines == ["rc=1", "err"]

    stat = _proc_stat(result.pid)
    assert stat is not None and _is_live(result.pid), stat
    assert stat[1] == result.pid, stat
    assert stat[1] != os.getpgid(0), stat
    assert os.readlink(f"/proc/{result.pid}/fd/0") == "/dev/null"


@pytest.mark.qa("QA-APP-006")
def test_no_shell_metacharacters_literal(session: AutomationEngine, tmp_path: Path) -> None:
    first_marker = tmp_path / f"pwned-{uuid4().hex}"
    second_marker = tmp_path / f"pwned2-{uuid4().hex}"

    # sh runs only 'echo x'; ';', 'touch' and the path are its positional args.
    first = session.app_launch(f"sh -c 'echo x' ; touch {first_marker}")
    assert _log_after_exit(session, first.pid) == ["x"]
    assert first.command == f"sh -c 'echo x' ; touch {first_marker}"

    # '$(touch' is the program name; there is no shell to expand it.
    with pytest.raises(RuntimeError) as exc_info:
        session.app_launch(f"$(touch {second_marker})")
    assert isinstance(exc_info.value.__cause__, FileNotFoundError), exc_info.value
    assert exc_info.value.__cause__.filename == "$(touch"

    assert not first_marker.exists()
    assert not second_marker.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.slow
@pytest.mark.timeout(600)
@pytest.mark.qa("QA-APP-008")
def test_e2big_command_is_tool_error(session: AutomationEngine) -> None:
    apps_before = _app_pids(session)
    command = "echo " + "a" * 300_000

    with pytest.raises(RuntimeError) as exc_info:
        session.app_launch(command)

    message = str(exc_info.value)
    cause = exc_info.value.__cause__
    assert isinstance(cause, OSError) and cause.errno == errno.E2BIG, repr(cause)
    assert message.startswith("Failed to launch 'echo aaa"), message[:200]
    assert f"[Errno {errno.E2BIG}]" in message.splitlines()[0][-200:], message[-300:]
    assert message.splitlines()[-1] == ROLLBACK_LINE
    assert session.session_info().session is not None
    assert _app_pids(session) == apps_before
    assert session.app_launch("true").pid > 0


def test_launch_rejects_missing_command(session: AutomationEngine) -> None:
    """A missing absolute path is a launch failure naming the path.

    Emulation caveat (plan §7): under qemu-user (amd64 on arm64) a missing
    execve target can surface as an app that exits 127 instead of ENOENT, so
    this test only runs on native rows (CI covers amd64 natively).
    """
    command = "/definitely/not/a/kwin-mcp-app"

    with pytest.raises(RuntimeError) as exc_info:
        session.app_launch(command)

    cause = exc_info.value.__cause__
    assert isinstance(cause, FileNotFoundError), repr(cause)
    assert cause.filename == command
    assert str(exc_info.value).splitlines() == [
        f"Failed to launch {command!r}: {cause}",
        ROLLBACK_LINE,
    ]


def test_app_launch_bad_command_raises_a_clear_error(session: AutomationEngine) -> None:
    kwin_before = _live_kwin_pids()
    apps_before = _app_pids(session)

    with pytest.raises(RuntimeError) as exc_info:
        session.app_launch(BAD_BINARY)

    cause = exc_info.value.__cause__
    assert isinstance(cause, FileNotFoundError), repr(cause)
    assert cause.errno == errno.ENOENT
    assert cause.filename == BAD_BINARY
    message = str(exc_info.value)
    assert "No such file or directory" in message, message
    assert BAD_BINARY in message, message
    assert message.splitlines()[-1] == ROLLBACK_LINE
    # The failed launch touched neither the compositor nor the session.
    assert _live_kwin_pids() == kwin_before
    assert _app_pids(session) == apps_before
    assert session.session_info().session is not None
    # A following launch works.
    assert _log_after_exit(session, session.app_launch("echo ok").pid) == ["ok"]


def test_app_launch_propagates_environment_to_the_app(session: AutomationEngine) -> None:
    variable = "KWIN_MCP_E2E_SESSION_ENV"
    value = "session-start-env-propagated"
    command = f"""sh -c 'printf "env:%s" "${variable}"'"""

    result = session.app_launch(command, env={variable: value})

    assert result.command == command
    assert _log_after_exit(session, result.pid) == [f"env:{value}"]


def test_virtual_session_apps_never_inherit_the_host_display(
    session: AutomationEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A host X server the server process can see must stay out of the virtual
    # session; X11 apps would otherwise open on the user's real desktop.
    monkeypatch.setenv("DISPLAY", ":4242")
    command = """sh -c 'printf "display:%s" "${DISPLAY-unset}"'"""

    launched = session.app_launch(command)
    explicit = session.app_launch(command, env={"DISPLAY": ":7"})

    assert _log_after_exit(session, launched.pid) == ["display:unset"]
    # A caller that asks for a display on purpose still gets it.
    assert _log_after_exit(session, explicit.pid) == ["display::7"]


def test_live_session_apps_keep_the_inherited_display(
    engine: AutomationEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only virtual sessions drop DISPLAY: app_launch after session_connect
    # keeps the inherited value unchanged.
    monkeypatch.setenv("DISPLAY", ":4242")
    with live_kwin() as live:
        engine.session_connect(dbus_address=live.dbus_address, wayland_display=live.wayland_display)
        try:
            result = engine.app_launch("""sh -c 'printf "display:%s" "${DISPLAY-unset}"'""")
            assert _log_after_exit(engine, result.pid) == ["display::4242"]
        finally:
            engine.session_stop()


@pytest.mark.qa("QA-APP-005")
def test_virtual_display_stripped_live_display_kept(
    session: AutomationEngine, engine: AutomationEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPLAY", ":5151")

    virtual = session.app_launch("env")
    assert "DISPLAY" not in _env_of(_log_after_exit(session, virtual.pid))

    with live_kwin() as live:
        info = engine.session_connect(
            dbus_address=live.dbus_address, wayland_display=live.wayland_display
        )
        assert info.kind == "live", info
        try:
            result = engine.app_launch("env")
            live_env = _env_of(_log_after_exit(engine, result.pid))
            assert live_env.get("DISPLAY") == os.environ["DISPLAY"] == ":5151", live_env
            assert live_env["WAYLAND_DISPLAY"] == info.wayland_socket
        finally:
            engine.session_stop()


@pytest.mark.anyio
@pytest.mark.qa("QA-APP-007")
async def test_failed_app_launch_keeps_the_session_and_names_session_stop() -> None:
    """Two-call rollback: the failed launch keeps the session; session_stop ends it.

    The end state after session_stop matches 0.11's atomic failed start: no
    compositor and no session directories remain.
    """
    kwin_before = _live_kwin_pids()
    async with running_mcp_server() as client:
        started, _ = await client.call_structured(
            "session_start", {"screen_width": SCREEN_WIDTH, "screen_height": SCREEN_HEIGHT}
        )
        artifacts_dir = Path(started["artifacts_dir"])
        socket = Path(os.environ["XDG_RUNTIME_DIR"]) / started["wayland_socket"]
        session_kwin = _live_kwin_pids() - kwin_before
        assert session_kwin, "the session started no live kwin_wayland"

        lines = await client.call_tool_error("app_launch", {"command": "/nonexistent/bin"})
        assert lines[0].startswith(
            "Error executing tool app_launch: Failed to launch '/nonexistent/bin': [Errno 2] "
        ), lines
        assert lines[1] == ROLLBACK_LINE, lines

        info_data, _ = await client.call_structured("session_info")
        info = SessionInfoResult.model_validate(info_data)
        assert info.session is not None
        assert info.session.wayland_socket == started["wayland_socket"]
        assert info.apps == []
        assert all(_is_live(pid) for pid in session_kwin), session_kwin
        assert artifacts_dir.is_dir() and socket.is_socket()

        # Parity: an app that launches and exits at once is not a failure.
        launched, _ = await client.call_structured("app_launch", {"command": "true"})
        assert launched["command"] == "true"

        stop_data, _ = await client.call_structured("session_stop")
        assert SessionStopResult.model_validate(stop_data).stopped is True

        _wait_until(
            lambda: not any(_is_live(pid) for pid in session_kwin), "the session KWin to exit"
        )
        assert not artifacts_dir.exists(), artifacts_dir
        assert not socket.exists(), socket
        after, _ = await client.call_structured("session_info")
        assert after["session"] is None


# ── app_log ─────────────────────────────────────────────────────────────────


@pytest.mark.qa("QA-APP-009")
def test_app_log_tail_default_and_zero(session: AutomationEngine) -> None:
    result = session.app_launch("sh -c 'seq 1 120'")
    _wait_exit(session, result.pid)
    on_disk = Path(result.log_path).read_text().splitlines()
    assert on_disk == [str(n) for n in range(1, 121)]

    default = session.app_log(result.pid)
    assert default.lines == on_disk[70:]
    assert default.truncated is True
    assert default.render().splitlines()[0] == (
        f"App log of PID {result.pid}: {result.log_path} (last 50 lines)"
    )

    five = session.app_log(result.pid, last_n_lines=5)
    assert five.lines == ["116", "117", "118", "119", "120"]
    assert five.truncated is True

    whole = session.app_log(result.pid, last_n_lines=0)
    assert whole.lines == on_disk
    assert whole.truncated is False
    assert whole.render() == f"App log of PID {result.pid}: {result.log_path}\n" + "\n".join(
        on_disk
    )
    for model in (default, five, whole):
        assert_render_roundtrip(AppLogResult, model.model_dump(mode="json"), model.render())


@pytest.mark.qa("QA-APP-010")
def test_app_log_tail_zero_unknown_pid_and_empty(session: AutomationEngine) -> None:
    session.app_launch("true")
    with pytest.raises(ValueError) as exc_info:
        session.app_log(pid=999999)
    message = str(exc_info.value)
    prefix = "No app with PID 999999. Available PIDs: "
    assert message.startswith(prefix), message
    listed = ast.literal_eval(message.removeprefix(prefix))
    assert set(listed) == _app_pids(session), (listed, _app_pids(session))
    assert len(listed) == len(set(listed))

    sleeper = session.app_launch("sleep 5")
    empty = session.app_log(sleeper.pid)
    assert _is_live(sleeper.pid)
    assert os.stat(sleeper.log_path).st_size == 0
    assert empty == AppLogResult(
        pid=sleeper.pid, log_path=sleeper.log_path, lines=[], truncated=False
    )
    assert empty.render() == f"App log of PID {sleeper.pid}: {sleeper.log_path}\n{NO_LOG_OUTPUT}"


def test_app_log_returns_all_lines_and_requested_tail(session: AutomationEngine) -> None:
    log_lines = ["observation-line-one", "observation-line-two", "observation-line-three"]
    command = "printf '" + "\\n".join(log_lines) + "\\n'"
    result = session.app_launch(command)

    assert _log_after_exit(session, result.pid) == log_lines
    tail = session.app_log(pid=result.pid, last_n_lines=2)
    assert tail.lines == log_lines[-2:]
    assert tail.truncated is True


def test_app_log_rejects_an_unknown_pid(session: AutomationEngine) -> None:
    with pytest.raises(ValueError) as exc_info:
        session.app_log(pid=-1)

    message = str(exc_info.value)
    assert message.startswith("No app with PID -1. Available PIDs: ["), message
    assert message.endswith("]"), message


def test_app_log_reports_empty_process_output(session: AutomationEngine) -> None:
    result = session.app_launch("true")
    _wait_exit(session, result.pid)

    log = session.app_log(pid=result.pid, last_n_lines=0)
    assert log.lines == []
    assert log.truncated is False
    assert log.render().splitlines()[-1] == NO_LOG_OUTPUT
