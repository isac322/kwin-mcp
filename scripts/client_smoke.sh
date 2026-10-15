#!/usr/bin/env bash
# Per-client MCP smoke for kwin-mcp with real agent CLIs (QA-CLT-001, -002, -008).
#
# Usage:
#   scripts/client_smoke.sh [--dev] claude
#   scripts/client_smoke.sh [--dev] codex
#   scripts/client_smoke.sh [--dev] hang (claude|codex) --live-consent (elicit|refuse)
#
# The server is the e2e image's live mode, registered behind the stdio tee:
#   python3 scripts/mcp_tee.py run --out <dir> -- \
#       docker run -i --rm kwin-mcp-e2e /opt/kwin-mcp-venv/bin/kwin-mcp-smoke --live [...]
# kwin-mcp-smoke starts a test-owned live KWin, window_zoo ("Zoo", "Zoo Utility") and the
# org.kwinmcp.Smoke D-Bus service, then runs kwin-mcp on the container's stdio
# (docker/kwin-mcp-smoke.py). The client gets a fixed prompt of literal calls; the oracle
# is the tee's server-side call log (server-calls.jsonl), checked by
# `scripts/mcp_tee.py check`: 29 tools listed, one schema-valid isError:false call per
# union arm, and no schema warning on the client's stderr.
#
# claude / codex  (schema leg): artifacts/client-smoke/<client>/
# hang            (QA-CLT-008): artifacts/client-smoke/hang/<client>-<consent>/
#     session_connect + window_close on a live window under a 120 s wall-clock guard.
#     There is no server-side consent timeout by design, so with --live-consent=elicit the
#     result documents whether the headless client hangs; with refuse it must not hang and
#     window_close must report consent "unsupported" (the run fails otherwise).
#
# Each directory keeps prompt.txt, the client config, client-transcript.jsonl (stdout),
# client-stderr.log, transcript.jsonl / server-calls.jsonl / server-stderr.log (tee),
# versions.txt, check.json and result.txt.
#
# --dev mounts this working tree's src/kwin_mcp, tests/ and the docker/ smoke files into
# the image, so an image built before them works (default image then: kwin-mcp-e2e:s).
#
# Environment: KWIN_MCP_SMOKE_IMAGE (default kwin-mcp-e2e, or kwin-mcp-e2e:s with --dev),
# KWIN_MCP_SMOKE_TIMEOUT (schema leg guard in seconds, default 900),
# KWIN_MCP_SMOKE_HANG_TIMEOUT (default 120). Local only: needs the maintainer's model
# auth (claude: `claude auth status`; codex: ~/.codex/auth.json or CODEX_API_KEY).
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
dev=0
if [ "${1:-}" = "--dev" ]; then
    dev=1
    shift
fi

usage() {
    sed -n '4,6p' "$0" | sed 's/^# //' >&2
    exit 64
}

mode=${1:-}
case "$mode" in
    claude | codex)
        client=$mode
        consent=elicit
        [ "$#" -eq 1 ] || usage
        out="$REPO/artifacts/client-smoke/$client"
        guard=${KWIN_MCP_SMOKE_TIMEOUT:-900}
        ;;
    hang)
        client=${2:-}
        case "$client" in claude | codex) ;; *) usage ;; esac
        [ "${3:-}" = "--live-consent" ] || usage
        consent=${4:-}
        case "$consent" in elicit | refuse) ;; *) usage ;; esac
        [ "$#" -eq 4 ] || usage
        out="$REPO/artifacts/client-smoke/hang/$client-$consent"
        guard=${KWIN_MCP_SMOKE_HANG_TIMEOUT:-120}
        ;;
    *) usage ;;
esac

if [ "$dev" -eq 1 ]; then
    image=${KWIN_MCP_SMOKE_IMAGE:-kwin-mcp-e2e:s}
else
    image=${KWIN_MCP_SMOKE_IMAGE:-kwin-mcp-e2e}
fi

command -v "$client" >/dev/null || { echo "error: $client is not installed" >&2; exit 69; }
command -v docker >/dev/null || { echo "error: docker is not installed" >&2; exit 69; }
docker image inspect "$image" >/dev/null 2>&1 || {
    echo "error: image $image not found; build it (scripts/run-e2e-docker.sh) or set KWIN_MCP_SMOKE_IMAGE" >&2
    exit 69
}

rm -rf "$out"
mkdir -p "$out/work"

# ---------------------------------------------------------------- server command
docker_args=(docker run -i --rm)
if [ "$dev" -eq 1 ]; then
    docker_args+=(
        --volume "$REPO/src/kwin_mcp:/opt/kwin-mcp-venv/lib/python3.13/site-packages/kwin_mcp:ro"
        --volume "$REPO/tests:/app/tests:ro"
        --volume "$REPO/docker/e2e-entrypoint.sh:/app/docker/e2e-entrypoint.sh:ro"
        --volume "$REPO/docker/kwin-mcp-smoke.py:/app/docker/kwin-mcp-smoke.py:ro"
    )
fi
docker_args+=("$image" /opt/kwin-mcp-venv/bin/kwin-mcp-smoke --live "--live-consent=$consent")
server_cmd=(python3 "$REPO/scripts/mcp_tee.py" run --out "$out" -- "${docker_args[@]}")

json_array() {
    python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@"
}

# ---------------------------------------------------------------- prompt
if [ "$mode" = hang ]; then
    cat >"$out/prompt.txt" <<'EOF'
You are a scripted MCP client smoke test. Use only the tools of the MCP server named "kwin".
Make exactly these tool calls, in this order, with exactly these JSON arguments. Do not call
any other tool, do not retry, and do not change an argument.
1. session_connect {}
2. window_close {"window": {"app_id": "window-zoo", "caption": "Zoo Utility"}}
3. session_stop {}
Then reply with one line per call: the tool name, whether it returned an error, and for
window_close the value of its "consent" field.
EOF
else
    cat >"$out/prompt.txt" <<'EOF'
You are a scripted MCP client smoke test. Use only the tools of the MCP server named "kwin".
Make exactly these tool calls, in this order, with exactly these JSON arguments. Do not call
any other tool, do not retry with a different argument shape, and do not add, drop or
rename any argument. REF stands for the "ref" string of the element that call 2 returns.
1. session_connect {}
2. element_query {"locator": {"app": "window-zoo", "query": "OK", "role": "button"}}
3. pointer_click {"target": {"x": 1200, "y": 760}}
4. pointer_click {"target": {"ref": REF, "dx": 2, "dy": 1}}
5. element_act {"element": {"ref": REF}, "action": "click"}
6. element_act {"element": {"strict": {"app": "window-zoo", "query": "Play"}}, "action": "click"}
7. dbus_call {"service": "org.kwinmcp.Smoke", "path": "/org/kwinmcp/Smoke", "interface": "org.kwinmcp.Smoke", "method": "Add", "args": ["int32:5", {"type": "int32", "value": 5}]}
8. pointer_scroll {"target": {"x": 640, "y": 400}, "delta": 1, "unit": "detent"}
9. window_list {"filter": {"kind": ["normal"]}}
10. element_query {"locator": {"app": "window-zoo", "query": "kwin-mcp-smoke-never-present"}, "wait": {"until": "absent", "timeout_ms": 500}}
11. window_close {"window": {"app_id": "window-zoo", "caption": "Zoo Utility"}}
12. session_stop {}
Then reply with one line per call: the tool name and whether it returned an error.
EOF
fi
prompt=$(cat "$out/prompt.txt")

# ---------------------------------------------------------------- client command
scratch=""
cleanup() {
    if [ -n "$scratch" ]; then
        rm -f "$scratch/auth.json"
    fi
}
trap cleanup EXIT

case "$client" in
    claude)
        python3 - "$out/mcp-config.json" "${server_cmd[@]}" <<'EOF'
import json, sys
path, command, *args = sys.argv[1:]
config = {"mcpServers": {"kwin": {"type": "stdio", "command": command, "args": args}}}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=1)
EOF
        client_cmd=(
            env MCP_TIMEOUT=120000 MCP_TOOL_TIMEOUT=600000
            claude -p "$prompt"
            --mcp-config "$out/mcp-config.json" --strict-mcp-config
            --allowedTools 'mcp__kwin__*'
            --output-format stream-json --verbose
        )
        ;;
    codex)
        scratch="$out/codex-home"
        mkdir -p "$scratch"
        if [ -f "$HOME/.codex/auth.json" ]; then
            install -m 0600 "$HOME/.codex/auth.json" "$scratch/auth.json"
        elif [ -z "${CODEX_API_KEY:-}" ]; then
            echo "error: codex has no credentials (no ~/.codex/auth.json and no CODEX_API_KEY)" >&2
            exit 77
        fi
        {
            printf 'approval_policy = "never"\n'
            printf 'sandbox_mode = "read-only"\n\n'
            printf '[mcp_servers.kwin]\n'
            printf 'command = "python3"\n'
            printf 'args = %s\n' "$(json_array "${server_cmd[@]:1}")"
            printf 'startup_timeout_sec = 120\n'
            printf 'tool_timeout_sec = 600\n'
        } >"$scratch/config.toml"
        cp "$scratch/config.toml" "$out/codex-config.toml"
        client_cmd=(
            env CODEX_HOME="$scratch"
            codex exec --skip-git-repo-check --ephemeral --json -C "$out/work" "$prompt"
        )
        ;;
esac

{
    echo "date: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "client: $("$client" --version 2>&1 | head -n 1)"
    echo "image: $image $(docker image inspect -f '{{.Id}}' "$image")"
    echo "dev mounts: $dev"
    echo "live consent: $consent"
    echo "guard seconds: $guard"
} >"$out/versions.txt"

# ---------------------------------------------------------------- run under the guard
started=$(date +%s)
(cd "$out/work" && exec "${client_cmd[@]}") \
    </dev/null >"$out/client-transcript.jsonl" 2>"$out/client-stderr.log" &
client_pid=$!
timed_out=0
while kill -0 "$client_pid" 2>/dev/null; do
    if [ $(($(date +%s) - started)) -ge "$guard" ]; then
        timed_out=1
        pkill -TERM -P "$client_pid" 2>/dev/null || true
        kill -TERM "$client_pid" 2>/dev/null || true
        sleep 5
        pkill -KILL -P "$client_pid" 2>/dev/null || true
        kill -KILL "$client_pid" 2>/dev/null || true
        break
    fi
    sleep 1
done
client_status=0
wait "$client_pid" || client_status=$?
elapsed=$(($(date +%s) - started))
# The tee exits when the client closes its stdin; give it a moment to flush.
sleep 2

# ---------------------------------------------------------------- verdict
suite=schema
[ "$mode" = hang ] && suite=hang
check_status=0
# --no-project: the checker needs only jsonschema, not kwin-mcp's Linux-only dependencies.
(cd "$REPO" && uv run --quiet --no-project --with 'jsonschema>=4.26' python scripts/mcp_tee.py \
    check --out "$out" --suite "$suite" --client-stderr "$out/client-stderr.log") \
    | tee "$out/check.txt" || check_status=$?

verdict=pass
if [ "$mode" = hang ]; then
    if [ "$consent" = refuse ]; then
        refused=$(python3 - "$out/server-calls.jsonl" <<'EOF'
import json, sys
calls = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
# A 2026-07-28 client resends tools/call after an input_required round; judge final rounds.
closes = [
    c
    for c in calls
    if c.get("name") == "window_close" and c.get("result_type") != "input_required"
]
ok = bool(closes) and all(
    c.get("responded") and (c.get("structuredContent") or {}).get("consent") == "unsupported"
    for c in closes
)
print("yes" if ok else "no")
EOF
)
        if [ "$timed_out" -eq 1 ] || [ "$refused" != yes ] || [ "$check_status" -ne 0 ]; then
            verdict=fail
        fi
    else
        # elicit: documentation run (F215: no server-side timeout). Hanging is a finding,
        # not a failure; the outcome lands in result.txt for the README headless guidance.
        verdict=documented
    fi
elif [ "$timed_out" -eq 1 ] || [ "$check_status" -ne 0 ]; then
    verdict=fail
fi

{
    echo "client: $client"
    echo "mode: $mode"
    echo "live consent: $consent"
    echo "client exit status: $client_status"
    echo "elapsed seconds: $elapsed"
    echo "hung (killed by the ${guard}s guard): $([ "$timed_out" -eq 1 ] && echo yes || echo no)"
    echo "call-log check exit status: $check_status"
    echo "verdict: $verdict"
} | tee "$out/result.txt"

[ "$verdict" != fail ]
