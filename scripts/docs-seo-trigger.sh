#!/usr/bin/env bash
# docs-seo-trigger.sh — detect code changes that require docs-seo agent evaluation
#
# Usage (as a git post-commit hook):
#   git config core.hooksPath .githooks
#   ln -s ../../scripts/docs-seo-trigger.sh .githooks/post-commit
#
# Usage (manual):
#   bash scripts/docs-seo-trigger.sh [file-path]
#
# Trigger file patterns match the docs-seo agent's "Trigger Conditions":
#   src/kwin_mcp/server.py  → tool-registration, code-general
#   src/kwin_mcp/session.py → session-api
#   src/kwin_mcp/core.py    → engine-api, code-general
#   src/kwin_mcp/*.py       → code-general
#   pyproject.toml          → package-metadata
#   CHANGELOG.md            → changelog-update
#   README.md               → readme-update
#   ROADMAP.md              → roadmap-update
#   .claude/positioning.yml → manifest-update

set -euo pipefail

PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$PROJECT_ROOT"

# ---- Collect changed files ------------------------------------------------
if [[ "${1:-}" == "" ]]; then
  # Called as git hook: inspect last commit's changed files
  CHANGED_FILES="$(git diff-tree --no-commit-id -r --name-only HEAD 2>/dev/null || echo "")"
else
  CHANGED_FILES="$1"
fi

[[ -z "$CHANGED_FILES" ]] && exit 0

# ---- Classify each changed file -------------------------------------------
declare -A TRIGGERS_SEEN=()

while IFS= read -r f; do
  case "$f" in
    src/kwin_mcp/server.py)
      TRIGGERS_SEEN["tool-registration"]=1
      TRIGGERS_SEEN["code-general"]=1
      ;;
    src/kwin_mcp/session.py)
      TRIGGERS_SEEN["session-api"]=1
      ;;
    src/kwin_mcp/core.py)
      TRIGGERS_SEEN["engine-api"]=1
      TRIGGERS_SEEN["code-general"]=1
      ;;
    src/kwin_mcp/*.py)
      TRIGGERS_SEEN["code-general"]=1
      ;;
    pyproject.toml)
      TRIGGERS_SEEN["package-metadata"]=1
      ;;
    CHANGELOG.md)
      TRIGGERS_SEEN["changelog-update"]=1
      ;;
    README.md)
      TRIGGERS_SEEN["readme-update"]=1
      ;;
    ROADMAP.md)
      TRIGGERS_SEEN["roadmap-update"]=1
      ;;
    .claude/positioning.yml)
      TRIGGERS_SEEN["manifest-update"]=1
      ;;
  esac
done <<< "$CHANGED_FILES"

[[ ${#TRIGGERS_SEEN[@]} -eq 0 ]] && exit 0   # no-op: no trigger files changed

LABELS="${!TRIGGERS_SEEN[*]}"
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  📚  docs-seo evaluation triggered                           ║"
echo "╠══════════════════════════════════════════════════════════════╣"
printf "║  Labels : %-51s ║\n" "$LABELS"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Invoke the docs-seo agent to check for stale documentation. ║"
echo "║  A no-op conclusion is fine if nothing is stale.             ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
