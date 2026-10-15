"""Verify the E2E image runs the installed kwin-mcp 1.0.0 distribution."""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import subprocess
import tomllib
from pathlib import Path

import pytest
from mcp_harness import running_mcp_server
from tool_surface import TOOL_NAMES

from kwin_mcp import server

VENV_ROOT = Path("/opt/kwin-mcp-venv")
VENV_BIN = VENV_ROOT / "bin"
PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
RELEASE_VERSION = "1.0.0"
STABLE_CLASSIFIER = "Development Status :: 5 - Production/Stable"
ALPHA_CLASSIFIER = "Development Status :: 3 - Alpha"
EXPECTED_ENTRY_POINTS = {
    "kwin-mcp": ("kwin_mcp.server", "main"),
    "kwin-mcp-cli": ("kwin_mcp.cli", "main"),
}
# Every module of the 1.0 package, plus the typing marker; results.py was removed in 1.0.
PACKAGE_FILES = (
    *tuple(
        f"kwin_mcp/{module}.py"
        for module in (
            "__init__",
            "__main__",
            "server",
            "cli",
            "core",
            "models",
            "session",
            "screenshot",
            "accessibility",
            "input",
            "clipboard",
            "progress",
            "geometry",
            "dbus_args",
        )
    ),
    "kwin_mcp/py.typed",
)
REMOVED_PACKAGE_FILES = ("kwin_mcp/results.py",)
RUNTIME_IMPORTS = (
    "mcp.server.mcpserver",
    "dbus.mainloop.glib",
    "PIL.Image",
)
SERVER_FLAGS = (
    "--default-live-session",
    "--screenshot-images",
    "--screenshot-max-edge",
    "--live-consent {elicit,refuse}",
)
DOCUMENTATION_LINE = "Documentation: https://github.com/isac322/kwin-mcp#readme\n"
SERVER_HELP_COMMANDS = (
    (VENV_BIN / "kwin-mcp", "--help"),
    (VENV_BIN / "kwin-mcp", "-h"),
    (VENV_BIN / "python", "-m", "kwin_mcp", "--help"),
    (VENV_BIN / "kwin-mcp", "--default-live-session", "--help"),
    # Help wins over any other argument, even an invalid one before or after it.
    (VENV_BIN / "kwin-mcp", "--bogus", "--help"),
    (VENV_BIN / "kwin-mcp", "--screenshot-max-edge", "abc", "--help"),
    (VENV_BIN / "kwin-mcp", "--help", "--screenshot-max-edge", "abc"),
    (VENV_BIN / "kwin-mcp", "--live-consent=bogus", "--help"),
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
    # Arabic-Indic three: int() accepts it, but it is not an ASCII pixel count.
    (("--screenshot-max-edge", "\u0663"), "needs a pixel count"),
    (("--screenshot-max-edge=-1",), "needs a pixel count"),
    (("--screenshot-max-edge", "0x10"), "needs a pixel count"),
    (
        ("--screenshot-max-edge", "1", "--screenshot-max-edge", "2"),
        "--screenshot-max-edge may only be specified once",
    ),
    (("--live-consent=bogus",), "argument --live-consent: invalid choice"),
    (("--live-consent",), "argument --live-consent: expected one argument"),
    (
        ("--live-consent=elicit", "--live-consent=refuse"),
        "--live-consent may only be specified once",
    ),
)


def _pyproject_project() -> dict[str, object]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]


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


@pytest.mark.qa("QA-PKG-001")
def test_wheel_contents() -> None:
    """The installed wheel's RECORD lists every 1.0 module and nothing removed or cached."""
    distribution = importlib.metadata.distribution("kwin-mcp")
    assert distribution.files is not None
    installed = {file.as_posix() for file in distribution.files}

    missing = [path for path in PACKAGE_FILES if path not in installed]
    assert missing == []
    assert [path for path in REMOVED_PACKAGE_FILES if path in installed] == []
    assert [path for path in installed if "__pycache__" in path and "kwin_mcp" in path] == []
    assert [path for path in installed if path.startswith("tests/")] == []
    metadata = distribution.metadata
    assert metadata["Requires-Python"] == ">=3.12"
    requirements = [req.replace(" ", "") for req in distribution.requires or []]
    assert any(req.startswith("mcp") and "<3" in req and ">=2.2.0" in req for req in requirements)
    summary = metadata["Summary"]
    assert len(summary) <= 120
    for term in ("MCP server", "Linux", "GUI automation", "KDE Plasma", "Wayland"):
        assert term in summary, term


@pytest.mark.qa("QA-PKG-005")
def test_classifier_production_stable() -> None:
    classifiers = importlib.metadata.distribution("kwin-mcp").metadata.get_all("Classifier") or []

    assert STABLE_CLASSIFIER in classifiers
    assert ALPHA_CLASSIFIER not in classifiers
    assert classifiers == _pyproject_project()["classifiers"]


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
        assert entry_points[command].value == f"{module_name}:{attribute_name}"
        assert entry_points[command].load() is expected_callable


@pytest.mark.qa("QA-PKG-002")
@pytest.mark.anyio
async def test_console_entry_points_and_version() -> None:
    """Installed metadata, pyproject.toml and the server's serverInfo agree on 1.0.0."""
    installed_version = importlib.metadata.version("kwin-mcp")

    async with running_mcp_server() as client:
        server_info = client.initialize_result.server_info
        listing = await client.session.list_tools()

    assert installed_version == _pyproject_project()["version"] == RELEASE_VERSION
    assert server_info.name == "kwin-mcp"
    assert server_info.version == installed_version
    assert sorted(tool.name for tool in listing.tools) == sorted(TOOL_NAMES)


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


@pytest.mark.qa("QA-MCP-021")
@pytest.mark.parametrize(
    "command",
    SERVER_HELP_COMMANDS,
    ids=(
        "kwin-mcp--help",
        "kwin-mcp-h",
        "python-m-kwin_mcp--help",
        "default-live-session-then-help",
        "unknown-flag-then-help",
        "invalid-max-edge-then-help",
        "help-then-invalid-max-edge",
        "invalid-live-consent-then-help",
    ),
)
def test_server_help_prints_usage_without_starting_the_stdio_server(
    command: tuple[Path | str, ...],
) -> None:
    usage = server._USAGE

    returncode, stdout, stderr = _run_server_with_stdin_open([str(arg) for arg in command])

    assert returncode == 0, stderr
    # Byte-equal to the shipped text that the forbidden-string scan checks.
    assert stdout == usage
    assert stdout.startswith("usage: kwin-mcp")
    assert "-h, --help" in stdout
    for flag in SERVER_FLAGS:
        assert flag in stdout, flag
    assert stdout.endswith(DOCUMENTATION_LINE)
    assert stderr == ""


@pytest.mark.qa("QA-MCP-022")
@pytest.mark.parametrize(
    ("args", "message"),
    SERVER_USAGE_ERRORS,
    ids=(
        "unknown-option",
        "positional",
        "abbreviation",
        "superscript-max-edge",
        "arabic-indic-max-edge",
        "negative-max-edge",
        "hex-max-edge",
        "repeated-max-edge",
        "invalid-live-consent",
        "live-consent-without-value",
        "repeated-live-consent",
    ),
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


@pytest.mark.qa("QA-MCP-022")
@pytest.mark.anyio
@pytest.mark.parametrize("consent", ["elicit", "refuse"])
async def test_server_accepts_each_live_consent_value(consent: str) -> None:
    async with running_mcp_server(f"--live-consent={consent}") as client:
        listing = await client.session.list_tools()

    assert sorted(tool.name for tool in listing.tools) == sorted(TOOL_NAMES)
