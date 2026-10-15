#!/usr/bin/env python3
"""Stdio tee between an MCP client and kwin-mcp, plus the client-smoke call-log checker.

``mcp_tee.py run --out DIR -- COMMAND [ARGS...]``
    Start ``COMMAND`` (the MCP server, e.g. ``docker run -i --rm kwin-mcp-e2e
    /opt/kwin-mcp-venv/bin/kwin-mcp-smoke --live``) and relay newline-delimited
    JSON-RPC between this process's stdio (the client) and the server's stdio,
    byte for byte. Nothing in the product is hooked: the log is what crossed the
    pipe. Writes, in ``DIR``:

    - ``transcript.jsonl``: every line in both directions
      (``{"t", "dir": "c2s"|"s2c", "message"}``; ``"raw"`` for a non-JSON line);
    - ``server-calls.jsonl``: one record per ``tools/call`` request
      (``id, name, arguments, requested_at, responded``, then ``responded_at,
      isError, rpc_error, result_type, text, structuredContent`` once answered).
      Requests still unanswered when the stream ends are written with
      ``responded: false`` (QA-CLT-008: the hang check reads them);
    - ``server-stderr.log``: the server's stderr (clients differ in whether and
      where they keep it, so the tee owns it).

    The exit status is the server's.

``mcp_tee.py check --out DIR --suite {schema,hang} [--client-stderr FILE]``
    Read ``DIR``'s logs and verify the smoke: the ``tools/list`` reply lists exactly
    the 29 tools of ``tests/tool_surface.py``, every listed arm has at least one call
    whose arguments validate against the tool's published ``inputSchema``
    (jsonschema) and whose result is ``isError: false``, and the client's stderr has
    no schema-conversion warning. Writes ``DIR/check.json`` and prints one line per
    check; exits 1 when any check fails. Needs ``jsonschema`` (``uv run --no-project
    --with jsonschema python scripts/mcp_tee.py check ...``).

The run mode uses the standard library only, so any ``python3`` can host the tee.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import IO

REPO_ROOT = Path(__file__).resolve().parent.parent
TEXT_LIMIT = 4000
CLAUDE_SCHEMA_WARNING = "does not support oneOf, allOf, or anyOf at the top level"


class _CallLog:
    """Pairs ``tools/call`` requests with their responses and writes both logs."""

    def __init__(self, out: Path) -> None:
        out.mkdir(parents=True, exist_ok=True)
        self._transcript = (out / "transcript.jsonl").open("w", encoding="utf-8")
        self._calls = (out / "server-calls.jsonl").open("w", encoding="utf-8")
        self._pending: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def record(self, direction: str, line: bytes) -> None:
        now = time.time()
        try:
            message = json.loads(line)
        except ValueError:
            raw = line.decode("utf-8", "replace")
            entry: dict[str, Any] = {"t": now, "dir": direction, "raw": raw}
            message = None
        else:
            entry = {"t": now, "dir": direction, "message": message}
        with self._lock:
            if self._transcript.closed:
                return
            self._transcript.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._transcript.flush()
            if isinstance(message, dict):
                self._pair(direction, message, now)

    def _pair(self, direction: str, message: dict[str, Any], now: float) -> None:
        key = json.dumps(message.get("id"))
        if direction == "c2s" and message.get("method") == "tools/call":
            params = message.get("params") or {}
            self._pending[key] = {
                "id": message.get("id"),
                "name": params.get("name"),
                "arguments": params.get("arguments") or {},
                "requested_at": now,
                "responded": False,
            }
            return
        if direction != "s2c" or "method" in message or key not in self._pending:
            return
        call = self._pending.pop(key)
        call["responded"] = True
        call["responded_at"] = now
        error = message.get("error")
        result = message.get("result") or {}
        call["rpc_error"] = error
        call["result_type"] = result.get("resultType")
        call["isError"] = True if error is not None else bool(result.get("isError", False))
        texts = [
            block.get("text", "")
            for block in result.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        call["text"] = "\n".join(texts)[:TEXT_LIMIT]
        call["structuredContent"] = result.get("structuredContent")
        self._write_call(call)

    def _write_call(self, call: dict[str, Any]) -> None:
        self._calls.write(json.dumps(call, ensure_ascii=False) + "\n")
        self._calls.flush()

    def close(self) -> None:
        with self._lock:
            for call in self._pending.values():
                self._write_call(call)
            self._pending.clear()
            self._transcript.close()
            self._calls.close()


def _pump(source: IO[bytes], sink: IO[bytes], direction: str, log: _CallLog) -> None:
    """Relay lines from ``source`` to ``sink`` until EOF, logging each; then close ``sink``."""
    try:
        for line in iter(source.readline, b""):
            try:
                sink.write(line)
                sink.flush()
            except (BrokenPipeError, ValueError):
                log.record(direction, line)
                break
            log.record(direction, line)
    finally:
        with contextlib.suppress(BrokenPipeError, OSError):
            sink.close()


def run(out: Path, command: list[str]) -> int:
    log = _CallLog(out)
    with (out / "server-stderr.log").open("wb") as stderr:
        server = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr
        )
    assert server.stdin is not None and server.stdout is not None

    def forward(signum: int, _frame: object) -> None:
        if server.poll() is None:
            server.send_signal(signum)

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, forward)

    to_server = threading.Thread(
        target=_pump, args=(sys.stdin.buffer, server.stdin, "c2s", log), daemon=True
    )
    to_server.start()
    _pump(server.stdout, sys.stdout.buffer, "s2c", log)
    status = server.wait()
    log.close()
    return status


# ---------------------------------------------------------------- check mode


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _published_tools(transcript: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """name -> inputSchema from the server's ``tools/list`` replies."""
    tools: dict[str, dict[str, Any]] = {}
    for entry in transcript:
        message = entry.get("message")
        if entry.get("dir") != "s2c" or not isinstance(message, dict):
            continue
        result = message.get("result")
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            for tool in result["tools"]:
                tools[tool["name"]] = tool.get("inputSchema") or {}
    return tools


def _get(mapping: object, *path: str) -> object:
    for key in path:
        if not isinstance(mapping, dict):
            return None
        mapping = mapping.get(key)
    return mapping


Arm = tuple[str, str, "Callable[[dict[str, Any]], bool]"]

SCHEMA_ARMS: list[Arm] = [
    ("session_connect()", "session_connect", lambda a: True),
    (
        "pointer_click(target={x,y})",
        "pointer_click",
        lambda a: (
            isinstance(a.get("target"), dict)
            and {"x", "y"} <= set(a["target"])
            and "ref" not in a["target"]
        ),
    ),
    (
        "pointer_click(target={ref,dx,dy})",
        "pointer_click",
        lambda a: isinstance(a.get("target"), dict) and {"ref", "dx", "dy"} <= set(a["target"]),
    ),
    (
        "element_act(element={ref})",
        "element_act",
        lambda a: isinstance(a.get("element"), dict) and "ref" in a["element"],
    ),
    (
        "element_act(element={strict:{app,query}})",
        "element_act",
        lambda a: (
            isinstance(_get(a, "element", "strict"), dict)
            and {"app", "query"} <= set(a["element"]["strict"])
        ),
    ),
    (
        "dbus_call(args=['int32:5',{type:'int32',value:5}])",
        "dbus_call",
        lambda a: a.get("args") == ["int32:5", {"type": "int32", "value": 5}],
    ),
    ("pointer_scroll(unit='detent')", "pointer_scroll", lambda a: a.get("unit") == "detent"),
    (
        "window_list(filter={kind:['normal']})",
        "window_list",
        lambda a: _get(a, "filter", "kind") == ["normal"],
    ),
    (
        "element_query(wait={until:'absent',timeout_ms:500})",
        "element_query",
        lambda a: _get(a, "wait", "until") == "absent" and _get(a, "wait", "timeout_ms") == 500,
    ),
    ("window_close(live window)", "window_close", lambda a: True),
]

HANG_ARMS: list[Arm] = [
    ("session_connect()", "session_connect", lambda a: True),
    ("window_close(live window)", "window_close", lambda a: True),
]


def _tool_names() -> tuple[str, ...]:
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from tool_surface import TOOL_NAMES

    return tuple(TOOL_NAMES)


def check(out: Path, suite: str, client_stderr: Path | None) -> int:
    from jsonschema import validators

    transcript = _load_jsonl(out / "transcript.jsonl")
    calls = _load_jsonl(out / "server-calls.jsonl")
    published = _published_tools(transcript)
    results: list[dict[str, Any]] = []

    expected = _tool_names()
    results.append(
        {
            "check": "tools/list names",
            "ok": sorted(published) == sorted(expected) and len(expected) == 29,
            "detail": f"{len(published)} published; "
            f"missing {sorted(set(expected) - set(published))}; "
            f"extra {sorted(set(published) - set(expected))}",
        }
    )

    def schema_errors(call: dict[str, Any]) -> list[str]:
        schema = published.get(call.get("name") or "")
        if schema is None:
            return [f"tool {call.get('name')!r} was not published"]
        validator = validators.validator_for(schema)(schema)
        return [error.message for error in validator.iter_errors(call.get("arguments") or {})]

    for label, tool, matches in SCHEMA_ARMS if suite == "schema" else HANG_ARMS:
        shaped = [c for c in calls if c.get("name") == tool and matches(c.get("arguments") or {})]
        good = [
            c
            for c in shaped
            if c.get("responded")
            and c.get("result_type") != "input_required"
            and not c.get("isError")
            and not schema_errors(c)
        ]
        detail: dict[str, Any] = {
            "calls": len(shaped),
            "arguments": [c.get("arguments") for c in shaped],
            "isError": [c.get("isError") for c in shaped],
            "responded": [c.get("responded") for c in shaped],
            "result_type": [c.get("result_type") for c in shaped],
            "schema_errors": [schema_errors(c) for c in shaped],
            "text": [c.get("text", "")[:400] for c in shaped],
        }
        if tool == "window_close":
            detail["consent"] = [_get(c.get("structuredContent"), "consent") for c in shaped]
            detail["seconds"] = [
                round(c["responded_at"] - c["requested_at"], 2) if c.get("responded") else None
                for c in shaped
            ]
        results.append({"check": label, "ok": bool(good), "detail": detail})

    if client_stderr is not None:
        stderr = (
            client_stderr.read_text(encoding="utf-8", errors="replace")
            if client_stderr.is_file()
            else ""
        )
        results.append(
            {
                "check": "no client schema warning",
                "ok": CLAUDE_SCHEMA_WARNING not in stderr,
                "detail": str(client_stderr),
            }
        )

    (out / "check.json").write_text(json.dumps(results, indent=1, ensure_ascii=False) + "\n")
    for result in results:
        print(f"{'PASS' if result['ok'] else 'FAIL'}  {result['check']}")
        if not result["ok"]:
            print(f"      {json.dumps(result['detail'], ensure_ascii=False)[:1500]}")
    return 0 if all(result["ok"] for result in results) else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mcp_tee.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    run_parser = sub.add_parser("run", help="relay stdio and log the JSON-RPC traffic")
    run_parser.add_argument("--out", type=Path, required=True)
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    check_parser = sub.add_parser("check", help="verify a smoke run's logs")
    check_parser.add_argument("--out", type=Path, required=True)
    check_parser.add_argument("--suite", choices=("schema", "hang"), required=True)
    check_parser.add_argument("--client-stderr", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "run":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            parser.error("run needs a server command after --")
        return run(args.out, command)
    return check(args.out, args.suite, args.client_stderr)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
