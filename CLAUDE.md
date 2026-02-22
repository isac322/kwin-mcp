# Claude Code Working Guidelines

## Project Overview

kwin-mcp is an MCP (Model Context Protocol) server for GUI automation in KDE Plasma 6 Wayland environments.
**Purpose**: Implement a feedback loop where Claude Code autonomously launches/manipulates/observes the GUI of KDE Plasma apps.

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
- `session.py`: dbus-run-session + kwin_wayland --virtual (isolated environment)
- `screenshot.py`: KWin ScreenShot2 D-Bus (screenshots)
- `accessibility.py`: AT-SPI2 (widget tree)
- `input.py`: inputsynth / fake-input (input injection)
- `server.py`: MCP server (tool registration)

## Pre-work Checklist

1. Read `ROADMAP.md` to understand current progress
2. Start from the first incomplete item in the next milestone
3. After code changes, run `uv run ruff check .` + `uv run ruff format .` + `uv run ty check`
4. Update ROADMAP.md checklist when a milestone item is completed

## System Dependencies (Arch/Manjaro)

- `at-spi2-core`: AT-SPI2 accessibility framework (installed)
- `python-gobject`: GObject introspection Python bindings (installed)
- `kwin`: KWin Wayland compositor (installed)
- `spectacle`: Screenshot tool (installed, fallback)
- `selenium-webdriver-at-spi`: inputsynth binary (AUR, may need installation)
