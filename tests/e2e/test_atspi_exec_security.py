"""End-to-end regressions for direct AT-SPI service-file execution."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from test_session_atspi_direct_start import (
    A11Y_BUS_NAME,
    _accessibility_daemons,
    _assert_real_session_state,
    _deny_first_a11y_activation,
    _install_interpreter_shim,
    _real_launcher_argv,
    _real_service_file,
    _service_data_dir,
)

from kwin_mcp.dbus_service_file import service_file_argv
from kwin_mcp.session import ATSPI_DIRECT_START_ENV

if TYPE_CHECKING:
    from collections.abc import Callable

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import SessionInfo


_DIRECT_SUCCESS_FRAGMENT = "directly instead"
_REGISTRY_BUS_NAME = "org.a11y.atspi.Registry"
_KWIN_BUS_NAME = "org.kde.KWin"


def _name_has_owner(engine: AutomationEngine, name: str) -> bool:
    reply = engine.dbus_call(
        service="org.freedesktop.DBus",
        path="/org/freedesktop/DBus",
        interface="org.freedesktop.DBus",
        method="NameHasOwner",
        args=[f"string:{name}"],
    )
    assert isinstance(reply.reply, bool), reply
    return reply.reply


def _real_registry_argv() -> list[str]:
    return service_file_argv(
        _real_service_file("accessibility-services/org.a11y.atspi.Registry.service").encode(),
        _REGISTRY_BUS_NAME,
    )


def _registry_has_owner(engine: AutomationEngine) -> bool:
    address = engine.dbus_call(
        service=A11Y_BUS_NAME,
        path="/org/a11y/bus",
        interface=A11Y_BUS_NAME,
        method="GetAddress",
    ).reply
    assert isinstance(address, str), address
    reply = subprocess.run(
        [
            "dbus-send",
            f"--bus={address}",
            "--print-reply=literal",
            "--reply-timeout=1000",
            "--dest=org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus.NameHasOwner",
            f"string:{_REGISTRY_BUS_NAME}",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
        timeout=3,
    )
    return reply.stdout.split()[-1:] == ["true"]


def _write_recording_program(
    path: Path,
    marker: Path,
    real_argv: list[str],
    *,
    shebang: bool,
) -> None:
    lines = ["#!/bin/sh"] if shebang else []
    lines.append(f"printf 'executed\\n' > {shlex.quote(str(marker))}")
    lines.append(f"exec {shlex.join(real_argv)}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o755)


def _install_service_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launcher_exec: str,
    registry_exec: str,
) -> Path:
    data_dir = _service_data_dir(tmp_path, monkeypatch, launcher_exec=launcher_exec)
    (data_dir / "dbus-1/accessibility-services/org.a11y.atspi.Registry.service").write_text(
        f"[D-BUS Service]\nName={_REGISTRY_BUS_NAME}\nExec={registry_exec}\n"
    )
    return data_dir


def _wait_for_daemons(expected: int) -> None:
    deadline = time.monotonic() + 3.0
    while _accessibility_daemons() != expected and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _accessibility_daemons() == expected


def _run_rejected_direct_start(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    marker: Path,
    daemons_before: int,
    *,
    launcher_marker: Path | None = None,
) -> None:
    try:
        output = start_session().render()
        assert "Session started. Wayland socket: " in output, output
        assert not marker.exists(), f"service command was evaluated: {marker}"
        assert _DIRECT_SUCCESS_FRAGMENT not in output, output
        assert "Warning:" in output, output
        if launcher_marker is not None:
            assert launcher_marker.exists(), "valid registry Exec prevented launcher execution"
        assert not _name_has_owner(engine, A11Y_BUS_NAME)
        # A rejected registry must also remove its launcher's private bus, not
        # leave it running until session_stop or the shared fixture cleans up.
        _wait_for_daemons(daemons_before)
        assert _name_has_owner(engine, _KWIN_BUS_NAME)
        assert "Input backend: KWin EIS" in output, output
        _assert_real_session_state(engine)
    finally:
        # Do not defer ownership/lifecycle cleanup to the shared fixture. This
        # catches a direct-start child that escaped before the fixture teardown.
        engine.session_stop()
    _wait_for_daemons(daemons_before)


@pytest.mark.parametrize(
    ("position", "builtin"),
    [
        ("launcher", "eval"),
        ("launcher", "source"),
        ("registry", "eval"),
        ("registry", "source"),
    ],
    ids=["launcher-eval", "launcher-source", "registry-eval", "registry-source"],
)
@pytest.mark.qa("QA-SES-028")
def test_direct_atspi_exec_rejects_shell_builtins_without_evaluation(
    position: str,
    builtin: str,
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare Bash builtins must not evaluate a service-file argument as shell code."""
    marker = tmp_path / f"{position}-{builtin}-marker"
    payload = tmp_path / f"{position}-{builtin}-payload"
    _write_recording_program(
        payload,
        marker,
        _real_launcher_argv() if position == "launcher" else _real_registry_argv(),
        shebang=True,
    )

    real_launcher = shlex.join(_real_launcher_argv())
    real_registry = shlex.join(_real_registry_argv())
    builtin_exec = shlex.join([builtin, str(payload)])
    _install_service_pair(
        tmp_path,
        monkeypatch,
        launcher_exec=builtin_exec if position == "launcher" else real_launcher,
        registry_exec=builtin_exec if position == "registry" else real_registry,
    )
    monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
    _deny_first_a11y_activation(tmp_path, monkeypatch)
    daemons_before = _accessibility_daemons()

    _run_rejected_direct_start(engine, start_session, marker, daemons_before)


@pytest.mark.parametrize("position", ["launcher", "registry"], ids=["launcher", "registry"])
def test_direct_atspi_exec_does_not_call_wrapper_cleanup_function(
    position: str,
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service named cleanup must not call the wrapper's same-named Bash function."""
    sentinel = subprocess.Popen(
        ["sleep", "60"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        # cleanup() reads KWIN_PID before the compositor has been started.
        # An owned sentinel makes that unintended function invocation visible
        # without touching any process belonging to another test or session.
        monkeypatch.setenv("KWIN_PID", str(sentinel.pid))
        _install_service_pair(
            tmp_path,
            monkeypatch,
            launcher_exec="cleanup"
            if position == "launcher"
            else shlex.join(_real_launcher_argv()),
            registry_exec="cleanup"
            if position == "registry"
            else shlex.join(_real_registry_argv()),
        )
        monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
        _deny_first_a11y_activation(tmp_path, monkeypatch)
        daemons_before = _accessibility_daemons()
        try:
            output = start_session().render()
            assert sentinel.poll() is None, "service command called the wrapper cleanup function"
            assert _DIRECT_SUCCESS_FRAGMENT not in output, output
            assert not _name_has_owner(engine, A11Y_BUS_NAME)
            _wait_for_daemons(daemons_before)
            assert _name_has_owner(engine, _KWIN_BUS_NAME)
            assert "Input backend: KWin EIS" in output, output
        finally:
            engine.session_stop()
        _wait_for_daemons(daemons_before)
    finally:
        if sentinel.poll() is None:
            sentinel.terminate()
        sentinel.wait(timeout=3)


@pytest.mark.parametrize("position", ["launcher", "registry"], ids=["launcher", "registry"])
def test_direct_atspi_exec_rejects_path_only_helper(
    position: str,
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct execution must not resolve a service command through PATH."""
    marker = tmp_path / f"path-only-{position}-marker"
    helper_dir = tmp_path / "path-only-bin"
    helper_dir.mkdir()
    helper_name = f"atspi-exec-helper-{uuid4().hex}"
    helper = helper_dir / helper_name
    _write_recording_program(
        helper,
        marker,
        _real_launcher_argv() if position == "launcher" else _real_registry_argv(),
        shebang=True,
    )
    monkeypatch.setenv("PATH", f"{helper_dir}{os.pathsep}{os.environ['PATH']}")

    real_launcher = shlex.join(_real_launcher_argv())
    real_registry = shlex.join(_real_registry_argv())
    _install_service_pair(
        tmp_path,
        monkeypatch,
        launcher_exec=helper_name if position == "launcher" else real_launcher,
        registry_exec=helper_name if position == "registry" else real_registry,
    )
    monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
    _deny_first_a11y_activation(tmp_path, monkeypatch)
    daemons_before = _accessibility_daemons()

    _run_rejected_direct_start(engine, start_session, marker, daemons_before)


@pytest.mark.parametrize("position", ["launcher", "registry"], ids=["launcher", "registry"])
def test_direct_atspi_exec_rejects_absolute_no_shebang_program(
    position: str,
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An executable text file without #! must fail with ENOEXEC, not shell fallback."""
    marker = tmp_path / f"no-shebang-{position}-marker"
    program = tmp_path / f"no-shebang-{position}"
    _write_recording_program(
        program,
        marker,
        _real_launcher_argv() if position == "launcher" else _real_registry_argv(),
        shebang=False,
    )

    real_launcher = shlex.join(_real_launcher_argv())
    launcher_marker = None
    if position == "registry":
        launcher_marker = tmp_path / "no-shebang-registry-launcher-marker"
        launcher = tmp_path / "no-shebang-registry-launcher"
        _write_recording_program(launcher, launcher_marker, _real_launcher_argv(), shebang=True)
        real_launcher = shlex.quote(str(launcher))
    real_registry = shlex.join(_real_registry_argv())
    _install_service_pair(
        tmp_path,
        monkeypatch,
        launcher_exec=str(program) if position == "launcher" else real_launcher,
        registry_exec=str(program) if position == "registry" else real_registry,
    )
    monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
    _deny_first_a11y_activation(tmp_path, monkeypatch)
    daemons_before = _accessibility_daemons()

    _run_rejected_direct_start(
        engine, start_session, marker, daemons_before, launcher_marker=launcher_marker
    )


@pytest.mark.parametrize("position", ["launcher", "registry"], ids=["launcher", "registry"])
@pytest.mark.parametrize(
    "command",
    ["./eval", "eval", "source"],
    ids=["relative", "bare-eval", "bare-source"],
)
def test_direct_atspi_exec_supports_relative_shebang_program_and_preserves_argv(
    position: str,
    command: str,
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relative and bare names execute a real program, without a name-based denylist."""
    monkeypatch.chdir(tmp_path)
    recorded = tmp_path / f"{position}-argv"
    program = tmp_path / Path(command).name
    real_argv = _real_launcher_argv() if position == "launcher" else _real_registry_argv()
    program.write_text(
        f'#!/bin/sh\nprintf \'%s\\n\' "$@" > {shlex.quote(str(recorded))}\nshift\nexec "$@"\n'
    )
    program.chmod(0o755)
    recorder_exec = shlex.join([command, "--label=two words", *real_argv])
    _install_service_pair(
        tmp_path,
        monkeypatch,
        launcher_exec=recorder_exec
        if position == "launcher"
        else shlex.join(_real_launcher_argv()),
        registry_exec=recorder_exec
        if position == "registry"
        else shlex.join(_real_registry_argv()),
    )
    monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
    if command == "./eval" and position == "launcher":
        # Compare with genuine activation by the private dbus-daemon from the
        # same CWD, before introducing the denied-activation fault fixture.
        control_daemons = _accessibility_daemons()
        try:
            control_output = start_session().render()
            assert _DIRECT_SUCCESS_FRAGMENT not in control_output, control_output
            assert recorded.read_text().splitlines() == ["--label=two words", *real_argv]
            assert _name_has_owner(engine, A11Y_BUS_NAME)
            assert _name_has_owner(engine, _KWIN_BUS_NAME)
        finally:
            engine.session_stop()
        _wait_for_daemons(control_daemons)
        recorded.unlink()
    _deny_first_a11y_activation(tmp_path, monkeypatch)
    daemons_before = _accessibility_daemons()

    try:
        output = start_session().render()
        assert "Session started. Wayland socket: " in output, output
        assert _DIRECT_SUCCESS_FRAGMENT in output, output
        assert recorded.read_text().splitlines() == ["--label=two words", *real_argv]
        assert _name_has_owner(engine, A11Y_BUS_NAME)
        assert _registry_has_owner(engine)
        assert _name_has_owner(engine, _KWIN_BUS_NAME)
    finally:
        engine.session_stop()
    _wait_for_daemons(daemons_before)


@pytest.mark.parametrize("invalid_registry", [False, True], ids=["success", "lookup-failure"])
def test_direct_atspi_exec_helper_ignores_python_customization(
    invalid_registry: bool,
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python customization must not run during either successful exec or failed lookup."""
    site_dir = tmp_path / "python-site"
    site_dir.mkdir()
    marker = tmp_path / "python-customization-marker"
    site_dir.joinpath("sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('sitecustomize')\n"
    )
    startup = tmp_path / "startup.py"
    startup.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('startup')\n")
    control_env = os.environ.copy()
    control_env["PYTHONPATH"] = str(site_dir)
    control_env.pop("PYTHONINSPECT", None)
    subprocess.run(
        [sys.executable, "-c", "pass"],
        check=True,
        env=control_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=5,
    )
    assert marker.exists(), "control Python process did not load sitecustomize"
    marker.unlink()

    monkeypatch.setenv("PYTHONPATH", str(site_dir))
    monkeypatch.setenv("PYTHONSTARTUP", str(startup))
    monkeypatch.setenv("PYTHONINSPECT", "1")
    monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
    launcher_marker = tmp_path / "customization-launcher-marker"
    launcher = tmp_path / "customization-launcher"
    _write_recording_program(launcher, launcher_marker, _real_launcher_argv(), shebang=True)
    data_dir = _install_service_pair(
        tmp_path,
        monkeypatch,
        launcher_exec=shlex.quote(str(launcher)),
        registry_exec=shlex.join(_real_registry_argv()),
    )
    if invalid_registry:
        (data_dir / "dbus-1/accessibility-services/org.a11y.atspi.Registry.service").write_text(
            "[D-BUS Service]\nName=org.a11y.atspi.Registry\nExec='unterminated\n"
        )
    _deny_first_a11y_activation(tmp_path, monkeypatch)
    daemons_before = _accessibility_daemons()

    try:
        output = start_session().render()
        assert "Session started. Wayland socket: " in output, output
        assert "Warning:" in output, output
        assert "Input backend: KWin EIS" in output, output
        assert not marker.exists(), "Python customization code ran in the role helper"
        if invalid_registry:
            assert _DIRECT_SUCCESS_FRAGMENT not in output, output
            assert not launcher_marker.exists(), "launcher ran before registry preflight"
            assert not _name_has_owner(engine, A11Y_BUS_NAME)
            _wait_for_daemons(daemons_before)
        else:
            assert _DIRECT_SUCCESS_FRAGMENT in output, output
            assert launcher_marker.exists(), "the valid launcher was not executed"
            assert _name_has_owner(engine, A11Y_BUS_NAME)
            assert _registry_has_owner(engine)
        _assert_real_session_state(engine)
        assert not marker.exists(), "Python customization ran after the startup handshake"
    finally:
        engine.session_stop()
    _wait_for_daemons(daemons_before)


@pytest.mark.parametrize("invalid_registry", [False, True], ids=["success", "lookup-failure"])
def test_direct_atspi_role_stdout_cannot_forge_the_session_handshake(
    invalid_registry: bool,
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Role stdout is untrusted data, never startup state or executable shell code."""
    source_marker = tmp_path / "stdout-source-marker"
    launcher_marker = tmp_path / "stdout-launcher-marker"
    launcher = tmp_path / "stdout-launcher"
    _write_recording_program(launcher, launcher_marker, _real_launcher_argv(), shebang=True)
    data_dir = _install_service_pair(
        tmp_path,
        monkeypatch,
        launcher_exec=shlex.quote(str(launcher)),
        registry_exec=shlex.join(_real_registry_argv()),
    )
    if invalid_registry:
        (data_dir / "dbus-1/accessibility-services/org.a11y.atspi.Registry.service").write_text(
            "[D-BUS Service]\nName=not.the.registry\nExec=/bin/true\n"
        )
    log = _install_interpreter_shim(
        tmp_path,
        monkeypatch,
        stall_role="none",
        spoof_stdout=True,
        spoof_source_marker=source_marker,
    )
    monkeypatch.setenv(ATSPI_DIRECT_START_ENV, "1")
    _deny_first_a11y_activation(tmp_path, monkeypatch)
    daemons_before = _accessibility_daemons()

    try:
        output = start_session().render()
        assert "Session started. Wayland socket: " in output, output
        assert "Warning:" in output, output
        assert "Input backend: KWin EIS" in output, output
        assert not source_marker.exists(), "helper stdout was evaluated as shell code"
        roles = ["launcher"] if invalid_registry else ["launcher", "registry"]
        assert [line.split()[0] for line in log.read_text().splitlines()] == roles
        for role in roles:
            argv = Path(f"{log}.argv.{role}").read_bytes().split(b"\0")[:-1]
            assert len(argv) == 4, argv
            assert argv[:2] == [b"-I", b"-S"], argv
            assert Path(os.fsdecode(argv[2])).is_absolute(), argv
            assert argv[3] == role.encode(), argv
        if invalid_registry:
            assert _DIRECT_SUCCESS_FRAGMENT not in output, output
            assert not launcher_marker.exists(), "invalid registry did not prevent launcher exec"
            assert not _name_has_owner(engine, A11Y_BUS_NAME)
            _wait_for_daemons(daemons_before)
        else:
            assert _DIRECT_SUCCESS_FRAGMENT in output, output
            assert launcher_marker.exists(), "the valid launcher did not run"
            assert _name_has_owner(engine, A11Y_BUS_NAME)
            assert _registry_has_owner(engine)
        _assert_real_session_state(engine)
        assert not source_marker.exists(), "helper stdout ran code after the startup handshake"
    finally:
        engine.session_stop()
    _wait_for_daemons(daemons_before)
