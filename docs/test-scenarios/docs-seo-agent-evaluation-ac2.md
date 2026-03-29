# docs-seo Agent Evaluation — Sub-AC 2 Verification

**Date**: 2026-03-29
**Purpose**: Verify the docs-seo agent produces concrete documentation update proposals when given a simulated new tool as input.

## Simulated Input

New MCP tool `session_record` (tool #31) was provided as input:

```python
@mcp.tool()
def session_record(
    duration_ms: int = 5000,
    fps: int = 10,
    output_format: str = "gif",       # "gif", "mp4", or "screenshots"
    include_cursor: bool = False,
) -> str:
    """Record the active session as an animated GIF, MP4 video, or screenshot sequence.

    Captures the virtual or live desktop session over the specified duration
    using KWin ScreenShot2 D-Bus at the given frame rate. Returns the path
    to the output file (GIF/MP4) or the list of screenshot file paths.
    Useful for creating CI artifacts, documentation animations, and
    debugging interaction sequences.
    """
```

Tool count change: 30 → 31.

## Evaluation Results

### Trigger Conditions Fired

| Trigger | Status |
|---|---|
| Trigger 1 — New product capability (session recording category) | **Fired** |
| Trigger 4 — pyproject.toml keyword drift (4 new terms absent) | **Fired** |
| Trigger 5 — Tool count change (30 → 31) | **Fired** |

### Decision Criteria

All four decision criteria pointed to "proceed":
1. New capability affects core product identity ✓
2. New terms absent from Target Search Intents ✓
3. Concrete numbers (30 MCP tools) become stale ✓
4. New recording use case warrants SEO rule coverage ✓

### Concrete Proposals Produced

#### 1. README.md — New tool table row

New "Recording (1 tool)" section after "Observation" section:

```markdown
### Recording (1 tool)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `session_record` | `duration_ms?` `int` (5000), `fps?` `int` (10), `output_format?` `str` ("gif"), `include_cursor?` `bool` (false) | Record the active session as an animated GIF, MP4 video, or screenshot sequence using KWin ScreenShot2 D-Bus. `output_format` accepts `"gif"`, `"mp4"`, or `"screenshots"`. Returns the output file path (GIF/MP4) or a list of screenshot file paths. Useful for CI artifact collection, documentation animations, and debugging interaction sequences. |
```

#### 2. README.md — Updated intro paragraph

Replace `"With 30 MCP tools covering mouse, keyboard, touch, clipboard, accessibility tree inspection, screenshot capture, and window management..."` with `"With 31 MCP tools covering mouse, keyboard, touch, clipboard, accessibility tree inspection, screenshot capture, session recording, and window management..."`

#### 3. README.md — ASCII architecture diagram

Update `kwin-mcp server  (30 tools)` → `kwin-mcp server  (31 tools)` and add:
```
  |-- session_record ------------> KWin ScreenShot2 D-Bus (burst capture → GIF/MP4/files)
```

#### 4. README.md — New "Use Cases" entry

```markdown
### Session Recording for CI Artifacts and Documentation

Use `session_record` to capture animated GIFs or MP4 videos of automated desktop interactions. Attach recordings as CI artifacts to document test failures, generate documentation animations, or create reproducible visual evidence of UI behavior without manual screen recording.
```

#### 5. pyproject.toml — 4 new keywords

```toml
"session-recording",
"screen-recording",
"gif-capture",
"ci-artifacts",
```

#### 6. CLAUDE.md — Updated Target Keywords

- Secondary tier: add `session_record`, `screen recording`
- Long-tail tier: add `record desktop session as GIF`, `Linux desktop screen recording MCP`, `CI artifact desktop recording`

#### 7. docs-seo.md — Updated Current Product Positioning

Tool count: `30 MCP tools` → `31 MCP tools` in both occurrences. Add `session recording` to the tool capability list.

#### 8. docs-seo.md — 2 new Target Search Intents

- **Session recording for CI/CD**: recording desktop automation runs as GIF or MP4 for CI artifacts
- **Documentation animations**: generating animated GIFs of desktop interactions using `session_record`

#### 9. docs-seo.md — Post-Edit Quality Checklist

Update `"30 MCP tools"` → `"31 MCP tools"` in the checklist item.

### Files That Would Be Modified

1. `README.md` — New tool table section, updated counts, new use case
2. `pyproject.toml` — 4 new keywords
3. `CLAUDE.md` — Updated Target Keywords
4. `.claude/agents/docs-seo.md` — Updated positioning, intents, checklist

## Verification Result

**PASS** — The docs-seo agent:
- Executed without errors
- Correctly identified 3 trigger conditions
- Applied all 4 decision criteria
- Produced 9 concrete, actionable proposals covering tool description, feature list, and SEO metadata
- No-op conditions correctly identified (SEO Principle 7 — no change needed)
- Observed file write constraint (proposals only, no files written)
