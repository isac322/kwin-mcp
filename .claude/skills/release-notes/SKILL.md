# Release Notes Generator

Generate SEO-optimized GitHub release notes for kwin-mcp.

## Usage

```
/release-notes <version>
```

Example: `/release-notes v0.5.1`

## Instructions

### Step 1: Gather Context

1. Read `CHANGELOG.md` and find the entry for the specified version
2. Read `pyproject.toml` to confirm the current version and project metadata
3. Check existing GitHub releases for style consistency:
   ```bash
   gh release list --limit 5
   gh release view <previous-version>
   ```

### Step 1.5: Verify plugin manifests + bundled SKILL.md are in sync

Before generating release notes, ensure all integrations (`.claude-plugin/marketplace.json`, `integrations/claude-code/.claude-plugin/plugin.json`, `integrations/opencode/plugin/package.json`, and the OpenCode plugin's bundled `SKILL.md`) reflect the version being released:

```bash
python3 scripts/sync_plugin_version.py        # write synced state
python3 scripts/sync_plugin_version.py --check # verify (CI also runs this)
```

If new MCP tools were added or renamed in this release, also confirm that `integrations/claude-code/skills/kwin-desktop-automation/SKILL.md` reflects them — the OpenCode plugin auto-mirrors that file on its next `npm run build`. The release notes' New Tools table should match what the SKILL.md exposes.

### Step 2: Determine Release Type

- **Patch release** (x.y.Z): bug fixes, minor improvements
- **Minor release** (x.Y.0): new features, new tools, non-breaking changes
- **Major release** (X.0.0): breaking changes

### Step 3: Generate Release Notes

#### Minor+ Release Template

```markdown
kwin-mcp vX.Y.Z <one-line value proposition with primary keywords>.

## Highlights

- **Feature name**: Description mentioning exact technology (AT-SPI2, libei, EIS, D-Bus, etc.)
- **Feature name**: Description with concrete numbers (e.g. "17 new MCP tools")
- ...

## New Tools

| Tool | Description |
|------|-------------|
| `tool_name` | What it does |
| ... | ... |

## Installation

\```bash
# Using uv (recommended)
uv tool install kwin-mcp

# Using pip
pip install kwin-mcp
\```

**Full Changelog**: https://github.com/isac322/kwin-mcp/compare/vPREVIOUS...vCURRENT
```

#### Patch Release Template

```markdown
kwin-mcp vX.Y.Z <one-line summary of what was fixed/improved>.

## What's Changed

- **Change description**: Detailed explanation mentioning exact technologies and tool names

**Full Changelog**: https://github.com/isac322/kwin-mcp/compare/vPREVIOUS...vCURRENT
```

### Step 4: SEO Rules for Release Notes

- **Language**: Always English
- **First sentence**: Must convey the value proposition (what the user gains)
- **Technology names**: Always use exact names — AT-SPI2, libei, EIS, KWin ScreenShot2, D-Bus, PyGObject, wl-clipboard, wtype
- **Tool names**: Use backtick code format with bare names (e.g. `mouse_click`, `accessibility_tree`). Hosts add their own prefix (`mcp__kwin-mcp__*` for Claude Code, `kwin-mcp_*` for OpenCode) — never hardcode prefixes in release notes
- **Tool counts**: Include total tool count when relevant (e.g. "now with 29 MCP tools"). When the count changes, also update `.claude/positioning.yml § product.tool_count`, `.claude/positioning.yml § drift_detection.tool_count_canonical`, `check_docs_seo.py § TOOL_COUNT_CANONICAL`, README tool tables, and the SKILL.md "X capabilities" reference before tagging the release
- **Plugin sync**: If the release ships new tools, mention that the bundled plugins were updated. Existing users will see the new guidance after a `/plugin update` (Claude Code) or after the OpenCode plugin's next `npm run build` mirrors the SKILL.md
- **Comparison link**: Always end with a Full Changelog comparison link
- **No emojis**: Do not use emojis in release notes

### Step 5: Present for Review

Show the generated release notes to the user for review before creating the GitHub release. Ask for confirmation before running:

```bash
gh release create vX.Y.Z --title "vX.Y.Z" --notes "..."
```
