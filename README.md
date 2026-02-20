# kwin-mcp

[Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for GUI automation on KDE Plasma 6 Wayland.

Enables AI assistants (such as [Claude Code](https://docs.anthropic.com/en/docs/claude-code)) to autonomously launch, interact with, and observe GUI applications in an isolated KWin Wayland session — without affecting the user's desktop.

## Features

- **Isolated sessions** — `dbus-run-session` + `kwin_wayland --virtual` sandbox with no host interference
- **Screenshots** — Capture via KWin's ScreenShot2 D-Bus interface (spectacle CLI)
- **Accessibility tree** — AT-SPI2 widget discovery with roles, names, states, and coordinates
- **Element search** — Find UI elements by name, role, or description
- **Mouse input** — Click, move, scroll, and drag via KWin EIS (Emulated Input Server)
- **Keyboard input** — Type text and press key combinations via KWin EIS

## System Requirements

- **KDE Plasma 6** (Wayland session) with `kwin_wayland` supporting `--virtual`
- **Python 3.12+**
- **spectacle** (KDE screenshot tool, CLI mode)
- **AT-SPI2** (`at-spi2-core`) for accessibility tree support
- **PyGObject** system bindings (`python-gobject` / `python3-gi`)
- **D-Bus** Python bindings (`dbus-python`)
- **libei** (usually bundled with KWin 6.x) for input emulation

## Installation

```bash
pip install kwin-mcp
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install kwin-mcp
```

## Usage

### Claude Code

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "kwin-mcp": {
      "command": "kwin-mcp"
    }
  }
}
```

Or if installed via `uvx`:

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

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "kwin-mcp": {
      "command": "kwin-mcp"
    }
  }
}
```

### Running Directly

```bash
# As installed script
kwin-mcp

# Or as a module
python -m kwin_mcp
```

## Available Tools

| Tool | Description |
|------|-------------|
| `session_start` | Start an isolated KWin Wayland session, optionally launching an app |
| `session_stop` | Stop the session and clean up all processes |
| `screenshot` | Capture a screenshot (returns base64-encoded PNG) |
| `accessibility_tree` | Get the AT-SPI2 widget tree with roles, names, states, and coordinates |
| `find_ui_elements` | Search for UI elements by name, role, or description |
| `mouse_click` | Click (left/right/middle, single/double) at coordinates |
| `mouse_move` | Move the mouse cursor to coordinates |
| `mouse_scroll` | Scroll vertically or horizontally at coordinates |
| `mouse_drag` | Drag from one point to another |
| `keyboard_type` | Type text into the focused element |
| `keyboard_key` | Press a key or key combination (e.g., `ctrl+c`, `alt+F4`) |

## How It Works

1. **Session isolation**: `session_start` launches `kwin_wayland --virtual` inside a private `dbus-run-session`, creating a fully isolated Wayland compositor with its own D-Bus bus.

2. **Input injection**: Mouse and keyboard events are injected through KWin's EIS (Emulated Input Server) D-Bus interface, which provides a `libei` file descriptor for low-level input emulation.

3. **Screenshot capture**: Screenshots are taken using `spectacle --dbus --background --nonotify` connected to the isolated session's D-Bus, then captured via the `org.kde.KWin.ScreenShot2` interface.

4. **Accessibility**: The AT-SPI2 accessibility bus in the isolated session is queried via PyGObject to discover widget trees, element positions, and available actions.

## License

[MIT](LICENSE)
