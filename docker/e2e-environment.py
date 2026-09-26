#!/usr/bin/env python3
"""Record deterministic container environment metadata for end-to-end runs."""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TypedDict


class EnvironmentRecord(TypedDict):
    """Allowlisted data written to environment.json."""

    architecture: str
    argv: list[str]
    container: dict[str, str | None]
    system_packages: dict[str, str | None]
    distributions: dict[str, dict[str, str | None]]
    kwin_version: str | None
    python_version: str


def distribution_details(name: str) -> dict[str, str | None]:
    """Return stable installed-distribution metadata without importing the package."""
    try:
        distribution = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return {"location": None, "version": None}

    return {
        "location": str(Path(distribution.locate_file("")).resolve()),
        "version": distribution.version,
    }


def distribution_version(name: str) -> str | None:
    """Return an installed distribution version when available."""
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def command_output(command: list[str]) -> str | None:
    """Return deterministic command output without exposing the parent environment."""
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env={"LC_ALL": "C", "PATH": os.environ.get("PATH", "")},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    output = result.stdout.strip() or result.stderr.strip()
    return output or None


def package_query_command(packages: list[str]) -> list[str] | None:
    """Return a command printing `name<TAB or space>version` lines for installed packages."""
    if shutil.which("dpkg-query"):
        return ["dpkg-query", "--show", "--showformat=${Package}\t${Version}\n", *packages]
    if shutil.which("pacman"):
        return ["pacman", "--query", *packages]
    if shutil.which("rpm"):
        return ["rpm", "--query", "--queryformat=%{NAME}\t%{VERSION}-%{RELEASE}\n", *packages]
    return None


def system_package_versions() -> dict[str, str | None]:
    """Return versions for the distro packages each image lists in KWIN_MCP_SYSTEM_PACKAGES."""
    packages = os.environ.get("KWIN_MCP_SYSTEM_PACKAGES", "").split()
    versions: dict[str, str | None] = dict.fromkeys(packages)
    command = package_query_command(packages) if packages else None
    output = command_output(command) if command else None
    if output is None:
        return versions

    for line in output.splitlines():
        package, _, version = line.replace("\t", " ", 1).partition(" ")
        if version and package in versions:
            versions[package] = version
    return versions


def environment_record(argv: list[str]) -> EnvironmentRecord:
    """Build the allowlisted environment record."""
    return {
        "architecture": platform.machine(),
        "argv": argv,
        "container": {
            "base_image": os.environ.get("KWIN_MCP_BASE_IMAGE"),
            "snapshot": os.environ.get("KWIN_MCP_BASE_SNAPSHOT"),
        },
        "system_packages": system_package_versions(),
        "distributions": {
            "kwin-mcp": distribution_details("kwin-mcp"),
            "mcp": {"version": distribution_version("mcp")},
        },
        "kwin_version": command_output(["kwin_wayland", "--version"]),
        "python_version": platform.python_version(),
    }


def write_atomic(destination: Path, record: EnvironmentRecord) -> None:
    """Write JSON beside its final path, then atomically replace the destination."""
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=".environment.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            os.fchmod(temporary_file.fileno(), 0o644)
            json.dump(record, temporary_file, indent=2, sort_keys=True)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, destination)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def main() -> int:
    """Write environment.json to the configured artifact directory."""
    artifact_value = os.environ.get("KWIN_MCP_ARTIFACT_DIR")
    if not artifact_value:
        print("environment recorder: KWIN_MCP_ARTIFACT_DIR is not set", file=sys.stderr)
        return 2

    destination = Path(artifact_value) / "environment.json"
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(destination, environment_record(sys.argv[1:]))
    except Exception as error:
        print(f"environment recorder: {error}", file=sys.stderr)
        return 1

    print(f"environment recorder: wrote {destination}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
