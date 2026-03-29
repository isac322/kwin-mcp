# Contributing to kwin-mcp

Thank you for your interest in contributing to kwin-mcp, an MCP server for Linux desktop GUI automation on KDE Plasma 6 Wayland.

## Development Setup

### Prerequisites

- Python 3.12+
- KDE Plasma 6 on Wayland (for running and testing)
- [uv](https://docs.astral.sh/uv/) package manager

### System Dependencies

Install the required system packages for your distribution:

**Arch Linux / Manjaro:**

```bash
sudo pacman -S kwin spectacle at-spi2-core python-gobject dbus-python-common

# Optional: for clipboard and Unicode input
sudo pacman -S wl-clipboard wtype wayland-utils
```

**Fedora (KDE Spin):**

```bash
sudo dnf install kwin-wayland spectacle at-spi2-core python3-gobject dbus-python

# Optional
sudo dnf install wl-clipboard wtype wayland-utils
```

**Kubuntu / KDE Neon:**

```bash
sudo apt install kwin-wayland spectacle at-spi2-core python3-gi gir1.2-atspi-2.0 python3-dbus

# Optional
sudo apt install wl-clipboard wtype wayland-utils
```

### Clone and Install

```bash
git clone https://github.com/isac322/kwin-mcp.git
cd kwin-mcp
uv sync

# Install git hooks (docs-seo reminder after source commits)
bash scripts/install_hooks.sh
```

The `install_hooks.sh` script installs a `post-commit` hook that prints a docs-seo reminder and runs `scripts/check_docs_seo.py` automatically whenever `src/kwin_mcp/*.py` or `pyproject.toml` files are committed.

### Claude Code PostToolUse Hook (optional)

If you use Claude Code for development, you can enable in-editor docs-seo reminders by adding the following hook to `.claude/settings.json` in the project root:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 scripts/check_docs_seo_hook.py",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

This causes Claude Code to print a reminder whenever it edits a source file, prompting you to run `@docs-seo` if documentation may need updating.

## Code Style

This project uses [ruff](https://docs.astral.sh/ruff/) for linting and formatting, and [ty](https://docs.astral.sh/ty/) for type checking.

```bash
uv run ruff check .       # Lint
uv run ruff format .      # Format
uv run ty check           # Type check
```

Key style rules:

- Python 3.12+ syntax (use `type` aliases, `|` unions, etc.)
- Double quotes for strings
- Line length: 100 characters
- Type hints required for all function signatures
- All code comments and docstrings in English

## Testing Changes

After modifying kwin-mcp code, verify your changes via the interactive CLI:

```bash
uv run python -m kwin_mcp.cli
```

The CLI provides the same functionality as the MCP server and allows you to test tools interactively.

### Virtual Session Testing (Isolated)

Use `session_start` to launch an isolated KWin Wayland session — safe for automated tests and CI since it does not touch your real desktop:

```
> session_start
```

### Live Session Testing

Use `session_connect` to attach to your existing KDE Plasma desktop session. This is useful when testing features that require a real desktop environment (e.g., clipboard integration, real application interaction):

```
> session_connect
```

`session_connect` defaults to the current session via `$DBUS_SESSION_BUS_ADDRESS` and `$WAYLAND_DISPLAY`. You can also pass explicit values:

```
> session_connect dbus_address=unix:path=/run/user/1000/bus wayland_display=wayland-1
```

You can also start the CLI in live-session-default mode with `--default-live-session`:

```bash
uv run python -m kwin_mcp.cli --default-live-session
```

> **Note**: Do not test via the MCP server if it was started before your code changes -- it will still be running the old code. Always use the CLI for verification.

## Project Structure

```
src/kwin_mcp/
├── core.py            # AutomationEngine — MCP-independent automation logic
├── server.py          # MCP server (thin wrappers around AutomationEngine)
├── cli.py             # Interactive REPL + pipe mode
├── session.py         # KWin session management (isolated virtual + live desktop)
├── screenshot.py      # Screenshot capture via KWin ScreenShot2 D-Bus
├── accessibility.py   # AT-SPI2 accessibility tree inspection
└── input.py           # Input injection via KWin EIS D-Bus + libei
```

## Pull Request Process

1. Fork the repository and create a feature branch
2. Make your changes following the code style guidelines above
3. Run all checks: `uv run ruff check . && uv run ruff format --check . && uv run ty check`
4. Update `CHANGELOG.md` if your change is user-facing (new tools, bug fixes, behavior changes)
5. Update `README.md` if you add new tools or change existing tool behavior
6. Open a pull request with a clear description of the changes

## Reporting Issues

- **Bug reports**: Use the [bug report template](https://github.com/isac322/kwin-mcp/issues/new?template=bug_report.md) and include your kwin-mcp version, OS, KDE Plasma version, and steps to reproduce
- **Feature requests**: Use the [feature request template](https://github.com/isac322/kwin-mcp/issues/new?template=feature_request.md) with a clear use case description

## License

By contributing to kwin-mcp, you agree that your contributions will be licensed under the [MIT License](LICENSE).
