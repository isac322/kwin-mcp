"""The ``element_query`` text over MCP and the CLI: header forms and the 0.11 tree body.

S §2 "One renderer": the header follows the call's own inputs (an empty locator
gets 0.11's ``# Accessibility Tree (N elements)``, any other ``Found N elements
matching <desc>:`` or ``No elements found matching <desc>``; a wait that timed
out puts ``NOT SATISFIED: ...`` first), and the body is always 0.11's tree
format. Every call goes through ``call_structured``, which validates the
structured content against the published schema and asserts the text equals
``render()`` of it; the lines are then checked against the 0.11 line format
rebuilt here from the 0.11 golden (``golden/tool_text_0_11.json``), never
against the renderer itself.

The only intended body difference from 0.11 is the placeholder for an empty
name: ``(unnamed)`` for an application and ``(untitled)`` for a top-level
(QA-ELEM-010); the 1.0 tags ``[ref=..., children=..., app=..., window=...]``
are appended to each line.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from _asserts import assert_render_roundtrip
from atspi_oracle import AtspiOracle
from mcp_harness import running_mcp_server

from kwin_mcp.models import ElementList, SessionInfo

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from mcp_harness import McpTestClient

E2E_DIR = Path(__file__).resolve().parent
GUI_PROBE = E2E_DIR / "gui_probe.py"
GOLDEN = E2E_DIR / "golden" / "tool_text_0_11.json"
SCREEN = {"screen_width": 1280, "screen_height": 800}
GTK_ENV = {
    "GDK_BACKEND": "wayland",
    "GTK_MODULES": "gail:atk-bridge",
    "NO_AT_BRIDGE": "0",
    "XDG_SESSION_TYPE": "wayland",
}
_TAGS = re.compile(
    r" \[ref=e\d+\.\d+(?:, children=\d+)?(?:, app=(?:'[^']*'|\"[^\"]*\"))?"
    r"(?:, window=(?:none|\{[0-9a-f-]+\}))?\]$"
)
_TREE_HEADER = re.compile(r"# Accessibility Tree \((\d+) elements\)")
_FOUND_HEADER = re.compile(r"Found (\d+) elements matching (.+):")
# One 0.11 tree line: two spaces per depth, role, name, states, then the extents.
_TREE_LINE = re.compile(
    r"(?P<indent>(?:  )*)- \[(?P<role>[^\]]+)\] \"(?P<name>[^\"]*)\" \((?P<states>[a-z, -]*)\) "
    r"@ (?:screen \(-?\d+, -?\d+, \d+x\d+\)|unavailable \([a-z-]+\))"
)
_NOT_SATISFIED = re.compile(
    r"NOT SATISFIED: Timeout after 200 ms \(until=present, elapsed (\d+) ms\)"
)


@pytest.fixture
def anyio_backend() -> str:
    """The MCP stdio client is exercised on asyncio."""
    return "asyncio"


@asynccontextmanager
async def _probe_session(client: McpTestClient) -> AsyncGenerator[SessionInfo]:
    """A virtual session running the GTK GUI probe, queryable over AT-SPI."""
    try:
        info = cast("SessionInfo", await client.call_model("session_start", SCREEN))
        await client.call_model(
            "app_launch", {"command": f"python3 {shlex.quote(str(GUI_PROBE))}", "env": GTK_ENV}
        )
        AtspiOracle(info.dbus_address).wait_for(
            lambda ns: any(n.name == "Hover Target" for n in ns), app="probe"
        )
        yield info
    finally:
        await client.call_result("session_stop")


async def _query(client: McpTestClient, arguments: dict[str, Any]) -> tuple[ElementList, str]:
    structured, text = await client.call_structured("element_query", arguments)
    return assert_render_roundtrip(ElementList, structured, text), text


def _legacy_line(element: dict[str, Any]) -> str:
    """One 0.11 ``format_tree`` line, with 1.0's empty-name placeholders."""
    depth = element["depth"]
    name = element["name"] or {0: "(unnamed)", 1: "(untitled)"}.get(depth, "")
    states = f" ({', '.join(element['states'])})" if element["states"] else ""
    rect = element["rect"]
    if rect is None:
        position = f"@ unavailable ({element['unavailable']})"
    else:
        position = f"@ screen ({rect['x']}, {rect['y']}, {rect['width']}x{rect['height']})"
    text = f" text={element['text']!r}" if element["text"] else ""
    value = ""
    if element["value"] is not None and element["value_max"] is not None:
        value = f" value={element['value']:g}/{element['value_max']:g}"
    actions = f" [actions: {', '.join(element['actions'])}]" if element["actions"] else ""
    head = f'{"  " * max(depth, 0)}- [{element["role"]}] "{name}"{states}'
    return f"{head} {position}{text}{value}{actions}"


def _assert_tree_body(lines: list[str], found: ElementList) -> None:
    """Each body line is the 0.11 tree line of its element followed by the 1.0 tags."""
    assert len(lines) == len(found.elements), (len(lines), len(found.elements))
    elements = found.model_dump(mode="json")["elements"]
    for line, element in zip(lines, elements, strict=True):
        tags = _TAGS.search(line)
        assert tags is not None, line
        assert line[: tags.start()] == _legacy_line(element), (line, element)
        assert f"ref={element['ref']}" in tags.group(0), line
        if element["states"]:
            assert f" ({', '.join(element['states'])}) @ " in line, line


def _numbers_masked(line: str) -> str:
    return re.sub(r"-?\d+", "N", line)


def _shown_name(role: str | None, name: str | None) -> str:
    """How the tree body shows an element name: an unnamed frame is "(untitled)"."""
    return name or ("(untitled)" if role == "frame" else "")


@pytest.mark.anyio
@pytest.mark.qa("QA-ELEM-012")
async def test_tree_header_for_empty_locator() -> None:
    async with running_mcp_server() as client, _probe_session(client):
        found, text = await _query(client, {})

        lines = text.splitlines()
        header = _TREE_HEADER.fullmatch(lines[0])
        assert header is not None, lines[0]
        assert int(header.group(1)) == len(found.elements) > 0
        assert lines[1] == ""
        _assert_tree_body(lines[2:], found)
        assert found.wait is None
        assert any("probe" in e.name.lower() for e in found.elements if e.depth == 0)


@pytest.mark.anyio
@pytest.mark.qa("QA-ELEM-012")
async def test_found_header_for_nonempty_locator_with_tree_body() -> None:
    async with running_mcp_server() as client, _probe_session(client) as info:
        found, text = await _query(client, {"locator": {"app": "probe"}})
        lines = text.splitlines()
        header = _FOUND_HEADER.fullmatch(lines[0])
        assert header is not None, lines[0]
        assert (int(header.group(1)), header.group(2)) == (len(found.elements), "app='probe'")
        assert lines[1] == ""
        _assert_tree_body(lines[2:], found)
        # The body is the tree: indentation follows depth, states are printed.
        assert {e.depth for e in found.elements} >= {0, 1, 2}
        assert any(e.states for e in found.elements)

        none, text = await _query(client, {"locator": {"query": "zz-none"}})
        assert none.elements == []
        assert text == "No elements found matching query='zz-none'"

        # The same body as 0.11's tree. The golden was recorded on Debian's KCalc/Qt;
        # other builds expose a different KCalc tree (more nodes, named frame/toolbar).
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        (legacy,) = [
            call["text"]
            for call in golden["calls"]
            if call["tool"] == "accessibility_tree"
            and call["arguments"] == {"app_name": "kcalc", "max_depth": 2}
        ]
        await client.call_model("app_launch", {"command": "kcalc"})
        await client.call_model(
            "element_query",
            {"locator": {"app": "kcalc", "query": "Seven"}, "wait": {"timeout_ms": 15000}},
        )
        kcalc, text = await _query(client, {"locator": {"app": "kcalc"}, "max_depth": 2})
        assert text.splitlines()[0] == f"Found {len(kcalc.elements)} elements matching app='kcalc':"
        body = [_TAGS.sub("", line) for line in text.splitlines()[2:]]
        legacy_lines = legacy.splitlines()
        nodes = AtspiOracle(info.dbus_address).walk(app="kcalc", max_depth=2)
        assert len(body) == len(kcalc.elements) == len(nodes), [(n.role, n.name) for n in nodes]
        # Every line has the 0.11 grammar, and the independent AT-SPI walk (same
        # pre-order) decides each line's depth, role and name.
        assert all(_TREE_LINE.fullmatch(line) for line in legacy_lines[2:]), legacy_lines
        for line, node in zip(body, nodes, strict=True):
            match = _TREE_LINE.fullmatch(line)
            assert match is not None, line
            shown = (len(match["indent"]) // 2, match["role"], match["name"])
            assert shown == (node.depth, node.role, _shown_name(node.role, node.name)), line
        legacy_roles = [m["role"] for m in map(_TREE_LINE.fullmatch, legacy_lines[2:]) if m]
        if [node.role for node in nodes] == legacy_roles:
            # The KCalc tree the golden recorded: the 0.11 text line for line, apart
            # from names newer Qt fills in, positions, and the run-dependent active state.
            assert legacy_lines[0] == f"# Accessibility Tree ({len(kcalc.elements)} elements)"
            expected = [
                line.replace('] "" (', f'] "{_shown_name(node.role, node.name)}" (', 1)
                for line, node in zip(legacy_lines[2:], nodes, strict=True)
            ]
            assert [_numbers_masked(line.replace("active, ", "")) for line in body] == [
                _numbers_masked(line.replace("active, ", "")) for line in expected
            ]


@pytest.mark.anyio
@pytest.mark.qa("QA-ELEM-012")
async def test_not_satisfied_first_line() -> None:
    wait_args = {"locator": {"query": "OK"}, "wait": {"until": "present", "timeout_ms": 200}}
    async with running_mcp_server() as client, _probe_session(client) as info:
        nodes = AtspiOracle(info.dbus_address).walk(max_depth=15)
        assert not any(
            "ok" in (value or "").lower()
            for n in nodes
            for value in (n.name, n.role, n.description)
        )
        found, text = await _query(client, wait_args)
        assert found.wait is not None and not found.wait.satisfied
        assert found.elements == []
        lines = text.splitlines()
        first = _NOT_SATISFIED.fullmatch(lines[0])
        assert first is not None, lines[0]
        assert int(first.group(1)) == found.wait.elapsed_ms >= 200
        assert lines[1:] == ["No elements found matching query='OK'"]

    # The CLI prints the same render() of the same calls.
    commands = "\n".join(
        [
            f"session_start {json.dumps(SCREEN)}",
            f"element_query {json.dumps({'locator': {'query': 'zz-none'}})}",
            f"element_query {json.dumps(wait_args)}",
            "session_stop",
            "",
        ]
    )
    cli = subprocess.run(
        [sys.executable, "-m", "kwin_mcp.cli"],
        input=commands,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert cli.returncode == 0, cli.stderr[-2000:]
    out = cli.stdout.splitlines()
    assert "No elements found matching query='zz-none'" in out, cli.stdout[-2000:]
    index = next(i for i, line in enumerate(out) if line.startswith("NOT SATISFIED:"))
    assert _NOT_SATISFIED.fullmatch(out[index]) is not None, out[index]
    assert out[index + 1] == "No elements found matching query='OK'", out[index : index + 2]
