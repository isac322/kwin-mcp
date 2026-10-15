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

1. **`scripts/check_docs_seo.py`** — validates SEO keywords, canonical tool counts (29), and plugin manifest keyword alignment across `README.md`, `CLAUDE.md`, `ROADMAP.md`, `CONTRIBUTING.md`, `pyproject.toml`, and `integrations/*`, and verifies that the Claude Code source `SKILL.md` and the OpenCode plugin's bundled `SKILL.md` remain byte-identical.
2. **`scripts/sync_plugin_version.py --check`** — verifies that `pyproject.toml [project].version` matches the version recorded in every plugin manifest, and that the OpenCode plugin's bundled `SKILL.md` mirrors the Claude Code source. Without `--check`, the script *writes* the synced state.

Invoke locally:

```bash
python3 scripts/check_docs_seo.py
python3 scripts/sync_plugin_version.py --check
```

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

The CLI delegates to the same `AutomationEngine` as the MCP server, exposes the same 29 tools as
commands, and prints the same text the server returns, so you can exercise tools without relying
on an MCP process that may have loaded older source code. Object, list and union parameters take
JSON, for example `pointer_click target='{"x": 100, "y": 200}'`.

### Unit Tests (No KWin)

The KWin-free tests run on any host and gate every pull request:

```bash
uv run pytest tests/unit
```

They check the 29 tool schemas against the design (names, parameters, annotations, output
models), the removed-tool and unknown-argument errors, the text renderers, held-input rules,
selector matching, shard-status aggregation behavior, and the documentation gates: the README
tool tables, the CHANGELOG migration table, and the repository-wide scan that keeps removed tool
names out of current docs. Mark a test that covers a QA item with `@pytest.mark.qa("QA-...")`.

### Virtual Session Testing (Isolated)

Use `session_start` to launch an isolated KWin Wayland session that does not touch the host
desktop:

```
> session_start
> app_launch command=kcalc
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

To run the same suite on another distribution, pass `--distro fedora`, `--distro opensuse`,
or `--distro archlinux`. These build `docker/e2e-fedora.Dockerfile` (Fedora 44),
`docker/e2e-opensuse.Dockerfile` (openSUSE Tumbleweed), and `docker/e2e-arch.Dockerfile`
(the official `archlinux:base` image). They are variants with the same stages, virtual environment,
user, and test command; only the distro package layer differs. Use them when a change touches
distro-sensitive behavior such as libei, KWin, Qt, or packaged tool versions:

```bash
scripts/run-e2e-docker.sh --distro fedora
scripts/run-e2e-docker.sh --distro opensuse
scripts/run-e2e-docker.sh --distro archlinux
```

The suite collects every test under `tests/e2e`. Together they prove:

- engine-level behavior in isolated virtual KWin sessions;
- the installed `kwin-mcp` entry point over real MCP stdio JSON-RPC, including the exact input and
  output schemas, structured results, resources, and live-consent elicitation of all 29 tools;
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
- server and CLI exit cleanup driven through the real installed processes: stdin EOF,
  `SIGTERM`, `SIGHUP`, and `SIGINT` must stop the session exactly as `session_stop`
  would (live-session KWin and pre-existing apps untouched), a tool outliving the exit
  drain must not delay the exit, nor may an undrained stdout or a full stderr pipe, and a
  signal arriving mid-teardown must not interrupt the launched-app kill loop;
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

CI runs the same installed-package architecture natively. The Debian image and the Fedora 44 and
openSUSE Tumbleweed variants run on both `ubuntu-26.04` amd64 and `ubuntu-26.04-arm` arm64
runners; the Arch Linux variant runs on amd64 only. CI builds one image for each of the seven
distro-and-architecture rows, uploads a compressed image artifact within the same workflow run,
and shares that artifact with the row's five test shards. The 35 shards feed seven independent
required checks, one per row; each check reads only its own five shard results and collection
counts. Shard execution keeps the `not slow` selection, strict skip handling, layer-shell
requirement, and existing timeouts. Image artifacts are retained for one day; diagnostic and status
artifacts keep the repository's default retention. Artifact names include the workflow attempt.
shard statuses; an individual job rerun does not reuse artifacts from another attempt. The local
`scripts/run-e2e-docker.sh` interface is unchanged.

See `docker/README.md` for backend details and test-by-test coverage.

## Project Structure

```
src/kwin_mcp/
├── models.py          # Shared argument types and result models; one render() per result (server text == CLI output)
├── core.py            # AutomationEngine: the 29 tool methods, MCP-independent, taking and returning models.py types
├── server.py          # MCP server: tool registration, schema post-processing, gates, consent, resources, flags
├── cli.py             # Interactive REPL + pipe mode exposing the same 29 commands
├── session.py         # KWin session management (isolated virtual + live) and the owned-process registry for exit cleanup
├── dbus_service_file.py # Bounded D-Bus service-file parser and isolated direct AT-SPI exec helper
├── screenshot.py      # ScreenShot2, Spectacle, and X11/scrot capture normalized to logical pixels
├── accessibility.py   # AT-SPI2 worker: element queries, refs, and actions
├── spoken_name.py     # Deterministic Unicode normalization and spoken accessible-name matching
├── geometry.py        # Windows, virtual desktops, activation, and output topology via KWin scripting
├── clipboard.py       # Private Wayland data-control helper for keyboard_type(method="unicode") paste
├── dbus_args.py       # dbus_call argument parser (dbus-send strings and typed JSON)
├── progress.py        # Per-call progress and screenshot-image scope (MCP-free; no-op in the CLI)
└── input.py           # Input injection via KWin EIS D-Bus + libei, and the held-input state

docker/
├── e2e.Dockerfile          # Reproducible installed-package E2E image (Debian trixie)
├── e2e-arch.Dockerfile     # Arch Linux (archlinux:base) variant of the same E2E image
├── e2e-fedora.Dockerfile   # Fedora 44 variant of the E2E image
├── e2e-opensuse.Dockerfile # openSUSE Tumbleweed variant of the E2E image
├── e2e-entrypoint.sh       # Records environment evidence, then executes the test command
├── e2e-environment.py      # Writes allowlisted environment.json provenance
└── README.md               # Container backends, coverage, test-file inventory, and limitations

scripts/
├── run-e2e-docker.sh        # Recommended local Docker E2E runner and artifact collector
├── check_e2e_shards.py      # Validates one row's five shard results and collection counts
├── check_docs_seo.py        # Documentation/SEO consistency checker (tool count, keywords, SKILL mirror)
├── check_wheel.py           # Built-wheel gate: file list, version, classifier, and description
├── sync_plugin_version.py   # Syncs project version and SKILL.md into integrations
└── update_e2e_durations.py  # Refreshes tests/e2e/durations.json used for E2E sharding

tests/
├── conftest.py         # Suite policy: markers (qa, slow, visual), strict skips, sharding
├── tool_surface.py     # The 29 tool names and the 28 removed names, written out independently of the server
├── unit/               # KWin-free schemas, renderers, held input, selectors, CLI, and docs gates
│   ├── test_spoken_name.py       # Spoken-name Unicode, whitespace, punctuation, and word-boundary rules
│   ├── test_dbus_service_file.py # Service-file parsing and argv parity with dbus-daemon activation
│   └── test_e2e_shards.py        # Row-local E2E shard aggregation behavior
└── e2e/                # Installed-package Docker tests: per-tool, cross-cutting, and MCP stdio
    ├── test_atspi_service_acquisition.py # Bounded service-file acquisition, descriptor guards, preflight
    ├── test_atspi_exec_security.py       # Literal argv execution; no implicit PATH or shell fallback
    ├── test_session_atspi_direct_start.py # Explicit opt-in, activation denial, rollback, and XDG roots
    └── transient_probe.py               # GTK parent, child, and nested transient-window test fixture
                        #   Harnesses: mcp_harness.py, session_harness.py, visual_harness.py
                        #   Oracles: kwin_oracle.py, atspi_oracle.py; other per-tool files omitted here

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

Two explicit session entry points (`session_start` and `session_connect`) are used without a `session_list` discovery tool because cross-bus D-Bus discovery is not possible: each virtual KWin runs on its own isolated `dbus-run-session` bus with no central registry, while a live KWin registers `org.kde.KWin` on `$DBUS_SESSION_BUS_ADDRESS`.

The exit path is part of `session.py`'s `OwnedProcessRegistry` contract and is worth reading before changing session teardown: the serialized `session_stop` on the tool thread is the primary cleanup when the server exits, and the registry's direct process-group termination and temp-dir removal is the fallback when an in-flight tool still holds the thread after the 2 s drain. The registry signals a group only while its leader is still an unreaped child of the process, so a live-session KWin and pre-existing apps are never reached; `kwin-mcp-cli` reuses the same registry to sweep owned groups that were never attached to a session on exit.

## Pull Request Process

1. Fork the repository and create a feature branch
2. Make your changes following the code style guidelines above
3. Run all static checks, including test code: `uv run ruff check . && uv run ruff format --check . && uv run ty check src tests`
4. For runtime changes, run `scripts/run-e2e-docker.sh` and inspect the printed artifact directory
5. Update `CHANGELOG.md` if your change is user-facing (new tools, bug fixes, behavior changes)
6. Update `README.md` if you add new tools or change existing tool behavior
7. **If you bumped `pyproject.toml [project].version` or modified the MCP tool list (`src/kwin_mcp/server.py`)**: run `python3 scripts/sync_plugin_version.py` to keep `.claude-plugin/marketplace.json`, `integrations/claude-code/.claude-plugin/plugin.json`, `integrations/opencode/plugin/package.json`, and the OpenCode plugin's bundled SKILL.md in sync with the source. Verify with `python3 scripts/sync_plugin_version.py --check` (CI runs the same check).
8. **If you added/renamed/removed an MCP tool**: also update `integrations/claude-code/skills/kwin-desktop-automation/SKILL.md` (the source of truth — the OpenCode plugin auto-syncs from it during `npm run build`).
9. **If you changed `pyproject.toml [project].keywords` or a plugin manifest's `keywords`**: run `python3 scripts/check_docs_seo.py` to verify keyword alignment across manifests.
10. Open a pull request with a clear description of the changes

## Reporting Issues

- **Bug reports**: Use the [bug report template](https://github.com/isac322/kwin-mcp/issues/new?template=bug_report.md) and include your kwin-mcp version, OS, KDE Plasma version, and steps to reproduce
- **Feature requests**: Use the [feature request template](https://github.com/isac322/kwin-mcp/issues/new?template=feature_request.md) with a clear use case description

## License

By contributing to kwin-mcp, you agree that your contributions will be licensed under the [MIT License](LICENSE).
