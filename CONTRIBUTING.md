# Contributing to kwin-mcp

Thank you for your interest in contributing to kwin-mcp, an MCP server for Linux desktop GUI automation on KDE Plasma 6 Wayland.

## Development Setup

### Prerequisites

- Python 3.12+
- KDE Plasma 6 on Wayland (for running and testing)
- [uv](https://docs.astral.sh/uv/) package manager

### System Dependencies

Install the runtime packages and native build dependencies described in the README's [Installing System Dependencies](README.md#installing-system-dependencies) section before running `uv sync`. That section is the single maintained source; this guide does not duplicate its package lists.

The build dependencies matter for development too: `uv sync` creates an isolated `.venv` that cannot import the distribution's `gi` (PyGObject) or `dbus` Python modules, so uv builds `pygobject` and `dbus-python` from source there. Without a C compiler, `pkg-config`, Python headers, and the cairo, GObject Introspection, and D-Bus development files, that build fails.

`src/kwin_mcp/clipboard.py` loads `libwayland-client` through `ctypes` at runtime and adds no Python dependency. KWin already depends on that library, so no extra package is needed.

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

Run linting, formatting, and type checking across both application and test code:

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check src tests
```

Key style rules:

- Python 3.12+ syntax (use `type` aliases, `|` unions, etc.)
- Double quotes for strings
- Line length: 100 characters
- Type hints required for all function signatures
- All code comments and docstrings in English

## Testing Changes

After modifying kwin-mcp code, use the interactive CLI for focused manual checks:

```bash
uv run python -m kwin_mcp.cli
```

The CLI delegates to the same `AutomationEngine` as the MCP server and lets you exercise tools
without relying on an MCP process that may have loaded older source code.

### Virtual Session Testing (Isolated)

Use `session_start` to launch an isolated KWin Wayland session that does not touch the host
desktop:

```
> session_start
```

### Live Session Testing

Use `session_connect` to attach to an existing KDE Plasma desktop when a change needs a real
session, clipboard integration, or interaction with an already running application:

```
> session_connect
```

`session_connect` defaults to `$DBUS_SESSION_BUS_ADDRESS` and `$WAYLAND_DISPLAY`. You can also
pass explicit values:

```
> session_connect dbus_address=unix:path=/run/user/1000/bus wayland_display=wayland-1
```

To make live-session mode the CLI default:

```bash
uv run python -m kwin_mcp.cli --default-live-session
```

> **Note**: Do not test through an MCP server process started before your code changes. That
> process still has the old code loaded. Use the CLI or the Docker E2E command below.

### Complete Docker End-to-End Proof

Before opening a pull request with runtime changes, run the installed-package Docker suite from
the repository root:

```bash
scripts/run-e2e-docker.sh
```

Pass pytest selectors after `--` when you need a focused run:

```bash
scripts/run-e2e-docker.sh -- -k screenshot -v
```

The full command builds `docker/e2e.Dockerfile`, builds a wheel from the checkout, and installs
that wheel into the runtime virtual environment with standard `Requires-Dist` resolution:
PyGObject, pycairo, and dbus-python compile from source in a builder-only stage, while `mcp`,
Pillow, and the remaining dependencies resolve fresh from PyPI within the declared ranges. The
dev dependency group is installed separately from `pyproject.toml`; `uv.lock` is not used inside
the image. The image pins both its Debian base-image digest and its dated Debian package
snapshot.

The suite collects every test under `tests/e2e`. Together they prove:

- engine-level behavior in isolated virtual KWin sessions;
- the installed `kwin-mcp` entry point over real MCP stdio JSON-RPC, including the exact schemas
  and wrappers for all 33 tools;
- AT-SPI2 observation and KWin EIS keyboard, pointer, touch, clipboard, window, and D-Bus paths;
- GUI pixels, cursor capture, repaint changes, and frame bursts in test-owned nested Xvfb/KWin
  sessions using the explicitly selected X11 `scrot` capture mode;
- screenshot coordinate mapping at output scales 1.0 and 1.45: pixels read from a screenshot,
  offset by its reported origin, must click the intended widget;
- lifecycle, error propagation, input-state reset, process/session teardown, temporary artifact
  retention rules, and container cleanup;
- failing-compositor lifecycle regressions driven by `PATH` stubs for `kwin_wayland` and
  `dbus-run-session`: startup must stay deadline-bounded, report the captured session stderr and
  partial stdout, and leave the owned process group fully reaped — including when the session
  leader was already reaped or a descendant ignores `SIGTERM`.
- accessibility-bus startup: `org.a11y.Bus` must have an owner as soon as `session_start`
  returns, and a failed activation (a `dbus-send` stub on `PATH`) must reach the caller as a
  `Warning:` line.

The image needs no `--privileged`, GPU, `/dev/dri`, or other device flags. Engine tests use
KWin's exact virtual backend with llvmpipe. Visual tests start Xvfb and a nested KWin compositor
inside the same container, then connect the installed MCP server to that session. One legacy
exact-virtual ScreenShot2 success test remains intentionally skipped because that backend does
not return captures. Exact-virtual failure behavior is covered, while screenshot success is
proved through the nested X11 `scrot` mode. Multi-output layouts, negative origins, ScreenShot2
`CaptureWorkspace` normalization, and Spectacle partial coverage are not exercised in the
container.

The runner always removes its named container, including after a failure or handled signal, and
prints the retained artifact directory:

```text
E2E artifacts: .../artifacts/e2e/<timestamp>-<pid>
```

Inspect that directory before reporting a failure or claiming the full loop passed:

```bash
artifact_dir=artifacts/e2e/<timestamp>-<pid>
cat "$artifact_dir/environment.json"
cat "$artifact_dir/pytest.log"
find "$artifact_dir" -maxdepth 2 -type f -print
```

`environment.json` records allowlisted architecture, container, Python, KWin, distribution, and
Debian package provenance. `junit.xml` contains the machine-readable test result. Nested
`visual-kwin-*` directories retain PNG oracles plus Xvfb, KWin, MCP server, and application logs.
On failure, the runner also records Docker inspection, container log, and process-list
diagnostics.

CI runs the same installed-package architecture natively on `ubuntu-24.04` amd64 and
`ubuntu-24.04-arm` arm64 runners and uploads the artifact directory for each architecture.

See `docker/README.md` for backend details and test-by-test coverage.

## Project Structure

```
src/kwin_mcp/
├── core.py            # AutomationEngine — MCP-independent automation logic
├── server.py          # MCP server (thin wrappers around AutomationEngine)
├── cli.py             # Interactive REPL + pipe mode
├── session.py         # KWin session management (isolated virtual + live desktop)
├── screenshot.py      # ScreenShot2, Spectacle, and X11/scrot capture normalized to logical pixels
├── accessibility.py   # AT-SPI2 accessibility tree inspection
├── geometry.py        # Window geometry, activation, and output topology via KWin scripting
├── clipboard.py       # Private Wayland data-control helper for keyboard_type_unicode paste
├── dbus_args.py       # dbus_call argument parser (dbus-send strings and typed JSON)
└── input.py           # Input injection via KWin EIS D-Bus + libei

docker/
├── e2e.Dockerfile     # Reproducible installed-package E2E image
├── e2e-entrypoint.sh  # Records environment evidence, then executes the test command
├── e2e-environment.py # Writes allowlisted environment.json provenance
└── README.md          # Container backends, coverage, and limitations

scripts/
├── run-e2e-docker.sh      # Recommended local Docker E2E runner and artifact collector
├── check_docs_seo.py      # Documentation/SEO consistency checker
└── sync_plugin_version.py # Syncs project version and SKILL.md into integrations

tests/e2e/
├── _asserts.py                         # Shared observable-result assertions
├── conftest.py                         # Engine fixtures and guaranteed teardown
├── mcp_harness.py                      # Installed MCP stdio client harness
├── visual_harness.py                   # Nested Xvfb/KWin lifecycle and logs
├── session_harness.py                  # Test-owned live KWin for live-session and helper tests
├── gui_probe.py                        # Deterministic visual oracle application
├── counted_gui_probe.py                # GUI probe with a persistent click counter
├── interaction_probe.py                # Input and screenshot probe application
├── test_installed_package.py           # Wheel, dependencies, and entry points
├── test_environment_evidence.py        # Safe environment.json provenance
├── test_mcp_protocol.py                # All 33 tool schemas and protocol errors
├── test_mcp_session_observation.py     # Installed stdio session and observation flow
├── test_mcp_pointer_keyboard.py        # Installed stdio pointer and keyboard flow
├── test_mcp_touch_clipboard.py         # Installed stdio touch and clipboard flow
├── test_screenshot_behavior.py         # Capture backends, frames, errors, and cleanup
├── test_visual_qa.py                   # Pixel-backed nested compositor oracles
├── test_interaction_probe.py           # Full probe interaction feedback loop
├── test_input_cleanup.py               # Input state reset across sessions and failures
├── test_session_lifecycle.py           # Ownership, teardown, retention, and bounded-startup cleanup
├── test_terminal_unicode_paste.py      # Unicode paste chord and next-key integrity in Konsole
├── test_unicode_clipboard_lifecycle.py # Unicode paste clipboard snapshot, restore, and cleanup
├── test_clipboard_helper_protocol.py   # Private clipboard helper framing, timeouts, and restore
├── test_virtual_session_smoke.py       # Minimum isolated KWin contract
├── test_observation_tools.py           # Accessibility, window, log, and Wayland tools
├── test_atspi_worker.py                # AT-SPI worker reuse across session changes and crashes
├── test_window_geometry.py             # Global window geometry and element coordinate space
├── test_window_close.py                # Window ids, active window, and closing one window by id
├── test_window_control.py              # Focus, scroll, drag, and touch behavior
├── test_pointer_reconnect.py           # Clicks reach a window mapped under a parked pointer
├── test_dbus_args.py                   # dbus_call argument parser (no session needed)
├── test_dbus_call.py                   # dbus_call arguments as received by a test D-Bus service
├── test_lazy_libei.py                  # Import without libei and the missing-libei input backend path
└── test_input_injection.py             # Engine-level EIS input behavior

integrations/
├── claude-code/
│   ├── .claude-plugin/plugin.json                            # Claude Code plugin manifest
│   ├── .mcp.json                                              # MCP server config (uvx kwin-mcp)
│   └── skills/kwin-desktop-automation/SKILL.md                # source-of-truth skill
└── opencode/
    ├── opencode.json.example                                  # manual fallback opencode config
    └── plugin/                                                 # @isac322/kwin-mcp-opencode npm package
        ├── package.json                                        # npm manifest
        ├── src/index.ts                                        # config-hook plugin
        └── skill/kwin-desktop-automation/SKILL.md              # generated skill mirror

.claude-plugin/
└── marketplace.json                                            # Claude Code marketplace catalog
```

## Pull Request Process

1. Fork the repository and create a feature branch
2. Make your changes following the code style guidelines above
3. Run all static checks, including test code: `uv run ruff check . && uv run ruff format --check . && uv run ty check src tests`
4. For runtime changes, run `scripts/run-e2e-docker.sh` and inspect the printed artifact directory
5. Update `CHANGELOG.md` if your change is user-facing (new tools, bug fixes, behavior changes)
6. Update `README.md` if you add new tools or change existing tool behavior
7. **If you bumped `pyproject.toml [project].version` or modified the MCP tool list (`src/kwin_mcp/server.py`)**: run `python3 scripts/sync_plugin_version.py` to keep `.claude-plugin/marketplace.json`, `integrations/claude-code/.claude-plugin/plugin.json`, `integrations/opencode/plugin/package.json`, and the OpenCode plugin's bundled SKILL.md in sync with the source. Verify with `python3 scripts/sync_plugin_version.py --check` (CI runs the same check).
8. **If you added/renamed/removed an MCP tool**: also update `integrations/claude-code/skills/kwin-desktop-automation/SKILL.md` (the source of truth — the OpenCode plugin auto-syncs from it during `npm run build`).
9. **If you bumped any plugin keyword set or `pyproject.toml [project].keywords`**: ensure the plugin manifests' keyword arrays still satisfy the subset rule defined in `.claude/positioning.yml § drift_detection.plugin_keywords_min_overlap` (the `check_docs_seo.py` plugin keyword check enforces this).
10. Open a pull request with a clear description of the changes

## Reporting Issues

- **Bug reports**: Use the [bug report template](https://github.com/isac322/kwin-mcp/issues/new?template=bug_report.md) and include your kwin-mcp version, OS, KDE Plasma version, and steps to reproduce
- **Feature requests**: Use the [feature request template](https://github.com/isac322/kwin-mcp/issues/new?template=feature_request.md) with a clear use case description

## License

By contributing to kwin-mcp, you agree that your contributions will be licensed under the [MIT License](LICENSE).
