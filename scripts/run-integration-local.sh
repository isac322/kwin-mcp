#!/usr/bin/env bash
# Reproduce a single distro's integration CI job locally via Docker.
#
# Usage:   scripts/run-integration-local.sh [distro]
# Default: archlinux
#
# Available distros match scripts/ci/setup-<distro>.sh:
#   archlinux, fedora, opensuse-tumbleweed, ubuntu
set -euo pipefail

distro=${1:-archlinux}
setup_script="scripts/ci/setup-${distro}.sh"

if [[ ! -f "$setup_script" ]]; then
    echo "No setup script for distro '${distro}'. Expected: $setup_script" >&2
    exit 1
fi

case "$distro" in
    archlinux)              image="archlinux:latest" ;;
    fedora)                 image="fedora:latest" ;;
    opensuse-tumbleweed)    image="opensuse/tumbleweed:latest" ;;
    ubuntu)                 image="ubuntu:24.04" ;;
    *)
        echo "Unknown distro '${distro}'" >&2
        exit 1
        ;;
esac

repo_root=$(cd "$(dirname "$0")/.." && pwd)

docker run --rm \
    -v "${repo_root}:/work" \
    -w /work \
    -e LANG=C.UTF-8 \
    -e LC_ALL=C.UTF-8 \
    -e XDG_RUNTIME_DIR=/tmp/xdg-runtime-root \
    -e LIBGL_ALWAYS_SOFTWARE=1 \
    "$image" \
    bash -c "
        set -euo pipefail
        mkdir -p /tmp/xdg-runtime-root && chmod 700 /tmp/xdg-runtime-root
        bash ${setup_script}
        export PATH=\"\$HOME/.local/bin:\$PATH\"
        uv sync --group test
        uv run pytest tests/integration/ -v
    "
