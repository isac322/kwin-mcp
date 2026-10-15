#!/usr/bin/env python3
"""Check built wheels against the [project] table of pyproject.toml and the 1.0 layout.

Usage: python3 scripts/check_wheel.py dist/*.whl

For every wheel:

- the file name carries the distribution name and the pyproject.toml version;
- METADATA reports the same name, version, Requires-Python and classifier list as
  pyproject.toml, requires ``mcp>=2.2.0,<3``, carries the production-stable status
  classifier and no alpha one, and has a summary of at most 120 characters naming
  the product's primary keywords;
- the archive holds every kwin_mcp module and ``py.typed``, and no removed module,
  bytecode cache or test file.

Exits 1 and lists every mismatch otherwise. Only the standard library is used, so
the gate runs before any project dependency is installed.
"""

from __future__ import annotations

import email.message
import email.parser
import re
import sys
import tomllib
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PACKAGE = "kwin_mcp"
REQUIRED_MODULES = (
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
REQUIRED_FILES = (*(f"{PACKAGE}/{module}.py" for module in REQUIRED_MODULES), f"{PACKAGE}/py.typed")
# Modules that 1.0 removed; a wheel that still ships one was built from a stale tree.
REMOVED_FILES = (f"{PACKAGE}/results.py",)

STABLE_CLASSIFIER = "Development Status :: 5 - Production/Stable"
ALPHA_CLASSIFIER = "Development Status :: 3 - Alpha"
REQUIRED_MCP = "<3,>=2.2.0"
SUMMARY_MAX_CHARS = 120
SUMMARY_TERMS = ("MCP server", "Linux", "GUI automation", "KDE Plasma", "Wayland")


def normalize_name(name: str) -> str:
    """Return the PEP 503 normalized form of a distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def load_project() -> dict[str, object]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        return tomllib.load(file)["project"]


def read_metadata(archive: zipfile.ZipFile) -> email.message.Message:
    names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
    if len(names) != 1:
        raise ValueError(f"expected one .dist-info/METADATA, found {len(names)}")
    text = archive.read(names[0]).decode("utf-8")
    return email.parser.Parser().parsestr(text)


def _requirement_specifier(requirement: str, distribution: str) -> str | None:
    """The version specifier of ``distribution`` in a Requires-Dist line, without spaces.

    Environment markers are ignored; specifier clauses are sorted so ``>=2.2.0,<3``
    and ``<3,>=2.2.0`` compare equal.
    """
    requirement = requirement.split(";", 1)[0].strip()
    match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*\(?([^)]*)\)?", requirement)
    if match is None or normalize_name(match.group(1)) != normalize_name(distribution):
        return None
    clauses = [clause.strip().replace(" ", "") for clause in match.group(2).split(",")]
    return ",".join(sorted(clause for clause in clauses if clause))


def check_file_name(wheel: Path, name: str, version: str) -> list[str]:
    file_parts = wheel.name.split("-")
    if len(file_parts) < 5 or not wheel.name.endswith(".whl"):
        return [f"file name {wheel.name!r} is not a wheel file name"]
    problems: list[str] = []
    if normalize_name(file_parts[0]) != normalize_name(name):
        problems.append(f"file name distribution {file_parts[0]!r} != {name!r}")
    if file_parts[1] != version:
        problems.append(f"file name version {file_parts[1]!r} != {version!r}")
    return problems


def check_contents(names: list[str]) -> list[str]:
    present = set(names)
    problems = [f"wheel lacks {path}" for path in REQUIRED_FILES if path not in present]
    problems.extend(f"wheel ships removed {path}" for path in REMOVED_FILES if path in present)
    for path in names:
        parts = path.split("/")
        if "__pycache__" in parts or path.endswith((".pyc", ".pyo")):
            problems.append(f"wheel ships bytecode {path}")
        elif parts[0] == "tests" or any(
            part.startswith("test_") and part.endswith(".py") for part in parts
        ):
            problems.append(f"wheel ships test file {path}")
    return problems


def check_metadata(metadata: email.message.Message, project: dict[str, object]) -> list[str]:
    problems: list[str] = []
    name = str(project["name"])
    version = str(project["version"])
    requires_python = str(project["requires-python"])
    classifiers_value = project.get("classifiers", [])
    classifiers = [str(c) for c in classifiers_value] if isinstance(classifiers_value, list) else []

    metadata_name = metadata.get("Name", "")
    if normalize_name(metadata_name) != normalize_name(name):
        problems.append(f"METADATA Name {metadata_name!r} != {name!r}")
    if metadata.get("Version") != version:
        problems.append(f"METADATA Version {metadata.get('Version')!r} != {version!r}")
    if metadata.get("Requires-Python") != requires_python:
        problems.append(
            f"METADATA Requires-Python {metadata.get('Requires-Python')!r} != {requires_python!r}"
        )

    metadata_classifiers = metadata.get_all("Classifier") or []
    problems.extend(
        f"METADATA lacks classifier {c!r}" for c in classifiers if c not in metadata_classifiers
    )
    problems.extend(
        f"METADATA has classifier {c!r} not in pyproject.toml"
        for c in metadata_classifiers
        if c not in classifiers
    )
    if STABLE_CLASSIFIER not in metadata_classifiers:
        problems.append(f"METADATA lacks classifier {STABLE_CLASSIFIER!r}")
    if ALPHA_CLASSIFIER in metadata_classifiers:
        problems.append(f"METADATA still has classifier {ALPHA_CLASSIFIER!r}")

    mcp_specifiers = [
        specifier
        for requirement in metadata.get_all("Requires-Dist") or []
        if (specifier := _requirement_specifier(requirement, "mcp")) is not None
    ]
    if mcp_specifiers != [REQUIRED_MCP]:
        problems.append(f"METADATA Requires-Dist mcp {mcp_specifiers!r} != [{REQUIRED_MCP!r}]")

    summary = metadata.get("Summary", "")
    if len(summary) > SUMMARY_MAX_CHARS:
        problems.append(f"METADATA Summary has {len(summary)} characters (> {SUMMARY_MAX_CHARS})")
    problems.extend(
        f"METADATA Summary lacks {term!r}" for term in SUMMARY_TERMS if term not in summary
    )
    return problems


def check_wheel(wheel: Path, project: dict[str, object]) -> list[str]:
    problems = check_file_name(wheel, str(project["name"]), str(project["version"]))
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            metadata = read_metadata(archive)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        problems.append(f"cannot read wheel: {exc}")
        return problems
    problems.extend(check_contents(names))
    problems.extend(check_metadata(metadata, project))
    return problems


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python3 scripts/check_wheel.py WHEEL [WHEEL ...]", file=sys.stderr)
        return 2
    project = load_project()
    failed = False
    for arg in argv:
        wheel = Path(arg)
        problems = check_wheel(wheel, project)
        if problems:
            failed = True
            print(f"FAIL {wheel}")
            for problem in problems:
                print(f"  - {problem}")
        else:
            print(f"OK   {wheel}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
