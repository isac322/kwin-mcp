# Runtime Contract for kwin-mcp Docker Harness

This document defines the immutable cross-distro runtime contract for kwin-mcp automation containers. All distro-specific Dockerfiles (Arch, Ubuntu, Fedora, etc.) must conform to these specifications to ensure predictable behavior across the test suite.

## Mount paths

Every container invocation requires the following four mount points:

- `/wheels`: Read-only. Host directory containing the kwin-mcp wheel and its dependencies.
- `/evidence`: Read-write. Host directory where the container writes all test artifacts and logs.
- `/opt/docker/smoke_test.py`: Read-only. The Python smoke test script that drives the automation.
- `/opt/docker/smoke_app.qml`: Read-only. The QML application used for visual and accessibility verification.

## User

The container must run as a non-root user to match typical desktop environments:

- **User Name**: `kwinmcp`
- **UID**: `1000`
- **GID**: `1000`
- **Home Directory**: `/home/kwinmcp`
- **Shell**: `/bin/bash`

## Venv

To avoid polluting system Python packages and ensure a clean runtime, all kwin-mcp dependencies must reside in a virtual environment:

- **Path**: `/opt/kwinmcp-venv`
- **Ownership**: Owned by `kwinmcp`.
- **Population**: Created during the Docker build process. The entrypoint script must populate it at runtime using `uv pip install /wheels/*.whl` to ensure the latest local build is tested.

## XDG_RUNTIME_DIR

A valid `XDG_RUNTIME_DIR` is mandatory for Wayland and D-Bus communication:

- **Path**: `/run/user/1000`
- **Permissions**: `0700` (Required by the [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/basedir-spec-latest.html)).
- **Ownership**: Owned by `kwinmcp`.
- **Lifecycle**: Created by the Dockerfile during the build phase, not at runtime.

## Screen size

The virtual KWin display defaults to a standard resolution to ensure consistent UI element positioning:

- **Resolution**: 1920×1080
- **Note**: This matches the default in `src/kwin_mcp/core.py`. Do not override this unless a specific test application requires a different aspect ratio.

## Locale

Consistent character encoding is required for log parsing and Unicode input testing:

- **Variables**: `LANG=C.UTF-8`, `LC_ALL=C.UTF-8`
- **Requirement**: These must be set in the Dockerfile `ENV`. If the base image does not include the `C.UTF-8` locale, it must be generated during the build.

## Env vars

Environment variables are categorized by their source of truth:

### Dockerfile-set
These define the base environment:
- `LANG`: `C.UTF-8`
- `LC_ALL`: `C.UTF-8`
- `XDG_RUNTIME_DIR`: `/run/user/1000`
- `PATH`: `/opt/kwinmcp-venv/bin:$PATH` (Ensures the venv takes precedence)

### Entrypoint-set
Set during container startup:
- `PYTHONUNBUFFERED=1`: Ensures Python logs are flushed immediately to `stdout`/`stderr` for capture.

### kwin-mcp-managed
The following variables are managed internally by `kwin-mcp` (see `src/kwin_mcp/session.py`) and should **not** be duplicated in the Dockerfile:
- `KDE_FULL_SESSION`
- `XDG_SESSION_TYPE`
- `QT_LINUX_ACCESSIBILITY_ALWAYS_ON`
- `ATSPI_DBUS_IMPLEMENTATION`
- `KWIN_SCREENSHOT_NO_PERMISSION_CHECKS`
- `KWIN_WAYLAND_NO_PERMISSION_CHECKS`

## Test app

The primary verification tool is a lightweight QML application:

- **Name**: `smoke_app.qml`
- **Launch command**: `qml6 /opt/docker/smoke_app.qml`
- **Arch package**: None (Transitive dependency of `kwin`).
- **Accessible name table**:
  | Element | Accessible Name |
  |---------|-----------------|
  | TextField | "Smoke entry" |
  | Button | "Ping button" |
  | Label | "Status text" |
- **Accessible ID table**:
  | Element | Accessible ID |
  |---------|---------------|
  | TextField | "entry-field" |
  | Button | "ping-button" |
  | Label | "status-text" |

*Note: If `qml6` fails in a specific environment, `python-pyqt6` is the approved fallback for launching the test UI.*

## Package substitutions

The Dockerfile's `pacman -S` list deviates from T6 spec where Manjaro repos differ from Arch:

| T6 spec name | Actual installed | Reason |
|--------------|------------------|--------|
| dbus-python-common | python-dbus | Original name not in Manjaro 20260322 x86_64 repos; `python-dbus` provides Python D-Bus bindings there. |
| (transitive) | dbus (explicit) | `dbus-daemon` binary required by `dbus-run-session`. |
| (transitive via kwin) | qt6-declarative (explicit) | `qml6` launcher safety; redundant with `kwin` transitive runtime on current Manjaro packaging but defends against future repackaging. |

Removed from earlier Dockerfile drafts (T6 explicit ban):
- `base-devel`, `pkgconf` (no in-image compilation; wheel is pre-built by host)
- `python-cairo` (not required by any kept hard dependency; verified via `pacman -Si`)

## Base image decision

The harness uses a rolling-release base to match the latest KDE Plasma 6 developments.

- **Chosen base**: `manjarolinux/base:20260322`
- **Rationale**: Manjaro provides official multi-arch (linux/amd64 and linux/arm64) images on Docker Hub and uses the same `pacman` package manager as Arch Linux, ensuring compatibility with our primary development target.

### Rejected alternatives
- `archlinux:base`: Rejected because the official image is currently `amd64`-only on Docker Hub, which would require maintaining separate Dockerfiles for `arm64` support.
- `@sha256:` pinning: Rejected by project policy. We use date-tags (YYYYMMDD format) to balance human readability with predictable rebuild cycles.

## Evidence layout

All test results must be written to `/evidence/<timestamp>/` using the following structure:

- `summary.json`: Final test verdict and high-level metadata.
- `stdout.log`: Captured standard output from the test process.
- `stderr.log`: Captured standard error from the test process.
- `screenshots/`: Directory containing PNG captures (e.g., `initial.png`, `post-click.png`, `post-typing.png`).
- `a11y/`: Directory containing accessibility tree dumps as **formatted text strings** (e.g., `before.txt`, `after.txt`). Note: These are `.txt` files because `accessibility_tree()` returns a string.
- `install.json`: Metadata about the wheel installation (wheel_basename, wheel_sha256, kwin_mcp_version, package_versions, image_tag).

## Exit code semantics

The container exit code communicates the specific failure stage:

- `0`: Pass. All smoke test assertions passed.
- `1`: Smoke assertion failed (e.g., UI element not found or incorrect state).
- `2`: Environment setup failed (D-Bus, KWin, or XDG setup errors).
- `3`: Wheel installation failed.
- `≥10`: Uncaught exception in the test runner or harness.

## Forbidden flags

The following runtime flags are **permanently forbidden**. No Dockerfile, entrypoint, or wrapper script in this project may use them:

- `--privileged`
- `--cap-add=SYS_ADMIN`
- `--device=/dev/uinput`
- `--device=/dev/input`
- `--device=/dev/dri`

**Explanation**:
- KWin's virtual backend uses `QPainterCompositing` as a fallback, so `/dev/dri` is not required for rendering.
- `libei` is UNIX-socket based; `/dev/uinput` is a server-side concern handled by the host or a specialized proxy, not the test container.
- AT-SPI2 auto-activates via D-Bus; no elevated privileges or direct input device access are needed for accessibility inspection or input injection.

## Render-node passthrough policy (Waiver D)

The "Forbidden flags" list above prohibits `--device=/dev/dri` (blanket). This list intentionally targets **DRI control nodes** (`card0`, `card1`) which are root-only and control display + GPU. Render-only nodes (`renderD128`, `renderD129`) are NOT control nodes — they are world-writable by udev (perms 0666), provide DRM render context only, and are explicitly allowed conditional passthrough via the `dri_args` block in `scripts/test-distro.sh` (Waiver D, see `.sisyphus/notepads/archlinux-docker-harness/decisions.md`). KWin's ScreenShot2 D-Bus pipeline requires render-node access even with software rendering to complete within its async-call timeout.
