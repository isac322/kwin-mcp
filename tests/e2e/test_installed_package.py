"""Verify the E2E image runs the installed kwin-mcp distribution."""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import subprocess
from pathlib import Path

import pytest

VENV_ROOT = Path("/opt/kwin-mcp-venv")
VENV_BIN = VENV_ROOT / "bin"
EXPECTED_ENTRY_POINTS = {
    "kwin-mcp": ("kwin_mcp.server", "main"),
    "kwin-mcp-cli": ("kwin_mcp.cli", "main"),
}
RUNTIME_IMPORTS = (
    "mcp.server.mcpserver",
    "dbus.mainloop.glib",
    "PIL.Image",
)
SERVER_FLAGS = ("--default-live-session", "--screenshot-images", "--screenshot-max-edge")
SERVER_HELP_COMMANDS = (
    (VENV_BIN / "kwin-mcp", "--help"),
    (VENV_BIN / "kwin-mcp", "-h"),
    (VENV_BIN / "python", "-m", "kwin_mcp", "--help"),
    # Help wins over any other argument, even an invalid one before or after it.
    (VENV_BIN / "kwin-mcp", "--screenshot-max-edge", "abc", "--help"),
    (VENV_BIN / "kwin-mcp", "--help", "--screenshot-max-edge", "abc"),
)
# Arguments the server rejects as a usage error (exit 2) before it starts serving,
# with the stable fragment of argparse's error line.
SERVER_USAGE_ERRORS = (
    (("--bogus-flag",), "unrecognized arguments: --bogus-flag"),
    (("extra",), "unrecognized arguments: extra"),
    # An abbreviation of --default-live-session must not switch on live mode.
    (("--default",), "unrecognized arguments: --default"),
    # Superscript two: str.isdigit() accepts it, int() raises ValueError.
    (("--screenshot-max-edge", "\u00b2"), "needs a pixel count"),
)


def test_kwin_mcp_is_loaded_from_the_runtime_venv() -> None:
    package = importlib.import_module("kwin_mcp")
    package_path = Path(package.__file__).resolve()

    assert package_path.is_relative_to(VENV_ROOT.resolve()), package_path
    assert "site-packages" in package_path.parts, package_path


def test_distribution_metadata_owns_the_imported_package() -> None:
    package = importlib.import_module("kwin_mcp")
    distribution = importlib.metadata.distribution("kwin-mcp")

    assert distribution.metadata["Name"] == "kwin-mcp"
    assert distribution.version
    assert (
        Path(str(distribution.locate_file("kwin_mcp"))).resolve()
        == Path(package.__file__).resolve().parent
    )


@pytest.mark.parametrize("module_name", RUNTIME_IMPORTS)
def test_runtime_dependency_is_importable(module_name: str) -> None:
    assert importlib.import_module(module_name) is not None


def test_atspi_runtime_dependency_is_usable() -> None:
    gi = importlib.import_module("gi")
    gi.require_version("Atspi", "2.0")

    atspi = importlib.import_module("gi.repository.Atspi")

    assert atspi.Accessible is not None


def test_console_entry_points_are_installed_and_load_expected_callables() -> None:
    distribution = importlib.metadata.distribution("kwin-mcp")
    entry_points = {
        entry_point.name: entry_point
        for entry_point in distribution.entry_points
        if entry_point.group == "console_scripts"
    }

    assert EXPECTED_ENTRY_POINTS.keys() <= entry_points.keys()
    for command, (module_name, attribute_name) in EXPECTED_ENTRY_POINTS.items():
        script = VENV_BIN / command
        expected_callable = getattr(importlib.import_module(module_name), attribute_name)

        assert script.is_file(), script
        assert os.access(script, os.X_OK), script
        assert entry_points[command].load() is expected_callable


def test_cli_help_runs_without_a_kwin_executable_on_path() -> None:
    environment = os.environ.copy()
    environment["PATH"] = str(VENV_BIN)

    result = subprocess.run(
        [str(VENV_BIN / "kwin-mcp-cli"), "--help"],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert "--default-live-session" in result.stdout


def _run_server_with_stdin_open(argv: list[str]) -> tuple[int, str, str]:
    """Run the server with stdin held open; return its exit code, stdout and stderr.

    A server that went on to serve MCP would block on the open stdin instead of
    exiting, so the bounded wait fails the test rather than passing on EOF.
    """
    environment = os.environ.copy()
    environment["PATH"] = str(VENV_BIN)
    process = subprocess.Popen(
        argv,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        try:
            returncode = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail(f"{argv} started the MCP server instead of exiting")
        assert process.stdout is not None
        assert process.stderr is not None
        stdout = process.stdout.read()
        stderr = process.stderr.read()
    finally:
        process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    return returncode, stdout, stderr


@pytest.mark.parametrize(
    "command",
    SERVER_HELP_COMMANDS,
    ids=(
        "kwin-mcp--help",
        "kwin-mcp-h",
        "python-m-kwin_mcp--help",
        "invalid-max-edge-then-help",
        "help-then-invalid-max-edge",
    ),
)
def test_server_help_prints_usage_without_starting_the_stdio_server(
    command: tuple[Path | str, ...],
) -> None:
    returncode, stdout, stderr = _run_server_with_stdin_open([str(arg) for arg in command])

    assert returncode == 0, stderr
    assert "usage: kwin-mcp" in stdout
    for flag in SERVER_FLAGS:
        assert flag in stdout, flag
    assert stderr == ""


@pytest.mark.parametrize(
    ("args", "message"),
    SERVER_USAGE_ERRORS,
    ids=("unknown-option", "positional", "abbreviation", "non-ascii-max-edge"),
)
def test_server_rejects_invalid_arguments_with_a_usage_error(
    args: tuple[str, ...], message: str
) -> None:
    returncode, stdout, stderr = _run_server_with_stdin_open([str(VENV_BIN / "kwin-mcp"), *args])

    assert returncode == 2, stderr
    assert stderr.startswith("usage: kwin-mcp "), stderr
    assert "kwin-mcp: error: " in stderr, stderr
    assert message in stderr, stderr
    assert "Traceback" not in stderr, stderr
    # Rejected before the server starts: no MCP output and no exit-cleanup report.
    assert stdout == ""
    assert "exit cleanup" not in stderr, stderr
