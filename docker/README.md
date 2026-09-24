# Containerized KWin session for end-to-end tests

`e2e.Dockerfile` builds the installed-package end-to-end environment used locally and in `.github/workflows/e2e.yml`. The current suite collects 99 tests across engine-level virtual sessions, a real installed MCP server over stdio, and nested visual pixel QA.

## Reproducibility

The image fixes the inputs that define the test environment:

- the build helper is `ghcr.io/astral-sh/uv:0.10.8@sha256:88234bc9e09c2b2f6d176a3daf411419eb0370d450a08129257410de9cfafd2a`;
- the runtime base is `debian:trixie-slim@sha256:d7e12182ce18b85b93007c1dedf31f2d29e01ccf3182cc4017c709b6259bc132`;
- APT reads Debian trixie, trixie-updates, and trixie-security from snapshot `20260913T000000Z`;
- the project is built as a wheel and installed into `/opt/kwin-mcp-venv` with standard `Requires-Dist` resolution. Tests import that installed distribution and launch its `kwin-mcp` and `kwin-mcp-cli` console entry points.

All Python dependencies — `mcp` within the declared 1.x range, PyGObject, pycairo, dbus-python, Pillow, and their transitives — resolve fresh from the package index at image-build time, so Python dependency versions vary within the declared ranges between builds. PyGObject, pycairo, and dbus-python compile from source in the isolated `venv-builder` stage; the runtime image adds only the `libgirepository-2.0-0` shared library they need and carries no compiler or development headers. The virtual environment keeps system site packages so distro-only modules such as NumPy stay importable, and the dev dependency group is installed separately from `pyproject.toml`; `uv.lock` is not used inside the image. `environment.json` records the architecture, base digest, snapshot, Python and KWin versions, installed Debian package versions, and installed `kwin-mcp` and `mcp` distribution metadata for each run, so the resolved Python versions remain part of the retained provenance.

## Process topology

```text
scripts/run-e2e-docker.sh
  |-- docker build
  |     |-- build the kwin-mcp wheel
  |     +-- install wheel with resolved dependencies in /opt/kwin-mcp-venv
  |
  +-- docker run (tester, UID 1000)
        |-- e2e-entrypoint.sh
        |     +-- write /artifacts/environment.json
        |
        +-- pytest tests/e2e
              |-- engine-level virtual tests
              |     +-- AutomationEngine
              |           +-- dbus-run-session
              |                 |-- AT-SPI2 bus
              |                 +-- kwin_wayland --virtual
              |                       +-- KCalc, KWrite, and test probes
              |
              |-- installed MCP stdio tests
              |     +-- MCP ClientSession <-> installed `kwin-mcp`
              |           +-- AutomationEngine and the virtual or connected session
              |
              +-- visual tests
                    |-- Xvfb inside the container
                    +-- dbus-run-session
                          |-- AT-SPI2 bus
                          +-- nested kwin_wayland --x11-display ...
                                |-- Wayland applications
                                +-- installed `kwin-mcp` via session_connect
                                      +-- explicit X11/scrot pixel capture
```

The virtual and nested compositors use Mesa llvmpipe software rendering. The container requires no `--privileged`, `--cap-add`, `/dev/dri`, GPU, or other device flags. Xvfb is installed and started inside the container only for nested visual tests; the Docker host does not need an X server.

## Commands

Run commands from the repository root. The local runner is the recommended interface because it creates a writable artifact directory, names and removes the container, and collects diagnostics after failures.

Full suite:

```bash
scripts/run-e2e-docker.sh
```

One test file:

```bash
scripts/run-e2e-docker.sh -- tests/e2e/test_visual_qa.py -v
```

A pytest selection:

```bash
scripts/run-e2e-docker.sh -- -k "mcp and not screenshot" -v
```

A custom image tag:

```bash
scripts/run-e2e-docker.sh --image-tag kwin-mcp-e2e-local
```

To invoke Docker directly:

```bash
artifact_dir="$PWD/artifacts/e2e/manual"
mkdir -p "$artifact_dir"
chmod 0777 "$artifact_dir"
docker build --file docker/e2e.Dockerfile --tag kwin-mcp-e2e .
docker run --rm \
  --volume "$artifact_dir:/artifacts" \
  kwin-mcp-e2e \
  /opt/kwin-mcp-venv/bin/python -m pytest tests/e2e -v \
  --junitxml=/artifacts/junit.xml
```

Additional arguments after the image command can select a file, node ID, marker, or `-k` expression.

## Test-file inventory

| Test file | Coverage |
|---|---|
| `test_environment_evidence.py` | Allowlisted `environment.json` provenance, installed versions, and atomic replacement. |
| `test_input_cleanup.py` | Held EIS modifier/button cleanup across fresh connections, invalid input errors, and installed-server survival. |
| `test_input_injection.py` | Engine-level mouse aim/click/press/release, keyboard input and modifiers, Unicode, clipboard paste, and touch tap with observable KCalc/KWrite results. |
| `test_installed_package.py` | Runtime-venv imports, distribution ownership, system Python dependencies, installed console entry points, and CLI help. |
| `test_interaction_probe.py` | Installed stdio coverage for material click, scroll, drag, keyboard, touch hold, pinch, and multi-swipe options with retained state and image evidence. |
| `test_mcp_pointer_keyboard.py` | Pointer and keyboard wrappers crossing the installed MCP stdio transport and changing application state. |
| `test_mcp_protocol.py` | Initialization, exact names and JSON input schemas for all 31 tools, invalid argument rejection, tool-error conversion, and server survival. |
| `test_mcp_session_observation.py` | Installed stdio session, app launch, accessibility, windows, geometry, logs, Wayland, D-Bus, focus, polling, lifecycle, and virtual screenshot-error paths. |
| `test_mcp_touch_clipboard.py` | Clipboard and touch wrappers over installed stdio, including observable GUI changes and compositor gesture limits. |
| `test_observation_tools.py` | Accessibility filters and depth, element queries and states, polling, multi-window focus, app logs, Wayland protocol filtering, and generic D-Bus calls. |
| `test_screenshot_behavior.py` | Explicit nested X11/scrot capture, cursor pixels, action frame paths, screenshot retention, exact-virtual backend errors, and server survival. |
| `test_session_lifecycle.py` | Start/stop idempotence, environment and geometry, isolated HOME, artifact retention, socket/process cleanup, live-session ownership, and connection errors. |
| `test_virtual_session_smoke.py` | Minimum virtual KWin contract: KCalc launch, AT-SPI2 visibility and widgets, EIS keyboard delivery, plus the intentionally skipped exact-virtual ScreenShot2 success probe. |
| `test_visual_qa.py` | Pixel-backed GUI probe and KCalc oracles: hover repaint, cursor localization, animation bursts, CJK-versus-tofu rendering, and binary-value transitions. |
| `test_window_control.py` | Focus, smooth/discrete scroll, drag selection, touch swipe/multi-swipe/pinch delivery, and scrollbar values. |
| `test_window_geometry.py` | Global client/frame geometry, centered placement, surface-local accessibility offsets, and unknown-window behavior. |

Together, the installed MCP files exercise every server wrapper over a real MCP 1.x stdio client/server connection. The engine-level files retain direct coverage of lower-level behavior and cleanup.

## Visual pixel oracles

Visual tests do not infer success from process exit alone. They preserve PNGs and compare material pixel differences with accessibility state:

- KCalc's displayed binary value changes from the initial state to `111`, then to `1000110`, with corresponding screenshot transitions;
- moving the pointer over the GUI probe changes both its AT-SPI2 status and the hover target's pixels;
- captures with and without `include_cursor` must differ near the requested pointer coordinates rather than elsewhere on the frame;
- `screenshot_after_ms` returns distinct animation frames for the requested delays;
- the CJK sample `GUI 검증 42` must render with materially different pixels and color content from the same-sized tofu control `□□`.

The screenshot behavior tests also drive frame-producing mouse, keyboard, touch, and drag wrappers through installed MCP stdio.

## Evidence layout

The local runner creates one directory per invocation:

```text
artifacts/e2e/<UTC-timestamp>-<pid>/
  environment.json
  junit.xml
  pytest.log
  quality.log
  visual-kwin-*/
    capture-backend.txt
    dbus-address.txt
    xvfb.stdout.log
    xvfb.stderr.log
    kwin.stdout.log
    kwin.stderr.log
    mcp-server.stderr.log
    *.png
    additional probe logs and text evidence
```

When the run fails, the runner also writes:

```text
docker-inspect.json
docker-inspect.stderr
docker.log
docker-processes.txt
```

CI stores the same evidence under `artifacts/e2e/amd64/` or `artifacts/e2e/arm64/` and uploads it as `e2e-amd64` or `e2e-arm64`. The native runner matrix is:

| Architecture | Runner |
|---|---|
| `amd64` | `ubuntu-24.04` |
| `arm64` | `ubuntu-24.04-arm` |

## Screenshot backends and exact-virtual limitation

ScreenShot2 and Spectacle remain the normal Wayland screenshot backends. The nested visual fixture explicitly sets `KWIN_MCP_X11_SCREENSHOT=1` and supplies its Xvfb `DISPLAY`, selecting the test-only X11/scrot path so pixel assertions do not depend on KWin's exact virtual screenshot behavior.

KWin's `kwin_wayland --virtual` backend in this image does not return ScreenShot2 capture data. The legacy `test_screenshot_captures_the_session` success probe therefore remains skipped in normal runs; setting `KWIN_MCP_E2E_SCREENSHOT=1` only re-enables that exact-backend probe and is not the supported visual-test path. The suite still verifies that ScreenShot2/Spectacle failures become actionable tool errors without killing the engine or installed MCP server.

Screenshot success is covered through the nested KWin/Xvfb fixture with explicit scrot selection, including cursor pixels, action frame bursts, retention, and image oracles. This separates the known exact-virtual ScreenShot2 limitation from screenshot behavior that the suite can verify deterministically.
