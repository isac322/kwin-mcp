"""End-to-end coverage for ``session_info``.

The launched apps are children of this test process (the engine runs in it), so
``/proc/<pid>`` and ``os.waitpid`` observe their state independently of the
tool: a reported exit means the leader was already reaped, a running sleeper is
still an unreaped live child.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import AppEntry, AppLaunchResult, SessionInfo, SessionInfoResult

APP_EXIT_TIMEOUT_S = 10.0


def _proc_state(pid: int) -> str | None:
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return stat.rpartition(")")[2].split()[0]


def test_session_info_without_a_session_is_null(engine: AutomationEngine) -> None:
    status = engine.session_info()
    assert status.session is None
    assert status.apps == []
    assert status.render() == "No session running."


@pytest.mark.qa("QA-SES-024")
def test_apps_list_pid_command_running_exit_code_log_path(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
) -> None:
    start_session()
    exit7 = launch("sh -c 'exit 7'")
    false = launch("false")
    sleeper = launch("sleep 60")

    def snapshot() -> tuple[SessionInfoResult, dict[int, AppEntry]]:
        status = engine.session_info()
        return status, {app.pid: app for app in status.apps}

    deadline = time.monotonic() + APP_EXIT_TIMEOUT_S
    status, apps = snapshot()
    while time.monotonic() < deadline and (apps[exit7.pid].running or apps[false.pid].running):
        time.sleep(0.05)
        status, apps = snapshot()

    assert [app.pid for app in status.apps] == [exit7.pid, false.pid, sleeper.pid]

    assert apps[exit7.pid].running is False
    assert apps[exit7.pid].exit_code == 7
    assert apps[exit7.pid].command.startswith("sh -c "), apps[exit7.pid]
    assert apps[false.pid].running is False
    assert apps[false.pid].exit_code == 1
    assert apps[false.pid].command == "false"
    assert apps[sleeper.pid].running is True
    assert apps[sleeper.pid].exit_code is None
    assert apps[sleeper.pid].command == "sleep 60"
    for launched in (exit7, false, sleeper):
        entry = apps[launched.pid]
        assert entry.log_path == launched.log_path
        assert Path(entry.log_path).is_file(), entry

    # Exited leaders are already reaped by the product; the sleeper is a live,
    # unreaped child of this process.
    for exited in (exit7.pid, false.pid):
        assert _proc_state(exited) is None, exited
        with pytest.raises(ChildProcessError):
            os.waitpid(exited, os.WNOHANG)
    assert _proc_state(sleeper.pid) not in (None, "Z", "X")
    assert os.waitpid(sleeper.pid, os.WNOHANG) == (0, 0)

    lines = status.render().splitlines()
    assert "Apps (3):" in lines, lines
    assert f"- PID {exit7.pid}: {apps[exit7.pid].command} (exited 7); log: {exit7.log_path}" in (
        lines
    )
    assert f"- PID {false.pid}: false (exited 1); log: {false.log_path}" in lines
    assert f"- PID {sleeper.pid}: sleep 60 (running); log: {sleeper.log_path}" in lines
