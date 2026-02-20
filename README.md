# kwin-mcp

MCP server for GUI automation on KDE Plasma 6 Wayland.

Enables Claude Code to autonomously launch, interact with, and observe GUI applications in an isolated KWin Wayland session — without affecting the user's desktop.

## Features

- **Isolated environment**: `dbus-run-session` + `kwin_wayland --virtual` (no host interference)
- **Screenshots**: KWin ScreenShot2 D-Bus capture
- **Accessibility tree**: AT-SPI2 widget discovery with coordinates
- **Input injection**: KDE fake-input Wayland protocol (mouse, keyboard, scroll, drag)

## Requirements

- KDE Plasma 6.x (Wayland)
- Python 3.12+
- System packages: `at-spi2-core`, `python-gobject`, `kwin`

## Installation

```bash
uv sync
```

## Usage

See [ROADMAP.md](ROADMAP.md) for current development status.
