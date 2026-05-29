# Docker Test Harness

## Overview
The kwin-mcp Docker harness provides a single-command smoke test to verify that the automation engine runs correctly on various Linux distributions. It uses isolated containers to build the project wheel, install it into a clean environment, and execute a standardized smoke test against a virtual KWin session. This ensures that the core automation logic, input injection, and accessibility inspection remain functional across different package versions and distribution configurations.

As an MCP (Model Context Protocol) server, kwin-mcp relies on complex interactions between D-Bus, KWin, and AT-SPI2. The Docker harness allows developers to validate these interactions in a controlled, reproducible environment that mimics a fresh installation. This is particularly important for catching regressions in input injection (via EIS/libei) and accessibility tree traversal, which can be sensitive to system-level library updates.

The harness leverages triple isolation to ensure test integrity:
- **D-Bus Isolation**: Each test run uses a private D-Bus session bus, preventing interference with the host's session services. This is achieved by running the entire test process inside `dbus-run-session`, which creates a temporary bus that is destroyed when the process exits.
- **Display Isolation**: KWin runs in virtual mode, rendering to a software framebuffer rather than a physical display. This allows the tests to run in headless environments without requiring a GPU or a physical monitor.
- **Input Isolation**: Input events are injected directly into the virtual compositor's EIS interface, ensuring they never leak to the host desktop. This prevents accidental clicks or keystrokes from affecting the developer's work while the tests are running.

By combining these isolation layers, the harness provides a robust and safe environment for testing complex GUI interactions. It allows for high-fidelity testing of the Model Context Protocol server without the risks associated with running automation on a live desktop.

This harness is designed for local developer verification and is not a replacement for full CI workflows. By running tests in a containerized environment, developers can catch distribution-specific regressions without needing to maintain multiple physical or virtual machines. The harness provides a high degree of isolation, ensuring that the host system remains unaffected by the test execution. It does not currently handle image publishing or automated registry management, as those tasks are deferred to future development phases.

## Quick Start
To run the smoke test for Manjaro via `scripts/test-distro.sh manjaro`, ensure you have the following prerequisites met on your host machine:

### Prerequisites
- **Docker Daemon**: The Docker service must be running and accessible on your host. You can check this by running `docker ps`.
- **uv**: The `uv` package manager must be installed on the host system to handle wheel building. It is used to create the `.whl` file that is mounted into the container.
- **Repository**: The repository must be checked out and you should be at the root directory.
- **Architecture**: The host should be either `x86_64` (amd64) or `aarch64` (arm64). The harness is designed to be multi-arch compatible.

### Execution
Execute the following command from the repository root to start the test:

```bash
scripts/test-distro.sh manjaro
```

The script will automatically build the local wheel, create a test image, and run the containerized smoke test. All logs and artifacts will be written to the evidence directory upon completion, allowing you to inspect the results.

## What it does
The test harness follows a standardized execution flow to ensure reproducibility and thoroughness across different host environments:

1. **Host Build**: The host environment builds a fresh `kwin-mcp` wheel from the current source code using `uv build`. This guarantees that the latest updates are always the ones being tested, preventing stale builds from masking issues. The wheel is placed in the `dist/` directory and mounted into the container.
2. **Image Construction**: A distribution-specific Docker image is built using the corresponding Dockerfile in the `docker/` directory. This step installs all necessary system dependencies including KWin, AT-SPI2, Python bindings, and utility tools like `wl-clipboard`. The build process also handles distribution-specific quirks, such as stripping capabilities from the KWin binary to allow it to run in a container without elevated privileges.
3. **Container Execution**: A container is launched with the wheel, smoke test scripts, and a test QML application mounted as read-only volumes. This ensures that the test environment is clean and consistent across runs, with no side effects from previous executions.
4. **Environment Setup**: The container's entrypoint script performs several critical tasks:
    - Installs the `kwin-mcp` wheel into a dedicated virtual environment (`/opt/kwinmcp-venv`).
    - Prepares the mandatory XDG runtime directory (`/run/user/1000`) with the correct permissions (0700) and ownership.
    - Sets up the D-Bus session bus, which is required for communication between KWin and the automation engine.
5. **Smoke Test Execution**: A Python script launches a virtual KWin session using `dbus-run-session` and `kwin_wayland --virtual`. It then starts the QML test application and performs a series of input and observation tasks, such as clicking buttons, typing text, and verifying the accessibility tree state.
6. **Result Capture**: Throughout the test, the process captures screenshots, accessibility tree dumps, and standard output/error logs. These are written directly to a mounted evidence directory on the host, ensuring that artifacts persist even after the container exits.
7. **Verdict**: The container exits with a status code indicating whether the smoke test assertions passed or at which stage an error occurred. For example, an exit code of 0 indicates success, while 1 indicates a smoke assertion failure, and 2 indicates an environment setup error.

## Evidence layout
All test results and artifacts are written to the host at `.sisyphus/evidence/<distro>/<timestamp>/`. This directory provides a complete record of the test run for debugging and verification. The layout includes:

- **summary.json**: Contains the final test verdict, total execution time, and high-level metadata about the test run. It includes fields for the distribution name, host architecture, and a summary of the test steps performed.
- **stdout.log**: Captured standard output from the test process. This includes detailed logs from the `AutomationEngine`, the test runner's progress messages, and any output from the test application itself.
- **stderr.log**: Captured standard error from the test process. This is the primary source for debugging session startup issues, D-Bus communication errors, or unexpected crashes in the compositor or test application.
- **screenshots/**: A directory containing PNG captures of the virtual display at various stages of the test. These are invaluable for visual verification of the UI state. Common files include:
    - `initial.png`: The state of the application immediately after launch.
    - `post-click.png`: The state after a mouse click or touch tap has been performed.
    - `post-typing.png`: The state after text has been entered into a field.
- **a11y/**: A directory containing accessibility tree dumps as formatted text strings. These allow for precise verification of the widget hierarchy, element roles, and states (e.g., "focused", "enabled"). Common files include:
    - `before.txt`: The tree state before an interaction.
    - `after.txt`: The tree state after an interaction.
- **install.json**: Metadata about the wheel installation, including the wheel filename, SHA256 hash, and versions of key packages installed in the container (e.g., `kwin`, `at-spi2-core`, `python-gobject`).

For the canonical schema and path definitions, refer to the `docker/runtime-contract.md`.

## Adding a new distro
To add support for a new Linux distribution to the harness, follow this systematic checklist:

1. **Write Dockerfile**: Create a new Dockerfile at `docker/<distro>.Dockerfile`. It must conform to the specifications in `docker/runtime-contract.md`, including the user UID/GID (1000), mount paths, and environment variables.
2. **Update Script**: Add the `<distro>` name to the `SUPPORTED` array in `scripts/test-distro.sh` to enable the host-side wrapper and argument validation.
3. **Iterate**: Run `scripts/test-distro.sh <distro>` and iterate on the Dockerfile until the smoke test passes consistently. Pay close attention to package names, as they vary between distributions.
4. **Document**: Update the "Supported distros" list in this document to include the new entry and any distribution-specific notes or base image choices.
5. **Roadmap**: Add a corresponding entry to the `ROADMAP.md` to track the distribution's support status and mark it as completed once verified.

## Supported distros
- **manjaro**: The primary test target and development environment. It uses `manjarolinux/base` as the base image to provide multi-arch support while maintaining full `pacman` and Arch-family compatibility. This ensures that the latest KDE Plasma 6 packages are available for testing, which is critical for validating the automation engine against the most recent compositor updates.

Note that support for other major distributions such as Ubuntu, Debian, Fedora, and openSUSE is planned for future milestones but is not yet implemented. These will be added as the project matures and the runtime contract is further refined to handle different init systems, package managers, and library versions. Each new distribution will require its own Dockerfile and validation cycle to ensure consistent behavior across the entire test suite.

## Architecture
The harness is designed to support both `amd64` and `arm64` architectures using a single multi-arch base image. The Dockerfile filename `docker/manjaro.Dockerfile` corresponds to the user-facing distro family slot used in the test script. This design allows for a unified testing interface regardless of the underlying hardware, simplifying the development and maintenance of the test suite.

The `FROM` instruction in the Dockerfile points to `manjarolinux/base:20260322` because the official Arch Linux image on Docker Hub is currently limited to `amd64`. Manjaro provides a compatible rolling-release environment with multi-arch support, ensuring that the harness can run on both traditional servers and ARM-based development machines. The use of date-tags for the base image ensures that builds are reproducible and not subject to unexpected breakages from upstream updates.

A key architectural requirement is the removal of file capabilities from the KWin binary. By default, `kwin_wayland` ships with `cap_sys_nice=ep` for realtime scheduling, which causes execution failures in standard container environments. The Dockerfile explicitly strips these capabilities using `setcap -r` to ensure that the compositor can launch successfully as a non-root user. Other architectures such as `armv7`, `ppc64le`, or `riscv64` are currently out of scope for this project.

## Known limitations
- **Software Rendering**: The harness relies on Mesa llvmpipe for software rendering within the container. No GPU passthrough or hardware acceleration is utilized, which may result in slower performance compared to native execution. This is a deliberate choice to ensure that the harness can run on any host without requiring specialized hardware or drivers.
- **No Elevated Privileges**: The runtime contract enforces that the container runs without elevated Docker privileges, host-device passthrough, or special kernel capability grants. This ensures that the tests run in a secure and restricted environment, mirroring the constraints of a typical user session.
- **Local Execution**: Integration with GitHub Actions is currently deferred to a follow-up plan. The harness is optimized for local developer workflows and manual verification of updates before they are committed.
- **Registry Management**: Registry publishing (e.g., `GHCR`) is currently out of scope and not supported by the current scripts. The focus remains on local image builds and execution.
- **Validated Manjaro Path**: End-to-end Manjaro harness validation passed on 2026-05-04; see `.sisyphus/evidence/manjaro/20260504T201603Z/` for the canonical evidence bundle. Continue using that evidence layout when comparing future local runs.

## Troubleshooting
If the test harness fails to execute or the smoke test does not complete, check the following common failure modes and their respective resolutions:

- **Docker Daemon**: Ensure the Docker daemon is running and accessible on your host. If you are using a remote Docker host, ensure the `DOCKER_HOST` environment variable is correctly set. You can verify the connection by running `docker info`.
- **Missing Dependencies**: Verify that `uv` is installed on the host, as it is required to build the project wheel before it can be mounted into the container. The script will fail early if the `uv` command is not found in your `PATH`.
- **Base Image Availability**: In rare cases, the pinned `manjarolinux/base:20260322` date-tag may no longer be pullable from Docker Hub due to registry garbage collection or tag rotation. If this occurs, you will see a "manifest not found" error during the image build phase. To fix this, visit the [Manjaro Docker Hub page](https://hub.docker.com/r/manjarolinux/base/tags) to find a more recent date-tag and update the `FROM` line in `docker/manjaro.Dockerfile`.
- **Session Startup Failure**: If the smoke test exits during session startup, inspect the latest evidence directory first, then compare it with the validated 2026-05-04 run at `.sisyphus/evidence/manjaro/20260504T201603Z/`. The most useful diagnostic artifact is `stderr.log`, followed by `summary.json` and the presence or absence of generated screenshots.

## Debugging
The test harness provides flags to pause execution or keep the container alive for manual inspection of the virtual environment.

### Pause at a specific step
Use `--pause-at=<step>` to halt the smoke test after a specific milestone. The container will wait until you signal it to continue.

```bash
scripts/test-distro.sh manjaro --pause-at=screenshot_initial
```

Valid steps are: `launch_app`, `screenshot_initial`, `mouse_click_ping`, `keyboard_type`, and `screenshot_post_typing`. When paused, the stdout will show `paused at <step>`. To resume, touch the `.continue` file in the active evidence directory:

```bash
touch .sisyphus/evidence/manjaro/<timestamp>/.continue
```

The test will then print `resumed from <step>` and proceed.

### Keep the container alive
Use `--keep` to prevent the container from exiting after the smoke test completes, regardless of the verdict.

```bash
scripts/test-distro.sh manjaro --keep
```

The entrypoint will print `Container kept alive` and tail `/dev/null`. This allows you to attach a shell to the running container for deep inspection of the environment state.

### Watch screenshots from the host
Since the evidence directory is mounted to the host, you can watch screenshots in real-time even when the test is paused. Use a file observer or a simple watch command to monitor the screenshots directory:

```bash
# List screenshots as they appear
watch -n 1 ls -l .sisyphus/evidence/manjaro/<timestamp>/screenshots/

# Or view them with auto-reload (requires feh)
feh --auto-reload .sisyphus/evidence/manjaro/<timestamp>/screenshots/initial.png
```

### Inspect the running KWin/qml6 stack
When a container is kept alive or paused, you can enter it to inspect the D-Bus bus, Wayland sockets, or process tree. The wrapper prints the deterministic container name (e.g., `kwin-mcp-smoke-manjaro-20260505T120000Z`).

```bash
docker exec -it <container_name> bash
```

Inside the container, use these cheat-sheet commands for inspection:

```bash
# Check process tree for KWin, qml6, and AT-SPI
pgrep -a "kwin_wayland|qml6|dbus-daemon|at-spi-bus-launcher"

# Inspect qml6 application logs
cat /tmp/kwin-mcp-screenshots-*/app_qml6_*.log

# Interrogate KWin via D-Bus
busctl --user list
qdbus org.kde.KWin /KWin org.kde.KWin.supportInformation
```

Note that certain runtime flags are restricted for security; refer to `docker/runtime-contract.md` for the full specification.

### Combining --pause-at and --keep
You can combine both flags to pause at a specific state and ensure the container remains available for inspection even after you resume and finish the test.

```bash
scripts/test-distro.sh manjaro --pause-at=mouse_click_ping --keep
```

## Terminal output
Every smoke run ends with a 4-7 line summary block printed to standard output. This summary is designed for easy consumption by CI systems and developers, providing an immediate verdict and a path to the full evidence bundle without requiring manual inspection of log files.

### Pass output
When all smoke test tasks complete successfully, the summary block follows this template:

```text
==> Smoke summary: PASS
==> Evidence: /evidence/20260505T120000Z
==> Tasks passed: 14
==> Screenshots: initial.png, post-click.png, post-typing.png
```

### Failure output
If a smoke test assertion fails, the summary includes the error type and the specific reason for the failure. The `Error type` line is omitted if the failure does not have a specific classification.

```text
==> Smoke summary: FAIL
==> Error type: assertion
==> Reason: accessibility tree text did not change
==> Evidence: /evidence/20260505T120000Z
==> Tasks passed: 11
==> Screenshots: initial.png, post-click.png
==> See: summary.json, stdout.log, stderr.log
```

### Error output
If the test environment fails to start, or if the summary file is missing or malformed, an ERROR summary is printed. This is also the fallback output for unexpected container exits.

```text
==> Smoke summary: ERROR
==> Error type: RuntimeError
==> Reason: failed to connect to session bus
==> Evidence: /evidence/20260505T120000Z
==> See: stdout.log, stderr.log
```

### Container vs host evidence path
The evidence path printed in the terminal (e.g., `/evidence/20260505T120000Z`) is the internal container path. On the host machine, this directory is mapped to `.sisyphus/evidence/<distro>/<timestamp>/` via a bind mount. For example, a Manjaro run's evidence can be found at:

```text
.sisyphus/evidence/manjaro/20260505T120000Z/
```

### Consuming summary in CI
CI pipelines can use simple grep patterns to extract the test verdict or failure reason from the job logs:

```bash
# Extract the verdict
grep -E '^==> Smoke summary:' smoke.log

# Extract the failure reason
grep '^==> Reason:' smoke.log
```
