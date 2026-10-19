# kwin-mcp

**Model Context Protocol server for Linux desktop GUI automation on KDE Plasma 6 Wayland**

[![PyPI version](https://img.shields.io/pypi/v/kwin-mcp)](https://pypi.org/project/kwin-mcp/)
[![Downloads](https://img.shields.io/pypi/dm/kwin-mcp)](https://pypi.org/project/kwin-mcp/)
[![Python 3.12+](https://img.shields.io/pypi/pyversions/kwin-mcp)](https://pypi.org/project/kwin-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/isac322/kwin-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/isac322/kwin-mcp/actions/workflows/ci.yml)

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that enables AI agents (Claude Code, Cursor, and other MCP clients) to launch, interact with, and observe any Wayland application in an isolated virtual KWin session -- without affecting the user's desktop display or input. It also supports **live desktop automation** by connecting to an existing KWin session (real desktop or container) for collaborative workflows. With 29 MCP tools covering sessions, apps, screen capture, the AT-SPI2 accessibility tree, window management, mouse, keyboard, touch, clipboard, and D-Bus, kwin-mcp provides everything needed for end-to-end GUI testing and desktop automation on Linux.

> [!IMPORTANT]
> **kwin-mcp 1.0 renamed and reshaped the tools.** The 29 tools of 1.0 replace the 37 tools of 0.12, with shared argument types and structured output on every tool. A removed tool name returns an error that names its replacement. See [Migrating from 0.11](#migrating-from-011) (also covers 0.12).

## Table of Contents

- [Why kwin-mcp?](#why-kwin-mcp)
- [Use Cases](#use-cases)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Server Flags](#server-flags)
- [Available Tools](#available-tools)
- [Shared Argument Types](#shared-argument-types)
- [How It Works](#how-it-works)
- [System Requirements](#system-requirements)
- [Installation](#installation)
- [Migrating from 0.11](#migrating-from-011)
- [Limitations](#limitations)
- [End-to-End Testing](#end-to-end-testing)
- [Contributing](#contributing)
- [License](#license)

## Why kwin-mcp?

- **Isolated sessions** -- Each session runs in its own `dbus-run-session` + `kwin_wayland --virtual` compositor. Your host desktop never receives its windows or input. This is not a filesystem or network sandbox; see [Isolation scope](#isolation-scope).
- **Live session support** -- Connect to a real KDE Plasma desktop or a KWin instance inside a container (e.g. `systemd-nspawn`) for collaborative "share my screen" workflows.
- **No screenshots required for interaction** -- The AT-SPI2 accessibility tree gives the AI agent structured widget data (roles, names, coordinates, states, available actions). Every element comes with a `ref` that any pointer or touch tool accepts as a target, and `element_act` invokes an element's action without pointer coordinates at all.
- **Typed results** -- Every tool declares an MCP `outputSchema` and returns `structuredContent` next to a readable text rendering, so an agent can read window ids, element rectangles, and frame paths without parsing text.
- **Zero authorization prompts** -- Uses KWin's private EIS (Emulated Input Server) D-Bus interface directly, bypassing the XDG RemoteDesktop portal. No user confirmation dialogs.
- **Works with any Wayland app** -- Anything that runs on KDE Plasma 6 Wayland works: Qt, GTK, Electron, and more. Input is injected via the standard `libei` protocol.
- **Full input coverage** -- Mouse, keyboard, multi-touch, and clipboard -- all injected through the isolated session for complete desktop automation.

## Use Cases

### Automated GUI Testing

Run end-to-end GUI tests for KDE/Qt/GTK applications in headless isolated sessions. kwin-mcp launches each app in its own virtual KWin compositor, interacts via mouse, keyboard, and touch input, then verifies results through screenshots and the accessibility tree -- all without a physical display.

### AI-Driven Desktop Automation

Let AI agents like Claude Code autonomously operate desktop applications. The agent reads the accessibility tree to understand the UI, performs actions through 29 MCP tools, and observes the results via screenshots -- creating a complete feedback loop for any Wayland application.

### Live Desktop Collaboration

Connect to your real desktop session and let Claude observe and interact with what you see. Use `session_connect` or pass `--default-live-session` to make live mode the default. Also supports attaching to KWin running inside containers (e.g. `systemd-nspawn`) for isolated agent desktops.

### Headless GUI Testing in CI/CD

Integrate Linux desktop GUI testing into CI/CD pipelines. kwin-mcp's virtual sessions require no X11 or physical display server, making it suitable for headless environments like GitHub Actions or GitLab CI runners on Linux.

### Kiosk and Embedded Device Automation

Automate kiosk interfaces and embedded Linux desktops running KDE Plasma or a bare KWin Wayland compositor. Use `session_start` for isolated virtual testing of kiosk UIs, or `session_connect` to attach directly to a live kiosk or embedded device session for real-time automation and diagnostics.

## Quick Start

> Requires KDE Plasma 6 on Wayland. See [System Requirements](#system-requirements) for details.

**1. Install system and build dependencies**

`uv tool install` and `uvx` always install kwin-mcp into an isolated Python environment, and `pip install` does the same when run inside a virtual environment. An isolated environment cannot reuse your distribution's `python3-gi` or `python3-dbus` packages, so PyGObject, pycairo, and dbus-python are built from source during installation and need a C compiler and development headers. Install the packages from [Installing System Dependencies](#installing-system-dependencies) first. On Debian 13 (Trixie):

```bash
sudo apt-get install -y --no-install-recommends build-essential pkg-config python3-dev libcairo2-dev libgirepository-2.0-dev libdbus-1-dev
```

**2. Install** (uv itself: see the [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/))

```bash
# Using uv (recommended)
uv tool install kwin-mcp

# Or using pip
pip install kwin-mcp
```

**3. Configure Claude Code**

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "kwin-mcp": {
      "command": "uvx",
      "args": ["kwin-mcp"]
    }
  }
}
```

**4. Use it**

Ask Claude Code to launch and interact with any GUI application:

```
Start a KWin session, launch kcalc, and press the buttons to calculate 2 + 3.
```

Claude Code will autonomously start an isolated session, launch the app, read the accessibility tree to find buttons, click them, and take a screenshot to verify the result.

## Configuration

### Recommended: install as a plugin

The fastest way to wire kwin-mcp into your editor is to install one of the bundled plugins. Each plugin auto-registers the MCP server **and** ships the `kwin-desktop-automation` skill, which teaches the agent which tool to call when (session-mode selection, the observe → act → verify loop, element refs, US-QWERTY vs Unicode typing, global logical coordinates, held input, and other platform pitfalls).

**Claude Code** — install the plugin from the marketplace:

```text
/plugin marketplace add isac322/kwin-mcp
/plugin install kwin-mcp@kwin-mcp
```

**OpenCode** — add the npm plugin to the `plugins` array in your `opencode.json`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": ["@isac322/kwin-mcp-opencode"]
}
```

For manual OpenCode V2 setup without the plugin, add `kwin-mcp` under `mcp.servers`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "kwin-mcp": {
        "type": "local",
        "command": ["uvx", "kwin-mcp"]
      }
    }
  }
}
```

The bundled skill's source of truth is [`integrations/claude-code/skills/kwin-desktop-automation/SKILL.md`](integrations/claude-code/skills/kwin-desktop-automation/SKILL.md). To customize or override it, place a `SKILL.md` in one of OpenCode V2's skill locations:

- Personal: `~/.claude/skills/kwin-desktop-automation/SKILL.md`, `~/.config/opencode/skills/kwin-desktop-automation/SKILL.md`, or `~/.agents/skills/kwin-desktop-automation/SKILL.md`
- Project: `<project>/.claude/skills/kwin-desktop-automation/SKILL.md`, `<project>/.opencode/skills/kwin-desktop-automation/SKILL.md`, or `<project>/.agents/skills/kwin-desktop-automation/SKILL.md`

### Claude Code

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "kwin-mcp": {
      "command": "uvx",
      "args": ["kwin-mcp"]
    }
  }
}
```

Or if installed globally:

```json
{
  "mcpServers": {
    "kwin-mcp": {
      "command": "kwin-mcp"
    }
  }
}
```

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "kwin-mcp": {
      "command": "uvx",
      "args": ["kwin-mcp"]
    }
  }
}
```

### Running Directly

```bash
# As an installed script
kwin-mcp

# As a Python module
python -m kwin_mcp

# Print the server's usage and options, then exit (does not start the server)
kwin-mcp --help

# Interactive CLI (REPL for rapid testing)
kwin-mcp-cli

# Live session mode (default to real desktop instead of virtual)
kwin-mcp --default-live-session
kwin-mcp-cli --default-live-session

# Attach captured frames to tool results as MCP image content
kwin-mcp --screenshot-images

# Downscale every captured frame so its longer side is at most 2576 pixels
kwin-mcp --screenshot-images --screenshot-max-edge 2576

# Never ask before closing a window on the live desktop; refuse instead
kwin-mcp --live-consent refuse
```

`kwin-mcp-cli` exposes the same 29 tools as commands with the same names and parameters, and prints the same text the MCP server returns. Arguments are `key=value` pairs; object, list and union parameters take JSON:

```text
kwin-mcp> session_start
kwin-mcp> app_launch command=kcalc
kwin-mcp> element_query locator='{"app": "kcalc", "role": "push button"}'
kwin-mcp> pointer_click target='{"ref": "e1.12"}'
kwin-mcp> session_stop
```

## Server Flags

The server accepts only `--default-live-session`, `--screenshot-images`, `--screenshot-max-edge N`, `--live-consent {elicit,refuse}`, and `-h`/`--help`. `-h`/`--help` anywhere on the command line prints the usage and exits 0, whatever else is present. An unknown option, an abbreviation such as `--default`, a positional argument, a `--screenshot-max-edge` value that is not ASCII digits, a `--live-consent` value other than `elicit` or `refuse`, a flag without its value, or a repeated `--screenshot-max-edge` or `--live-consent` is a usage error: the server prints the usage and `kwin-mcp: error: ...` to stderr and exits with status 2 without starting.

| Flag | Effect |
|------|--------|
| `--default-live-session` | Tells the client that this server is meant for the live desktop: `session_connect`'s description recommends it and `session_start`'s description says to use it only for an isolated session, and the server `instructions` gain one sentence saying the same. Tool behavior is unchanged; `session_start` always starts a virtual session. |
| `--screenshot-images` | Attaches every captured frame to the tool result as MCP `image` content. See [Screenshot Images in Tool Results](#screenshot-images-in-tool-results---screenshot-images). |
| `--screenshot-max-edge N` | Downscales every captured frame so its longer side is at most `N` pixels (`0`, the default, keeps full resolution). See [Screenshot Size](#screenshot-size---screenshot-max-edge-region-max_edge). |
| `--live-consent {elicit,refuse}` | How `window_close` on a live desktop asks the user first. See [Live Window Close Consent](#live-window-close-consent---live-consent). |

### Screenshot Images in Tool Results (`--screenshot-images`)

By default, `screen_capture` and the `capture` frame bursts of the action tools return only the PNG file paths (and `kwin://screenshots/{name}` resource URIs), so the agent must read the files to look at them. Start the server with `--screenshot-images` to also attach each captured PNG to the tool result as MCP `image` content: the single `screen_capture` frame and every burst frame of the 15 tools that take `capture`, in capture order. The text of the result and its structured content are unchanged; image blocks are only appended after the text, and only for calls that actually captured frames.

Claude Code:

```bash
claude mcp add kwin-mcp -- uvx kwin-mcp --screenshot-images
```

Any JSON MCP config takes the flag as another argument:

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

The flag is off by default because every attached frame is sent to the model and costs context tokens, and a `capture` burst attaches all of its frames — there is no cap. In a live session (`session_connect` or `--default-live-session`) the images are your real desktop's pixels, so enabling the flag sends them to the model provider.

### Screenshot Size (`--screenshot-max-edge`, `region`, `max_edge`)

A screenshot of a large desktop can be too big for a model. A 7680x1440 workspace is an 11-megapixel PNG of several megabytes. A model provider downscales an oversized image before the model sees it, and the model's pixel coordinates then refer to the resized image, which the `Coordinate space` line does not describe. kwin-mcp can bound and crop the image itself, so the line stays exact:

- `--screenshot-max-edge N` downscales every saved screenshot and `capture` frame so its longer side is at most `N` pixels, keeping the aspect ratio. `0`, the default, keeps full resolution. The bound must keep the image within the provider's limits, or the provider resizes it again and the formula below no longer matches the model's coordinates. According to [Anthropic's vision documentation](https://platform.claude.com/docs/en/build-with-claude/vision#evaluate-image-size) (checked 2026-10-10), Claude 4.7 and later models take at most 2576 pixels on the long edge and 4784 visual tokens of 28x28 pixels; earlier models take 1568 pixels and 1568 tokens. 2576 suits a wide desktop such as 7680x1440 (2576x483); a 16:9 image fits at 2576x1449 on the newer models and 1456x819 on earlier ones, and a squarer image or crop needs a smaller bound. `N` must be ASCII digits, and the flag may be given only once; anything else is a usage error (exit status 2) before the server starts.
- `screen_capture(max_edge)` and `capture={max_edge}` override the flag for one call; `max_edge=0` returns full resolution.
- `screen_capture(region={x, y, width, height})` (global logical coordinates) crops the capture to that rectangle; `capture={region}` does the same for a burst. The part outside the captured workspace is dropped, and the `Coordinate space` line reports the crop's origin and size. A region that misses the workspace entirely is an error.

When an image was downscaled, its `Coordinate space` line keeps the logical origin and size and adds the image's pixel size and the mapping from the saved image's pixels: each image pixel maps to the logical point at its source pixel's center, for example `image 2576x483 downscaled: pixel (px, py) shows (0 + (px + 0.5) * 7680 / 2576 - 0.5, 0 + (py + 0.5) * 1440 / 483 - 0.5)`. The structured `Frame.mapping` carries the same `scale` and `origin`. Small text is unreadable in a downscaled overview of a wide desktop, so take the overview first, then a `region` around the area of interest at full resolution.

### Live Window Close Consent (`--live-consent`)

On a live desktop, `window_close` could discard unsaved work in a real application, so kwin-mcp asks the user before it closes anything:

- `--live-consent=elicit` (the default) shows the user an MCP form-elicitation prompt: `kwin-mcp wants to close window <id> (<app>) on your desktop. Unsaved work in that window may be lost.`, with a "Close this window" checkbox. The window closes only when the user accepts with the box checked. A decline tells the agent not to retry unless the user asks. A cancel (`consent: "cancelled"`) is not treated as a refusal, because a headless client such as `claude -p` cancels every prompt without showing it to anyone: the result says the confirmation was dismissed or never shown and points the agent to the app's own UI if the user asked for the close. A client that does not support form elicitation gets a non-error refusal (`consent: "unsupported"`) instead of a prompt.
- `--live-consent=refuse` never prompts and refuses every live `window_close` with the same non-error refusal, which points the agent to the app's own UI. Use it for headless clients that cannot answer a prompt, such as `claude -p`: with the default, such a client cancels the prompt, and in our runs Claude then declined to close the window any other way; with `refuse` it closed the window with the app's shortcut.

Virtual sessions are unaffected: they close without asking (`consent: "not_required"`). The result reports the outcome in `consent` (`not_required`, `accepted`, `not_confirmed`, `declined`, `cancelled` or `unsupported`) and, when nothing was closed, `refusal` (`no_window`, `not_closeable` or `no_consent`). Consent is a courtesy against an agent mistake, not a security boundary; the real boundary is your client's per-tool approval.

## Available Tools

kwin-mcp registers 29 tools. Parameters are listed in schema order: a bare name is required, `name?=default` is optional with its default (JSON). Every coordinate, in and out, is a global logical pixel. The argument types (`Target`, `Locator`, `WindowSelector`, `Capture`, ...) are described in [Shared Argument Types](#shared-argument-types).

### Session Management (4 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `session_start` | `screen_width?=1920`, `screen_height?=1080`, `enable_clipboard?=false`, `home?="host"`, `retention?={"artifacts":false,"home":false}` | Start an isolated virtual KWin Wayland session (one at a time) with its own `dbus-run-session` bus, `kwin_wayland --virtual` compositor, AT-SPI2 bus and EIS input. `screen_width`/`screen_height` must be positive integers. `enable_clipboard=true` serves the clipboard tools (off by default because `wl-copy` can hang on a freshly created bus). `home="isolated"` gives launched apps a temporary HOME with isolated XDG directories (not a security sandbox; see [Isolation scope](#isolation-scope)). `retention={artifacts, home}` decides now what survives the session end: the artifacts directory (screenshots, frames, app logs) and the isolated HOME; `home=true` requires `home="isolated"`. Startup is deadline-bounded and atomic: a failed step stops the partly started session and reports the captured session stderr. Then launch apps with `app_launch`. |
| `session_connect` | `dbus_address?=""`, `wayland_display?=""`, `enable_accessibility?=false`, `retention?={"artifacts":false,"home":false}` | Attach to an already running KWin session: the real desktop or a KWin inside a container. Empty values default to `$DBUS_SESSION_BUS_ADDRESS` and `$WAYLAND_DISPLAY` (a socket name is resolved under `$XDG_RUNTIME_DIR`). Clipboard is always enabled. Actions affect real applications, and `window_close` asks the user first (see [Live Window Close Consent](#live-window-close-consent---live-consent)). `enable_accessibility=true` turns on the desktop's `org.a11y.Status.IsEnabled` switch until `session_stop` (see [Accessibility Tree](#accessibility-tree)). `retention.home` is an error here; live sessions use the host HOME. |
| `session_stop` | _(none)_ | End the current session. Virtual: terminate the whole session process group (`SIGTERM`, then `SIGKILL` for members that remain), KWin, launched apps and the bus. Live: detach, kill only apps launched through kwin-mcp, restore the accessibility switch. Releases all held input and reports it. Idempotent: with no session it returns `stopped: false` and `No session running.` |
| `session_info` | _(none)_ | Report the current session (or `null`): its kind, Wayland socket, bus address, screen size, enabled backends (input, clipboard, accessibility), retention and artifacts directory, every app launched through kwin-mcp, and the held input. Works without a session. |

### App Launch and Logs (2 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `app_launch` | `command`, `env?=null` | Launch a program in the session, split with `shlex` (no shell), in its own process group, with stdin closed. `env` adds environment variables for this process. stdout and stderr go to a log file read with `app_log`. An empty command or unbalanced quotes is an error. A failed launch keeps the session running; call `session_stop` to roll back. Returns `pid`, `log_path` and `log_uri`. |
| `app_log` | `pid`, `last_n_lines?=50` | Return the tail of the captured stdout/stderr of an app launched with `app_launch` (`session_info` lists the PIDs). `last_n_lines=0` returns the whole log. |

### Screen Capture (1 tool)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `screen_capture` | `region?=null`, `max_edge?=null`, `include_cursor?=false` | Capture the whole workspace, or a `region` rectangle of it, as a PNG via KWin ScreenShot2 (Spectacle fallback). Returns a `Frame`: the file path, the `kwin://screenshots/{name}` URI, the image size, `size_kb`, the image-to-logical `mapping` and `coverage`, plus the `Coordinate space` text line. `max_edge` downscales (default from `--screenshot-max-edge`, `0` = full resolution); `include_cursor` draws the pointer. See [Screenshot Capture](#screenshot-capture). |

### Accessibility Elements (2 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `element_query` | `locator?={"match":"substring"}`, `max_depth?=15`, `wait?=null` | Return the AT-SPI2 elements matching `locator`, in pre-order, with role, name, description, states, rectangle (or the reason it is unavailable), actions, text content (capped at 200 characters), slider/scrollbar value, `app`, `window_id`, `child_count` and a `ref` to use as a target. `locator.match` selects `"substring"` (default, name/role/description), `"exact"` (normalized name equality), or `"spoken"` (spoken-label matching); see `Locator` below. An empty locator lists everything to `max_depth` (`0` or less = application nodes only, `1` = top-level windows). With `wait={until, timeout_ms, poll_interval_ms}` it polls until matching elements are `present` (default) or `absent`; a timeout is not an error but returns `satisfied: false` with the last observed matches and a `NOT SATISFIED: Timeout after N ms` line. Unreadable nodes are skipped. |
| `element_act` | `element`, `action`, `capture?=null` | Invoke a named AT-SPI action (case-insensitive, e.g. `Press`, `Toggle`, `Activate`) on exactly one element without pointer coordinates: `element={ref}` from `element_query`, or `element={strict: Locator}` for a strict search with the same `match` modes (depth 64, fails closed on any unreadable node, exactly one element exposing a requested action). `action` is an action name or a `"\|"`-separated fallback list such as `"Press\|click"` (the engine also accepts `["Press", "click"]`): choose the first action the element exposes, counting each element once. The result's `action` is the action actually dispatched. Zero or several candidates is an error that lists the actions the matches expose. When the application does not confirm a dispatched action, the result says it was sent but not confirmed (`dispatch: "sent_unconfirmed"`): the action may have run, so check the UI before retrying. Fallback selection is not a retry; actions are never retried automatically. |

### Window Management (5 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `window_list` | `filter?=null` | List KWin windows, dialogs, popups and shell surfaces included, with `id`, `app_id`, `caption`, `kind`, `active`, `frame` and `client` rectangles, `moveable`/`resizeable`/`closeable`, and `desktops`. `filter` is the same `WindowSelector` the mutators use, applied as an AND-ed filter: `{active: true}` gives the active window, `{kind: ["normal"]}` the normal windows. An empty list is a normal result. |
| `desktop_list` | _(none)_ | List KWin virtual desktops in pager order (`id`, `name`) with the current one marked. |
| `window_focus` | `window`, `capture?=null` | Raise and activate exactly one window through KWin scripting (`workspace.activeWindow`). The result reports the window's observed state afterwards, or that it vanished. |
| `window_place` | `window`, `frame?=null`, `desktop?=null`, `capture?=null` | Set the outer `frame` (any subset of `x`, `y`, `width`, `height`; omitted fields keep their value; signed 32-bit coordinates; width/height positive and at most the workspace size) and/or the virtual `desktop` (an id from `desktop_list`) of exactly one window, then read it back. KWin applies changes asynchronously, so fields it has not applied yet are listed as pending, and a window that vanished before the readback is reported as such, both as normal results. A desktop move that would also move related transient windows is refused with `nothing was changed`. |
| `window_close` | `window`, `capture?=null` | Ask exactly one window to close, like its titlebar button; the app may refuse or keep it open behind a "save changes?" prompt, so confirm with `window_list`. On a live session the user must confirm first. An unconfirmed close, a window that cannot be closed, and an `{id}` that matches no window are non-error refusals reported in `refusal`. |

### Mouse Input (5 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `pointer_click` | `target`, `button?="left"`, `count?=1`, `modifiers?=null`, `hold_ms?=0`, `capture?=null` | Move the pointer to `target` and click `count` times (1 single, 2 double, 3 triple) with `left`, `right` or `middle`. `modifiers` (e.g. `"ctrl+shift"`) are held during the click only; an unknown key name is an error raised before any input. `hold_ms` long-presses the last click (minimum press 25 ms). |
| `pointer_move` | `target`, `capture?=null` | Move the pointer onto `target` and leave it there (hover, tooltips). Refused while a mouse button is held; use `pointer_hold(phase="move")` to move a held button. |
| `pointer_scroll` | `target`, `delta`, `axis?="vertical"`, `unit?="smooth_step"`, `steps?=1`, `capture?=null` | Move to `target` and scroll. `delta` positive = down/right, negative = up/left. `unit="smooth_step"` scrolls 15 px per unit smoothly; `unit="detent"` sends wheel clicks. `steps` splits `delta` into increments 10 ms apart. |
| `pointer_drag` | `from`, `to`, `button?="left"`, `modifiers?=null`, `waypoints?=null`, `dwell_ms?=0`, `capture?=null` | Press a button at `from`, move with smooth interpolation through optional `waypoints` (`{x, y, dwell_ms}`, pausing `dwell_ms` at each), and release at `to` after pausing `dwell_ms` (for drop targets that need hover). The button and modifiers are always released, even on error. |
| `pointer_hold` | `phase`, `button?="left"`, `target`, `capture?=null` | Press, move or release one mouse button across calls, for custom drags: `press` (no event if already held), `move` (the button must be held), `release` (always sent). Every phase moves to `target` first. A held button stays held until released, `session_stop` or server exit, and every result reports it. |

### Keyboard Input (3 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `keyboard_press` | `keys`, `capture?=null` | Press and release one key or chord at the keyboard focus, e.g. `Return`, `ctrl+c`, `alt+F4`, `shift+ctrl+z`. Unknown key names are an error. |
| `keyboard_type` | `text`, `method?="keys"`, `capture?=null` | Insert text at the keyboard focus. `method="keys"` sends per-key US-QWERTY events (letters, digits, ASCII punctuation, space, tab, newline) and rejects any other character before typing anything, with an error that says to retry with `method="unicode"`. `method="unicode"` types any text through `wtype`, or, when `wtype` is unavailable or unsupported, a private clipboard paste whose chord follows the focused window (Ctrl+Shift+V in terminals that paste on it, Ctrl+V elsewhere) and that restores the previous clipboard in every format afterwards; the result's `route` says which was used. Text echoes are capped at 200 characters. |
| `keyboard_hold` | `phase`, `keys`, `capture?=null` | Press or release one key or chord across calls (modifiers first on press, reverse order on release), e.g. hold `ctrl` while clicking several items. `press` of an already held key sends no event; `release` is always sent. Held keys stay held until released, `session_stop` or server exit, and every result reports them. On a live desktop, release held input before ending your turn. |

### Touch Input (3 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `touch_tap` | `target`, `hold_ms?=0`, `capture?=null` | Tap `target` with one touch contact (does not move the pointer). `hold_ms` is the contact duration (minimum 10 ms; above 500 ms is a long-press). |
| `touch_swipe` | `from`, `to`, `fingers?=1`, `duration_ms?=300`, `capture?=null` | Swipe with `fingers` parallel contacts from one target to another over `duration_ms` (10 ms per step). Contacts are spread vertically 20 px apart around the path. KWin consumes swipes with 3 or more fingers as global gestures (see [Limitations](#limitations)). |
| `touch_pinch` | `center`, `start_distance`, `end_distance`, `duration_ms?=500`, `capture?=null` | Two contacts move symmetrically around `center`: `end_distance < start_distance` pinches in, `end_distance > start_distance` zooms out. |

### Clipboard (2 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `clipboard_read` | _(none)_ | Return the session clipboard text (`wl-paste`) as `{text}`. Virtual sessions need `enable_clipboard=true` and `wl-clipboard`. A missing `wl-paste` or an empty or unreadable clipboard is an error. |
| `clipboard_write` | `text` | Own the session clipboard with `text` (any size, including NUL characters) through `wl-copy`, waiting up to 5 s for ownership. Replaces the current selection. Same requirements as `clipboard_read`. The result echoes text of up to 200 characters; longer text is reported as its length. |

### Escape Hatches (2 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `dbus_call` | `service`, `path`, `interface`, `method`, `args?=null` | Call any method on the session bus, in-process through dbus-python. The method's declared signature is introspected (Qt overloads supported). Each argument is a dbus-send string (`"int32:42"`, `"dict:string:variant:k,string:v"`) or a typed JSON object (`{"type": "int32", "value": 42}`, with `element_type` for containers), and the two forms mix. Malformed values and arguments that fit none of the declared signatures are errors and nothing is sent; a D-Bus error reply is an error too. Returns `{reply, signature}`. Prefer the dedicated tools. |
| `wayland_list` | `filter?=""` | List the Wayland protocol globals the compositor advertises (`wayland-info`): the raw lines containing `filter` (case-sensitive) and the parsed `globals` (`interface`, `version`, `name`). Useful for verifying protocol access such as `org_kde_plasma_window_management`. |

### Output, Errors, Annotations, and Progress

Every tool declares an MCP `outputSchema` and returns `structuredContent` that matches it, plus a text block with the same data rendered for reading; `kwin-mcp-cli` prints that same text. Clients that read only the text lose nothing. Action results carry `at` (the point that was used), `element` (when the target was a ref), `frames`, `held` (the buttons and keys still held) and `warnings`; the text ends with `Still held: ...` whenever something is held.

A call that does not do what was asked comes back as an MCP tool error (`isError: true`, text `Error executing tool <name>: <message>`), and `kwin-mcp-cli` prints it as one `Error: <message>` line on stderr. The message has two lines: what failed, then a remedy the agent can copy, for example `No session running.` / `Call session_start (virtual) or session_connect (live desktop).` This covers invalid arguments, unknown argument names (the error names the replacement or the closest valid name), refusals and unmet preconditions, missing or ambiguous targets (an ambiguous window selector lists the candidate ids with their kind), and failed mechanisms (a KWin query, `wl-copy`/`wl-paste`, `wayland-info`, D-Bus, or a missing helper binary). Absence is data, not an error: an empty `element_query` or `window_list`, an `element_query` wait that timed out, a `window_close` refusal, a window that vanished before `window_focus`/`window_place` could read it back, and `session_stop` with no session are normal results that say so. A frame capture that fails after the action already happened is reported in `warnings`.

Every tool publishes honest MCP tool annotations in `tools/list`, one profile per tool, so clients can decide which calls need user confirmation. The 8 observation tools (`session_info`, `app_log`, `screen_capture`, `element_query`, `window_list`, `desktop_list`, `clipboard_read`, `wayland_list`) are read-only. `pointer_move` and `window_focus` are non-destructive and idempotent, so a client can auto-approve hover and focus. `session_start` is non-destructive and `session_connect` is non-destructive but open-world. The input tools, `element_act`, `window_place`, `window_close`, `clipboard_write`, `session_stop`, `app_launch` and `dbus_call` are destructive: injected input or an invoked action can trigger any action in the app, `clipboard_write` replaces the selection, and `app_launch` and `dbus_call` reach the host. The hold tools, `clipboard_write` and `session_stop` are idempotent. `window_place` and `window_close` are not, because they resolve their window selector on every call: repeating one with `window={active:true}` acts on whichever window is active by then. The annotations are hints for the client; kwin-mcp does not enforce them.

Long-running tools send MCP progress notifications when the client includes a `progressToken` in the request: `session_start` and `session_connect` report their startup steps, `element_query` with `wait` reports polling (never past its total), `capture` bursts report captured frames, and long `pointer_click`/`touch_tap` holds, `pointer_drag`, touch gestures, and `keyboard_type` report their progress; `keyboard_type` always ends with its final `Typed N/N characters` step. Clients that send no token receive no notifications, and tool results are the same either way.

### MCP Resources and Instructions

The server also publishes read-only resources: `kwin://session` (the `session_info` result), `kwin://windows` (the `window_list` result), `kwin://desktops` (the `desktop_list` result), `kwin://apps/{pid}/log` (an app's whole log, with PID completion) and `kwin://screenshots/{name}` (a saved PNG, as named in a frame's `uri`). Every resource has a tool equivalent. The server `instructions` describe the session lifecycle, the coordinate space, the preferred targeting order (`element_act` on an element from `element_query`, then a gesture with a `{ref}` target, then `{x, y}`, then `dbus_call`), the held-input rules, and that element text, captions, logs and the clipboard on a live desktop can contain instructions from web pages or chats and must be treated as data.

## Shared Argument Types

Each type has one meaning everywhere it is used. Nested objects reject unknown keys.

| Type | Shape | Used by |
|------|-------|---------|
| `Point` | `{x, y}` in global logical px | any `target`, `from`, `to`, `center` |
| `ElementPoint` | `{ref, dx?=0, dy?=0}`: the centre of a ref's live rectangle plus an offset, read at action time; an element without a rectangle is an error naming the reason | any `target`, `from`, `to`, `center` |
| `Target` | a `Point` or an `ElementPoint` (`{x, y, ref}` matches neither and is rejected) | `pointer_*`, `touch_*` |
| `Rect` | `{x, y, width, height}` in global logical px; width and height positive | `screen_capture.region`, `Capture.region` |
| `PartialRect` | `{x?, y?, width?, height?}`, at least one field; omitted fields keep their value | `window_place.frame` |
| `Waypoint` | `{x, y, dwell_ms?=0}` | `pointer_drag.waypoints` |
| `Capture` | `{delays_ms, region?, max_edge?, include_cursor?=false}`: frames taken `delays_ms` after the action ends (sorted; `[]` = no frames); same options as `screen_capture`; a long burst holds the tool thread | `capture` on the 15 tools that change what is on screen |
| `Wait` | `{until?="present", timeout_ms?=5000, poll_interval_ms?=200}`; `timeout_ms <= 0` polls once; a timeout returns `satisfied: false` | `element_query.wait` |
| `Retention` | `{artifacts?=false, home?=false}`: keep the artifacts directory (screenshots, frames, app logs) and/or the isolated HOME after the session ends, server exit included | `session_start.retention`, `session_connect.retention` |
| `KeyChord` | `"+"`-joined key names, modifiers first: `"ctrl+shift+t"`, `"Return"`, `"alt+F4"` | `keyboard_press.keys`, `keyboard_hold.keys`, `pointer_click.modifiers`, `pointer_drag.modifiers` |
| `Locator` | `{app?, query?, role?, states?, match?="substring"}`, AND-ed: `app` is a case-insensitive AT-SPI application-name substring, `role` is exact and case-insensitive, `states` are exact AT-SPI state names that must all hold. `match` selects `"substring"` (query searches name, role, description), `"exact"` (name only, case-folded equality after collapsing whitespace), or `"spoken"` (name only, spoken-label normalization described below). `{}` = everything | `element_query.locator` (including waits), `element_act` strict search |
| `ElementAddress` | `{ref}` or `{strict: Locator}` (`{ref, strict}` is rejected) | `element_act.element` |
| `WindowSelector` | `{id?, app_id?, caption?, active?, kind?}`, AND-ed over every KWin window: `id` exact, `app_id` (resource class) and `caption` case-insensitive substrings, `kind` a non-empty list of `normal`, `dialog`, `utility`, `popup`, `shell`, `other`; mutators need exactly one match | `window_focus`, `window_place`, `window_close` (`window`), `window_list` (`filter`) |
| `TypedArg` | `{type, value, element_type?}` | `dbus_call.args` |

**Name matching and action fallbacks.** `Locator.match` is `"substring" | "exact" | "spoken"`, defaulting to `"substring"`. Exact matching preserves punctuation. Spoken matching uses Unicode NFC and case folding (not compatibility normalization), collapses whitespace, and ignores a trailing run of `. , ! ? ; : …`; other punctuation stays significant. It also lets a multi-word query match the same words joined into one label (`save as` matches `SaveAs`), but never splits a one-word query to match a multi-word name. It is not fuzzy matching. Use the same locator in `element_query` to inspect candidates and in `element_act(element={strict: ...})` to act. An `element_act` strict search refuses ambiguity, including labels that normalize to the same spoken name. `element_act(action="Press|click")` accepts a single action or `"|"`-separated ordered alternatives (the in-process Python engine also accepts `action=["Press", "click"]`); it chooses one available action per element and reports the selected action, rather than trying the alternatives after dispatch.

**Element refs.** A ref looks like `e3.17`. Only `element_query` mints refs. Using one re-validates that the element is alive and its role unchanged, and reads its live rectangle, so a ref keeps working while a button moves or its label changes. Refs stay valid until the AT-SPI worker restarts or `session_stop`; a stale ref is an error whose remedy is to re-run `element_query`. An element's `window_id` can be passed straight to the window tools as `window={id}`.

**Held input.** Gestures (`pointer_click`, `pointer_scroll`, `pointer_drag`, `keyboard_press`, `keyboard_type`, touch gestures) release everything they press before they return, even on error. Only `pointer_hold(phase="press")` and `keyboard_hold(phase="press")` leave input held. A gesture that would press or release a held control is refused before it sends anything, and the error names the release call; gestures on other controls keep working (typing lowercase text while `ctrl` is held sends shortcuts, scrolling while `left` is held drags). If KWin's EIS connection drops, kwin-mcp can no longer send releases: the next result clears the held sets and warns that KWin may keep the input pressed (KWin 6.3.6 and 6.7.5 keep an emulated button down after the disconnect). If KWin replaces an input device (an output or keyboard-layout change), the app never receives a release for that device's controls: they stay held, the next result warns `held <controls> stays pressed`, and a later release goes through the replacement device. A held key then no longer modifies new keys, because KWin resets its modifier state; the warning says so. Reading the state (`session_info`) never sends input.

## How It Works

```
Claude Code / AI Agent
       |
       | MCP (stdio)
       v
kwin-mcp server (29 tools)          kwin-mcp-cli (interactive REPL)
  server.py: schemas, gates,          cli.py: key=value / JSON args
  consent, resources                       |
       +--------- both delegate to AutomationEngine (core.py) ---------+
                  models.py: shared argument types + result models,
                  one render() per result (server text == CLI output)
       |
       |-- session_start (virtual) ----> dbus-run-session          (session.py)
       |                                   |-- at-spi-bus-launcher (D-Bus activated)
       |                                   +-- kwin_wayland --virtual
       |                                         +-- [your app]
       |-- session_connect (live) -----> existing KWin (real desktop / container)
       |-- app_launch / app_log -------> subprocess spawn + log file (owned process groups)
       |
       |-- screen_capture / capture ---> KWin ScreenShot2 D-Bus (Spectacle fallback)  (screenshot.py)
       |-- element_query / element_act > AT-SPI2 worker via PyGObject (refs, Action interface)  (accessibility.py)
       |-- window_* / desktop_list ----> KWin scripting (loadScript + D-Bus reply)  (geometry.py)
       |-- pointer_* / keyboard_* /
       |   touch_* --------------------> KWin EIS D-Bus --> libei  (input.py)
       |-- keyboard_type(unicode) -----> wtype, or private Wayland data-control paste  (clipboard.py)
       |-- clipboard_* ----------------> wl-copy / wl-paste (wl-clipboard)
       |-- dbus_call ------------------> dbus-python (in-process, introspected signature)  (dbus_args.py)
       +-- wayland_list ---------------> wayland-info
```

### Triple Isolation (+ Optional Home Isolation)

kwin-mcp provides three layers of isolation from the host desktop:

1. **D-Bus isolation** -- `dbus-run-session` creates a private session bus. The isolated session's services (KWin, AT-SPI2, portals) are invisible to the host.
2. **Display isolation** -- `kwin_wayland --virtual` creates its own Wayland compositor with a virtual framebuffer. No windows appear on the host display.
3. **Input isolation** -- Input events are injected through KWin's EIS interface into the isolated compositor only. The host desktop receives no input from kwin-mcp.
4. **Home directory isolation** (optional) -- `session_start(home="isolated")` creates a temporary HOME directory with isolated XDG directories (`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME`). Apps that follow the XDG base directories read and write these instead of host user settings (e.g. `~/.config/kdeglobals`), improving test reproducibility. `XDG_RUNTIME_DIR` is intentionally not isolated because the Wayland socket resides there.

#### Isolation scope

These layers separate the session's bus, display, and input from the host desktop. They do not confine the processes: launched apps run as your user with your filesystem permissions and network access, and can still reach the original home directory by absolute path when `home="isolated"`. Run untrusted applications in a container or VM rather than relying on a virtual session.

### Automatic Session Cleanup at Exit

When the MCP server process ends — on stdio EOF, `SIGTERM`, `SIGHUP`, or `SIGINT` — it calls `session_stop`, so a session the client never stopped does not outlive the server, and the `retention` chosen at session start is honored. A virtual session's KWin compositor, its `dbus-run-session` wrapper bus, and every app launched through `app_launch` are terminated; in a live `session_connect` session the user's KWin and pre-existing apps are never signalled — only apps kwin-mcp launched are stopped. Held input is released on both. The exit code follows convention: 0 on a clean EOF, 1 on an unexpected server failure, and 128+signum on a signal (143 for `SIGTERM`, 129 for `SIGHUP`, 130 for `SIGINT`).

The final stop runs on the same single thread as tool calls, serialized with any in-flight tool. If a running tool still holds that thread after a 2 s drain, the server instead terminates the process groups it spawned directly and removes the temporary directories `session_stop` would remove for the session's `retention`. `kwin-mcp-cli` stops the session on `SIGTERM`/`SIGHUP` through the same path as Ctrl-C, and on exit also terminates any owned process group that was never attached to a session.

Known limits: `SIGKILL` runs no cleanup, so with `retention.artifacts=false` the session's `/tmp/kwin-mcp-screenshots-*` artifacts directory leaks when the server is killed with `SIGKILL` (remove it by hand); the next session never reuses it and always creates a fresh directory. A descendant that leaves its launched process group (a double-fork or `setsid` daemon) escapes; and a process group whose leader was already reaped is left alone, because ownership can no longer be proven.

### Input Injection

Mouse, keyboard, and touch events are injected through KWin's private `org.kde.KWin.EIS.RemoteDesktop` D-Bus interface. This returns a `libei` file descriptor that allows low-level input emulation without requiring the XDG RemoteDesktop portal (which would show a user authorization dialog). The connection uses:

- **Absolute pointer positioning** in KWin's global logical coordinates, the space that window frames, element rectangles, and screenshot pixels (after applying the frame mapping) share
- **evdev keycodes** with full US QWERTY mapping for keyboard input
- **Smooth drag interpolation** (10+ intermediate steps) for realistic drag operations
- **EIS touch emulation** for multi-touch gestures (tap, swipe with any number of fingers, pinch)

### Screenshot Capture

The normal Wayland capture path tries KWin's `org.kde.KWin.ScreenShot2` D-Bus interface first and uses the `spectacle` CLI as a fallback. Both capture the whole workspace across all outputs. `capture` bursts on the action tools use the same path, and Pillow converts ScreenShot2's raw pipe frames (RGB32, ARGB32, or RGBX8888 depending on the KWin version) to PNG. The Docker visual QA suite also has an explicit test-only X11 backend: when `KWIN_MCP_X11_SCREENSHOT=1` is set for a server connected to the nested KWin/Xvfb fixture, captures use `scrot`. Normal virtual and live Wayland sessions do not opt into this backend.

Each Spectacle capture has a deadline derived from the image Spectacle must produce, the workspace canvas at the output scale (on mixed-scale layouts, every output is upscaled to the next whole scale above the largest): 15 seconds plus 1 second per canvas megapixel, capped at 120 seconds. A 1920x1080 output gets 18 seconds; a mixed-scale 1.45 + 1.0 layout (a 6490x2160 canvas) gets 30 seconds. A capture that does not finish in time fails with `spectacle timed out after <N>s`.

Saved PNGs are normalized to KWin's global logical coordinate space, so a screenshot taken on a fractionally scaled output has one image pixel per logical pixel. The result states the mapping, for example on two outputs where a 1280x1024 screen sits left of a 1920x1080 screen:

```text
Screenshot saved: <path> (<size> KB)
Coordinate space: logical: origin (-1280, 0); size 3200x1080; backend screenshot2; coverage full; topology observed stable before/after capture
```

`origin` is the top-left of KWin's virtual screen geometry. It is negative when an output sits left of or above `(0, 0)`. To click image pixel `(px, py)`, pass `target={x: origin_x + px, y: origin_y + py}`; no scale conversion is needed. On a single output at scale 1, the origin is `(0, 0)` and the size equals the screen size.

Backends deliver different pixel spaces, and kwin-mcp normalizes each one:

- **ScreenShot2** `CaptureWorkspace` already renders logical pixels for the whole virtual screen, including outputs at negative positions.
- **Spectacle** captures device pixels, which kwin-mcp rescales to logical pixels. When all screens share one scale, Spectacle's composite clips outputs at negative logical positions. kwin-mcp cannot recreate those pixels: it leaves them transparent and reports `coverage partial` with the captured regions. With mixed scales, Spectacle releases before 6.7.90 (including all Gear-numbered releases such as 24.12) place each screen through OpenCV and abort (or distort the screen) when one lies outside their canvas, for example at a negative position. kwin-mcp detects that layout from the topology and does not start Spectacle; the capture fails with an error naming the output instead.
- **X11/scrot** (test-only) captures the X root, where each KWin output is an X window of `logical size * scale` pixels. kwin-mcp locates those windows with `xwininfo` and rescales each one to logical pixels.

kwin-mcp reads the output topology through KWin scripting immediately before and after each capture and checks the captured image against it, retrying once when the layout changed mid-capture. If the mapping still cannot be proven, `screen_capture` fails with an error containing `coordinate mapping cannot be proven` rather than returning guessed coordinates; a burst frame keeps its raw PNG and reports `Coordinate space: unavailable (reason)` with `mapping: null` instead. Observed stability is not an atomicity guarantee: a topology change that reverted between the two observations can go unnoticed.

### Accessibility Tree

The AT-SPI2 accessibility bus of the session is queried via PyGObject (`gi.repository.Atspi`) in a worker process. `element_query` returns a structured list of UI widgets with their roles (button, text field, menu item, etc.), names, states (focused, enabled, visible, etc.), screen coordinates, available actions (click, toggle, etc.), and a `ref` for each one. Its text uses the tree format (depth indentation, role, name, states, `@ screen (x, y, wxh)` or `@ unavailable (reason)`): an empty locator starts with `# Accessibility Tree (N elements)`, a filtered query with `Found N elements matching <filters>:`.

The virtual session's accessibility bus normally starts through D-Bus activation of the distro's `org.a11y.Bus` service. Fedora's SELinux policy can deny that activation from a private session bus ([fedora-selinux/selinux-policy#3385](https://github.com/fedora-selinux/selinux-policy/issues/3385)). Direct startup is disabled by default: only the exact value `1` in the server's `KWIN_MCP_ATSPI_DIRECT_START` environment variable enables it, not an application's `env` argument. Neither the server nor a healthy activation path performs direct-start service-file lookup. Only after activation fails does the private session wrapper start an owned child to look up and execute the launcher, then the registry.

For an SELinux-enforcing virtual-session host where activation is denied, opt in in the server environment:

```bash
KWIN_MCP_ATSPI_DIRECT_START=1 uvx kwin-mcp
```

The child reads the named AT-SPI service files from operator-trusted `XDG_DATA_DIRS` roots, defaulting to `/usr/local/share:/usr/share`; those roots need not mirror every root D-Bus activation searches. Before executing the launcher, it validates both service commands. The registry child reads and validates its own file again when it starts; if that file has become invalid, direct startup rolls back. `Exec=` parsing follows dbus-daemon's argument splitting. Acquisition retains the 128 KiB content limit and file-descriptor checks: missing or nonregular candidates and existing invalid-path/link cases remain skippable, while malformed, unreadable, or oversized regular files and inaccessible metadata, paths, or roots stop lookup.

Failed direct startup reports a `Warning:` line and lets KWin continue. Rollback targets the wrapper's owned Bash jobs, with a shared 3-second TERM grace followed by a shared 2-second KILL grace for the launcher and registry, not a separate wait for each child. Running lookup in those children bounds the caller's wait even when regular-file metadata or reads stall on NFS/FUSE; it does not guarantee that a kernel process stuck in uninterruptible I/O disappears immediately. Launcher descendants may remain in the owned session group until `session_stop`; programs that escape that group are not sandboxed by this mechanism. Normal D-Bus activation and the existing 60-second session-start watchdog are unchanged: a stall in normal activation itself still relies on that watchdog.

The private helper uses the server's existing `sys.executable` with `-I -S` to run the installed, standard-library-only `dbus_service_file.py`, then `os.execv` on the parsed executable. It is internal startup machinery, not another installation step or MCP tool. This avoids implicit `PATH` lookup, shell builtins, function evaluation, and `ENOEXEC` shell fallback; explicitly configured `/bin/sh -c` commands and legitimate `#!` scripts remain supported. Direct processes keep the same user, private bus, session process group, environment, and inherited working directory; relative executables resolve against the server's working directory, not an isolated directory. They start from the server process's SELinux domain rather than through normal D-Bus service activation, which can mean less confinement. The opt-in does not change SELinux policy or provide a general sandbox. Successful direct startup also reports a `Warning:` line. Tests simulate activation denial; they do not verify this path on an SELinux-enforcing Fedora host.

Firefox, like other apps that check it, exposes an accessibility tree only while the session's `org.a11y.Status.IsEnabled` switch is on, and a stock Plasma session leaves it off. Virtual sessions switch it on before any app starts; their accessibility bus is private, so the host desktop is unaffected. A live session (`session_connect`) does not change it by default; the result's `accessibility` field says whether it is on. Pass `enable_accessibility=true` to switch it on for the whole desktop until `session_stop`, which switches it back off (unless it was already on before the connection). The restore turns the switch back off whenever this connection turned it on, so a change the user makes to the switch while connected is overwritten at `session_stop`; enabling accessibility before connecting keeps it on. Every switch read and write is bounded to a few seconds, so a stalled desktop bus cannot hold `session_stop` or the server's exit. Firefox follows the change at runtime; an app that reads the switch only at startup must be restarted. The server's exit switches it back off too, including when a tool is still running. Restoring is best-effort: a server killed with `SIGKILL`, or a bus that refuses the call, leaves the switch on. Launch Chromium with `--force-renderer-accessibility` to expose web page content: in Chromium 154 the switch alone gave only empty window frames, and the flag gave the page tree whatever the switch.
## System Requirements

| Requirement | Details |
|-------------|---------|
| **OS** | Linux with KDE Plasma 6 (Wayland session) |
| **Python** | 3.12 or later |
| **KWin** | `kwin_wayland` with `--virtual` flag support (KDE Plasma 6.x) |
| **libei** | EIS input emulation client library. Not a `kwin-wayland` dependency on Debian/Ubuntu; install `libei1` (Arch's `kwin` package depends on it) |
| **libwayland-client** | Used by the `keyboard_type(method="unicode")` clipboard paste; already installed as a KWin dependency |
| **spectacle** | KDE screenshot tool (CLI mode); packaged as `kde-spectacle` on Debian and Ubuntu |
| **AT-SPI2** | `at-spi2-core` for accessibility tree support |
| **PyGObject** | GObject introspection Python bindings (built from source by uv/pip; see [build prerequisites](#build-prerequisites-for-uv-and-pip-installs)) |
| **D-Bus** | `dbus-python` bindings (built from source by uv/pip; needs libdbus development files) |
| **Build tools** | C compiler, `pkg-config`, Python headers, and cairo, GObject Introspection, and libdbus development files |

**Optional dependencies:**

| Package | Required for |
|---------|-------------|
| `wl-clipboard` (`wl-copy`, `wl-paste`) | `clipboard_read` and `clipboard_write` |
| `wtype` | `keyboard_type(method="unicode")` direct typing, tried first; when it is unavailable or unsupported by the session, the built-in clipboard paste is used instead |
| `wayland-utils` (`wayland-info`) | `wayland_list` tool |

### Installing System Dependencies

kwin-mcp needs two groups of system packages: runtime packages (KWin, Spectacle, AT-SPI2) and build prerequisites for the Python packages it installs from PyPI.

#### Build Prerequisites for uv and pip Installs

`uv tool install kwin-mcp` and `uvx kwin-mcp` always use an isolated Python environment, and `pip install kwin-mcp` does too when run inside a virtual environment. An isolated environment cannot see the system `gi` (PyGObject) or `dbus` modules installed by your distribution, so uv or pip builds [PyGObject](https://pypi.org/project/PyGObject/), [pycairo](https://pypi.org/project/pycairo/), and [dbus-python](https://pypi.org/project/dbus-python/) from source. These builds need a C compiler, `pkg-config`, the Python headers, and the cairo, GObject Introspection, and libdbus development files. Without them, installation fails while building those packages.

On Debian 13 (Trixie):

```bash
sudo apt-get install -y --no-install-recommends build-essential pkg-config python3-dev libcairo2-dev libgirepository-2.0-dev libdbus-1-dev
```

For other distributions, follow the "Installing from PyPI with pip" build dependency steps in the [PyGObject Getting Started guide](https://pygobject.gnome.org/getting_started.html), and also install your distribution's libdbus development package (it provides the `dbus-1` pkg-config file that dbus-python needs).

#### Runtime Packages

<details>
<summary><strong>Debian 13 (Trixie)</strong></summary>

Debian packages Spectacle as `kde-spectacle`; there is no `spectacle` package. `gir1.2-atspi-2.0` provides the AT-SPI2 GObject Introspection typelib that PyGObject loads at runtime. `libei1` provides the EIS client library used for input injection; `kwin-wayland` does not depend on it, so it must be installed explicitly. `kwin-common` ships KWin's EIS plugin (without it `session_start` reports `No input backend available`), and `qt6-wayland` lets Qt apps such as KCalc open windows on Wayland. `kwin-wayland` pulls in neither; a full Plasma desktop already has both, but a minimal system or container needs them listed.

```bash
sudo apt-get install -y --no-install-recommends kwin-wayland kwin-common qt6-wayland kde-spectacle libei1 at-spi2-core gir1.2-atspi-2.0

# Optional: for clipboard and Unicode input
sudo apt-get install -y --no-install-recommends wl-clipboard wtype wayland-utils
```

</details>

<details>
<summary><strong>Arch Linux / Manjaro</strong></summary>

```bash
sudo pacman -S kwin spectacle at-spi2-core python-gobject dbus-python-common

# Optional: for clipboard and Unicode input
sudo pacman -S wl-clipboard wtype wayland-utils
```

</details>

<details>
<summary><strong>Fedora (KDE Spin)</strong></summary>

```bash
sudo dnf install kwin-wayland spectacle at-spi2-core python3-gobject dbus-python

# Optional: for clipboard and Unicode input
sudo dnf install wl-clipboard wtype wayland-utils
```

</details>

<details>
<summary><strong>openSUSE (KDE)</strong></summary>

```bash
sudo zypper install kwin6 spectacle at-spi2-core python3-gobject python3-dbus-python

# Optional: for clipboard and Unicode input
sudo zypper install wl-clipboard wtype wayland-utils
```

</details>

<details>
<summary><strong>Kubuntu / KDE Neon (Plasma 6 releases only)</strong></summary>

Use a release that ships KDE Plasma 6 and Python 3.12 or later; older Kubuntu releases with Plasma 5 are not supported. Install the Debian build prerequisites above as well. Ubuntu packages Spectacle as `kde-spectacle`.

```bash
sudo apt install kwin-wayland kwin-common qt6-wayland kde-spectacle libei1 at-spi2-core python3-gi gir1.2-atspi-2.0 python3-dbus

# Optional: for clipboard and Unicode input
sudo apt install wl-clipboard wtype wayland-utils
```

</details>

## Installation

Install the [system and build dependencies](#installing-system-dependencies) before using any method below. The uv installs, pip installs into a virtual environment, and the from-source install build PyGObject, pycairo, and dbus-python from source and fail without them.

### Using uv (recommended)

```bash
uv tool install kwin-mcp
```

uv also provides `uvx`, which the `.mcp.json` examples above use to run kwin-mcp. If the `kwin-mcp` command is not found after `uv tool install`, run `uv tool update-shell` and restart your shell so uv's tool directory is on `PATH`.

### Using pip

```bash
pip install kwin-mcp
```

### From source

```bash
git clone https://github.com/isac322/kwin-mcp.git
cd kwin-mcp
uv sync
uv run kwin-mcp
```

## Migrating from 0.11

kwin-mcp 1.0.0 is a breaking release with no aliases. Every 0.11 and 0.12 capability is still available, under one of 29 tool names with shared argument types:

- 9 tool names are kept with new parameters or outputs, 28 are removed, and 20 are new. Calling a removed tool returns `Unknown tool: <name>. Removed in kwin-mcp 1.0; use <replacement>.`, so an agent that still knows the old names is told the new call.
- Unknown argument names are now rejected instead of silently ignored. A removed argument such as the old frame-capture delay list returns an error naming its replacement (`capture={delays_ms:[...]}`).
- Coordinates are objects (`target={x, y}`, `from`, `to`, `center`, `frame={x, y, width, height}`), key combinations are one string (`"ctrl+shift"`), and every action that changes the screen accepts the same `capture` option.
- `session_start` no longer launches an app: call `app_launch(command)` after it.
- 0.12.0 shipped 37 tools, including two virtual-desktop tools; use `desktop_list` and `window_place(window={id}, desktop)`. Its exact-name, spoken-name and fallback-action options on the old action tool become `Locator(query, match)` (`match="exact"` or `"spoken"`) and `element_act(element, action="Press|click")`. The same matching modes now work in `element_query`, including waits.
- The typed active-window output added after 0.12.0 becomes `window_list(filter={active:true})`, observing every window kind in one query. `KWIN_MCP_ATSPI_DIRECT_START=1` remains the same server-environment opt-in for virtual sessions.

The [1.0.0 entry of the CHANGELOG](CHANGELOG.md#100---2026-10-15) has the full tool migration table (every 0.11 and 0.12 tool and the 1.0 call that replaces it), the parameter migration table, and the 24 intentional behavior changes.

## Limitations

- **US QWERTY keyboard layout only** -- `keyboard_type(method="keys")` supports US QWERTY only: letters, digits, ASCII punctuation, space, tab, and newline. Text containing any other character (non-ASCII such as Korean, CJK, or accented letters, `\r`, or other control characters) is rejected before anything is typed, with an error naming the unsupported characters; use `keyboard_type(method="unicode")` for it. Virtual sessions pin the compositor keymap to US, so the host's keyboard layout settings do not change what is typed; a live session attached with `session_connect` keeps the desktop's own keymap, so its active layout must be US for `keyboard_type` and `keyboard_press` to produce the expected characters.
- **Unicode typing can paste through the clipboard** -- When `wtype` is unavailable or unsupported by the session, `keyboard_type(method="unicode")` briefly puts the text on the clipboard, then restores the previous selection and keeps serving it until another copy replaces it, even after `session_stop` disconnects a live session. The restore is best-effort, not atomic: Wayland has no compare-and-swap for the selection, so a copy another client makes at the same moment as the restore can be overwritten. Wayland also does not tell the clipboard owner which client read the text, so a clipboard manager that reads it after the paste chord can be taken for the target app, and any client that read it keeps its copy. The `x-kde-passwordManagerHint` marker keeps the text out of Klipper's history, but clipboard managers that ignore the marker can still record it.
- **Unicode typing into terminals is limited** -- Terminals do not paste on Ctrl+V, and one that does not bind it passes it to the program as `^V`, which changes the next key (a Return arrives as a literal CR). `keyboard_type(method="unicode")` therefore checks KWin's active window first and sends Ctrl+Shift+V to terminals known to paste on it (Konsole, Yakuake, GNOME Terminal, GNOME Console, kitty, Alacritty, WezTerm, foot, Ghostty, st, Xfce Terminal, Tilix, Terminator). Window classes recognized as terminals but with no clipboard paste chord (xterm and urxvt, whose Shift+Insert pastes the primary selection) and windows KWin cannot identify get an error without any key being pressed; a terminal whose class is not recognized still gets Ctrl+V. Only Konsole is exercised by the test suite; the other chords follow each terminal's documented default bindings, so a remapped paste shortcut breaks delivery there.
- **KDE Plasma 6+ required** -- Older KDE versions or other Wayland compositors (GNOME, Sway) are not supported.
- **AT-SPI2 availability varies** -- Some applications may not fully expose their widget tree via AT-SPI2.
- **Touch input is EIS-emulated** -- Touch events are emulated through KWin's EIS interface, not from a real touchscreen device. Most applications handle emulated touch correctly, but some may behave differently from physical touch.
- **Clipboard requires opt-in on virtual sessions** -- `clipboard_read` and `clipboard_write` are disabled by default in virtual sessions because `wl-copy` can hang in isolated sessions. Enable them with `session_start(enable_clipboard=true)`, and ensure `wl-clipboard` is installed. Live sessions always have the clipboard.
- **QMenu (native context menus) may not appear in AT-SPI2** -- Qt's AT-SPI2 bridge has incomplete support for popup menus on Wayland. Context menus may not be visible in `element_query`. Workaround: take a `screen_capture`, or click by coordinates derived from the parent widget's reported screen rectangle.
- **Screen edge triggers ignore EIS pointer events** -- Auto-hide panels and layer-shell strips do not react when the pointer reaches a screen edge through EIS. Use `dbus_call` to invoke KWin scripting or a keyboard shortcut instead of trying to hover the edge.
- **KWin claims multi-finger touch gestures** -- Three- and four-finger swipes are consumed by the compositor as global gestures and never reach the application; use `fingers=2` when the target is the app itself.
- **A multi-finger swipe that starts on a titlebar can block pointer clicks** -- A swipe with 3 or more fingers whose first (topmost) finger lands on a window's server-side titlebar or decoration can leave KWin withholding pointer button events from that window. The stuck state lasts until another window is activated; refocusing the same window does not clear it. `touch_swipe` puts its first finger at `from.y - (fingers-1)*10`, so keep that point inside the client area.
- **Window titles from AT-SPI2 are raw names** -- `element_query(max_depth=1)` reports each top-level window's AT-SPI name, which may be empty (`(untitled)`) or differ from the caption KWin shows; use `window_list` for KWin captions and ids, or the element's `window_id`.
- **Element coordinates are screen-global, or unavailable** -- `element_query` reports rectangles in the same global logical coordinates every `target` takes (`@ screen (x, y, wxh)`). When the element's window cannot be matched to exactly one KWin window — an app that masks its real process id (e.g. a D-Bus proxy), several identical windows of one process, or a window set that changed mid-query — the element reports `@ unavailable (reason)` with `rect: null` rather than a position that could click the wrong window, and a `{ref}` target on it is an error naming that reason.
- **Window name lookups can be ambiguous** -- A `WindowSelector` matches every window kind, so `{app_id: "kate"}` can match the main window and an open dialog or menu at once. Mutators need exactly one match and fail with the candidate ids and kinds; add `kind: ["normal"]` or use the `id`.
- **Spectacle fallback can return partial screenshots or refuse a layout** -- When ScreenShot2 is unavailable and Spectacle composites several screens of one scale, Spectacle clips outputs at negative logical positions. Those regions stay transparent, and the `Coordinate space` line reports `coverage partial` with the regions that were captured. When the screens have mixed scales and one lies outside Spectacle's canvas (such as a screen at a negative position), Spectacle before 6.7.90 (including Gear-numbered releases such as 24.12) aborts inside OpenCV, so kwin-mcp reports a capture error without starting it. ScreenShot2 captures the whole workspace in both cases.
- **`screen_capture` captures the current virtual desktop's whole workspace only; no single-window capture exists** -- `screen_capture` and `capture` frames use KWin ScreenShot2 `CaptureWorkspace` (with a Spectacle fallback, or the opt-in X11/scrot backend enabled by `KWIN_MCP_X11_SCREENSHOT=1`): a window on another, inactive virtual desktop is not in the image, and no kwin-mcp tool captures a single window by id (#34). `dbus_call` cannot reach `org.kde.KWin.ScreenShot2.CaptureWindow` either, because it cannot pass the unix file descriptor the capture methods require. In a live session, KWin also refuses ScreenShot2 calls with `org.kde.KWin.ScreenShot2.Error.NoAuthorized` unless the caller's executable (`/proc/<pid>/exe`) matches the first token of `Exec=` in a `.desktop` file that declares `X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2`. A user-level file under `~/.local/share/applications` is picked up after `kbuildsycoca6` with no KWin restart (kwin-mcp's own virtual sessions disable this check):

  ```ini
  [Desktop Entry]
  Type=Application
  Name=kwin-mcp ScreenShot2
  Exec=/path/to/copies-venv/bin/python3
  X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2
  ```

  The grant applies to every process running that exact binary. For a Python program the authorized executable is the interpreter, so whitelisting `/usr/bin/python3` authorizes every Python process that runs on it; KWin compares canonical paths, so a default venv's `bin/python3` is a symlink that resolves to the system interpreter and grants it too. Only a venv created with `python -m venv --copies` has its own interpreter binary that can be whitelisted on its own. Even with the whitelist, a window on an inactive desktop stops redrawing, so `CaptureWindow` returns the last frame drawn while it was visible, not its live state; only KWin's screencast path (`zkde_screencast_unstable_v1` `stream_window`) keeps an off-screen window rendering, gated by the same authorization mechanism under `X-KDE-Wayland-Interfaces`. KWin scripts and QML `WindowThumbnail` cannot save a window's pixels.

## End-to-End Testing

The Docker suite collects every test under `tests/e2e` against the packaged application, not an editable source checkout. `docker/e2e.Dockerfile` builds a wheel and installs it into `/opt/kwin-mcp-venv` with standard `Requires-Dist` resolution: PyGObject, pycairo, and dbus-python compile from source in a builder-only stage, while `mcp`, Pillow, and the remaining dependencies resolve fresh from PyPI within the declared ranges. The suite runs both `AutomationEngine` tests and the installed `kwin-mcp` console entry point. The MCP tests initialize a real client/server session over stdio JSON-RPC.

Run the complete suite from the repository root:

```bash
scripts/run-e2e-docker.sh
```

The runner builds the image for Docker's native architecture, creates `artifacts/e2e/<UTC-timestamp>-<pid>/`, runs pytest, and prints the artifact path. Additional arguments pass directly to pytest:

```bash
scripts/run-e2e-docker.sh -- tests/e2e/test_installed_package.py -v
scripts/run-e2e-docker.sh -- -k "visual or screenshot" -v
```

Fast checks that need no KWin run on the host: `uv run pytest tests/unit` covers the tool schemas against the design (names, parameters, annotations, output models), unknown-argument and removed-tool errors, the text renderers, held-input rules, selector matching, and the documentation gates (this README's tool tables and the CHANGELOG migration table).

Coverage of the Docker suite includes:

- virtual KWin engine tests for session lifecycle, AT-SPI2 observation, element refs, window listing, placement and closing (including ids that contain a quote), EIS pointer/keyboard/touch input and held input, clipboard, cleanup, and error handling;
- failing-session lifecycle regressions that stub `kwin_wayland`/`dbus-run-session` on `PATH`: `session_start` must fail within its startup deadline and surface the captured session stderr plus stray stdout (including a newline-free partial line), and teardown must reap the entire owned process group even when the session leader was already reaped or a descendant ignores `SIGTERM`;
- a KWin bus-name readiness regression whose `kwin_wayland` stub creates the Wayland socket before starting the real compositor: `session_start` must wait until `org.kde.KWin` has an owner before it sets up EIS input, and must fail with a clear error when KWin exits first or never takes the name;
- an accessibility-bus check that `org.a11y.Bus` has an owner as soon as `session_start` returns, before any app or AT-SPI2 query could activate it, and that a failed activation (a `dbus-send` stub on `PATH`) is reported as a `Warning:` line in the `session_start` output;
- the `org.a11y.Status.IsEnabled` switch: on in virtual sessions, and in live sessions reported, left off by default, switched on by `enable_accessibility=true`, and restored by `session_stop` and by a server exit while a tool is still running;
- exact input- and output-schema checks for all 29 registered tools, plus installed-server stdio calls through every MCP tool, resource and the live-consent elicitation flow;
- nested visual tests that start Xvfb and a test-owned KWin compositor inside the container, connect the installed MCP server to it, and verify pixels as well as accessibility state;
- KCalc before/after pixel transitions and a deterministic GUI probe for mouse hover, cursor inclusion, animation frame bursts, and CJK text rendered differently from a missing-glyph tofu control;
- screenshot coordinate mapping at output scales 1.0 and 1.45 (the fractional scale is set through `kscreen-doctor`): a probe button found by pixel color in the screenshot is clicked at origin plus pixel and must activate, and single captures and frame bursts report the logical workspace as their coordinate space;
- artifact retention and failure behavior, environment provenance, installed distribution metadata (version 1.0.0, the `Production/Stable` classifier), console entry points, and process/socket cleanup.

Pull requests run every E2E test except the ones marked `slow`; `.github/workflows/e2e-slow.yml` runs the `slow` set.

The container uses software rendering and needs no `--privileged`, `--cap-add`, GPU, or device flags. Its nested Xvfb server is part of the visual fixture; the host does not need an X server. CI runs the suite natively in four images: Debian trixie (`docker/e2e.Dockerfile`), Fedora 44 (`docker/e2e-fedora.Dockerfile`), openSUSE Tumbleweed (`docker/e2e-opensuse.Dockerfile`), and Arch Linux (`docker/e2e-arch.Dockerfile`, amd64 only). A failure in any of them fails the workflow.

| Jobs | GitHub Actions runner |
|---|---|
| `amd64`, `fedora-amd64`, `opensuse-amd64`, `archlinux-amd64` | `ubuntu-26.04` |
| `arm64`, `fedora-arm64`, `opensuse-arm64` | `ubuntu-26.04-arm` |

A completed run retains `environment.json`, `junit.xml`, `pytest.log`, nested-KWin/Xvfb/MCP logs, and visual PNG evidence below its artifact directory. Failed runs also collect Docker inspect, container log, and process-list diagnostics.

Exact-virtual ScreenShot2 coverage is version-dependent: on KWin 6.3–6.7, the legacy probe remains opt-in and is known to be unavailable in this container. KWin ≥6.8 requires `/dev/udmabuf` for its virtual software-render device; CI passes that device and its host group to the actual E2E container and opts the exact probe in. A historical native run confirmed the capability with a passing clipboard check and a real 1280×800 PNG, but this does not claim that the current PR's CI is green. The existing nested Xvfb fixture and visual oracles remain the legacy path; the KWin ≥6.8 nested visual fix is pending. Error propagation for the exact-virtual path is tested at both engine and MCP stdio levels; screenshot success, cursor pixels, frame bursts, and fractional-scale coordinate mapping are tested through the explicit nested X11/scrot visual mode. Multi-output layouts, negative origins, ScreenShot2 `CaptureWorkspace` normalization, and Spectacle partial coverage are not part of the container suite. See [docker/README.md](docker/README.md) for the process topology, complete test-file inventory, evidence layout, and targeted commands.

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, code style guidelines, and the pull request process.

```bash
git clone https://github.com/isac322/kwin-mcp.git
cd kwin-mcp
uv sync
uv run ruff check src/
uv run ruff format --check src/
uv run ty check src/
```

## License

[MIT](LICENSE)
