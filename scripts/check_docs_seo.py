#!/usr/bin/env python3
"""check_docs_seo.py — Documentation/SEO + plugin sync consistency checker.

Validates two scopes:

1) Documentation SEO consistency — keywords, positioning terms, mode names
   present across README.md, CLAUDE.md, ROADMAP.md, CONTRIBUTING.md, pyproject.toml.

2) Plugin & integrations sync — that integrations/* plugin manifests carry the
   required keyword subset (per .claude/positioning.yml drift_detection),
   that the Claude Code source SKILL.md and OpenCode plugin's bundled
   SKILL.md remain byte-identical, and that the canonical tool count is
   reflected consistently across all locations.

Exit code:
    0  — all checks passed (no-op)
    1  — one or more checks failed (documentation/plugin may need updating)
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Documentation SEO term checks
# ---------------------------------------------------------------------------

REQUIRED_TERMS: list[tuple[str, list[str]]] = [
    (
        "README.md",
        [
            "live desktop automation",
            "live session",
            "session_connect",
            "virtual",
            "KDE Plasma",
            "MCP server",
        ],
    ),
    (
        "CLAUDE.md",
        [
            "live desktop automation",
            "live session",
            "session_connect",
            "KDE Plasma",
        ],
    ),
    (
        "ROADMAP.md",
        [
            "live",
        ],
    ),
    (
        "CONTRIBUTING.md",
        [
            "session_connect",
            "sync_plugin_version.py",
        ],
    ),
    (
        "pyproject.toml",
        [
            "live-session",
            "session-connect",
            "live desktop automation",
        ],
    ),
]

DOCS_SEO_AGENT = ".claude/agents/docs-seo.md"
DOCS_SEO_REQUIRED: list[str] = [
    "live desktop automation",
    "live session",
    "session_connect",
]

# ---------------------------------------------------------------------------
# Plugin & integrations sync
# Canonical source of truth: .claude/positioning.yml § drift_detection.
# When manifest_version in positioning.yml is bumped, the docs-seo agent's
# manifest-update trigger is responsible for keeping these constants synced.
# ---------------------------------------------------------------------------

PLUGIN_MANIFEST_REQUIRED_KEYWORDS: list[str] = [
    "mcp",
    "kwin",
    "kde",
    "wayland",
    "gui-automation",
    "desktop-automation",
    "linux-desktop",
    "ai-agents",
    "accessibility",
    "at-spi2",
]
PLUGIN_KEYWORDS_MIN_OVERLAP: int = 10

# (relative_path, dotted accessor for the keywords array within the JSON document)
PLUGIN_MANIFESTS: list[tuple[str, str]] = [
    (".claude-plugin/marketplace.json", "plugins.0.keywords"),
    ("integrations/claude-code/.claude-plugin/plugin.json", "keywords"),
    ("integrations/opencode/plugin/package.json", "keywords"),
]

SKILL_SOURCE = "integrations/claude-code/skills/kwin-desktop-automation/SKILL.md"
SKILL_MIRRORS: list[str] = [
    "integrations/opencode/plugin/skill/kwin-desktop-automation/SKILL.md",
]

TOOL_COUNT_CANONICAL = 31
SERVER_PY = "src/kwin_mcp/server.py"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    file: str
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing


# ---------------------------------------------------------------------------
# Standard term checks
# ---------------------------------------------------------------------------


def check_file(rel_path: str, required: list[str]) -> CheckResult:
    path = ROOT / rel_path
    result = CheckResult(file=rel_path)

    if not path.exists():
        result.missing = [f"<file not found: {rel_path}>"]
        return result

    content = path.read_text(encoding="utf-8").lower()
    for term in required:
        if term.lower() not in content:
            result.missing.append(term)

    return result


def check_pyproject_keywords() -> CheckResult:
    path = ROOT / "pyproject.toml"
    result = CheckResult(file="pyproject.toml [keywords]")

    if not path.exists():
        result.missing = ["<pyproject.toml not found>"]
        return result

    content = path.read_text(encoding="utf-8")
    keywords_match = re.search(
        r"\[project\].*?keywords\s*=\s*\[(.*?)\]",
        content,
        re.DOTALL,
    )
    if not keywords_match:
        result.missing = ["<keywords section not found>"]
        return result

    keywords_block = keywords_match.group(1).lower()
    for term in [
        "live-session",
        "session-connect",
        "live-desktop",
        "desktop-automation-platform",
    ]:
        if term not in keywords_block:
            result.missing.append(term)

    return result


# ---------------------------------------------------------------------------
# Plugin manifest keyword sync
# ---------------------------------------------------------------------------


def _get_keywords_from_manifest(rel_path: str, accessor: str) -> list[str] | None:
    path = ROOT / rel_path
    if not path.exists():
        return None
    try:
        obj: Any = json.loads(path.read_text(encoding="utf-8"))
        for part in accessor.split("."):
            obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    except (KeyError, IndexError, ValueError, TypeError):
        return None
    if not isinstance(obj, list):
        return None
    return [str(k) for k in obj]


def check_plugin_keywords_sync() -> list[CheckResult]:
    results: list[CheckResult] = []
    required = {k.lower() for k in PLUGIN_MANIFEST_REQUIRED_KEYWORDS}
    for rel_path, accessor in PLUGIN_MANIFESTS:
        result = CheckResult(file=f"{rel_path} [keywords]")
        keywords = _get_keywords_from_manifest(rel_path, accessor)
        if keywords is None:
            result.missing = [f"<unable to read keywords at {accessor}>"]
            results.append(result)
            continue
        keywords_set = {str(k).lower() for k in keywords}
        present = required & keywords_set
        if len(present) < PLUGIN_KEYWORDS_MIN_OVERLAP:
            absent = sorted(required - keywords_set)
            result.missing = [
                f"only {len(present)}/{PLUGIN_KEYWORDS_MIN_OVERLAP} required keywords present"
                f"; missing: {', '.join(absent)}"
            ]
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# SKILL.md identical check
# ---------------------------------------------------------------------------


def check_skill_identical() -> list[CheckResult]:
    results: list[CheckResult] = []
    source_path = ROOT / SKILL_SOURCE
    if not source_path.exists():
        results.append(
            CheckResult(file=SKILL_SOURCE, missing=[f"<source not found: {SKILL_SOURCE}>"])
        )
        return results
    source_bytes = source_path.read_bytes()
    for mirror in SKILL_MIRRORS:
        result = CheckResult(file=mirror)
        mirror_path = ROOT / mirror
        if not mirror_path.exists():
            result.missing = [
                f"<mirror not found: {mirror}> (run `python3 scripts/sync_plugin_version.py`)"
            ]
            results.append(result)
            continue
        if mirror_path.read_bytes() != source_bytes:
            result.missing = [
                f"drift (not byte-identical to {SKILL_SOURCE});"
                f" run `python3 scripts/sync_plugin_version.py`"
            ]
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Tool count check (auto-detect from server.py @mcp.tool() decorators)
# ---------------------------------------------------------------------------


def check_tool_count() -> CheckResult:
    result = CheckResult(file=f"tool count vs {SERVER_PY}")
    server = ROOT / SERVER_PY
    if not server.exists():
        result.missing = [f"<{SERVER_PY} not found>"]
        return result
    actual = len(
        re.findall(
            r"^\s*@mcp\.tool\(\)\s*$",
            server.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    )
    if actual != TOOL_COUNT_CANONICAL:
        result.missing = [
            f"server.py has {actual} @mcp.tool() functions"
            f" but TOOL_COUNT_CANONICAL = {TOOL_COUNT_CANONICAL};"
            f" update .claude/positioning.yml § drift_detection.tool_count_canonical,"
            f" check_docs_seo.py § TOOL_COUNT_CANONICAL, README.md tool tables,"
            f" and integrations/claude-code/skills/kwin-desktop-automation/SKILL.md"
            f" '{TOOL_COUNT_CANONICAL} capabilities' references"
        ]
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    results: list[CheckResult] = []

    for rel_path, required in REQUIRED_TERMS:
        if rel_path == "pyproject.toml":
            results.append(check_file(rel_path, required))
            results.append(check_pyproject_keywords())
        else:
            results.append(check_file(rel_path, required))

    results.append(check_file(DOCS_SEO_AGENT, DOCS_SEO_REQUIRED))

    results.extend(check_plugin_keywords_sync())
    results.extend(check_skill_identical())
    results.append(check_tool_count())

    failed = [r for r in results if not r.ok]
    if not failed:
        print("✅  All documentation/plugin SEO checks passed.")
        return 0

    print("❌  Documentation/plugin SEO issues detected:\n")
    for r in failed:
        print(f"  {r.file}:")
        for term in r.missing:
            print(f"    - {term}")

    print("\nFix path:")
    print("  • For documentation drift: run the @docs-seo agent in Claude Code.")
    print("  • For plugin manifest version/SKILL drift: `python3 scripts/sync_plugin_version.py`.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
