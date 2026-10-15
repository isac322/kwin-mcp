"""Release gate 2: shipped text carries no design-doc leakage and no removed tool name.

Shipped text is every string a client or a person reads from the product: tool
titles and descriptions, every description inside the input and output schemas,
the server ``instructions`` (with and without ``--default-live-session``), and the
``kwin-mcp --help`` / ``kwin-mcp-cli --help`` output. Error texts are not shipped
text: the removed-tool remedy legitimately names the old tool.

The repository scan keeps the removed names out of everything a user reads about the
current product: README, CONTRIBUTING, docker/README, ``docs/``, ``integrations/``
(plugin manifests, the SKILL source and its mirror), the marketplace manifest,
``pyproject.toml``, the current sections of CHANGELOG ([Unreleased]) and ROADMAP
(milestones not marked ✅), plus the shipped server texts. Released CHANGELOG
versions (the 1.0.0 migration tables included) and completed ROADMAP milestones are
history, and blocks fenced by ``stale-name-scan: allow-begin`` / ``allow-end``
comments are exempt. Source and test code are not scanned: internal identifiers such
as an input backend's key method are not tool names.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from tool_surface import REMOVED_TOOL_NAMES, TOOL_NAMES

from kwin_mcp import cli, server
from kwin_mcp.server import build_server

REPO_ROOT = Path(__file__).resolve().parents[2]

# Design-doc leakage: a version citation, a pull-request reference, a source
# location, an implementation note about clamping.
FORBIDDEN = re.compile(r"0\.11|PR #|\.py:|(?i:clamp)|\b[\w./-]+\.\w+:\d+\b")


def _whole_word(names: tuple[str, ...]) -> re.Pattern[str]:
    # A hyphen joins words here, so the unchanged flag --screenshot-images does not
    # count as the bare name.
    return re.compile(r"(?<![\w-])(" + "|".join(map(re.escape, names)) + r")(?![\w-])")


REMOVED_NAME = _whole_word(REMOVED_TOOL_NAMES)


def _descriptions(schema: object, path: str) -> list[tuple[str, str]]:
    """Every ``description`` string anywhere in a schema, with its JSON path."""
    found: list[tuple[str, str]] = []
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "description" and isinstance(value, str):
                found.append((f"{path}/description", value))
            else:
                found.extend(_descriptions(value, f"{path}/{key}"))
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            found.extend(_descriptions(value, f"{path}/{index}"))
    return found


def _shipped_texts() -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    for argv in ([], ["--default-live-session"]):
        built = build_server(argv)
        label = " ".join(argv) or "default"
        assert built.instructions is not None
        texts.append((f"instructions ({label})", built.instructions))
        for tool in asyncio.run(built.list_tools()):
            data: dict[str, Any] = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
            name = data["name"]
            texts.append((f"{name}/title", data["title"]))
            texts.append((f"{name}/description ({label})", data["description"]))
            texts.extend(_descriptions(data["inputSchema"], f"{name}/inputSchema"))
            texts.extend(_descriptions(data.get("outputSchema"), f"{name}/outputSchema"))
    texts.append(("kwin-mcp --help", server._USAGE))
    texts.append(("kwin-mcp-cli --help", cli._build_parser().format_help()))
    return texts


@pytest.mark.qa("QA-SURF-019")
def test_no_forbidden_strings_in_shipped_text() -> None:
    offenders = [
        f"{where}: {match.group(0)!r} in {text!r}"
        for where, text in _shipped_texts()
        for pattern in (FORBIDDEN, REMOVED_NAME)
        for match in pattern.finditer(text)
    ]
    assert offenders == []


def test_forbidden_patterns_catch_their_targets() -> None:
    """Calibration: each pattern fires on the leak it exists for."""
    for leak in ("as in 0.11", "see PR #87", "server.py:503", "clamped to 1", "core.py:12"):
        assert FORBIDDEN.search(leak), leak
    for name in REMOVED_TOOL_NAMES:
        assert REMOVED_NAME.search(f"call {name} now"), name
        assert not REMOVED_NAME.search(f"call {name}_x or x_{name} or --{name}-flag")


# ── Repository scan ──────────────────────────────────────────────────────

_SCANNED = (
    "README.md",
    "CONTRIBUTING.md",
    "ROADMAP.md",
    "CHANGELOG.md",
    "pyproject.toml",
    "docker/README.md",
    "docs",
    "integrations",
    ".claude-plugin",
)
_EXTENSIONS = {".md", ".json", ".toml", ".yml", ".yaml", ".txt", ".ts"}
_SKIPPED_DIRS = {"node_modules", "dist", "__pycache__"}
_ALLOW_BEGIN = "stale-name-scan: allow-begin"
_ALLOW_END = "stale-name-scan: allow-end"

# Names that are also ordinary words are matched only where they are used as a tool
# name: in backticks, quoted, or called.
_COMMON_WORDS = {"screenshot"}


def _stale_name_pattern() -> re.Pattern[str]:
    compound = tuple(name for name in REMOVED_TOOL_NAMES if name not in _COMMON_WORDS)
    words = "|".join(map(re.escape, sorted(_COMMON_WORDS)))
    return re.compile(
        _whole_word(compound).pattern + rf"|[`\"']({words})[`\"']|(?<![\w-])({words})\("
    )


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for entry in _SCANNED:
        path = REPO_ROOT / entry
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(
                p
                for p in sorted(path.rglob("*"))
                if p.is_file()
                and p.suffix in _EXTENSIONS
                and _SKIPPED_DIRS.isdisjoint(p.relative_to(REPO_ROOT).parts)
            )
    return files


def _is_history_heading(path: Path, line: str) -> bool | None:
    """Whether ``line`` opens a history section of ``path`` (None: not a section heading).

    CHANGELOG.md: every ``## [version]`` section except ``## [Unreleased]``.
    ROADMAP.md: a ``###`` milestone marked ✅, up to the next heading of level 1-3.
    """
    if path.name == "CHANGELOG.md" and re.match(r"#{1,2} ", line):
        return line.startswith("## [") and not line.startswith("## [Unreleased]")
    if path.name == "ROADMAP.md" and re.match(r"#{1,3} ", line):
        return line.startswith("### ") and "✅" in line
    return None


def _current_lines(path: Path) -> list[tuple[int, str]]:
    """The lines of ``path`` that describe the current product.

    Allow-fenced blocks and history sections (see ``_is_history_heading``) are skipped.
    """
    lines: list[tuple[int, str]] = []
    fenced = False
    history = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        opens_history = _is_history_heading(path, line)
        if opens_history is not None:
            history = opens_history
        if _ALLOW_BEGIN in line:
            fenced = True
        if not fenced and not history:
            lines.append((number, line))
        if _ALLOW_END in line:
            fenced = False
    return lines


@pytest.mark.qa("QA-PKG-009")
def test_no_removed_tool_names_outside_history() -> None:
    assert len(TOOL_NAMES) == len(set(TOOL_NAMES)) == 29
    assert set(TOOL_NAMES).isdisjoint(REMOVED_TOOL_NAMES)
    pattern = _stale_name_pattern()
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}: {match.group(0)}"
        for path in _scanned_files()
        for number, line in _current_lines(path)
        for match in pattern.finditer(line)
    ]
    offenders.extend(
        f"{where}: {match.group(0)}"
        for where, text in _shipped_texts()
        for match in pattern.finditer(text)
    )
    assert offenders == [], "\n".join(offenders)


def test_history_sections_are_recognized() -> None:
    """Calibration: the scan skips released CHANGELOG versions and completed milestones."""
    changelog = Path("CHANGELOG.md")
    roadmap = Path("ROADMAP.md")
    assert _is_history_heading(changelog, "## [Unreleased]") is False
    assert _is_history_heading(changelog, "## [1.0.0] - 2026-10-12") is True
    assert _is_history_heading(changelog, "### Removed") is None
    assert _is_history_heading(roadmap, "### M6: Extended Input ✅") is True
    assert _is_history_heading(roadmap, "### M14: 1.0 Tool Surface Redesign") is False
    assert _is_history_heading(roadmap, "#### detail") is None
    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in _scanned_files()}
    assert {"README.md", "CHANGELOG.md"} <= scanned
    assert not any(name.startswith(("src/", "tests/")) for name in scanned)


def test_stale_name_pattern_calibration() -> None:
    pattern = _stale_name_pattern()
    compound = next(name for name in REMOVED_TOOL_NAMES if "_" in name)
    assert pattern.search(f"use {compound} here")
    assert not pattern.search(f"test_{compound}.py")
    for word in sorted(_COMMON_WORDS):
        assert pattern.search(f"call `{word}`")
        assert pattern.search(f'name="{word}"')
        assert pattern.search(f"{word}(x)")
        assert not pattern.search(f"a {word} of the workspace, --{word}-images")
