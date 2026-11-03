#!/bin/sh
set -eu

unset PYTHONPATH

if [ "$CASE_KIND" = capture ]; then
    export KWIN_MCP_PROBE_WORKTREE=/app
    export KWIN_MCP_ARTIFACT_DIR="/artifacts/capture-gaps/$PROBE_DIR"
    export KWIN_MCP_CAPTURE_GAPS_EVIDENCE="$KWIN_MCP_ARTIFACT_DIR"
    exec /opt/kwin-mcp-venv/bin/python -I \
        /probes/premerge_capture_gaps_probe.py --expect "$PROBE_EXPECT"
fi

if [ "$CASE_KIND" = outputs ]; then
    exec /opt/kwin-mcp-venv/bin/python -I /qa/premerge_multi_output_qa.py
fi

if [ "$CASE_KIND" = geometry ]; then
    exec /opt/kwin-mcp-venv/bin/python -I /qa/premerge_geometry_qa.py
fi

if [ "$CASE_KIND" != topology ]; then
    printf 'Unknown native QA case kind: %s\n' "$CASE_KIND" >&2
    exit 2
fi

cd /app
original_log=/artifacts/topology-original.log
set +e
timeout --signal=INT --kill-after=1m 15m \
    /opt/kwin-mcp-venv/bin/python -m pytest \
    tests/e2e/test_capture.py::test_topology_shift_mid_burst -v \
    --junitxml=/artifacts/topology-original-junit.xml \
    > "$original_log" 2>&1
original_rc=$?
set -e
cat "$original_log"

if [ "$TOPOLOGY_EXPECTATION" = indexerror ]; then
    [ "$original_rc" -ne 0 ] && [ "$original_rc" -ne 124 ]
    grep -q "IndexError: list index out of range" "$original_log"
    grep -q "test_topology_shift_mid_burst" "$original_log"
    ! grep -Eq "ERROR at setup|_StartupError|native virtual capture requires|could not parse installed KWin version" "$original_log"
else
    [ "$original_rc" -eq 0 ]
fi

healthy_log=/artifacts/qa-cap014.log
set +e
timeout --signal=INT --kill-after=1m 15m \
    /opt/kwin-mcp-venv/bin/python -m pytest \
    tests/e2e/test_capture.py::test_capture_failure_after_commit_is_warning_not_error -v \
    --junitxml=/artifacts/qa-cap014-junit.xml \
    > "$healthy_log" 2>&1
healthy_rc=$?
set -e
cat "$healthy_log"
[ "$healthy_rc" -eq 0 ]
