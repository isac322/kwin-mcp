#!/bin/sh

set -u

usage() {
    cat <<'EOF'
Usage: scripts/run-e2e-docker.sh [--image-tag TAG] [--] [PYTEST_ARG ...]

Build the E2E image and run the installed-package test suite on Docker's native
architecture. Additional arguments are passed directly to pytest.
EOF
}

image_tag=${KWIN_MCP_E2E_IMAGE_TAG:-kwin-mcp-e2e}

case ${1-} in
    -h|--help)
        usage
        exit 0
        ;;
    -t|--image-tag)
        if [ "$#" -lt 2 ] || [ -z "$2" ]; then
            printf '%s\n' 'error: --image-tag requires a non-empty value' >&2
            usage >&2
            exit 64
        fi
        image_tag=$2
        shift 2
        ;;
    --image-tag=*)
        image_tag=${1#*=}
        if [ -z "$image_tag" ]; then
            printf '%s\n' 'error: --image-tag requires a non-empty value' >&2
            usage >&2
            exit 64
        fi
        shift
        ;;
esac

if [ "${1-}" = "--" ]; then
    shift
fi

if [ ! -f docker/e2e.Dockerfile ] || [ ! -f pyproject.toml ] || [ ! -d tests/e2e ]; then
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
docker build -f docker/e2e.Dockerfile -t "$image_tag" . || status=$?

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
