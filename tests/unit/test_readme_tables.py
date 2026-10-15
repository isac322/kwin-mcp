"""Release gate: the README documents the 1.0 tool surface and server flags.

The oracle is the design's tool table (``fixtures/s_tools.json``) and the name sets in
``tests/tool_surface.py``, never the server's own listing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from tool_surface import REMOVED_TOOL_NAMES, TOOL_NAMES

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
S_TOOLS = Path(__file__).resolve().parent / "fixtures" / "s_tools.json"

SERVER_FLAGS = (
    "--default-live-session",
    "--screenshot-images",
    "--screenshot-max-edge",
    "--live-consent",
)
NO_PARAMETERS = "_(none)_"

_HEADING = re.compile(r"^(#{1,6}) (.+)$", re.MULTILINE)
_CODE = re.compile(r"`([^`]+)`")


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, drop punctuation, spaces become hyphens."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def _anchors(text: str) -> set[str]:
    """Every heading anchor of ``text``, with GitHub's ``-1``, ``-2`` suffixes for repeats."""
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    in_code = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        match = _HEADING.match(line)
        if in_code or match is None:
            continue
        slug = _slug(match.group(2))
        count = seen.get(slug, 0)
        anchors.add(slug if count == 0 else f"{slug}-{count}")
        seen[slug] = count + 1
    return anchors


def _section(text: str, title: str) -> str:
    """The body of the ``##`` section ``title``, up to the next ``##`` heading."""
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert match is not None, f"README has no '## {title}' section"
    return match.group(1)


def _tool_rows(section: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        name = _CODE.fullmatch(cells[0])
        if line.startswith("| `") and len(cells) >= 3 and name is not None:
            rows.append((name.group(1), cells[1]))
    return rows


def _expected_parameters(tool: dict[str, Any]) -> list[str]:
    tokens: list[str] = []
    for param in tool["params"]:
        if param["required"]:
            tokens.append(param["name"])
        else:
            default = json.dumps(param["default"], separators=(",", ":"))
            tokens.append(f"{param['name']}?={default}")
    return tokens


def _documented_parameters(cell: str) -> list[str]:
    if cell == NO_PARAMETERS:
        return []
    return _CODE.findall(cell)


@pytest.mark.qa("QA-PKG-007")
def test_readme_tables_match_s_tools() -> None:
    text = _readme()
    rows = _tool_rows(_section(text, "Available Tools"))
    names = [name for name, _ in rows]
    assert len(names) == len(set(names)), "a tool is listed twice"
    assert set(names) == set(TOOL_NAMES)
    assert "29 MCP tools" in text

    expected = {tool["name"]: tool for tool in json.loads(S_TOOLS.read_text())["tools"]}
    assert set(expected) == set(TOOL_NAMES)
    for name, cell in rows:
        assert _documented_parameters(cell) == _expected_parameters(expected[name]), name

    flags = _section(text, "Server Flags")
    for flag in SERVER_FLAGS:
        assert f"`{flag}" in flags, flag
    assert "elicit" in flags
    assert "refuse" in flags
    # --screenshot-images attaches the frames of screen_capture and of every capture burst.
    assert "screen_capture" in flags
    assert "`capture`" in flags

    architecture = _section(text, "How It Works")
    assert "models.py" in architecture
    assert "29 tools" in architecture


def test_readme_anchors_resolve() -> None:
    text = _readme()
    anchors = _anchors(text)
    links = re.findall(r"\]\(#([^)]+)\)", text)
    assert links, "the README has no in-page links"
    missing = sorted({link for link in links if link not in anchors})
    assert missing == []
    toc = _section(text, "Table of Contents")
    toc_links = re.findall(r"\]\(#([^)]+)\)", toc)
    second_level = {_slug(title) for title in re.findall(r"^## (.+)$", text, re.MULTILINE)}
    assert set(toc_links) == second_level - {"table-of-contents"}


def test_readme_has_no_removed_tool_name_outside_the_migration_section() -> None:
    text = _readme()
    migration = _section(text, "Migrating from 0.11")
    current = text.replace(migration, "")
    pattern = re.compile(
        r"(?<![\w-])("
        + "|".join(re.escape(name) for name in REMOVED_TOOL_NAMES if name != "screenshot")
        + r")(?![\w-])|`screenshot`|(?<![\w-])screenshot\("
    )
    assert pattern.findall(current) == []


@pytest.mark.qa("QA-SES-022")
def test_readme_documents_sigkill_retention_limit() -> None:
    cleanup = re.search(
        r"^### Automatic Session Cleanup at Exit\n(.*?)(?=^#{2,3} )",
        _readme(),
        re.MULTILINE | re.DOTALL,
    )
    assert cleanup is not None
    limits = next(
        paragraph
        for paragraph in cleanup.group(1).split("\n\n")
        if paragraph.startswith("Known limits")
    )
    assert "SIGKILL" in limits
    assert "retention.artifacts=false" in limits
    assert "leaks" in limits
    assert "never reuses" in limits


def test_readme_documents_matching_fallbacks_and_direct_atspi_opt_in() -> None:
    text = _readme()
    types = _section(text, "Shared Argument Types")
    for term in (
        'match?="substring"',
        '"substring" | "exact" | "spoken"',
        "name only",
        "NFC",
        "not fuzzy matching",
        'element_act(action="Press|click")',
        'action=["Press", "click"]',
        "rather than trying the alternatives after dispatch",
    ):
        assert term in types, term

    tools = _section(text, "Available Tools")
    query = next(line for line in tools.splitlines() if line.startswith("| `element_query` |"))
    assert "locator.match" in query
    for mode in ('"substring"', '"exact"', '"spoken"'):
        assert mode in query
    act = next(line for line in tools.splitlines() if line.startswith("| `element_act` |"))
    assert "fallback list" in act
    assert "action actually dispatched" in act

    architecture = _section(text, "How It Works")
    for term in (
        "KWIN_MCP_ATSPI_DIRECT_START=1",
        "only the exact value `1`",
        "server's `KWIN_MCP_ATSPI_DIRECT_START`",
        "not an application's `env`",
        "Only after activation fails",
        "XDG_DATA_DIRS",
        "128 KiB",
        "less confinement",
        "do not verify this path on an SELinux-enforcing Fedora host",
    ):
        assert term in architecture, term


def test_documentation_inventories_cover_ported_modules_and_tests() -> None:
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    docker = (REPO_ROOT / "docker" / "README.md").read_text(encoding="utf-8")
    for name in (
        "dbus_service_file.py",
        "spoken_name.py",
        "test_spoken_name.py",
        "test_dbus_service_file.py",
        "test_atspi_service_acquisition.py",
        "test_atspi_exec_security.py",
        "test_session_atspi_direct_start.py",
        "transient_probe.py",
    ):
        assert name in contributing, name
        assert name in docker, name


def test_anchor_rules_match_github() -> None:
    """Calibration of the anchor rules against anchors GitHub generates."""
    assert _slug("Screenshot Size (`--screenshot-max-edge`, `region`, `max_edge`)") == (
        "screenshot-size---screenshot-max-edge-region-max_edge"
    )
    assert _slug("Why kwin-mcp?") == "why-kwin-mcp"
    assert _anchors("# A\n## A\n```\n# A\n```\n") == {"a", "a-1"}
