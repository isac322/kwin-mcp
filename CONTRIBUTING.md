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
```

### Documentation Consistency Check

The project includes two consistency checkers that run in CI on pull requests:

1. **`scripts/check_docs_seo.py`** — validates SEO keywords and positioning terms across documentation files. It also verifies that the bundled plugin manifests (`.claude-plugin/marketplace.json`, `integrations/claude-code/.claude-plugin/plugin.json`, `integrations/opencode/plugin/package.json`) keep their keyword sets in sync with `.claude/positioning.yml`, and that the Claude Code source SKILL.md and the OpenCode plugin's bundled SKILL.md remain byte-identical.
2. **`scripts/sync_plugin_version.py --check`** — verifies that `pyproject.toml [project].version` matches the version recorded in every plugin manifest, and that the OpenCode plugin's bundled SKILL.md mirrors the Claude Code source. Without `--check`, the script *writes* the synced state.

Invoke locally:

```bash
python3 scripts/check_docs_seo.py
python3 scripts/sync_plugin_version.py --check
```

In Claude Code sessions, use the `/check-docs-seo` skill or the `@docs-seo` agent to evaluate and update documentation after code changes.

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

### Automated End-to-End Tests (Containerized)

`tests/e2e` drives a real virtual KWin session and runs in a reproducible container, which is what CI executes:

```bash
docker build -f docker/e2e.Dockerfile -t kwin-mcp-e2e .
docker run --rm kwin-mcp-e2e
```

See `docker/README.md` for what the suite covers (session lifecycle, AT-SPI2 queries, EIS keyboard injection) and the container's known limitations (screenshots, pointer events).

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

integrations/
├── claude-code/
│   ├── .claude-plugin/plugin.json                            # Claude Code plugin manifest
│   ├── .mcp.json                                              # MCP server config (uvx kwin-mcp)
│   └── skills/kwin-desktop-automation/SKILL.md                # source-of-truth skill
└── opencode/
    ├── opencode.json.example                                  # manual fallback opencode config
    └── plugin/                                                 # @isac322/kwin-mcp-opencode npm package
        ├── package.json                                        # npm manifest (mirrors pyproject version + keywords)
        ├── src/index.ts                                        # config-hook plugin (TypeScript)
        └── skill/kwin-desktop-automation/SKILL.md              # auto-synced from claude-code source on build

.claude-plugin/
└── marketplace.json                                            # Claude Code marketplace catalog (single-plugin)

scripts/
├── check_docs_seo.py                                           # documentation/SEO consistency checker (CI)
└── sync_plugin_version.py                                      # syncs pyproject version + SKILL.md to integrations/
```

## Pull Request Process

1. Fork the repository and create a feature branch
2. Make your changes following the code style guidelines above
3. Run all checks: `uv run ruff check . && uv run ruff format --check . && uv run ty check`
4. Update `CHANGELOG.md` if your change is user-facing (new tools, bug fixes, behavior changes)
5. Update `README.md` if you add new tools or change existing tool behavior
6. **If you bumped `pyproject.toml [project].version` or modified the MCP tool list (`src/kwin_mcp/server.py`)**: run `python3 scripts/sync_plugin_version.py` to keep `.claude-plugin/marketplace.json`, `integrations/claude-code/.claude-plugin/plugin.json`, `integrations/opencode/plugin/package.json`, and the OpenCode plugin's bundled SKILL.md in sync with the source. Verify with `python3 scripts/sync_plugin_version.py --check` (CI runs the same check).
7. **If you added/renamed/removed an MCP tool**: also update `integrations/claude-code/skills/kwin-desktop-automation/SKILL.md` (the source of truth — the OpenCode plugin auto-syncs from it during `npm run build`).
8. **If you bumped any plugin keyword set or `pyproject.toml [project].keywords`**: ensure the plugin manifests' keyword arrays still satisfy the subset rule defined in `.claude/positioning.yml § drift_detection.plugin_keywords_min_overlap` (the `check_docs_seo.py` plugin keyword check enforces this).
9. Open a pull request with a clear description of the changes

## Reporting Issues

- **Bug reports**: Use the [bug report template](https://github.com/isac322/kwin-mcp/issues/new?template=bug_report.md) and include your kwin-mcp version, OS, KDE Plasma version, and steps to reproduce
- **Feature requests**: Use the [feature request template](https://github.com/isac322/kwin-mcp/issues/new?template=feature_request.md) with a clear use case description

## License

By contributing to kwin-mcp, you agree that your contributions will be licensed under the [MIT License](LICENSE).
