#!/usr/bin/env python3
"""
check_docs_seo_hook.py — Claude Code PostToolUse hook for docs-seo trigger.

Reads the tool-use context JSON from stdin (provided by Claude Code when a
PostToolUse hook fires) and outputs a reminder to run the @docs-seo agent
if the modified file is a kwin-mcp source file.

Referenced from .claude/settings.json:
    {
      "hooks": {
        "PostToolUse": [
          {
            "matcher": "Edit|Write|MultiEdit",
            "hooks": [{"type": "command", "command": "python3 scripts/check_docs_seo_hook.py"}]
          }
        ]
      }
    }

Exit codes:
    0 — no action needed (non-source file or parse error)
    0 — reminder printed to stdout (source file detected)
"""

from __future__ import annotations

import json
import re
import sys

SOURCE_PATTERN = re.compile(r"^src/kwin_mcp/.*\.py$|^pyproject\.toml$")


def extract_file_path(data: dict) -> str:
    """Extract file path from Claude Code hook tool_input."""
    tool_input = data.get("tool_input", {})
    return tool_input.get("file_path") or tool_input.get("path") or ""


def normalize_path(path: str) -> str:
    """Strip absolute prefix to get a repo-relative path."""
    # Handle absolute paths by finding the kwin_mcp src component
    for prefix in [
        "/home/bhyoo/projects/python/kwin-mcp/",
        "./",
    ]:
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Not valid JSON — nothing to do
        return 0

    raw_path = extract_file_path(data)
    if not raw_path:
        return 0

    rel_path = normalize_path(raw_path)
    if not SOURCE_PATTERN.match(rel_path):
        return 0

    # Source file was modified — print a structured reminder for Claude Code
    tool_name = data.get("tool_name", "Edit")
    print(f"\n[docs-seo] Source file modified via {tool_name}: {rel_path}")
    print("[docs-seo] Please evaluate whether documentation needs updating.")
    print("[docs-seo] Invoke: @docs-seo review documentation for recent changes")
    print("[docs-seo] Or run: python3 scripts/check_docs_seo.py")

    return 0


if __name__ == "__main__":
    sys.exit(main())
