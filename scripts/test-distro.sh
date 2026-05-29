#!/usr/bin/env bash
# scripts/test-distro.sh — Host wrapper for kwin-mcp Docker smoke harness.
#
# Usage: scripts/test-distro.sh <distro>
#   <distro>  One of: manjaro (more distros coming; add Dockerfile + SUPPORTED entry)
#
# Flow: uv build --wheel → docker pull/build → docker run → exit with container exit code
# CI can set KWIN_MCP_TEST_IMAGE to reuse a prebuilt minimal test environment.
# Local runs build docker/<distro>.Dockerfile when KWIN_MCP_TEST_IMAGE is unset.
set -euo pipefail
IFS=$'\n\t'

: "${DOCKER_HOST:=tcp://localhost:2375}"
export DOCKER_HOST

SUPPORTED=(manjaro)
PAUSE_STEPS=(launch_app screenshot_initial mouse_click_ping keyboard_type screenshot_post_typing)
PAUSE_STEPS_DISPLAY=$(printf '%s ' "${PAUSE_STEPS[@]}")
PAUSE_STEPS_DISPLAY=${PAUSE_STEPS_DISPLAY% }

# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------
if [ $# -lt 1 ]; then
  echo "usage: $(basename "$0") <distro>" >&2
  echo "supported: ${SUPPORTED[*]}" >&2
  exit 2
fi

distro="$1"
shift
supported=false
for d in "${SUPPORTED[@]}"; do
  [ "$d" = "$distro" ] && supported=true && break
done

if [ "$supported" = false ]; then
  echo "error: distro '$distro' not supported (no docker/${distro}.Dockerfile defined)" >&2
  echo "supported distros: ${SUPPORTED[*]}" >&2
  exit 2
fi

pause_at=""
keep=0
for arg in "$@"; do
  case "$arg" in
    --pause-at=*)
      pause_at=${arg#--pause-at=}
      valid_pause=false
      for step in "${PAUSE_STEPS[@]}"; do
        if [ "$step" = "$pause_at" ]; then
          valid_pause=true
          break
        fi
      done
      if [ "$valid_pause" = false ]; then
        echo "error: invalid step '$pause_at' (valid: $PAUSE_STEPS_DISPLAY)" >&2
        exit 2
      fi
      ;;
    --keep)
      keep=1
      ;;
    *)
      echo "usage: $(basename "$0") <distro> [--pause-at=<step>] [--keep]" >&2
      echo "supported: ${SUPPORTED[*]}" >&2
      exit 2
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Resolve repo root
# ---------------------------------------------------------------------------
REPO=$(git rev-parse --show-toplevel 2>/dev/null || dirname "$(dirname "$(realpath "$0")")")

# ---------------------------------------------------------------------------
# Image selection
# ---------------------------------------------------------------------------
image="kwin-mcp-minimal-test-env:${distro}"
dockerfile="${distro}.Dockerfile"

# ---------------------------------------------------------------------------
# Build wheel (always rebuild — guarantees fresh code)
# ---------------------------------------------------------------------------
echo "==> Building kwin-mcp wheel..."
uv build --wheel --out-dir "$REPO/dist"
wheel=$(ls -t "$REPO/dist"/kwin_mcp-*.whl 2>/dev/null | head -1 || true)
if [ -z "$wheel" ]; then
  echo "error: no kwin-mcp wheel in dist/" >&2
  exit 3
fi
echo "==> Wheel: $wheel"

# ---------------------------------------------------------------------------
# Pull prebuilt image; fall back to local build on failure
# ---------------------------------------------------------------------------
# Pull failures (e.g. GHCR access denied for a private package, registry
# transient error) MUST NOT block the harness; the local Dockerfile is the
# source of truth. We retag the local build as the requested image so the
# downstream `docker run` reference is unchanged.
need_local_build=1
if [ -n "${KWIN_MCP_TEST_IMAGE:-}" ]; then
  image="$KWIN_MCP_TEST_IMAGE"
  echo "==> Pulling prebuilt Docker image $image..."
  if docker pull "$image"; then
    echo "==> Image ready: $image"
    need_local_build=0
  else
    echo "==> Pull failed; falling back to local build of docker/$dockerfile" >&2
  fi
fi

if [ "$need_local_build" -eq 1 ]; then
  if [ ! -f "$REPO/docker/$dockerfile" ]; then
    echo "error: docker/$dockerfile not found" >&2
    exit 2
  fi
  echo "==> Building Docker image $image..."
  docker build \
    --build-arg UID=1000 \
    --build-arg GID=1000 \
    -f "$REPO/docker/$dockerfile" \
    -t "$image" \
    "$REPO/docker"
  echo "==> Image built: $image"
fi

# ---------------------------------------------------------------------------
# Prepare evidence directory (chmod 0777 so container uid 1000 can write)
# ---------------------------------------------------------------------------
mkdir -p "$REPO/.sisyphus/evidence/${distro}"
chmod 0777 "$REPO/.sisyphus/evidence/${distro}"

# ---------------------------------------------------------------------------
# Run container (forbidden-flag policy: see docker/runtime-contract.md)
# ---------------------------------------------------------------------------
# Render-node passthrough (Waiver D, m0207 pattern):
# Conditionally pass /dev/dri/renderD12{8,9} when present on host.
# These are render-only nodes (no display, no input) — KWin's ScreenShot2
# D-Bus pipeline needs them even in software-rendering mode to complete
# within the default timeout. Without them, screenshot calls cancel mid-flight
# (DBusException 'Screenshot got cancelled'). Distinguished from the blanket
# DRI forbidden flag (see docker/runtime-contract.md 'Forbidden flags')
# because we only mount specific user-accessible render nodes
# (perms 0666 by udev), never card0/card1.
dri_args=()
[ -e /dev/dri/renderD128 ] && dri_args+=(--device /dev/dri/renderD128)
[ -e /dev/dri/renderD129 ] && dri_args+=(--device /dev/dri/renderD129)

echo "==> Running smoke test in container..."
container_name="kwin-mcp-smoke-${distro}-$(date -u +%Y%m%dT%H%M%SZ)"
echo "==> Container name: $container_name"
docker run --rm \
  --name "$container_name" \
  "${dri_args[@]}" \
  -e SMOKE_PAUSE_AT="$pause_at" \
  -e SMOKE_KEEP=$keep \
  -v "$REPO/dist:/wheels:ro" \
  -v "$REPO/docker/smoke_test.py:/opt/docker/smoke_test.py:ro" \
  -v "$REPO/docker/smoke_app.qml:/opt/docker/smoke_app.qml:ro" \
  -v "$REPO/docker/print_summary.py:/opt/docker/print_summary.py:ro" \
  -v "$REPO/.sisyphus/evidence/${distro}:/evidence" \
  "$image"

if [ "$keep" -eq 1 ]; then
  echo "==> Container kept alive: docker stop $container_name when done."
fi
