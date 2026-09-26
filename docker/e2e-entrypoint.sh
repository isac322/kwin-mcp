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

exec "$@"
