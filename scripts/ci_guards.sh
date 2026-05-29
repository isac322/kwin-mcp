#!/usr/bin/env bash
# CI Guards - enforce backend overhaul invariants.
#
# Each guard prints a clear failure message identifying the rule and
# the offending location(s), then exits non-zero. On a clean tree,
# the script exits 0 silently (per-guard "ok" lines are emitted to
# stderr for visibility in CI logs).
#
# Guards:
#   1. No reachable `print()` in src/kwin_mcp/ (excluding __main__ blocks)
#   2. Every `kwin_wayland` invocation in tests/, scripts/, docs/ has --virtual
#   3. core.py does not import accessibility_worker at module top
#   4. No os.fork or raw multiprocessing.Process( in src/kwin_mcp/

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

fail=0

log_ok() {
    echo "[ci_guards] OK: $1" >&2
}

log_fail() {
    echo "[ci_guards] FAIL: $1" >&2
    fail=1
}

# ---------------------------------------------------------------------------
# Guard 1: No reachable `print()` in MCP server code paths under src/kwin_mcp/.
# Excludes:
#   - __main__.py (module entrypoint guard blocks)
#   - cli.py (interactive REPL whose contract IS to write to stdout)
# Rationale: any print() reachable from server.py would corrupt the MCP
# stdio JSON-RPC stream.
# ---------------------------------------------------------------------------
guard_1_no_print() {
    local matches
    matches="$(grep -rn -E '^[[:space:]]*print\(' src/kwin_mcp/ \
        --include='*.py' 2>/dev/null \
        | grep -v '__main__' \
        | grep -v 'src/kwin_mcp/cli.py:' || true)"
    if [[ -n "$matches" ]]; then
        log_fail "Guard 1 - reachable print() found in MCP server code paths (forbidden, use logging instead):"
        echo "$matches" >&2
        return
    fi
    log_ok "Guard 1 - no reachable print() in MCP server code paths"
}

# ---------------------------------------------------------------------------
# Guard 2: Every kwin_wayland reference in tests/, scripts/, docs/ uses
# --virtual on the same line.
# ---------------------------------------------------------------------------
guard_2_kwin_virtual() {
    local matches=""
    for d in tests scripts docs; do
        if [[ -d "$d" ]]; then
            local found
            # Exclusions (non-launch references):
            #   - this guards script itself (documents kwin_wayland)
            #   - pkill cleanup lines (process kill, not a launch)
            #   - --version queries (introspection, not a launch)
            #   - lines whose first non-whitespace token after `path:lineno:` is `echo`
            #     (documentation strings inside shell scripts)
            found="$(grep -rn 'kwin_wayland' "$d" 2>/dev/null \
                | grep -v -- '--virtual' \
                | grep -v 'scripts/ci_guards.sh' \
                | grep -v 'pkill' \
                | grep -v -- '--version' \
                | grep -Pv '^\S+:\s*echo\s' || true)"
            if [[ -n "$found" ]]; then
                matches+="$found"$'\n'
            fi
        fi
    done
    if [[ -n "$matches" ]]; then
        log_fail "Guard 2 - kwin_wayland invocation without --virtual in tests/scripts/docs:"
        printf '%s' "$matches" >&2
        return
    fi
    log_ok "Guard 2 - all kwin_wayland references in tests/scripts/docs use --virtual"
}

# ---------------------------------------------------------------------------
# Guard 3: src/kwin_mcp/core.py must not import accessibility_worker at module
# top level. Uses Python AST so conditional/inline imports are tolerated.
# ---------------------------------------------------------------------------
guard_3_no_module_top_worker_import() {
    local target="src/kwin_mcp/core.py"
    if [[ ! -f "$target" ]]; then
        log_ok "Guard 3 - $target absent, skipping"
        return
    fi
    local result
    result="$(python3 - "$target" <<'PYEOF'
import ast
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    tree = ast.parse(f.read(), filename=path)

violations = []
for node in tree.body:
    if isinstance(node, ast.Import):
        for alias in node.names:
            if "accessibility_worker" in alias.name:
                violations.append(f"line {node.lineno}: import {alias.name}")
    elif isinstance(node, ast.ImportFrom):
        mod = node.module or ""
        if "accessibility_worker" in mod:
            names = ", ".join(a.name for a in node.names)
            violations.append(f"line {node.lineno}: from {mod} import {names}")
        else:
            for alias in node.names:
                if "accessibility_worker" in alias.name:
                    violations.append(
                        f"line {node.lineno}: from {mod} import {alias.name}"
                    )

if violations:
    print("\n".join(violations))
    sys.exit(1)
PYEOF
)" || {
        log_fail "Guard 3 - core.py has module-top accessibility_worker import (must be lazy/local):"
        echo "$result" >&2
        return
    }
    log_ok "Guard 3 - core.py has no module-top accessibility_worker import"
}

# ---------------------------------------------------------------------------
# Guard 4: No os.fork or raw multiprocessing.Process( in src/kwin_mcp/.
# ---------------------------------------------------------------------------
guard_4_no_fork_or_process() {
    local matches
    matches="$(grep -rn -E 'os\.fork\b|multiprocessing\.Process\(' src/kwin_mcp/ \
        --include='*.py' 2>/dev/null || true)"
    if [[ -n "$matches" ]]; then
        log_fail "Guard 4 - os.fork or multiprocessing.Process( found in src/kwin_mcp/ (forbidden):"
        echo "$matches" >&2
        return
    fi
    log_ok "Guard 4 - no os.fork or multiprocessing.Process( in src/kwin_mcp/"
}

guard_1_no_print
guard_2_kwin_virtual
guard_3_no_module_top_worker_import
guard_4_no_fork_or_process

if [[ "$fail" -ne 0 ]]; then
    echo "[ci_guards] One or more guards failed." >&2
    exit 1
fi

echo "[ci_guards] All guards passed." >&2
exit 0
