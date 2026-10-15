# AI Agent Integration

Three onboarding paths for `kwin-mcp`. Pick the one that matches your editor.

## Why use the integrations?

`kwin-mcp` exposes **29 MCP tools**. Without context, an AI agent often calls them in the wrong order — skipping `session_start`, typing non-ASCII text with the default `keyboard_type` method, clicking a guessed position instead of an element `ref`, forgetting to release held input, and so on.

Each integration package bundles:

- The MCP server config, so you don't have to edit JSON by hand.
- The `kwin-desktop-automation` **skill**, which loads operational guidance into the agent only when desktop-automation work is requested. The skill teaches:
  1. **Which session mode to pick** — `session_start` for virtual / isolated testing, `session_connect` for live desktop / container / kiosk.
  2. **The observe → act → verify loop**, with observation tools ordered by cost (`window_list` < `element_query` < `element_query` with `wait` < `screen_capture`) and actions ordered by robustness (`element_act`, then a `{ref}` target, then `{x, y}`).
  3. **Pitfalls** — US-QWERTY-only `keyboard_type(method="keys")`, Unicode via `keyboard_type(method="unicode")`, clipboard opt-in on virtual sessions, global logical coordinates, held input, QMenu invisibility, screen-edge triggers ignored by EIS.
  4. **Cleanup** — always call `session_stop`; `retention={"artifacts": true}` and `retention={"home": true}` keep `/tmp` directories that you must delete.

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

**Both plugins ship the same host-agnostic SKILL.md.** The Claude Code plugin keeps its copy under [`integrations/claude-code/skills/kwin-desktop-automation/SKILL.md`](../integrations/claude-code/skills/kwin-desktop-automation/SKILL.md) (the source of truth); the OpenCode plugin's `npm run build` copies that file into [`integrations/opencode/plugin/skill/kwin-desktop-automation/SKILL.md`](../integrations/opencode/plugin/skill/kwin-desktop-automation/SKILL.md) before packaging. The skill body uses bare tool names (`session_start`, `screen_capture`, ...) — each host adds its own prefix (`mcp__kwin-mcp__*` in Claude Code, `kwin-mcp_*` in OpenCode) when exposing the tools to the model.

## Customising the skill

Both plugins ship the same SKILL.md content; the Claude Code plugin's copy is the source of truth, and the OpenCode plugin re-copies it on every build. To override either for yourself:

- **Claude Code**: copy the shipped file to `~/.claude/skills/kwin-desktop-automation/SKILL.md` (personal scope) or `<project>/.claude/skills/kwin-desktop-automation/SKILL.md` (project scope). Personal and project skills override plugin skills with the same name.
- **OpenCode**: drop your own `SKILL.md` under `~/.config/opencode/skills/<name>/SKILL.md` (or `<project>/.opencode/skills/`). The plugin never writes into your skill directories — the shipped SKILL.md is read-only inside the npm package, served via `skills.paths` injection. OpenCode keys skills by their frontmatter `name`, so reuse `kwin-desktop-automation` to override the shipped one, or pick a unique name (e.g. `kwin-desktop-automation-custom`) to keep both.

## Server flags

`kwin-mcp` accepts four optional server flags. Pass them after the command in any MCP config:

- `--default-live-session` tells the client that the server is meant for the live desktop: `session_connect`'s description recommends it and the server instructions say so. Tool behavior is unchanged.
- `--screenshot-images` attaches every PNG captured by `screen_capture` and by the `capture` option of the action tools to the tool result as MCP image content, so the agent sees the frames without reading files. Result text is unchanged. It is off by default because each frame costs model context tokens, and in live sessions it sends real-desktop pixels to the model provider.
- `--screenshot-max-edge N` downscales every screenshot and frame so its longer side is at most `N` pixels, and states the pixel-to-logical mapping in the result. Use it when the desktop is larger than the model's image limit, and pick a bound that keeps each image within the provider's pixel and token limits, so the provider does not resize it again behind the coordinate line (according to [Anthropic's vision documentation](https://platform.claude.com/docs/en/build-with-claude/vision#evaluate-image-size), checked 2026-10-10, Claude 4.7 and later take 2576 pixels on the long edge and 4784 visual tokens). See [Screenshot Size](../README.md#screenshot-size---screenshot-max-edge-region-max_edge).
- `--live-consent {elicit,refuse}` decides how `window_close` on a live desktop asks the user first: `elicit` (the default) shows a confirmation form when the client supports MCP form elicitation; `refuse` never asks and refuses the close, for headless clients. See [Live Window Close Consent](../README.md#live-window-close-consent---live-consent).

Claude Code (`claude mcp add` or `.mcp.json`):

```bash
claude mcp add kwin-mcp -- uvx kwin-mcp --screenshot-images
```

```json
{
  "mcpServers": {
    "kwin-mcp": {
      "command": "uvx",
      "args": ["kwin-mcp", "--screenshot-images"]
    }
  }
}
```

OpenCode (the [manual fallback](#manual-fallback) config):

```jsonc
{
  "mcp": {
    "kwin-mcp": {
      "type": "local",
      "command": ["uvx", "kwin-mcp", "--screenshot-images"],
      "enabled": true
    }
  }
}
```

## Troubleshooting

### `uvx: command not found`

The MCP launch command is `uvx kwin-mcp`. This error means `uv` itself is missing; it is not a kwin-mcp build failure. Install [`uv`](https://docs.astral.sh/uv/) first:

```bash
# Arch / Manjaro
sudo pacman -S uv

# Fedora
sudo dnf install uv
```

If you prefer a global install of `kwin-mcp` itself, run `uv tool install kwin-mcp`. Then replace `"command": ["uvx", "kwin-mcp"]` with `"command": ["kwin-mcp"]` in the OpenCode config. In Claude Code's `.mcp.json`, set `"command": "kwin-mcp"` and remove the `"args": ["kwin-mcp"]` entry.

### `uvx kwin-mcp` or `uv tool install kwin-mcp` fails while building `pygobject` or `dbus-python`

`uv` is installed, but installing kwin-mcp failed while compiling a native dependency (for example, a missing compiler, `pkg-config`, `Python.h`, or a missing `cairo`, `girepository-2.0`, or `dbus-1` pkg-config package). `uvx` and `uv tool install` create an isolated environment that cannot use the distribution's `gi` (PyGObject) or `dbus` Python packages, so `pygobject` and `dbus-python` are built from source inside it.

Install the build dependencies for your distribution from the README's [Build Prerequisites for uv and pip Installs](../README.md#build-prerequisites-for-uv-and-pip-installs) section, then rerun the same command. The MCP config does not need to change.

### Server exits with `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`

This is a Python dependency problem, not a missing system package. kwin-mcp 0.7.0 on PyPI did not declare an upper bound for the `mcp` package, so a fresh install could resolve `mcp` 2.x, which no longer provides `mcp.server.fastmcp`. Releases 0.8.0–0.9.x cap `mcp<2`, and releases from 0.11.0 on run on `mcp>=2.2.0,<3`, so upgrading kwin-mcp resolves the error; installing system build dependencies does not.

### Plugin installs but the agent ignores it

The skill is only loaded when the agent decides it's relevant. Make your prompt mention desktop / GUI / Wayland / KDE work explicitly (for example, *"using kwin-mcp, launch kcalc and..."*). Both Claude Code and OpenCode index the skill's `description` and `when_to_use` frontmatter for this trigger decision.

### `session_start` works but `element_query` returns nothing

The launched app may not yet have registered with AT-SPI2. Poll with `element_query(locator={"app": "<app>"}, wait={"timeout_ms": 5000})` after launch, or wait for a state with `locator={"states": ["active"]}`. For some Qt apps, set `QT_ACCESSIBILITY=1` via the `env` argument of `app_launch`.

### Live session: input lands on the host instead of the target window

Make sure the target window is focused before injecting input. Call `window_focus(window={"id": ...})` with an id from `window_list` (or an element's `window_id`); otherwise use `window_focus(window={"app_id": ..., "kind": ["normal"]})`. EIS injects globally into the compositor, so whichever window currently has keyboard focus receives the event.

### Upgrading from kwin-mcp 0.11

kwin-mcp 1.0 renamed most tools and reshaped their arguments. An agent or prompt that still uses old tool names gets an error naming the 1.0 replacement. Update the plugin (the bundled skill already teaches the 1.0 names) and see the [migration tables in the CHANGELOG](../CHANGELOG.md#100---2026-10-12).

### Container / kiosk: `session_connect` cannot reach the live KWin

`session_connect` needs `DBUS_SESSION_BUS_ADDRESS` and `WAYLAND_DISPLAY` of the live KWin instance. In a container, mount the host's `XDG_RUNTIME_DIR` socket directory and pass these env vars through. See the README's [*Live Desktop Collaboration*](../README.md#live-desktop-collaboration) use case for the full setup.

## See also

- [README — Configuration](../README.md#configuration) — manual `.mcp.json` config
- [README — Limitations](../README.md#limitations) — full pitfall list
- [`integrations/claude-code/`](../integrations/claude-code/) — Claude Code plugin source
- [`integrations/opencode/plugin/`](../integrations/opencode/plugin/) — OpenCode plugin source
