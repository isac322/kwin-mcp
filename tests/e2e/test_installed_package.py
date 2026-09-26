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
    "mcp.server.fastmcp",
    "dbus.mainloop.glib",
    "PIL.Image",
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
