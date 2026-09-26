#!/bin/sh

set -u

usage() {
    cat <<'EOF'
Usage: scripts/run-e2e-docker.sh [--distro debian|fedora|opensuse|archlinux] [--image-tag TAG] [--] [PYTEST_ARG ...]

Build the E2E image and run the installed-package test suite on Docker's native
architecture. --distro selects the image variant: debian (docker/e2e.Dockerfile,
the default), fedora (docker/e2e-fedora.Dockerfile), opensuse
(docker/e2e-opensuse.Dockerfile), or archlinux (docker/e2e-arch.Dockerfile).
Additional arguments are passed directly to pytest.
EOF
}

option_value_error() {
    printf 'error: %s requires a non-empty value\n' "$1" >&2
    usage >&2
    exit 64
}

distro=${KWIN_MCP_E2E_DISTRO:-debian}
image_tag=${KWIN_MCP_E2E_IMAGE_TAG:-}

while [ "$#" -gt 0 ]; do
    case $1 in
        -h|--help)
            usage
            exit 0
            ;;
        -t|--image-tag)
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                option_value_error --image-tag
            fi
            image_tag=$2
            shift 2
            ;;
        --image-tag=*)
            image_tag=${1#*=}
            [ -n "$image_tag" ] || option_value_error --image-tag
            shift
            ;;
        --distro)
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                option_value_error --distro
            fi
            distro=$2
            shift 2
            ;;
        --distro=*)
            distro=${1#*=}
            [ -n "$distro" ] || option_value_error --distro
            shift
            ;;
        --)
            shift
            break
            ;;
        *)
            break
            ;;
    esac
done

case $distro in
    debian)
        dockerfile=docker/e2e.Dockerfile
        default_tag=kwin-mcp-e2e
        ;;
    fedora)
        dockerfile=docker/e2e-fedora.Dockerfile
        default_tag=kwin-mcp-e2e-fedora
        ;;
    opensuse)
        dockerfile=docker/e2e-opensuse.Dockerfile
        default_tag=kwin-mcp-e2e-opensuse
        ;;
    archlinux)
        dockerfile=docker/e2e-arch.Dockerfile
        default_tag=kwin-mcp-e2e-arch
        ;;
    *)
        printf 'error: unknown --distro %s (expected debian, fedora, opensuse, or archlinux)\n' "$distro" >&2
        usage >&2
        exit 64
        ;;
esac
image_tag=${image_tag:-$default_tag}

if [ ! -f "$dockerfile" ] || [ ! -f pyproject.toml ] || [ ! -d tests/e2e ]; then
    printf '%s\n' 'error: run this script from the kwin-mcp repository root' >&2
    exit 64
fi

repo_root=$(pwd -P) || exit 1
timestamp=$(date -u '+%Y%m%dT%H%M%SZ') || exit 1
artifact_dir="$repo_root/artifacts/e2e/$timestamp-$$"
container_name="kwin-mcp-e2e-$timestamp-$$"

if ! mkdir -p "$artifact_dir"; then
    printf 'error: could not create artifact directory: %s\n' "$artifact_dir" >&2
    exit 1
fi

# The image runs as UID 1000, which may differ from the host user on Linux.
if ! chmod 0777 "$artifact_dir"; then
    printf 'error: could not make artifact directory container-writable: %s\n' "$artifact_dir" >&2
    exit 1
fi

collect_diagnostics() {
    docker inspect "$container_name" \
        >"$artifact_dir/docker-inspect.json" \
        2>"$artifact_dir/docker-inspect.stderr" || :
    docker logs --timestamps "$container_name" \
        >"$artifact_dir/docker.log" 2>&1 || :
    docker top "$container_name" -eo pid,ppid,user,stat,etime,args \
        >"$artifact_dir/docker-processes.txt" 2>&1 || :
}

cleanup() {
    status=$1
    trap - 0 HUP INT TERM
    if [ "$status" -ne 0 ]; then
        collect_diagnostics
    fi
    docker rm -f "$container_name" >/dev/null 2>&1 || :
    printf 'E2E artifacts: %s\n' "$artifact_dir"
    exit "$status"
}

trap 'cleanup $?' 0
trap 'cleanup 129' HUP
trap 'cleanup 130' INT
trap 'cleanup 143' TERM

status=0
docker build -f "$dockerfile" -t "$image_tag" . || status=$?

if [ "$status" -eq 0 ]; then
    if docker run --rm \
        --volume "$repo_root:/workspace:ro" \
        --workdir /workspace \
        "$image_tag" \
        /bin/sh -c '
            /opt/kwin-mcp-venv/bin/ruff check --no-cache src tests &&
            /opt/kwin-mcp-venv/bin/ruff format --check --no-cache src tests &&
            /opt/kwin-mcp-venv/bin/ty check src tests
        ' >"$artifact_dir/quality.log" 2>&1; then
        status=0
    else
        status=$?
    fi
    cat "$artifact_dir/quality.log" || :
fi

if [ "$status" -eq 0 ]; then
    if docker run \
        --name "$container_name" \
        --volume "$artifact_dir:/artifacts" \
        "$image_tag" \
        /opt/kwin-mcp-venv/bin/python -m pytest tests/e2e -v \
        --junitxml=/artifacts/junit.xml \
        "$@" >"$artifact_dir/pytest.log" 2>&1; then
        status=0
    else
        status=$?
    fi
    cat "$artifact_dir/pytest.log" || :
fi

exit "$status"
