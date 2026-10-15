# Claude Code Working Guidelines

## Project Overview

kwin-mcp is an MCP (Model Context Protocol) server for Linux desktop GUI automation on KDE Plasma 6 Wayland.
**Purpose**: Enable AI agents (Claude Code and other MCP clients) to launch, interact with, and observe any Wayland application — either in a fully isolated virtual KWin session for reproducible GUI testing, or by connecting to a live KDE Plasma desktop (real session or container) for collaborative live desktop automation workflows.

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

Key modules in `src/kwin_mcp/`:
- `models.py`: Shared argument types and result models; one `render()` per result
- `core.py`: `AutomationEngine` — MCP-independent automation logic for all 29 tools
- `server.py`: Thin MCP wrappers delegating to `AutomationEngine`
- `cli.py`: Interactive REPL + pipe mode via `cmd.Cmd` (`kwin-mcp-cli` entry point)
- `session.py`: Virtual sessions (`dbus-run-session` + `kwin_wayland --virtual`) and live session attachment (`session_connect` to real desktop or container)
- `screenshot.py`: KWin ScreenShot2 D-Bus, Spectacle, and X11/scrot capture
- `accessibility.py`: AT-SPI2 widget queries, element refs, and actions
- `spoken_name.py`: Unicode normalization and spoken accessible-name matching
- `geometry.py`: Windows, virtual desktops, activation, and output topology via KWin scripting
- `clipboard.py`: Private Wayland data-control helper for Unicode paste
- `dbus_args.py`: `dbus_call` argument parser (`dbus-send` strings and typed JSON)
- `dbus_service_file.py`: Bounded D-Bus service-file parser and direct AT-SPI exec helper
- `input.py`: KWin EIS D-Bus + libei input injection and held-input tracking

## Verification & Sync Checks

1. After code changes, run `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check src tests`, and `uv run pytest tests/unit`.
2. **Verify with CLI, not MCP tools**: After modifying any kwin-mcp code, verify via the CLI (`uv run python -m kwin_mcp.cli`), NOT the running MCP server process (which still has old code loaded in memory). Use `retention='{"artifacts": true}'` in `session_start` to preserve `/tmp/kwin-mcp-screenshots-*` after `session_stop`, and remove it manually when done.
3. **Plugin and documentation sync**: After changing `pyproject.toml`, `src/kwin_mcp/server.py`, or `integrations/claude-code/skills/kwin-desktop-automation/SKILL.md` (the source of truth — never edit the OpenCode mirror directly), run `python3 scripts/sync_plugin_version.py` and `python3 scripts/check_docs_seo.py`.

## Release Gate

Before tagging a release, verify every kwin-mcp tool end to end on the release commit with the Docker E2E suite (`scripts/run-e2e-docker.sh`, once per `--distro` variant).
