#!/bin/sh
set -eu

if [ "$#" -eq 0 ]; then
    printf '%s\n' 'error: no command supplied to the e2e entrypoint' >&2
    exit 64
fi

artifact_dir=${KWIN_MCP_ARTIFACT_DIR:-}
if [ -n "$artifact_dir" ]; then
    if mkdir -p "$artifact_dir"; then
        if ! /opt/kwin-mcp-venv/bin/python /app/docker/e2e-environment.py "$@"; then
            printf '%s\n' 'warning: environment metadata could not be recorded; continuing' >&2
        fi
    else
        printf '%s\n' 'warning: artifact directory could not be created; continuing' >&2
    fi
fi

# kwin-mcp-smoke mode (scripts/client_smoke.sh, QA-CLT-001/002/008): the image has no
# kwin-mcp-smoke binary; this name runs kwin-mcp against a live-desktop stand-in.
case "$1" in
    kwin-mcp-smoke | /opt/kwin-mcp-venv/bin/kwin-mcp-smoke)
        shift
        exec /opt/kwin-mcp-venv/bin/python /app/docker/kwin-mcp-smoke.py "$@"
        ;;
esac

exec "$@"
