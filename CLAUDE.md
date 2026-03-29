# Claude Code Working Guidelines

## Project Overview

kwin-mcp is an MCP (Model Context Protocol) server for Linux desktop GUI automation on KDE Plasma 6 Wayland.
**Purpose**: Enable AI agents (Claude Code and other MCP clients) to launch, interact with, and observe any Wayland application — either in a fully isolated virtual KWin session for reproducible GUI testing, or by connecting to a live KDE Plasma desktop (real session or container) for collaborative desktop automation workflows.

## Toolchain

- **Package Manager**: `uv` (Astral). Always use uv instead of pip/poetry.
- **Lint + Format**: `ruff` (Astral). Config in `pyproject.toml` under `[tool.ruff]`.
- **Type Check**: `ty` (Astral). Config in `pyproject.toml` under `[tool.ty]`.
- **Build**: `uv build` (uv_build backend)

### Common Commands

```bash
uv sync                   # Install/sync dependencies
uv add <pkg>              # Add dependency
uv add --dev <pkg>        # Add dev dependency
uv run ruff check .       # Lint
uv run ruff format .      # Format
uv run ty check           # Type check
uv run python -m kwin_mcp # Run server
```

## Code Style

- Python 3.12+
- ruff rules: E, F, W, I, N, UP, B, A, SIM, TCH, RUF
- Line length: 100
- Quotes: double quote
- Type hints required
- All documents (.md files), code comments, and docstrings must be written in English

## Architecture

See ROADMAP.md. Key modules:
- `core.py`: AutomationEngine — MCP-independent automation logic (session, input, screenshot, a11y)
- `server.py`: Thin MCP wrappers delegating to AutomationEngine
- `cli.py`: Interactive REPL + pipe mode via `cmd.Cmd` (`kwin-mcp-cli` entry point)
- `session.py`: Virtual sessions (dbus-run-session + kwin_wayland --virtual) and live session attachment (session_connect to real desktop or container)
- `screenshot.py`: KWin ScreenShot2 D-Bus (screenshots)
- `accessibility.py`: AT-SPI2 (widget tree)
- `input.py`: KWin EIS D-Bus + libei (input injection)

## Pre-work Checklist

1. Read `ROADMAP.md` to understand current progress
2. Start from the first incomplete item in the next milestone
3. After code changes, run `uv run ruff check .` + `uv run ruff format .` + `uv run ty check`
4. Update ROADMAP.md checklist when a milestone item is completed
5. **Verify with CLI, not MCP tools**: After modifying any kwin-mcp code, verify via the CLI (`uv run python -m kwin_mcp.cli`), NOT the MCP server tools. The MCP server process is already running with the old code loaded in memory — calling MCP tools after editing source files will NOT reflect the changes. Use the CLI to launch a session and test the modified functionality. Use `keep_screenshots=true` in `session_start` to preserve screenshot files after `session_stop` (they are deleted by default). Files in `/tmp/kwin-mcp-screenshots-*` must be cleaned up manually when this option is used.

## System Dependencies (Arch/Manjaro)

- `at-spi2-core`: AT-SPI2 accessibility framework (installed)
- `python-gobject`: GObject introspection Python bindings (installed)
- `kwin`: KWin Wayland compositor (installed)
- `spectacle`: Screenshot tool (installed, fallback)
- `selenium-webdriver-at-spi`: inputsynth binary (AUR, may need installation)

## Documentation & SEO

When writing or editing any documentation (README.md, CHANGELOG.md, GitHub release notes, pyproject.toml metadata, or repository settings), follow these SEO guidelines.

### Target Keywords

**Primary**: kwin-mcp, MCP server, GUI automation, KDE Plasma, Wayland automation, Linux desktop automation, live desktop automation
**Secondary**: headless GUI testing, AI agent desktop, accessibility tree, AT-SPI2, libei, EIS, KWin virtual session, live session, session_connect, desktop testing framework, Wayland compositor testing, MCP tools, real desktop automation, desktop collaboration, kiosk automation, embedded device automation
**Long-tail**: headless Wayland GUI testing, KDE Plasma automated testing, Linux GUI test automation CI/CD, Claude Code desktop automation, AI-driven Linux desktop testing, live KDE Plasma session automation, AI agent real desktop control, connect to live KWin session, container desktop automation, kiosk desktop automation, embedded Linux desktop testing

### README.md Rules

- H1 must be the project name; the line immediately after must be a bold description under 160 characters (acts as meta description for GitHub/search engines)
- Include a badge row (PyPI version, downloads, Python version, license, CI status)
- Use keyword-rich headings (e.g. "Available Tools", "System Requirements", "How It Works")
- Maintain the tool reference tables with tool name, parameters, and description columns
- Include copy-paste installation commands (uv, pip, from source)
- Cross-link sections with a Table of Contents
- Keep the architecture diagram (ASCII art) up to date when adding tools or modules

### CHANGELOG.md Rules

- Follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format
- Use Added / Changed / Deprecated / Removed / Fixed / Security categories
- Name specific tools (e.g. `mouse_click`, `accessibility_tree`) in entries
- Include comparison links at the bottom (`[0.x.y]: https://github.com/...`)
- Maintain an `[Unreleased]` section at the top for in-progress changes

### GitHub Release Notes Rules

- Always written in English
- **Minor+ releases** structure: opening paragraph with value proposition -> Highlights (bullet list) -> New Tools table (if applicable) -> Installation commands -> Full Changelog comparison link
- **Patch releases** structure: What's Changed -> detailed description -> Full Changelog comparison link
- First sentence must convey the value proposition (what the user gains)
- Name exact technologies: AT-SPI2, libei, EIS, KWin ScreenShot2, D-Bus, PyGObject, wl-clipboard, wtype
- Include tool counts when relevant (e.g. "30 MCP tools" or "17 new tools")

### pyproject.toml SEO Rules

- `description` must be under 120 characters and include "MCP server" + "Linux" + "GUI automation" + "KDE Plasma" + "Wayland"
- Maintain 20+ keywords covering: protocol (mcp, model-context-protocol), compositor (kwin, kde, plasma, wayland), purpose (gui-automation, desktop-automation, testing, e2e-testing, headless, headless-testing), technology (at-spi2, libei, accessibility, ai-agents), ecosystem (claude-code, linux-desktop, wayland-automation, mcp-server)
- Keep classifiers up to date (Development Status, Python versions, Topics)
- `[project.urls]` must include Homepage, Repository, Issues, Changelog, and Documentation

### GitHub Repository Decoration Rules

- **About description**: must match pyproject.toml `description` or be a shorter variant with primary keywords
- **Topics**: maintain 15-20 topics mirroring pyproject.toml keywords plus GitHub-specific tags (e.g. `hacktoberfest` when applicable)
- **Homepage URL**: link to PyPI project page (`https://pypi.org/project/kwin-mcp/`)
- **Social preview**: use a branded 1280x640 image with project name, tagline, and technology logos/icons
