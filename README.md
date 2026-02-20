# kwin-mcp

[![PyPI version](https://img.shields.io/pypi/v/kwin-mcp)](https://pypi.org/project/kwin-mcp/)
[![Python 3.12+](https://img.shields.io/pypi/pyversions/kwin-mcp)](https://pypi.org/project/kwin-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/isac322/kwin-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/isac322/kwin-mcp/actions/workflows/ci.yml)

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for **Linux desktop GUI automation** on **KDE Plasma 6 Wayland**. It lets AI agents like [Claude Code](https://docs.anthropic.com/en/docs/claude-code) launch, interact with, and observe any Wayland application (Qt, GTK, Electron) in a fully isolated virtual KWin session — without touching the user's desktop.

## Why kwin-mcp?

- **Isolated sessions** — Each session runs in its own `dbus-run-session` + `kwin_wayland --virtual` sandbox. Your host desktop is never affected.
- **No screenshots required for interaction** — The AT-SPI2 accessibility tree gives the AI agent structured widget data (roles, names, coordinates, states, available actions), so it can interact with UI elements without relying solely on vision.
- **Zero authorization prompts** — Uses KWin's private EIS (Emulated Input Server) D-Bus interface directly, bypassing the XDG RemoteDesktop portal. No user confirmation dialogs.
- **Works with any Wayland app** — Anything that runs on KDE Plasma 6 Wayland works: Qt, GTK, Electron, and more. Input is injected via the standard `libei` protocol.

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

## Features

- **Session management** — Start and stop isolated KWin Wayland sessions with configurable screen resolution
- **Screenshot capture** — Capture the virtual display as PNG via KWin's ScreenShot2 D-Bus interface
- **Accessibility tree** — Read the full AT-SPI2 widget tree with roles, names, states, coordinates, and available actions
- **Element search** — Find UI elements by name, role, or description (case-insensitive)
- **Mouse input** — Click (left/right/middle, single/double), move, scroll (vertical/horizontal), and drag with smooth interpolation
- **Keyboard input** — Type text (full US QWERTY layout) and press key combinations with modifier support (Ctrl, Alt, Shift, Super)

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

### Installing System Dependencies

<details>
<summary><strong>Arch Linux / Manjaro</strong></summary>

```bash
sudo pacman -S kwin spectacle at-spi2-core python-gobject dbus-python-common
```

</details>

<details>
<summary><strong>Fedora (KDE Spin)</strong></summary>

```bash
sudo dnf install kwin-wayland spectacle at-spi2-core python3-gobject dbus-python
```

</details>

<details>
<summary><strong>openSUSE (KDE)</strong></summary>

```bash
sudo zypper install kwin6 spectacle at-spi2-core python3-gobject python3-dbus-python
```

</details>

<details>
<summary><strong>Kubuntu / KDE Neon</strong></summary>

```bash
sudo apt install kwin-wayland spectacle at-spi2-core python3-gi gir1.2-atspi-2.0 python3-dbus
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

### Session Management

| Tool | Parameters | Description |
|------|-----------|-------------|
| `session_start` | `app_command?`, `screen_width?`, `screen_height?` | Start an isolated KWin Wayland session, optionally launching an app |
| `session_stop` | _(none)_ | Stop the session and clean up all processes |

### Observation

| Tool | Parameters | Description |
|------|-----------|-------------|
| `screenshot` | `include_cursor?` | Capture a screenshot of the virtual display (saved as PNG, returns file path) |
| `accessibility_tree` | `app_name?`, `max_depth?` | Get the AT-SPI2 widget tree with roles, names, states, and coordinates |
| `find_ui_elements` | `query`, `app_name?` | Search for UI elements by name, role, or description (case-insensitive) |

### Mouse Input

| Tool | Parameters | Description |
|------|-----------|-------------|
| `mouse_click` | `x`, `y`, `button?`, `double?` | Click at coordinates (left/right/middle, single/double) |
| `mouse_move` | `x`, `y` | Move the cursor to coordinates without clicking |
| `mouse_scroll` | `x`, `y`, `delta`, `horizontal?` | Scroll at coordinates (positive = down/right, negative = up/left) |
| `mouse_drag` | `from_x`, `from_y`, `to_x`, `to_y` | Drag from one point to another with smooth interpolation |

### Keyboard Input

| Tool | Parameters | Description |
|------|-----------|-------------|
| `keyboard_type` | `text` | Type a string of text character by character (US QWERTY layout) |
| `keyboard_key` | `key` | Press a key or key combination (e.g., `Return`, `ctrl+c`, `alt+F4`, `shift+Tab`) |

## How It Works

```
Claude Code / AI Agent
  │
  │  MCP (stdio)
  ▼
kwin-mcp server
  │
  ├── session_start ─────────► dbus-run-session
  │                               ├── at-spi-bus-launcher
  │                               └── kwin_wayland --virtual
  │                                      └── [your app]
  │
  ├── screenshot ────────────► spectacle (via D-Bus)
  │
  ├── accessibility_tree ────► AT-SPI2 (via PyGObject)
  ├── find_ui_elements ──────► AT-SPI2 (via PyGObject)
  │
  └── mouse_* / keyboard_* ─► KWin EIS D-Bus ──► libei
```

### Triple Isolation

kwin-mcp provides three layers of isolation from the host desktop:

1. **D-Bus isolation** — `dbus-run-session` creates a private session bus. The isolated session's services (KWin, AT-SPI2, portals) are invisible to the host.
2. **Display isolation** — `kwin_wayland --virtual` creates its own Wayland compositor with a virtual framebuffer. No windows appear on the host display.
3. **Input isolation** — Input events are injected through KWin's EIS interface into the isolated compositor only. The host desktop receives no input from kwin-mcp.

### Input Injection

Mouse and keyboard events are injected through KWin's private `org.kde.KWin.EIS.RemoteDesktop` D-Bus interface. This returns a `libei` file descriptor that allows low-level input emulation without requiring the XDG RemoteDesktop portal (which would show a user authorization dialog). The connection uses:

- **Absolute pointer positioning** for precise coordinate-based interaction
- **evdev keycodes** with full US QWERTY mapping for keyboard input
- **Smooth drag interpolation** (10+ intermediate steps) for realistic drag operations

### Screenshot Capture

Screenshots are captured using `spectacle --dbus --background --nonotify` connected to the isolated session's D-Bus and Wayland socket. The KWin `org.kde.KWin.ScreenShot2` interface provides the framebuffer data.

### Accessibility Tree

The AT-SPI2 accessibility bus within the isolated session is queried via PyGObject (`gi.repository.Atspi`). This provides a structured tree of all UI widgets with their roles (button, text field, menu item, etc.), names, states (focused, enabled, visible, etc.), screen coordinates, and available actions (click, toggle, etc.).

## Limitations

- **US QWERTY keyboard layout only** — Other keyboard layouts are not yet supported for text typing.
- **KDE Plasma 6+ required** — Older KDE versions or other Wayland compositors (GNOME, Sway) are not supported.
- **AT-SPI2 availability varies** — Some applications may not fully expose their widget tree via AT-SPI2.

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
