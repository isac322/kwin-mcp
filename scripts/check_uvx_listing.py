#!/usr/bin/env python3
"""Check that ``uvx --from <wheel> kwin-mcp`` serves the 1.0 tool listing (QA-PKG-003).

Usage: ``python3 scripts/check_uvx_listing.py dist/kwin_mcp-1.0.0-py3-none-any.whl``

Runs the wheel the way users run kwin-mcp (``uvx``), speaks plain newline-delimited
JSON-RPC on its stdio, and checks the ``tools/list`` answer: exactly the 29 tool
names of ``tests/tool_surface.py`` and the model-visible listing within the token
budget (``tests/unit/test_schema_budget.py``'s chars/4 rule). Standard library only;
exits 1 with the reasons when a check fails.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_SURFACE = REPO_ROOT / "tests" / "tool_surface.py"
BUDGET_TOKENS = 9_510
# The keys a client passes to the model (tests/unit/test_schema_budget.py's V1 view).
MODEL_VISIBLE_KEYS = ("name", "title", "description", "inputSchema", "annotations")
PROTOCOL_VERSION = "2025-11-25"
TIMEOUT_S = 300.0


def _tool_names() -> tuple[str, ...]:
    """``TOOL_NAMES`` from tests/tool_surface.py, read without importing the tests."""
    tree = ast.parse(TOOL_SURFACE.read_text(encoding="utf-8"))
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "TOOL_NAMES" and node.value:
            names = ast.literal_eval(node.value)
            return tuple(str(name) for name in names)
    msg = f"TOOL_NAMES not found in {TOOL_SURFACE}"
    raise SystemExit(msg)


def _listing(wheel: Path) -> list[dict[str, object]]:
    """Start kwin-mcp from ``wheel`` with uvx and return its ``tools/list`` tools."""
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "check_uvx_listing", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    stdin = "".join(json.dumps(request) + "\n" for request in requests)
    process = subprocess.Popen(
        ["uvx", "--from", str(wheel), "kwin-mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(stdin)
    process.stdin.flush()
    try:
        for line in process.stdout:
            message = json.loads(line)
            if message.get("id") == 2:
                if "error" in message:
                    msg = f"tools/list failed: {message['error']}"
                    raise SystemExit(msg)
                return list(message["result"]["tools"])
        _, stderr = process.communicate(timeout=TIMEOUT_S)
        msg = f"kwin-mcp exited before answering tools/list (exit {process.returncode}):\n{stderr}"
        raise SystemExit(msg)
    finally:
        # Closing stdin ends the session; the server then exits on its own.
        process.stdin.close()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()


def _tokens(tools: list[dict[str, object]]) -> int:
    view = [{key: tool[key] for key in MODEL_VISIBLE_KEYS if key in tool} for tool in tools]
    text = json.dumps({"tools": view}, separators=(",", ":"), ensure_ascii=False)
    return round(len(text) / 4)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    wheel = Path(argv[0]).resolve()
    if not wheel.is_file():
        print(f"no such wheel: {wheel}", file=sys.stderr)
        return 2
    tools = _listing(wheel)
    problems: list[str] = []
    names = tuple(str(tool["name"]) for tool in tools)
    expected = _tool_names()
    if sorted(names) != sorted(expected) or len(names) != len(expected):
        missing = sorted(set(expected) - set(names))
        extra = sorted(set(names) - set(expected))
        problems.append(f"tool names differ: missing {missing}, unexpected {extra}")
    tokens = _tokens(tools)
    if tokens > BUDGET_TOKENS:
        problems.append(f"listing is {tokens} tokens, over the {BUDGET_TOKENS} budget")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"OK: uvx {wheel.name} serves {len(names)} tools in {tokens} tokens")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
