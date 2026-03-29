#!/usr/bin/env python3
"""
check_docs_seo.py — Automated documentation-SEO consistency checker.

Validates that key documentation files contain the expected SEO keywords and
positioning language reflecting the dual virtual+live desktop automation
platform narrative.  Called by the ``docs-seo`` CI workflow whenever source
code or project metadata changes.

Exit code:
    0  — all checks passed (no-op)
    1  — one or more checks failed (documentation may need updating)
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent

# Files that must contain certain keywords for SEO / positioning consistency.
# Each entry is (file_path_relative_to_root, [required_terms]).
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

# Patterns whose *absence* from the docs-seo agent prompt indicates stale
# positioning.
DOCS_SEO_AGENT = ".claude/agents/docs-seo.md"
DOCS_SEO_REQUIRED: list[str] = [
    "live desktop automation",
    "live session",
    "session_connect",
]


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    file: str
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing


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
    """Check that pyproject.toml keywords list contains live-session terms."""
    path = ROOT / "pyproject.toml"
    result = CheckResult(file="pyproject.toml [keywords]")

    if not path.exists():
        result.missing = ["<pyproject.toml not found>"]
        return result

    content = path.read_text(encoding="utf-8")
    # Find the keywords array
    keywords_match = re.search(
        r"\[project\].*?keywords\s*=\s*\[(.*?)\]",
        content,
        re.DOTALL,
    )
    if not keywords_match:
        result.missing = ["<keywords section not found>"]
        return result

    keywords_block = keywords_match.group(1).lower()
    for term in ["live-session", "session-connect", "live-desktop", "desktop-automation-platform"]:
        if term not in keywords_block:
            result.missing.append(term)

    return result


def main() -> int:
    results: list[CheckResult] = []

    # Standard term checks
    for rel_path, required in REQUIRED_TERMS:
        if rel_path == "pyproject.toml":
            # Full text check (description)
            results.append(check_file(rel_path, required))
            # Keyword array check
            results.append(check_pyproject_keywords())
        else:
            results.append(check_file(rel_path, required))

    # docs-seo agent self-consistency check
    results.append(check_file(DOCS_SEO_AGENT, DOCS_SEO_REQUIRED))

    # -----------------------------------------------------------------------
    # Report
    # -----------------------------------------------------------------------
    failed = [r for r in results if not r.ok]

    if not failed:
        print("✅  All documentation SEO checks passed.")
        return 0

    print("❌  Documentation SEO issues detected:\n")
    for r in failed:
        print(f"  {r.file}:")
        for term in r.missing:
            print(f"    - missing: {term!r}")

    print(
        "\nRun the @docs-seo agent in Claude Code to review and update affected documents."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
