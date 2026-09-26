"""Verify the E2E environment recorder produces safe, usable provenance evidence."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from visual_harness import nested_visual_kwin

RECORDER = Path("/app/docker/e2e-environment.py")
ENVIRONMENT_FIELDS = {
    "architecture",
    "argv",
    "container",
    "system_packages",
    "distributions",
    "kwin_version",
    "python_version",
}
SECRET_NAME = "KWIN_MCP_TEST_SECRET"
SECRET_VALUE = "environment-recorder-must-not-expose-this"


def run_recorder(artifact_dir: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    """Run the installed recorder with an isolated artifact destination."""
    environment = os.environ.copy()
    environment["KWIN_MCP_ARTIFACT_DIR"] = str(artifact_dir)
    environment[SECRET_NAME] = SECRET_VALUE
    return subprocess.run(
        [str(RECORDER), *argv],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=30,
    )


def read_record(artifact_dir: Path) -> tuple[str, dict[str, Any]]:
    """Read and decode the recorder's final artifact."""
    contents = (artifact_dir / "environment.json").read_text(encoding="utf-8")
    return contents, json.loads(contents)


def assert_no_temporary_files(artifact_dir: Path) -> None:
    """Assert the atomic writer left only its final artifact behind."""
    assert list(artifact_dir.glob(".environment.*.tmp")) == []


def assert_mode(path: Path, expected: int) -> None:
    """Assert permission bits without depending on the creating user's identity."""
    assert stat.S_IMODE(path.stat().st_mode) == expected


def test_recorder_creates_allowlisted_provenance_with_installed_versions(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "artifacts"
    argv = ("--pytest-args", "value with spaces", "--literal=$HOME")

    previous_umask = os.umask(0o077)
    try:
        result = run_recorder(artifact_dir, *argv)
    finally:
        os.umask(previous_umask)

    assert result.returncode == 0, result.stderr
    assert (artifact_dir / "environment.json").is_file()
    assert_mode(artifact_dir / "environment.json", 0o644)
    contents, record = read_record(artifact_dir)
    assert set(record) == ENVIRONMENT_FIELDS
    assert record["argv"] == list(argv)
    assert isinstance(record["architecture"], str) and record["architecture"]
    assert isinstance(record["python_version"], str) and record["python_version"]

    container = record["container"]
    assert set(container) == {"base_image", "snapshot"}
    assert isinstance(container["base_image"], str) and container["base_image"]
    assert isinstance(container["snapshot"], str) and container["snapshot"]

    distributions = record["distributions"]
    assert set(distributions) == {"kwin-mcp", "mcp"}
    assert set(distributions["kwin-mcp"]) == {"location", "version"}
    assert isinstance(distributions["kwin-mcp"]["location"], str)
    assert distributions["kwin-mcp"]["location"]
    assert isinstance(distributions["kwin-mcp"]["version"], str)
    assert distributions["kwin-mcp"]["version"]
    assert set(distributions["mcp"]) == {"version"}
    assert isinstance(distributions["mcp"]["version"], str)
    assert distributions["mcp"]["version"]

    assert isinstance(record["kwin_version"], str) and record["kwin_version"]
    assert "kwin" in record["kwin_version"].lower()
    expected_packages = os.environ.get("KWIN_MCP_SYSTEM_PACKAGES", "").split()
    assert expected_packages, "the image must list its distro packages in KWIN_MCP_SYSTEM_PACKAGES"
    packages = record["system_packages"]
    assert set(packages) == set(expected_packages)
    missing = [package for package in expected_packages if not packages[package]]
    assert missing == [], f"installed versions not recorded for: {missing}"

    assert SECRET_NAME not in contents
    assert SECRET_VALUE not in contents
    assert_no_temporary_files(artifact_dir)


def test_visual_artifact_directory_is_traversable_under_restrictive_umask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    monkeypatch.setenv("KWIN_MCP_ARTIFACT_DIR", str(artifact_root))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "missing-runtime"))

    previous_umask = os.umask(0o077)
    try:
        with (
            pytest.raises(
                RuntimeError,
                match="XDG_RUNTIME_DIR must name the existing runtime directory",
            ),
            nested_visual_kwin(),
        ):
            pass
    finally:
        os.umask(previous_umask)

    artifact_dirs = list(artifact_root.glob("visual-kwin-*"))
    assert len(artifact_dirs) == 1
    assert_mode(artifact_dirs[0], 0o755)


def test_repeated_execution_atomically_replaces_valid_json(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"

    first_result = run_recorder(artifact_dir, "first invocation")
    assert first_result.returncode == 0, first_result.stderr
    _, first_record = read_record(artifact_dir)
    assert first_record["argv"] == ["first invocation"]

    second_result = run_recorder(artifact_dir, "second", "invocation")
    assert second_result.returncode == 0, second_result.stderr
    contents, second_record = read_record(artifact_dir)

    assert second_record["argv"] == ["second", "invocation"]
    assert second_record != first_record
    assert SECRET_NAME not in contents
    assert SECRET_VALUE not in contents
    assert_no_temporary_files(artifact_dir)
