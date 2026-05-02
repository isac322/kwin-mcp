# Docs SEO Consistency Check

Check whether documentation needs updating after code changes.

## Usage

```
/check-docs-seo
```

Automatically invoked at git commit time via the PostToolUse hook. Can also be invoked manually at any time.

## Instructions

### Step 1: Run the consistency checker

Run `scripts/check_docs_seo.py` to detect missing SEO keywords, positioning terms, plugin manifest keyword drift, SKILL.md identity violations, and tool-count drift:

```bash
python3 scripts/check_docs_seo.py
```

The checker covers documentation SEO, plugin manifest keyword sync (`.claude-plugin/marketplace.json` + `integrations/*/plugin.json` + `package.json` must include the required keyword subset), SKILL.md byte-identity (Claude Code source ↔ OpenCode plugin mirror), and tool-count consistency (auto-detected from `src/kwin_mcp/server.py`).

### Step 1.5: Run the plugin version + SKILL sync checker

If `check_docs_seo.py` reports SKILL.md drift or version mismatches, run the dedicated sync script. It is idempotent and can either repair (no flag) or just verify (`--check`):

```bash
python3 scripts/sync_plugin_version.py --check  # CI mode, no writes
python3 scripts/sync_plugin_version.py          # writer mode
```

After repair, re-run `check_docs_seo.py` to confirm cleanliness.

### Step 2: Evaluate the result

- **All checks passed**: Report no-op and stop. No documentation changes needed.
- **Issues detected**: The script reports which files are missing which terms. Continue to Step 3.

### Step 3: Invoke the docs-seo agent

If issues are detected, determine the trigger labels by checking which files were changed in the current commit (or staged changes):

```bash
git diff --name-only HEAD~1 HEAD
```

Then use the `@docs-seo` agent with the changed files and trigger labels as context. The agent follows its Evaluation Workflow (defined in `.claude/agents/docs-seo.md`) to:

1. Read `.claude/positioning.yml` (canonical source of truth)
2. Assemble the input set for each active trigger label
3. Detect gaps in documentation files
4. Update only the output targets for active trigger labels
5. Report no-op for any scope where no changes are needed

### Step 4: Report

Summarize what was checked and what was changed (or that no changes were needed).
