# @isac322/kwin-mcp-opencode

OpenCode plugin for [`kwin-mcp`](https://github.com/isac322/kwin-mcp) — the MCP server for Linux desktop GUI automation on KDE Plasma 6 Wayland.

On every OpenCode backend startup the plugin's `config` hook:

1. **Registers the `kwin-mcp` MCP server** by injecting `mcp.kwin-mcp = { type: "local", command: ["uvx", "kwin-mcp"], enabled: true }` into the runtime config — equivalent to a static block in `opencode.json`, but added programmatically so users do not have to edit config.
2. **Adds the bundled skill directory to `skills.paths`** so OpenCode discovers the `kwin-desktop-automation` SKILL.md shipped inside the npm package. The skill teaches which kwin-mcp tool to call when, plus the platform pitfalls of Wayland / AT-SPI2 / EIS.

Both steps are idempotent — re-running OpenCode is a no-op. The plugin never copies files outside the npm package, so `npm uninstall @isac322/kwin-mcp-opencode` cleanly removes both the MCP server registration and the skill on next startup.

## Installation

In your `opencode.json`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["@isac322/kwin-mcp-opencode"]
}
```

OpenCode resolves the npm package and runs the plugin on every startup.

The plugin assumes [`uvx`](https://docs.astral.sh/uv/) is on your `PATH` and uses `uvx kwin-mcp` to launch the MCP server. Install `uv` first:

```bash
# Arch / Manjaro
sudo pacman -S uv

# Fedora
sudo dnf install uv

# Ubuntu / Debian
# see https://docs.astral.sh/uv/getting-started/installation/
```

You also need the kwin-mcp system dependencies (`kwin_wayland`, `at-spi2-core`, `spectacle`, `python-gobject`, `dbus-python`, optionally `wl-clipboard` and `wtype`). See [the kwin-mcp README](https://github.com/isac322/kwin-mcp#installing-system-dependencies) for distro-specific commands.

## Manual fallback

If the plugin's `config` hook ever fails to inject the MCP server (e.g. OpenCode's plugin contract changed), add a static block to `opencode.json`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "kwin-mcp": {
      "type": "local",
      "command": ["uvx", "kwin-mcp"],
      "enabled": true
    }
  }
}
```

A copy of this snippet ships at [`opencode.json.example`](https://github.com/isac322/kwin-mcp/blob/main/integrations/opencode/opencode.json.example).

## What the skill teaches

`kwin-desktop-automation` is a host-agnostic `SKILL.md` shipped inside this npm package. It uses bare tool names (`session_start`, `screenshot`, etc.) — OpenCode adds its own `kwin-mcp_` prefix when exposing the tools to the model, so the agent learns the actual tool symbols from OpenCode's tool list rather than from the skill body. (The same SKILL.md content is shared with the [Claude Code plugin](https://github.com/isac322/kwin-mcp/tree/main/integrations/claude-code); `npm run build` copies the source from `../../claude-code/skills/...` into this package's `skill/` directory before packaging.) The skill covers:

1. **Session mode selection** — when to call `session_start` (virtual / isolated) vs `session_connect` (live / real desktop / container / kiosk).
2. **The observe → act → verify loop** — observation tools ordered by cost (`list_windows` < `accessibility_tree` < `find_ui_elements` < `wait_for_element` < `screenshot`).
3. **Pitfalls** — `keyboard_type` is US-QWERTY only (use `keyboard_type_unicode` for CJK), clipboard is opt-in on virtual sessions, AT-SPI2 coordinates are surface-local, QMenu can be invisible to AT-SPI2, screen-edge triggers ignore EIS pointer events, container live sessions need Wayland/D-Bus mounts.
4. **Cleanup** — always `session_stop`; `keep_screenshots=true` and `keep_home=true` leak `/tmp` directories.

## Customising the skill

The shipped skill lives inside the npm package (read-only). To override or extend it without touching the package, drop your own `SKILL.md` into a directory OpenCode already scans:

- Personal scope: `~/.claude/skills/<name>/SKILL.md` or `~/.config/opencode/skills/<name>/SKILL.md`
- Project scope: `<project>/.claude/skills/<name>/SKILL.md` or `<project>/.opencode/skills/<name>/SKILL.md`

OpenCode keys skills by their frontmatter `name`, so reuse `kwin-desktop-automation` to override the shipped one, or pick a unique name (e.g. `kwin-desktop-automation-custom`) to keep both alongside each other.

## Uninstall

`npm uninstall @isac322/kwin-mcp-opencode`, then restart OpenCode. Because the plugin never copies files outside the npm package, no manual cleanup is needed — both the MCP server registration and the skill drop out of the next runtime config automatically.

## Building from source

```bash
cd integrations/opencode/plugin
npm install
npm run build  # copies SKILL.md from ../../claude-code/skills/, then swc compiles src/ → dist/index.js (no declaration emit)
```

The published npm tarball ships only `dist/`, `skill/`, `README.md`, and `LICENSE` — `src/` and `tsconfig.json` are dev-only.

## Compatibility

- OpenCode (anomalyco/opencode) v1.14+ — uses the canonical `Plugin` function contract from [`@opencode-ai/plugin`](https://www.npmjs.com/package/@opencode-ai/plugin) and the `config` hook.
- Bun runtime (OpenCode runs plugins on Bun).
- Build toolchain: [`swc`](https://swc.rs/) for transpile (`src/` → `dist/index.js`) and [`tsgo`](https://github.com/microsoft/typescript-go) (TypeScript v7 native preview, package `@typescript/native-preview`) for typecheck-only.
- Linux with KDE Plasma 6 Wayland to actually run the MCP server.

## License

MIT — see [LICENSE](https://github.com/isac322/kwin-mcp/blob/main/LICENSE).
