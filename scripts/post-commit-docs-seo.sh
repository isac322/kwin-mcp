#!/usr/bin/env bash
# post-commit-docs-seo.sh — post-commit git hook for docs-seo trigger.
#
# Installed automatically by scripts/install_hooks.sh (not invoked directly).
# Detects whether the just-committed changes include source code files that
# warrant a documentation review, and prints a reminder when they do.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"

# Files changed in the most recent commit
CHANGED=$(git diff-tree --no-commit-id -r --name-only HEAD 2>/dev/null || true)

# Check whether any source or metadata files were touched
SOURCE_CHANGED=$(echo "$CHANGED" | grep -E '^src/kwin_mcp/.*\.py$|^pyproject\.toml$' || true)

if [ -z "$SOURCE_CHANGED" ]; then
    # No relevant files — clean no-op
    exit 0
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  📝  Docs & SEO reminder"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  The following source files were just committed:"
echo "$SOURCE_CHANGED" | sed 's/^/    /'
echo ""
echo "  Run a quick consistency check:"
echo "    python3 scripts/check_docs_seo.py"
echo ""
echo "  If documentation is out of date, invoke the docs-seo agent"
echo "  in Claude Code:  @docs-seo review documentation for recent changes"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Run the consistency checker automatically (non-fatal)
if command -v python3 &>/dev/null && [ -f "$REPO_ROOT/scripts/check_docs_seo.py" ]; then
    python3 "$REPO_ROOT/scripts/check_docs_seo.py" || true
fi

exit 0
