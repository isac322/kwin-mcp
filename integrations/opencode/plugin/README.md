# @isac322/kwin-mcp-opencode

OpenCode plugin for [`kwin-mcp`](https://github.com/isac322/kwin-mcp) — the MCP server for Linux desktop GUI automation on KDE Plasma 6 Wayland.

On every OpenCode backend startup the plugin's V2 `setup` hook:

1. **Registers the `kwin-mcp` MCP server** through `ctx.mcp.transform`, equivalent to a static local server entry but without requiring users to edit config.
2. **Registers the bundled skill** through `ctx.skill.transform`. The skill teaches which kwin-mcp tool to call when, plus the platform pitfalls of Wayland / AT-SPI2 / EIS.

Both steps are idempotent — re-running OpenCode is a no-op. The plugin never copies files outside the npm package, so removing the plugin from the configured `plugins` list and reloading OpenCode removes both registrations from the active runtime config.

## Installation

In your `opencode.json`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": ["@isac322/kwin-mcp-opencode"]
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

If you do not want to install the plugin, configure the local `kwin-mcp` server through OpenCode's V2 MCP configuration and add the bundled skill through another V2 skill source.

## Bundled skill

This package ships the host-agnostic `kwin-desktop-automation` skill (mirrored from [`integrations/claude-code/skills/kwin-desktop-automation/SKILL.md`](https://github.com/isac322/kwin-mcp/blob/main/integrations/claude-code/skills/kwin-desktop-automation/SKILL.md) during `npm run build`), covering session-mode selection (`session_start` vs `session_connect`), the observe → act → verify loop, Wayland/AT-SPI2/EIS platform pitfalls, and session cleanup.

## Customising the skill

The shipped skill lives inside the npm package (read-only). To override or extend it without touching the package, drop your own `SKILL.md` into a directory OpenCode already scans:

- Personal scope: `~/.claude/skills/<name>/SKILL.md`, `~/.config/opencode/skills/<name>/SKILL.md`, or `~/.agents/skills/<name>/SKILL.md`
- Project scope: `<project>/.claude/skills/<name>/SKILL.md`, `<project>/.opencode/skills/<name>/SKILL.md`, or `<project>/.agents/skills/<name>/SKILL.md`

The `.agents/skills` paths are OpenCode's official Agent Skills-compatible locations. OpenCode keys skills by their frontmatter `name`, so reuse `kwin-desktop-automation` to override the shipped one, or pick a unique name (e.g. `kwin-desktop-automation-custom`) to keep both alongside each other.

## Uninstall

1. Remove the configured plugin entry from `opencode.json`:

   ```jsonc
   {
     "plugins": []
   }
   ```

   If other plugins are configured, remove only `"@isac322/kwin-mcp-opencode"` (or the target you configured).
2. Remove the cached OpenCode package:

   ```bash
   opencode plugin remove @isac322/kwin-mcp-opencode
   ```

   Use the exact configured target if it differs. OpenCode installs npm plugins into its own cache, so `npm uninstall` in a project does not remove the cached package.
3. Reload or restart OpenCode. The MCP server registration and bundled skill will no longer be loaded.

## Building from source

```bash
cd integrations/opencode/plugin
npm install
npm run build  # copies SKILL.md from ../../claude-code/skills/, then swc compiles src/ → dist/index.js (no declaration emit)
```

The published npm tarball ships only `dist/`, `skill/`, `README.md`, and `LICENSE` — `src/` and `tsconfig.json` are dev-only.

## Compatibility

- OpenCode (anomalyco/opencode) v2.0+ — uses the V2 `Plugin.define({ id, setup })` contract from [`@opencode/plugin`](https://www.npmjs.com/package/@opencode/plugin), with MCP and skill transforms.
- This package is V2-only. OpenCode V1 is not supported.
- Bun runtime (OpenCode runs plugins on Bun).
- Build toolchain: [`swc`](https://swc.rs/) for transpile (`src/` → `dist/index.js`) and [`tsgo`](https://github.com/microsoft/typescript-go) (TypeScript v7 native preview, package `@typescript/native-preview`) for typecheck-only.
- Linux with KDE Plasma 6 Wayland to actually run the MCP server.

## License

MIT — see [LICENSE](https://github.com/isac322/kwin-mcp/blob/main/LICENSE).
