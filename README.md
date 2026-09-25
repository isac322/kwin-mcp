# kwin-mcp

**Model Context Protocol server for Linux desktop GUI automation on KDE Plasma 6 Wayland**

[![PyPI version](https://img.shields.io/pypi/v/kwin-mcp)](https://pypi.org/project/kwin-mcp/)
[![Downloads](https://img.shields.io/pypi/dm/kwin-mcp)](https://pypi.org/project/kwin-mcp/)
[![Python 3.12+](https://img.shields.io/pypi/pyversions/kwin-mcp)](https://pypi.org/project/kwin-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/isac322/kwin-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/isac322/kwin-mcp/actions/workflows/ci.yml)

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that enables AI agents (Claude Code, Cursor, and other MCP clients) to launch, interact with, and observe any Wayland application in a fully isolated virtual KWin session -- without affecting the user's desktop. It also supports **live desktop automation** by connecting to an existing KWin session (real desktop or container) for collaborative workflows. With 31 MCP tools covering mouse, keyboard, touch, clipboard, accessibility tree inspection, screenshot capture, and window management, kwin-mcp provides everything needed for end-to-end GUI testing and desktop automation on Linux.

## Table of Contents

- [Why kwin-mcp?](#why-kwin-mcp)
- [Use Cases](#use-cases)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Available Tools](#available-tools)
- [How It Works](#how-it-works)
- [System Requirements](#system-requirements)
- [Installation](#installation)
- [Limitations](#limitations)
- [End-to-End Testing](#end-to-end-testing)
- [Contributing](#contributing)
- [License](#license)

## Why kwin-mcp?

- **Isolated sessions** -- Each session runs in its own `dbus-run-session` + `kwin_wayland --virtual` sandbox. Your host desktop is never affected.
- **Live session support** -- Connect to a real KDE Plasma desktop or a KWin instance inside a container (e.g. `systemd-nspawn`) for collaborative "share my screen" workflows.
- **No screenshots required for interaction** -- The AT-SPI2 accessibility tree gives the AI agent structured widget data (roles, names, coordinates, states, available actions), so it can interact with UI elements without relying solely on vision.
- **Zero authorization prompts** -- Uses KWin's private EIS (Emulated Input Server) D-Bus interface directly, bypassing the XDG RemoteDesktop portal. No user confirmation dialogs.
- **Works with any Wayland app** -- Anything that runs on KDE Plasma 6 Wayland works: Qt, GTK, Electron, and more. Input is injected via the standard `libei` protocol.
- **Full input coverage** -- Mouse, keyboard, multi-touch, and clipboard -- all injected through the isolated session for complete desktop automation.

## Use Cases

### Automated GUI Testing

Run end-to-end GUI tests for KDE/Qt/GTK applications in headless isolated sessions. kwin-mcp launches each app in its own virtual KWin compositor, interacts via mouse, keyboard, and touch input, then verifies results through screenshots and the accessibility tree -- all without a physical display.

### AI-Driven Desktop Automation

Let AI agents like Claude Code autonomously operate desktop applications. The agent reads the accessibility tree to understand the UI, performs actions through 31 MCP tools, and observes the results via screenshots -- creating a complete feedback loop for any Wayland application.

### Live Desktop Collaboration

Connect to your real desktop session and let Claude observe and interact with what you see. Use `session_connect` or pass `--default-live-session` to make live mode the default. Also supports attaching to KWin running inside containers (e.g. `systemd-nspawn`) for isolated agent desktops.

### Headless GUI Testing in CI/CD

Integrate Linux desktop GUI testing into CI/CD pipelines. kwin-mcp's virtual sessions require no X11 or physical display server, making it suitable for headless environments like GitHub Actions or GitLab CI runners on Linux.

### Kiosk and Embedded Device Automation

Automate kiosk interfaces and embedded Linux desktops running KDE Plasma or a bare KWin Wayland compositor. Use `session_start` for isolated virtual testing of kiosk UIs, or `session_connect` to attach directly to a live kiosk or embedded device session for real-time automation and diagnostics.

## Quick Start

> Requires KDE Plasma 6 on Wayland. See [System Requirements](#system-requirements) for details.

> [!NOTE]
> **Fixed in 0.8.0:** the published kwin-mcp 0.7.0 package on PyPI did not cap its `mcp` dependency, so a fresh install could resolve `mcp` 2.x and the server failed at startup with `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`. Release 0.8.0 ships the `mcp>=1.0.0,<2` constraint in its package metadata. If you are still on 0.7.0, upgrade before following the steps below.

**1. Install system and build dependencies**

`uv tool install` and `uvx` always install kwin-mcp into an isolated Python environment, and `pip install` does the same when run inside a virtual environment. An isolated environment cannot reuse your distribution's `python3-gi` or `python3-dbus` packages, so PyGObject, pycairo, and dbus-python are built from source during installation and need a C compiler and development headers. Install the packages from [Installing System Dependencies](#installing-system-dependencies) first. On Debian 13 (Trixie):

```bash
sudo apt-get install -y --no-install-recommends build-essential pkg-config python3-dev libcairo2-dev libgirepository-2.0-dev libdbus-1-dev
```

**2. Install**

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

The fastest way to wire kwin-mcp into your editor is to install one of the bundled plugins. Each plugin auto-registers the MCP server **and** ships the `kwin-desktop-automation` skill, which teaches the agent which tool to call when (session-mode selection, the observe → act → verify loop, US-QWERTY vs Unicode typing, AT-SPI2 surface-local coordinates, and other platform pitfalls).

**Claude Code** — install the plugin from the marketplace:

```text
/plugin marketplace add isac322/kwin-mcp
/plugin install kwin-mcp@kwin-mcp
```

**OpenCode** — add the npm plugin to your `opencode.json`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["@isac322/kwin-mcp-opencode"]
}
```

For the full integration guide (manual fallback, customising the skill, troubleshooting), see [docs/ai-agent-integration.md](docs/ai-agent-integration.md).

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

# Interactive CLI (REPL for rapid testing)
kwin-mcp-cli

# Live session mode (default to real desktop instead of virtual)
kwin-mcp --default-live-session
kwin-mcp-cli --default-live-session
```

## Available Tools

### Session Management (3 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `session_start` | `app_command?` `str`, `screen_width?` `int` (1920), `screen_height?` `int` (1080), `enable_clipboard?` `bool` (false), `keep_screenshots?` `bool` (false), `isolate_home?` `bool` (false), `keep_home?` `bool` (false), `env?` `dict` | Start an isolated KWin Wayland session, optionally launching an app. Set `enable_clipboard=true` to enable clipboard tools (requires `wl-clipboard`). Set `keep_screenshots=true` to preserve screenshot files after `session_stop`. Set `isolate_home=true` to create a temporary HOME with isolated XDG directories (config, data, cache, state), preventing apps from reading/writing host user settings. Set `keep_home=true` to preserve the isolated home directory after `session_stop`. Pass extra environment variables via `env`. Startup is deadline-bounded: a failed handshake raises `RuntimeError` with the captured session stderr and stray stdout. |
| `session_connect` | `dbus_address?` `str`, `wayland_display?` `str`, `keep_screenshots?` `bool` (false) | Connect to an existing KWin session (real desktop or container). Defaults to `$DBUS_SESSION_BUS_ADDRESS` and `$WAYLAND_DISPLAY`. Clipboard is always enabled. `session_stop` only disconnects without killing KWin or pre-existing apps. |
| `session_stop` | _(none)_ | Stop the session and clean up. For virtual sessions: signals the whole session process group (`SIGTERM`, then `SIGKILL` for members that remain), so descendants still stop even if the session leader already exited. For live sessions: disconnects without killing KWin or pre-existing apps. |

### Observation (3 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `screenshot` | `include_cursor?` `bool` (false) | Capture the whole workspace as a PNG. Returns the file path and a `Coordinate space` line with the image's logical origin, size, capture backend, and coverage. Image pixel `(px, py)` is the global logical point `(origin_x + px, origin_y + py)`, the same space `mouse_click` and `touch_tap` take. |
| `accessibility_tree` | `app_name?` `str`, `max_depth?` `int` (15), `role?` `str` | Get the AT-SPI2 widget tree with roles, names, states, coordinates, the text content of editors and entries (`text='...'`, capped at 200 characters), and scrollbar/slider positions (`value=current/max`). Use `role` to filter to specific element types (e.g. `"button"`, `"check box"`). Non-matching elements are hidden but their children are still traversed. |
| `find_ui_elements` | `query` `str`, `app_name?` `str`, `states?` `list[str]` | Search for UI elements by name, role, or description (case-insensitive); matches report their text content and scrollbar/slider value when they have one. Optionally filter by AT-SPI2 states (e.g. `["focused"]`, `["active", "visible"]`). `query` can be empty when filtering by states only. |

### Mouse Input (6 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `mouse_click` | `x` `int`, `y` `int`, `button?` `str` ("left"), `double?` `bool`, `triple?` `bool`, `modifiers?` `list[str]`, `hold_ms?` `int` (0), `screenshot_after_ms?` `list[int]` | Click at coordinates. Supports left/right/middle, single/double/triple click, modifier keys (e.g. `["ctrl", "shift"]`), and long-press via `hold_ms`. |
| `mouse_move` | `x` `int`, `y` `int`, `screenshot_after_ms?` `list[int]` | Move the cursor (hover) to coordinates without clicking |
| `mouse_scroll` | `x` `int`, `y` `int`, `delta` `int`, `horizontal?` `bool`, `discrete?` `bool`, `steps?` `int` (1) | Scroll at coordinates. `delta` positive = down/right, negative = up/left. Use `discrete=true` for wheel ticks, `steps` to split into smooth increments. |
| `mouse_drag` | `from_x` `int`, `from_y` `int`, `to_x` `int`, `to_y` `int`, `button?` `str` ("left"), `modifiers?` `list[str]`, `waypoints?` `list[[x,y,dwell_ms]]`, `screenshot_after_ms?` `list[int]` | Drag from one point to another with smooth interpolation. Supports custom `waypoints` for complex drag paths. |
| `mouse_button_down` | `x` `int`, `y` `int`, `button?` `str` ("left") | Press a mouse button at coordinates without releasing. Use with `mouse_button_up` for manual drag control. |
| `mouse_button_up` | `x` `int`, `y` `int`, `button?` `str` ("left") | Release a previously pressed mouse button at coordinates |

### Keyboard Input (5 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `keyboard_type` | `text` `str`, `screenshot_after_ms?` `list[int]` | Type a string of text character by character (US QWERTY layout) |
| `keyboard_type_unicode` | `text` `str`, `screenshot_after_ms?` `list[int]` | Type arbitrary Unicode text (Korean, CJK, etc.) via `wtype`, or, when `wtype` is unavailable or unsupported by the session, a temporary clipboard paste (Ctrl+V). The previous clipboard content, in all its formats, is restored after the paste, and Klipper keeps the typed text out of its history. Reports failure if the clipboard cannot be taken, nothing reads the text after Ctrl+V, or the restore fails. Does not need `enable_clipboard`. |
| `keyboard_key` | `key` `str`, `screenshot_after_ms?` `list[int]` | Press a key or key combination (e.g., `Return`, `ctrl+c`, `alt+F4`, `shift+Tab`) |
| `keyboard_key_down` | `key` `str` | Press and hold a key without releasing. Useful for holding modifiers across multiple actions (e.g., hold Ctrl while clicking items). |
| `keyboard_key_up` | `key` `str` | Release a previously held key |

### Touch Input (4 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `touch_tap` | `x` `int`, `y` `int`, `hold_ms?` `int` (0), `screenshot_after_ms?` `list[int]` | Tap at coordinates. Use `hold_ms` for long-press gestures. |
| `touch_swipe` | `from_x` `int`, `from_y` `int`, `to_x` `int`, `to_y` `int`, `duration_ms?` `int` (300), `screenshot_after_ms?` `list[int]` | Swipe from one point to another with configurable duration |
| `touch_pinch` | `center_x` `int`, `center_y` `int`, `start_distance` `int`, `end_distance` `int`, `duration_ms?` `int` (500), `screenshot_after_ms?` `list[int]` | Two-finger pinch gesture. `end_distance < start_distance` = pinch in, `end_distance > start_distance` = pinch out. |
| `touch_multi_swipe` | `from_x` `int`, `from_y` `int`, `to_x` `int`, `to_y` `int`, `fingers?` `int` (3), `duration_ms?` `int` (300), `screenshot_after_ms?` `list[int]` | Multi-finger swipe gesture (2-5 fingers) for system gestures like workspace switching |

### Clipboard (2 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `clipboard_get` | _(none)_ | Read the current clipboard text content. Requires `enable_clipboard=true` in `session_start` and `wl-clipboard` installed. |
| `clipboard_set` | `text` `str` | Set the clipboard text content. Same requirements as `clipboard_get`. |

### Window Management (4 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `launch_app` | `command` `str`, `env?` `dict` | Launch an application inside the running session. Returns PID and log path. |
| `list_windows` | _(none)_ | List all accessible application windows with per-window titles and active/focused state markers via AT-SPI2 |
| `focus_window` | `app_name` `str` | Activate and raise a window by application name (case-insensitive match), via KWin scripting |
| `window_geometry` | `app_name?` `str` | Report window frame and client rectangles in **global screen coordinates** via KWin scripting — the same space `find_ui_elements` / `accessibility_tree` report and `mouse_click` / `touch_tap` take. |

### UI Polling (1 tool)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `wait_for_element` | `query` `str`, `app_name?` `str`, `timeout_ms?` `int` (5000), `poll_interval_ms?` `int` (200), `expected_states?` `list[str]` | Poll the accessibility tree until an element matching the query and/or states appears or timeout expires. Use `expected_states` to wait for state changes (e.g. `["active"]`, `["checked"]`). `query` can be empty when waiting for state changes only. |

### Advanced (3 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `dbus_call` | `service` `str`, `path` `str`, `interface` `str`, `method` `str`, `args?` `list[str]` | Call any D-Bus method in the isolated session. Useful for controlling KWin scripting, app-specific D-Bus APIs, and system services. |
| `read_app_log` | `pid` `int`, `last_n_lines?` `int` (50) | Read stdout/stderr output of a launched app by PID. Set `last_n_lines=0` for all output. |
| `wayland_info` | `filter_protocol?` `str` | List Wayland protocols available in the session. Useful for verifying protocol access (e.g., `plasma_window_management`). |

> **Frame capture:** Many action tools accept an optional `screenshot_after_ms` parameter (e.g., `[0, 50, 100, 200, 500]`) that captures screenshots at specified delays (in milliseconds) after the action completes. This is useful for observing transient UI states like hover effects, click animations, and menu transitions without extra MCP round-trips. Frame capture uses the fast KWin ScreenShot2 D-Bus interface (~30-70ms per frame). Each frame is followed by its own `Coordinate space` line in the same format as `screenshot`. A frame whose mapping cannot be proven — a topology change or a failed observation during the capture — still writes its PNG but reports `Coordinate space: unavailable (reason)`; that PNG holds the backend's raw unnormalized pixels, so only its timing observation is meaningful.

## How It Works

```
Claude Code / AI Agent
  |
  |  MCP (stdio)
  v
kwin-mcp server  (31 tools)       kwin-mcp-cli (interactive REPL)
  |                                  |
  +--- both delegate to AutomationEngine (core.py) ---+
  |
  |-- session_start (virtual) ---> dbus-run-session
  |                                 |-- at-spi-bus-launcher
  |                                 +-- kwin_wayland --virtual
  |                                       +-- [your app]
  |
  |-- session_connect (live) ----> existing KWin (real desktop / container)
  |
  |-- screenshot ---------------> KWin ScreenShot2 D-Bus (spectacle fallback)
  |
  |-- accessibility_tree -------> AT-SPI2 (via PyGObject)
  |-- find_ui_elements ---------> AT-SPI2 (via PyGObject)
  |-- wait_for_element ----------> AT-SPI2 (polling)
  |
  |-- mouse_* ------------------> KWin EIS D-Bus --> libei
  |-- keyboard_* ---------------> KWin EIS D-Bus --> libei
  |-- touch_* ------------------> KWin EIS D-Bus --> libei
  |    +-- screenshot_after_ms -> KWin ScreenShot2 D-Bus (fast frame capture)
  |
  |-- keyboard_type_unicode ----> wtype, or temporary Wayland data-control clipboard owner + Ctrl+V
  |-- clipboard_* --------------> wl-copy / wl-paste (wl-clipboard)
  |
  |-- launch_app / list_windows / focus_window
  |                                |-- subprocess spawn
  |                                +-- AT-SPI2 (via PyGObject)
  |
  |-- dbus_call -----------------> dbus-send (generic D-Bus)
  |-- read_app_log --------------> log file read
  +-- wayland_info --------------> wayland-info
```

### Triple Isolation (+ Optional Home Isolation)

kwin-mcp provides three layers of isolation from the host desktop:

1. **D-Bus isolation** -- `dbus-run-session` creates a private session bus. The isolated session's services (KWin, AT-SPI2, portals) are invisible to the host.
2. **Display isolation** -- `kwin_wayland --virtual` creates its own Wayland compositor with a virtual framebuffer. No windows appear on the host display.
3. **Input isolation** -- Input events are injected through KWin's EIS interface into the isolated compositor only. The host desktop receives no input from kwin-mcp.
4. **Home directory isolation** (optional) -- When `isolate_home=true` is set in `session_start`, a temporary HOME directory is created with isolated XDG directories (`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME`). Apps in the session cannot read or modify host user settings (e.g. `~/.config/kdeglobals`), improving test reproducibility and safety. `XDG_RUNTIME_DIR` is intentionally not isolated because the Wayland socket resides there.

### Input Injection

Mouse, keyboard, and touch events are injected through KWin's private `org.kde.KWin.EIS.RemoteDesktop` D-Bus interface. This returns a `libei` file descriptor that allows low-level input emulation without requiring the XDG RemoteDesktop portal (which would show a user authorization dialog). The connection uses:

- **Absolute pointer positioning** in KWin's global logical coordinates, the space `window_geometry`, element rectangles, and screenshot pixels (after adding the screenshot origin) share
- **evdev keycodes** with full US QWERTY mapping for keyboard input
- **Smooth drag interpolation** (10+ intermediate steps) for realistic drag operations
- **EIS touch emulation** for multi-touch gestures (tap, swipe, pinch, multi-finger swipe)

### Screenshot Capture

The normal Wayland capture path tries KWin's `org.kde.KWin.ScreenShot2` D-Bus interface first and uses the `spectacle` CLI as a fallback. Both capture the whole workspace across all outputs. Action tools with `screenshot_after_ms` use the same path for frame bursts, and Pillow converts ScreenShot2's raw pipe frames (RGB32, ARGB32, or RGBX8888 depending on the KWin version) to PNG. The Docker visual QA suite also has an explicit test-only X11 backend: when `KWIN_MCP_X11_SCREENSHOT=1` is set for a server connected to the nested KWin/Xvfb fixture, captures use `scrot`. Normal virtual and live Wayland sessions do not opt into this backend.

Each Spectacle capture has a deadline derived from the image Spectacle must produce, the workspace canvas at the output scale (on mixed-scale layouts, every output is upscaled to the next whole scale above the largest): 15 seconds plus 1 second per canvas megapixel, capped at 120 seconds. A 1920x1080 output gets 18 seconds; a mixed-scale 1.45 + 1.0 layout (a 6490x2160 canvas) gets 30 seconds. A capture that does not finish in time fails with `spectacle timed out after <N>s`.

Saved PNGs are normalized to KWin's global logical coordinate space, so a screenshot taken on a fractionally scaled output has one image pixel per logical pixel. The result states the mapping, for example on two outputs where a 1280x1024 screen sits left of a 1920x1080 screen:

```text
Screenshot saved: <path> (<size> KB)
Coordinate space: logical; origin (-1280, 0); size 3200x1080; backend screenshot2; coverage full; topology observed stable before/after capture
```

`origin` is the top-left of KWin's virtual screen geometry. It is negative when an output sits left of or above `(0, 0)`. To click image pixel `(px, py)`, pass `x = origin_x + px` and `y = origin_y + py` to `mouse_click`; no scale conversion is needed. On a single output at scale 1, the origin is `(0, 0)` and the size equals the screen size.

Backends deliver different pixel spaces, and kwin-mcp normalizes each one:

- **ScreenShot2** `CaptureWorkspace` already renders logical pixels for the whole virtual screen, including outputs at negative positions.
- **Spectacle** captures device pixels, which kwin-mcp rescales to logical pixels. When Spectacle composites several screens, it clips outputs at negative logical positions. kwin-mcp cannot recreate those pixels: it leaves them transparent and reports `coverage partial` with the captured regions.
- **X11/scrot** (test-only) captures the X root, where each KWin output is an X window of `logical size * scale` pixels. kwin-mcp locates those windows with `xwininfo` and rescales each one to logical pixels.

kwin-mcp reads the output topology through KWin scripting immediately before and after each capture and checks the captured image against it, retrying once when the layout changed mid-capture. If the mapping still cannot be proven, `screenshot` fails with an error containing `coordinate mapping cannot be proven` rather than returning guessed coordinates; a burst frame keeps its raw PNG and reports `Coordinate space: unavailable (reason)` instead. Observed stability is not an atomicity guarantee: a topology change that reverted between the two observations can go unnoticed.

### Accessibility Tree

The AT-SPI2 accessibility bus within the isolated session is queried via PyGObject (`gi.repository.Atspi`). This provides a structured tree of all UI widgets with their roles (button, text field, menu item, etc.), names, states (focused, enabled, visible, etc.), screen coordinates, and available actions (click, toggle, etc.).

## System Requirements

| Requirement | Details |
|-------------|---------|
| **OS** | Linux with KDE Plasma 6 (Wayland session) |
| **Python** | 3.12 or later |
| **KWin** | `kwin_wayland` with `--virtual` flag support (KDE Plasma 6.x) |
| **libei** | Usually bundled with KWin 6.x (EIS input emulation) |
| **libwayland-client** | Used by the `keyboard_type_unicode` clipboard paste; already installed as a KWin dependency |
| **spectacle** | KDE screenshot tool (CLI mode); packaged as `kde-spectacle` on Debian and Ubuntu |
| **AT-SPI2** | `at-spi2-core` for accessibility tree support |
| **PyGObject** | GObject introspection Python bindings (built from source by uv/pip; see [build prerequisites](#build-prerequisites-for-uv-and-pip-installs)) |
| **D-Bus** | `dbus-python` bindings (built from source by uv/pip; needs libdbus development files) |
| **Build tools** | C compiler, `pkg-config`, Python headers, and cairo, GObject Introspection, and libdbus development files |

**Optional dependencies:**

| Package | Required for |
|---------|-------------|
| `wl-clipboard` (`wl-copy`, `wl-paste`) | `clipboard_get` and `clipboard_set` |
| `wtype` | `keyboard_type_unicode` direct typing, tried first; when it is unavailable or unsupported by the session, the built-in clipboard paste is used instead |
| `wayland-utils` (`wayland-info`) | `wayland_info` tool |

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

Debian packages Spectacle as `kde-spectacle`; there is no `spectacle` package. `gir1.2-atspi-2.0` provides the AT-SPI2 GObject Introspection typelib that PyGObject loads at runtime.

```bash
sudo apt-get install -y --no-install-recommends kwin-wayland kde-spectacle at-spi2-core gir1.2-atspi-2.0

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
sudo apt install kwin-wayland kde-spectacle at-spi2-core python3-gi gir1.2-atspi-2.0 python3-dbus

# Optional: for clipboard and Unicode input
sudo apt install wl-clipboard wtype wayland-utils
```

</details>

## Installation

Install the [system and build dependencies](#installing-system-dependencies) before using any method below. The uv installs, pip installs into a virtual environment, and the from-source install build PyGObject, pycairo, and dbus-python from source and fail without them.

> [!NOTE]
> kwin-mcp 0.7.0 on PyPI did not cap its `mcp` dependency, so a fresh install could resolve `mcp` 2.x and the server failed at startup with `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`. Release 0.8.0 ships the `mcp>=1.0.0,<2` constraint; install 0.8.0 or later.

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

## Limitations

- **US QWERTY keyboard layout only** -- `keyboard_type` supports US QWERTY only. For non-ASCII text (Korean, CJK, etc.), use `keyboard_type_unicode`.
- **Unicode typing can paste through the clipboard** -- When `wtype` is unavailable or unsupported by the session, `keyboard_type_unicode` briefly puts the text on the clipboard, then restores the previous selection and keeps serving it until another copy replaces it, even after `session_stop` disconnects a live session. The restore is best-effort, not atomic: Wayland has no compare-and-swap for the selection, so a copy another client makes at the same moment as the restore can be overwritten. Wayland also does not tell the clipboard owner which client read the text, so a clipboard manager that reads it after Ctrl+V can be taken for the target app, and any client that read it keeps its copy. The `x-kde-passwordManagerHint` marker keeps the text out of Klipper's history, but clipboard managers that ignore the marker can still record it.
- **KDE Plasma 6+ required** -- Older KDE versions or other Wayland compositors (GNOME, Sway) are not supported.
- **AT-SPI2 availability varies** -- Some applications may not fully expose their widget tree via AT-SPI2.
- **Touch input is EIS-emulated** -- Touch events are emulated through KWin's EIS interface, not from a real touchscreen device. Most applications handle emulated touch correctly, but some may behave differently from physical touch.
- **Clipboard requires opt-in** -- Clipboard tools (`clipboard_get`, `clipboard_set`) are disabled by default because `wl-copy` can hang in isolated sessions. Enable with `enable_clipboard=true` in `session_start`, and ensure `wl-clipboard` is installed.
- **QMenu (native context menus) may not appear in AT-SPI2** -- Qt's AT-SPI2 bridge has incomplete support for popup menus on Wayland. Context menus may not be visible in `accessibility_tree` or `find_ui_elements`. Workaround: click by coordinates derived from the parent widget's reported screen rectangle.
- **Screen edge triggers ignore EIS pointer events** -- Auto-hide panels and layer-shell strips do not react when the pointer reaches a screen edge through EIS. Use `dbus_call` to invoke KWin scripting or a keyboard shortcut instead of trying to hover the edge.
- **KWin claims multi-finger touch gestures** -- Three- and four-finger swipes are consumed by the compositor as global gestures and never reach the application; use `fingers=2` when the target is the app itself.
- **Element coordinates are screen-global, or unavailable** -- `find_ui_elements`, `accessibility_tree` and `wait_for_element` report rectangles in the same global screen coordinates `mouse_click` and `touch_tap` take (`@ screen (x, y, wxh)`). When the element's window cannot be matched to exactly one KWin window — an app that masks its real process id (e.g. a D-Bus proxy), several identical windows of one process, or a window set that changed mid-query — the element reports `@ unavailable (reason)` with no coordinates rather than a position that could click the wrong window.
- **Spectacle fallback can return partial screenshots** -- When ScreenShot2 is unavailable and Spectacle composites several screens, Spectacle clips outputs at negative logical positions. Those regions stay transparent, and the `Coordinate space` line reports `coverage partial` with the regions that were captured. ScreenShot2 captures the whole workspace.

## End-to-End Testing

The Docker suite collects every test under `tests/e2e` against the packaged application, not an editable source checkout. `docker/e2e.Dockerfile` builds a wheel and installs it into `/opt/kwin-mcp-venv` with standard `Requires-Dist` resolution: PyGObject, pycairo, and dbus-python compile from source in a builder-only stage, while `mcp`, Pillow, and the remaining dependencies resolve fresh from PyPI within the declared ranges. The suite runs both `AutomationEngine` tests and the installed `kwin-mcp` console entry point. The MCP tests initialize a real client/server session over stdio JSON-RPC.

Run the complete suite from the repository root:

```bash
scripts/run-e2e-docker.sh
```

The runner builds the image for Docker's native architecture, creates `artifacts/e2e/<UTC-timestamp>-<pid>/`, runs pytest, and prints the artifact path. Additional arguments pass directly to pytest:

```bash
scripts/run-e2e-docker.sh -- tests/e2e/test_mcp_protocol.py -v
scripts/run-e2e-docker.sh -- -k "visual or screenshot" -v
```

Coverage includes:

- virtual KWin engine tests for session lifecycle, AT-SPI2 observation, window geometry and control, EIS pointer/keyboard/touch input, clipboard, cleanup, and error handling;
- failing-session lifecycle regressions that stub `kwin_wayland`/`dbus-run-session` on `PATH`: `session_start` must fail within its startup deadline and surface the captured session stderr plus stray stdout (including a newline-free partial line), and teardown must reap the entire owned process group even when the session leader was already reaped or a descendant ignores `SIGTERM`;
- exact input-schema checks for all 31 registered tools, plus installed-server stdio calls through every MCP wrapper;
- nested visual tests that start Xvfb and a test-owned KWin compositor inside the container, connect the installed MCP server to it, and verify pixels as well as accessibility state;
- KCalc before/after pixel transitions and a deterministic GUI probe for mouse hover, cursor inclusion, animation frame bursts, and CJK text (`GUI 검증 42`) rendered differently from a tofu control (`□□`);
- screenshot coordinate mapping at output scales 1.0 and 1.45 (the fractional scale is set through `kscreen-doctor`): a probe button found by pixel color in the screenshot is clicked at origin plus pixel and must activate, and single screenshots and frame bursts report the logical workspace as their coordinate space;
- screenshot retention and failure behavior, environment provenance, installed distribution metadata, console entry points, and process/socket cleanup.

The container uses software rendering and needs no `--privileged`, `--cap-add`, GPU, or device flags. Its nested Xvfb server is part of the visual fixture; the host does not need an X server. CI runs the same image natively on both architectures:

| Architecture | GitHub Actions runner |
|---|---|
| `amd64` | `ubuntu-24.04` |
| `arm64` | `ubuntu-24.04-arm` |

A completed run retains `environment.json`, `junit.xml`, `pytest.log`, nested-KWin/Xvfb/MCP logs, and visual PNG evidence below its artifact directory. Failed runs also collect Docker inspect, container log, and process-list diagnostics.

One legacy success test for ScreenShot2 on KWin's exact `--virtual` backend remains intentionally skipped because that backend does not return capture data in this container. Error propagation for that path is tested at both engine and MCP stdio levels; screenshot success, cursor pixels, frame bursts, and fractional-scale coordinate mapping are tested through the explicit nested X11/scrot visual mode. Multi-output layouts, negative origins, ScreenShot2 `CaptureWorkspace` normalization, and Spectacle partial coverage are not part of the container suite. See [docker/README.md](docker/README.md) for the process topology, complete test-file inventory, evidence layout, and targeted commands.

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
