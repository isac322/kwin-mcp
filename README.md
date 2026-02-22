# kwin-mcp

**Model Context Protocol server for Linux desktop GUI automation on KDE Plasma 6 Wayland**

[![PyPI version](https://img.shields.io/pypi/v/kwin-mcp)](https://pypi.org/project/kwin-mcp/)
[![Downloads](https://img.shields.io/pypi/dm/kwin-mcp)](https://pypi.org/project/kwin-mcp/)
[![Python 3.12+](https://img.shields.io/pypi/pyversions/kwin-mcp)](https://pypi.org/project/kwin-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/isac322/kwin-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/isac322/kwin-mcp/actions/workflows/ci.yml)

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that enables AI agents (Claude Code, Cursor, and other MCP clients) to launch, interact with, and observe any Wayland application in a fully isolated virtual KWin session -- without affecting the user's desktop. With 29 MCP tools covering mouse, keyboard, touch, clipboard, accessibility tree inspection, screenshot capture, and window management, kwin-mcp provides everything needed for end-to-end GUI testing and desktop automation on Linux.

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
- [Contributing](#contributing)
- [License](#license)

## Why kwin-mcp?

- **Isolated sessions** -- Each session runs in its own `dbus-run-session` + `kwin_wayland --virtual` sandbox. Your host desktop is never affected.
- **No screenshots required for interaction** -- The AT-SPI2 accessibility tree gives the AI agent structured widget data (roles, names, coordinates, states, available actions), so it can interact with UI elements without relying solely on vision.
- **Zero authorization prompts** -- Uses KWin's private EIS (Emulated Input Server) D-Bus interface directly, bypassing the XDG RemoteDesktop portal. No user confirmation dialogs.
- **Works with any Wayland app** -- Anything that runs on KDE Plasma 6 Wayland works: Qt, GTK, Electron, and more. Input is injected via the standard `libei` protocol.
- **Full input coverage** -- Mouse, keyboard, multi-touch, and clipboard -- all injected through the isolated session for complete desktop automation.

## Use Cases

### Automated GUI Testing

Run end-to-end GUI tests for KDE/Qt/GTK applications in headless isolated sessions. kwin-mcp launches each app in its own virtual KWin compositor, interacts via mouse, keyboard, and touch input, then verifies results through screenshots and the accessibility tree -- all without a physical display.

### AI-Driven Desktop Automation

Let AI agents like Claude Code autonomously operate desktop applications. The agent reads the accessibility tree to understand the UI, performs actions through 29 MCP tools, and observes the results via screenshots -- creating a complete feedback loop for any Wayland application.

### Headless GUI Testing in CI/CD

Integrate Linux desktop GUI testing into CI/CD pipelines. kwin-mcp's virtual sessions require no X11 or physical display server, making it suitable for headless environments like GitHub Actions or GitLab CI runners on Linux.

## Quick Start

> Requires KDE Plasma 6 on Wayland. See [System Requirements](#system-requirements) for details.

**1. Install**

```bash
# Using uv (recommended)
uv tool install kwin-mcp

# Or using pip
pip install kwin-mcp
```

**2. Configure Claude Code**

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

**3. Use it**

Ask Claude Code to launch and interact with any GUI application:

```
Start a KWin session, launch kcalc, and press the buttons to calculate 2 + 3.
```

Claude Code will autonomously start an isolated session, launch the app, read the accessibility tree to find buttons, click them, and take a screenshot to verify the result.

## Configuration

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
```

## Available Tools

### Session Management (2 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `session_start` | `app_command?` `str`, `screen_width?` `int` (1920), `screen_height?` `int` (1080), `enable_clipboard?` `bool` (false), `env?` `dict` | Start an isolated KWin Wayland session, optionally launching an app. Set `enable_clipboard=true` to enable clipboard tools (requires `wl-clipboard`). Pass extra environment variables via `env`. |
| `session_stop` | _(none)_ | Stop the session and clean up all processes |

### Observation (3 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `screenshot` | `include_cursor?` `bool` (false) | Capture a screenshot of the virtual display (saved as PNG, returns file path) |
| `accessibility_tree` | `app_name?` `str`, `max_depth?` `int` (15) | Get the AT-SPI2 widget tree with roles, names, states, and coordinates |
| `find_ui_elements` | `query` `str`, `app_name?` `str` | Search for UI elements by name, role, or description (case-insensitive) |

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
| `keyboard_type_unicode` | `text` `str`, `screenshot_after_ms?` `list[int]` | Type arbitrary Unicode text (Korean, CJK, etc.) via `wtype` or clipboard fallback (`wl-copy` + Ctrl+V). Requires `wtype` or `wl-clipboard` installed. |
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

### Window Management (3 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `launch_app` | `command` `str`, `env?` `dict` | Launch an application inside the running session. Returns PID and log path. |
| `list_windows` | _(none)_ | List all accessible application windows in the session via AT-SPI2 |
| `focus_window` | `app_name` `str` | Focus a window by application name (case-insensitive match) |

### UI Polling (1 tool)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `wait_for_element` | `query` `str`, `app_name?` `str`, `timeout_ms?` `int` (5000), `poll_interval_ms?` `int` (200) | Poll the accessibility tree until an element matching the query appears or timeout expires. Useful for waiting on loading states and async UI updates. |

### Advanced (3 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `dbus_call` | `service` `str`, `path` `str`, `interface` `str`, `method` `str`, `args?` `list[str]` | Call any D-Bus method in the isolated session. Useful for controlling KWin scripting, app-specific D-Bus APIs, and system services. |
| `read_app_log` | `pid` `int`, `last_n_lines?` `int` (50) | Read stdout/stderr output of a launched app by PID. Set `last_n_lines=0` for all output. |
| `wayland_info` | `filter_protocol?` `str` | List Wayland protocols available in the session. Useful for verifying protocol access (e.g., `plasma_window_management`). |

> **Frame capture:** Many action tools accept an optional `screenshot_after_ms` parameter (e.g., `[0, 50, 100, 200, 500]`) that captures screenshots at specified delays (in milliseconds) after the action completes. This is useful for observing transient UI states like hover effects, click animations, and menu transitions without extra MCP round-trips. Frame capture uses the fast KWin ScreenShot2 D-Bus interface (~30-70ms per frame).

## How It Works

```
Claude Code / AI Agent
  |
  |  MCP (stdio)
  v
kwin-mcp server  (29 tools)
  |
  |-- session_start / stop -----> dbus-run-session
  |                                 |-- at-spi-bus-launcher
  |                                 +-- kwin_wayland --virtual
  |                                       +-- [your app]
  |
  |-- screenshot ---------------> spectacle (via D-Bus)
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
  |-- keyboard_type_unicode ----> wtype / wl-copy + Ctrl+V
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

### Triple Isolation

kwin-mcp provides three layers of isolation from the host desktop:

1. **D-Bus isolation** -- `dbus-run-session` creates a private session bus. The isolated session's services (KWin, AT-SPI2, portals) are invisible to the host.
2. **Display isolation** -- `kwin_wayland --virtual` creates its own Wayland compositor with a virtual framebuffer. No windows appear on the host display.
3. **Input isolation** -- Input events are injected through KWin's EIS interface into the isolated compositor only. The host desktop receives no input from kwin-mcp.

### Input Injection

Mouse, keyboard, and touch events are injected through KWin's private `org.kde.KWin.EIS.RemoteDesktop` D-Bus interface. This returns a `libei` file descriptor that allows low-level input emulation without requiring the XDG RemoteDesktop portal (which would show a user authorization dialog). The connection uses:

- **Absolute pointer positioning** for precise coordinate-based interaction
- **evdev keycodes** with full US QWERTY mapping for keyboard input
- **Smooth drag interpolation** (10+ intermediate steps) for realistic drag operations
- **EIS touch emulation** for multi-touch gestures (tap, swipe, pinch, multi-finger swipe)

### Screenshot Capture

The `screenshot` tool uses `spectacle` CLI for reliable full-screen capture. For action tools with the `screenshot_after_ms` parameter, screenshots are captured directly via the KWin `org.kde.KWin.ScreenShot2` D-Bus interface, which is much faster (~30-70ms vs ~200-300ms per frame) because it avoids process spawn overhead. Raw ARGB pixel data is read from a pipe and converted to PNG using Pillow.

### Accessibility Tree

The AT-SPI2 accessibility bus within the isolated session is queried via PyGObject (`gi.repository.Atspi`). This provides a structured tree of all UI widgets with their roles (button, text field, menu item, etc.), names, states (focused, enabled, visible, etc.), screen coordinates, and available actions (click, toggle, etc.).

## System Requirements

| Requirement | Details |
|-------------|---------|
| **OS** | Linux with KDE Plasma 6 (Wayland session) |
| **Python** | 3.12 or later |
| **KWin** | `kwin_wayland` with `--virtual` flag support (KDE Plasma 6.x) |
| **libei** | Usually bundled with KWin 6.x (EIS input emulation) |
| **spectacle** | KDE screenshot tool (CLI mode) |
| **AT-SPI2** | `at-spi2-core` for accessibility tree support |
| **PyGObject** | GObject introspection Python bindings |
| **D-Bus** | `dbus-python` bindings |

**Optional dependencies:**

| Package | Required for |
|---------|-------------|
| `wl-clipboard` (`wl-copy`, `wl-paste`) | `clipboard_get`, `clipboard_set`, and `keyboard_type_unicode` clipboard fallback |
| `wtype` | `keyboard_type_unicode` (preferred over clipboard fallback) |
| `wayland-utils` (`wayland-info`) | `wayland_info` tool |

### Installing System Dependencies

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
<summary><strong>Kubuntu / KDE Neon</strong></summary>

```bash
sudo apt install kwin-wayland spectacle at-spi2-core python3-gi gir1.2-atspi-2.0 python3-dbus

# Optional: for clipboard and Unicode input
sudo apt install wl-clipboard wtype wayland-utils
```

</details>

## Installation

### Using uv (recommended)

```bash
uv tool install kwin-mcp
```

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

- **US QWERTY keyboard layout only** -- `keyboard_type` supports US QWERTY only. For non-ASCII text (Korean, CJK, etc.), use `keyboard_type_unicode`, which requires `wtype` or `wl-clipboard` installed.
- **KDE Plasma 6+ required** -- Older KDE versions or other Wayland compositors (GNOME, Sway) are not supported.
- **AT-SPI2 availability varies** -- Some applications may not fully expose their widget tree via AT-SPI2.
- **Touch input is EIS-emulated** -- Touch events are emulated through KWin's EIS interface, not from a real touchscreen device. Most applications handle emulated touch correctly, but some may behave differently from physical touch.
- **Clipboard requires opt-in** -- Clipboard tools (`clipboard_get`, `clipboard_set`) are disabled by default because `wl-copy` can hang in isolated sessions. Enable with `enable_clipboard=true` in `session_start`, and ensure `wl-clipboard` is installed.

## Contributing

Contributions are welcome! Please open an issue or pull request on [GitHub](https://github.com/isac322/kwin-mcp).

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
