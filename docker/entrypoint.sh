#!/usr/bin/env bash
# Container entrypoint for kwin-mcp smoke harness.
#
# Exit codes (contract: docker/runtime-contract.md):
#   0    pass
#   1    smoke assertion failed   (smoke_test.py)
#   2    environment setup failed (smoke_test.py)
#   3    wheel install failed
#   >=10 uncaught exception       (smoke_test.py)
#
# install.json schema (consumed by smoke_test.py — exactly 5 keys):
#   wheel_basename, wheel_sha256, kwin_mcp_version, package_versions, image_tag
set -euo pipefail
IFS=$'\n\t'

EVIDENCE_DIR="/evidence/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$EVIDENCE_DIR/screenshots" "$EVIDENCE_DIR/a11y"
export EVIDENCE_DIR

exec > >(tee "$EVIDENCE_DIR/stdout.log") 2> >(tee "$EVIDENCE_DIR/stderr.log" >&2)

trap '_ec=$?; if [ "$_ec" -ne 0 ] && [ ! -f "$EVIDENCE_DIR/summary.json" ]; then
  python3 -c "import json,sys; json.dump({\"verdict\": \"error\", \"exit_code\": int(sys.argv[1]), \"reason\": \"entrypoint_failed\"}, open(\"$EVIDENCE_DIR/summary.json\", \"w\"), indent=2)" "$_ec" || true
fi' EXIT

export PYTHONUNBUFFERED=1

wheel=$(ls -t /wheels/kwin_mcp-*.whl 2>/dev/null | head -1 || true)
if [ -z "${wheel:-}" ]; then
  python3 -c "import json; json.dump({'verdict': 'error', 'reason': 'no_wheel_found'}, open('$EVIDENCE_DIR/summary.json', 'w'), indent=2)" || true
  echo "error: no kwin_mcp-*.whl found in /wheels/" >&2
  exit 3
fi

echo "Installing wheel: $wheel"
if ! uv pip install --python /opt/kwinmcp-venv/bin/python "$wheel"; then
  python3 -c "import json; json.dump({'verdict': 'error', 'reason': 'wheel_install_failed'}, open('$EVIDENCE_DIR/summary.json', 'w'), indent=2)" || true
  echo "error: wheel install failed" >&2
  exit 3
fi

WHEEL_BASENAME=$(basename "$wheel")
WHEEL_SHA256=$(sha256sum "$wheel" | awk '{print $1}')
KWIN_MCP_VERSION=$(/opt/kwinmcp-venv/bin/python -c "from importlib.metadata import version; print(version('kwin-mcp'))")
IMAGE_TAG="${KWIN_MCP_IMAGE_TAG:-unknown}"
export WHEEL_BASENAME WHEEL_SHA256 KWIN_MCP_VERSION IMAGE_TAG

python3 - <<'PYEOF' > "$EVIDENCE_DIR/install.json"
import json
import os
import subprocess

pkg_versions: dict[str, str] = {}
try:
    result = subprocess.run(
        ["pacman", "-Q", "kwin", "spectacle", "at-spi2-core", "qt6-declarative", "python"],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            pkg_versions[parts[0]] = parts[1]
except FileNotFoundError:
    pass

print(
    json.dumps(
        {
            "wheel_basename": os.environ.get("WHEEL_BASENAME", ""),
            "wheel_sha256": os.environ.get("WHEEL_SHA256", ""),
            "kwin_mcp_version": os.environ.get("KWIN_MCP_VERSION", ""),
            "package_versions": pkg_versions,
            "image_tag": os.environ.get("IMAGE_TAG", "unknown"),
        },
        indent=2,
    )
)
PYEOF

echo "install.json written: $EVIDENCE_DIR/install.json"

set +e
/opt/kwinmcp-venv/bin/python /opt/docker/smoke_test.py
smoke_exit=$?
EVIDENCE_DIR="$EVIDENCE_DIR" python3 /opt/docker/print_summary.py || true
set -e

if [ "${SMOKE_KEEP:-0}" = "1" ]; then
  container_identifier="${HOSTNAME:-$(hostname)}"
  echo "Smoke test exit code: $smoke_exit"
  echo "==> Container kept alive (--keep). Inspect with: docker exec -it $container_identifier bash"
  echo "==> Container will exit when you run: docker stop $container_identifier"
  echo "Use the deterministic container name printed by the test wrapper when available."
  keep_tail_pid=""
  trap 'trap - TERM INT; if [ -n "${keep_tail_pid:-}" ]; then kill "$keep_tail_pid" 2>/dev/null || true; wait "$keep_tail_pid" 2>/dev/null || true; fi; exit "$smoke_exit"' TERM INT
  tail -f /dev/null &
  keep_tail_pid=$!
  wait "$keep_tail_pid"
  exit "$smoke_exit"
fi

exit "$smoke_exit"
