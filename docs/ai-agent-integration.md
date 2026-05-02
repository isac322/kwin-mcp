# AI Agent Integration

Three onboarding paths for `kwin-mcp`. Pick the one that matches your editor.

## Why use the integrations?

`kwin-mcp` exposes **30 MCP tools**. Without context, an AI agent often calls them in the wrong order — skipping `session_start`, mixing up `keyboard_type` vs `keyboard_type_unicode`, ignoring the AT-SPI2 surface-local coordinate system, and so on.

Each integration package bundles:

- The MCP server config, so you don't have to edit JSON by hand.
- The `kwin-desktop-automation` **skill**, which loads operational guidance into the agent only when desktop-automation work is requested. The skill teaches:
  1. **Which session mode to pick** — `session_start` for virtual / isolated testing, `session_connect` for live desktop / container / kiosk.
  2. **The observe → act → verify loop**, with observation tools ordered by cost (`list_windows` < `accessibility_tree` < `find_ui_elements` < `wait_for_element` < `screenshot`).
  3. **Pitfalls** — US-QWERTY-only `keyboard_type`, Unicode via `keyboard_type_unicode`, clipboard opt-in on virtual sessions, AT-SPI2 surface-local coordinates, QMenu invisibility, screen-edge triggers ignored by EIS.
  4. **Cleanup** — always call `session_stop`; `keep_screenshots=true` and `keep_home=true` leak `/tmp` directories.

## Scenario A — Claude Code only

```text
/plugin marketplace add isac322/kwin-mcp
/plugin install kwin-mcp@kwin-mcp
```

That installs:

- `kwin-mcp` MCP server (`uvx kwin-mcp`) registered for the current project.
- `kwin-desktop-automation` skill, auto-loaded by Claude Code when the user asks for desktop / GUI / Wayland / KDE automation.

If you prefer to skip the plugin and configure manually, see [README — Configuration](../README.md#configuration).

## Scenario B — OpenCode only

In your project's `opencode.json` (or `~/.config/opencode/opencode.json` for global):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["@isac322/kwin-mcp-opencode"]
}
```

On the next OpenCode startup the plugin's `config` hook:

1. Injects the `kwin-mcp` MCP server (`uvx kwin-mcp`) into OpenCode's runtime config — equivalent to writing a `mcp.kwin-mcp` block into `opencode.json` by hand, but added programmatically (idempotent — re-runs are no-ops).
2. Adds the bundled skill directory to `skills.paths` so OpenCode discovers `kwin-desktop-automation` SKILL.md shipped inside the npm package.

OpenCode loads the skill lazily via the `skill` tool whenever the agent decides desktop automation is in scope. The plugin never copies files outside the npm package, so `npm uninstall @isac322/kwin-mcp-opencode` cleanly removes both registrations on next startup.

### Manual fallback

If the plugin's `config` hook ever fails to inject the MCP server (e.g. OpenCode's plugin contract changed), drop the contents of [`integrations/opencode/opencode.json.example`](../integrations/opencode/opencode.json.example) into your `opencode.json`:

```jsonc
{
  "mcp": {
    "kwin-mcp": {
      "type": "local",
      "command": ["uvx", "kwin-mcp"],
      "enabled": true
    }
  }
}
```

## Scenario C — Both Claude Code and OpenCode

Do both Scenario A and Scenario B. Each integration is self-contained — installing one does not affect the other.

**Both plugins ship the same host-agnostic SKILL.md.** The Claude Code plugin keeps its copy under [`integrations/claude-code/skills/kwin-desktop-automation/SKILL.md`](../integrations/claude-code/skills/kwin-desktop-automation/SKILL.md) (the source of truth); the OpenCode plugin's `npm run build` copies that file into [`integrations/opencode/plugin/skill/kwin-desktop-automation/SKILL.md`](../integrations/opencode/plugin/skill/kwin-desktop-automation/SKILL.md) before packaging. The skill body uses bare tool names (`session_start`, `screenshot`, ...) — each host adds its own prefix (`mcp__kwin-mcp__*` in Claude Code, `kwin-mcp_*` in OpenCode) when exposing the tools to the model.

## Customising the skill

Both plugins ship the same SKILL.md content; the Claude Code plugin's copy is the source of truth, and the OpenCode plugin re-copies it on every build. To override either for yourself:

- **Claude Code**: copy the shipped file to `~/.claude/skills/kwin-desktop-automation/SKILL.md` (personal scope) or `<project>/.claude/skills/kwin-desktop-automation/SKILL.md` (project scope). Personal and project skills override plugin skills with the same name.
- **OpenCode**: drop your own `SKILL.md` under `~/.config/opencode/skills/<name>/SKILL.md` (or `<project>/.opencode/skills/`). The plugin never writes into your skill directories — the shipped SKILL.md is read-only inside the npm package, served via `skills.paths` injection. OpenCode keys skills by their frontmatter `name`, so reuse `kwin-desktop-automation` to override the shipped one, or pick a unique name (e.g. `kwin-desktop-automation-custom`) to keep both.

## Troubleshooting

### `uvx: command not found`

The MCP launch command is `uvx kwin-mcp`. Install [`uv`](https://docs.astral.sh/uv/) first:

```bash
# Arch / Manjaro
sudo pacman -S uv

# Fedora
sudo dnf install uv
```

If you prefer a global install of `kwin-mcp` itself, run `uv tool install kwin-mcp` and replace `"command": ["uvx", "kwin-mcp"]` with `"command": ["kwin-mcp"]` in the OpenCode config (or the equivalent for Claude Code's `.mcp.json`).

### Plugin installs but the agent ignores it

The skill is only loaded when the agent decides it's relevant. Make your prompt mention desktop / GUI / Wayland / KDE work explicitly (for example, *"using kwin-mcp, launch kcalc and..."*). Both Claude Code and OpenCode index the skill's `description` and `when_to_use` frontmatter for this trigger decision.

### `session_start` works but `accessibility_tree` returns nothing

The launched app may not yet have registered with AT-SPI2. Add a `wait_for_element` poll after launch, or set `expected_states=["active"]`. For some Qt apps, set `QT_ACCESSIBILITY=1` via the `env` argument to `session_start` or `launch_app`.

### Live session: input lands on the host instead of the target window

Make sure the target window is focused before injecting input. Call `focus_window(app_name=...)` first. EIS injects globally into the compositor, so whichever window currently has keyboard focus receives the event.

### Container / kiosk: `session_connect` cannot reach the live KWin

`session_connect` needs `DBUS_SESSION_BUS_ADDRESS` and `WAYLAND_DISPLAY` of the live KWin instance. In a container, mount the host's `XDG_RUNTIME_DIR` socket directory and pass these env vars through. See the README's [*Live Desktop Collaboration*](../README.md#live-desktop-collaboration) use case for the full setup.

## See also

- [README — Configuration](../README.md#configuration) — manual `.mcp.json` config
- [README — Limitations](../README.md#limitations) — full pitfall list
- [`integrations/claude-code/`](../integrations/claude-code/) — Claude Code plugin source
- [`integrations/opencode/plugin/`](../integrations/opencode/plugin/) — OpenCode plugin source
