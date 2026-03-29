# Docs SEO Consistency Check

Check whether documentation needs updating after code changes.

## Usage

```
/check-docs-seo
```

Automatically invoked at git commit time via the PostToolUse hook. Can also be invoked manually at any time.

## Instructions

### Step 1: Run the consistency checker

Run `scripts/check_docs_seo.py` to detect missing SEO keywords and positioning terms:

```bash
python3 scripts/check_docs_seo.py
```

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
