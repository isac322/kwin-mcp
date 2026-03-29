#!/usr/bin/env bash
# PostToolUse hook: detect relevant code/doc changes and prompt docs-seo evaluation.
#
# Placed in .claude/hooks/ and referenced from .claude/settings.json so it runs
# automatically after Edit, Write, and Bash tool calls in every Claude Code session.
#
# When a changed file matches the trigger patterns defined in the docs-seo agent
# (.claude/agents/docs-seo.md section "Code Change -> Documentation Update Protocol"),
# this script outputs a JSON context message that instructs Claude Code to invoke
# the docs-seo agent with the relevant changed-file context.
#
# Exit 0 with no output = no-op (no relevant changes detected).

set -euo pipefail

INPUT="$(cat)"
TOOL="$(jq -r '.tool_name // empty' <<< "$INPUT")"

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"

# ---------------------------------------------------------------------------
# 1. Extract file path(s) from the tool input
# ---------------------------------------------------------------------------
FILES=()

case "$TOOL" in
  Edit|Write|MultiEdit)
    FILE="$(jq -r '.tool_input.file_path // empty' <<< "$INPUT")"
    [[ -n "$FILE" ]] && FILES+=("$FILE")
    ;;
  Bash)
    CMD="$(jq -r '.tool_input.command // empty' <<< "$INPUT")"
    # Only inspect git commit/add operations -- all other Bash calls are ignored
    if echo "$CMD" | grep -qE '(git commit|git add)'; then
      while IFS= read -r f; do
        [[ -n "$f" ]] && FILES+=("$PROJECT_DIR/$f")
      done < <(git -C "$PROJECT_DIR" diff --name-only HEAD 2>/dev/null || true)
      while IFS= read -r f; do
        [[ -n "$f" ]] && FILES+=("$PROJECT_DIR/$f")
      done < <(git -C "$PROJECT_DIR" diff --name-only --cached 2>/dev/null || true)
    else
      exit 0
    fi
    ;;
  *)
    exit 0
    ;;
esac

[[ ${#FILES[@]} -eq 0 ]] && exit 0

# ---------------------------------------------------------------------------
# 2. Map changed files to trigger labels (from docs-seo.md trigger table)
# ---------------------------------------------------------------------------
TRIGGER_LABELS=()
MATCHED_FILES=()

for FILE in "${FILES[@]}"; do
  # Normalize to project-relative path
  RELPATH="${FILE#"$PROJECT_DIR/"}"

  case "$RELPATH" in
    src/kwin_mcp/server.py)
      TRIGGER_LABELS+=("tool-registration")
      MATCHED_FILES+=("$RELPATH")
      ;;
    src/kwin_mcp/session.py)
      TRIGGER_LABELS+=("session-api")
      MATCHED_FILES+=("$RELPATH")
      ;;
    src/kwin_mcp/core.py)
      TRIGGER_LABELS+=("engine-api")
      MATCHED_FILES+=("$RELPATH")
      ;;
    src/kwin_mcp/*.py)
      TRIGGER_LABELS+=("code-general")
      MATCHED_FILES+=("$RELPATH")
      ;;
    pyproject.toml)
      TRIGGER_LABELS+=("package-metadata")
      MATCHED_FILES+=("$RELPATH")
      ;;
    CHANGELOG.md)
      TRIGGER_LABELS+=("changelog-update")
      MATCHED_FILES+=("$RELPATH")
      ;;
    README.md)
      TRIGGER_LABELS+=("readme-update")
      MATCHED_FILES+=("$RELPATH")
      ;;
    ROADMAP.md)
      TRIGGER_LABELS+=("roadmap-update")
      MATCHED_FILES+=("$RELPATH")
      ;;
    .claude/positioning.yml)
      TRIGGER_LABELS+=("manifest-update")
      MATCHED_FILES+=("$RELPATH")
      ;;
    *)
      # Also catch src/kwin_mcp/**/*.py with regex fallback
      if [[ "$RELPATH" =~ ^src/kwin_mcp/.*\.py$ ]]; then
        TRIGGER_LABELS+=("code-general")
        MATCHED_FILES+=("$RELPATH")
      fi
      ;;
  esac
done

[[ ${#TRIGGER_LABELS[@]} -eq 0 ]] && exit 0

# ---------------------------------------------------------------------------
# 3. Deduplicate and format for output
# ---------------------------------------------------------------------------
mapfile -t UNIQUE_LABELS < <(printf '%s\n' "${TRIGGER_LABELS[@]}" | sort -u)
mapfile -t UNIQUE_FILES  < <(printf '%s\n' "${MATCHED_FILES[@]}"  | sort -u)

LABELS_STR="$(IFS=', '; echo "${UNIQUE_LABELS[*]}")"
FILES_STR="$(IFS=', ';  echo "${UNIQUE_FILES[*]}")"

# ---------------------------------------------------------------------------
# 4. Emit context message instructing Claude Code to run the docs-seo agent
# ---------------------------------------------------------------------------
# Claude Code injects hookSpecificOutput.additionalContext into the next
# assistant turn so it is visible before Claude replies.
jq -n \
  --arg labels "$LABELS_STR" \
  --arg files  "$FILES_STR" \
  '{
    hookSpecificOutput: {
      hookEventName: "PostToolUse",
      additionalContext: (
        "⚡ [docs-seo auto-trigger] Trigger labels: [" + $labels + "]\n" +
        "Changed files: " + $files + "\n\n" +
        "ACTION REQUIRED: Use the docs-seo agent (`.claude/agents/docs-seo.md`) to evaluate documentation drift.\n" +
        "Pass context: changed_files=[" + $files + "], trigger_labels=[" + $labels + "]\n" +
        "The agent must: (1) read `.claude/positioning.yml` first, (2) assemble the input set for each active trigger label, (3) detect gaps in README.md / CONTRIBUTING.md / ROADMAP.md / CLAUDE.md / pyproject.toml, (4) update only the output targets listed in the docs-seo agent for the active labels, (5) report no-op if all no-op conditions are met."
      )
    }
  }'
