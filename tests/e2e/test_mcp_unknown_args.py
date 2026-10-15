"""Strict inputs over the installed server: unknown keys, nested extras, wrong types, refs.

Every rejected call must be a tool error (``isError``) that names the offending
key or field, must leave the server healthy, and must have no side effect: no
session is started, and the interaction probe's raw event log does not grow.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from mcp_harness import TOOL_NAMES, McpTestClient, running_mcp_server
from probe_session import PROBE_APP, PROBE_COMMAND, probe_env, read_events
from test_mcp_errors import MINIMAL_ARGUMENTS, POINT

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.anyio

SNAPSHOT = Path(__file__).parents[1] / "unit" / "snapshots" / "tool_surface.json"
SDK_PREFIX = "Error executing tool {name}: "
APP_WAIT = {"timeout_ms": 15_000}
QUIET_SECONDS = 0.5

# (tool, arguments, unknown key, remedy line): removed 0.11 arguments name their
# replacement (S §3.5 / §5.2), anything else the closest property or the valid list.
UNKNOWN_KEY_CASES: list[tuple[str, dict[str, Any], str, str]] = [
    (
        "session_start",
        {"app_command": "kcalc"},
        "app_command",
        "Call app_launch(command) after session_start instead.",
    ),
    (
        "session_start",
        {"keep_screenshots": True},
        "keep_screenshots",
        "Use retention={artifacts:true} instead.",
    ),
    ("pointer_click", {"x": 10, "y": 10}, "x", "Use target={x,y} instead."),
    (
        "screen_capture",
        {"screenshot_after_ms": [100]},
        "screenshot_after_ms",
        "Use capture={delays_ms:[...]} instead.",
    ),
    (
        "element_act",
        {"element": {"ref": "e1.1"}, "action": "Press", "window_id": "{x}"},
        "window_id",
        "Use window={id} instead.",
    ),
    (
        "pointer_scroll",
        {"target": POINT, "delta": 1, "discrete": True},
        "discrete",
        "Use unit='detent' instead.",
    ),
    (
        "pointer_scroll",
        {"target": POINT, "delta": 1, "horizontal": True},
        "horizontal",
        "Use axis='horizontal' instead.",
    ),
    (
        "keyboard_type",
        {"text": "a", "unicode": True},
        "unicode",
        "Valid arguments: text, method, capture.",
    ),
    (
        "pointer_click",
        {"target": POINT, "modifers": "ctrl"},
        "modifers",
        "Did you mean 'modifiers'?",
    ),
]

# (tool, arguments, token the validation error must name): nested models forbid
# extra keys, and a Target / ElementAddress matches exactly one arm.
NESTED_CASES: list[tuple[str, dict[str, Any], str]] = [
    ("pointer_click", {"target": {"x": 1, "y": 2, "ref": "e1.2"}}, "ref"),
    ("pointer_click", {"target": {"x": 10}}, "y"),
    ("pointer_click", {"target": {}}, "target"),
    (
        "element_act",
        {"element": {"ref": "e1.1", "strict": {"query": "x"}}, "action": "Press"},
        "strict",
    ),
    ("element_query", {"locator": {"app": "x", "bogus": 1}}, "bogus"),
    ("element_query", {"wait": {"timeout_ms": 1, "bogus": 1}}, "bogus"),
    ("pointer_click", {"target": POINT, "capture": {"delays_ms": [0], "foo": 1}}, "foo"),
    ("window_focus", {"window": {"app_id": "x", "name": "y"}}, "name"),
    ("window_list", {"filter": {"app_id": "x", "bogus": 1}}, "bogus"),
    ("pointer_drag", {"from": POINT, "to": POINT, "waypoints": [{"x": 1, "y": 1, "z": 1}]}, "z"),
]

# A well-formed ref of a worker generation this session never minted, so it can
# never name a live element (and nothing can be clicked by accident).
STALE_REF = "e999.2"

MALFORMED_REFS = (
    "",
    "e",
    "e1",
    "e1.",
    ".1",
    "e-1.2",
    "e1.-2",
    "E1.2",
    "e1.2 ",
    "e1.2\u0000",
    "e9999999999999999999.1",
)


@pytest.fixture
def anyio_backend() -> str:
    """The installed MCP client runs on asyncio."""
    return "asyncio"


def _field_token(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")


def _without_prefix(name: str, lines: tuple[str, ...]) -> tuple[str, ...]:
    prefix = SDK_PREFIX.format(name=name)
    assert lines[0].startswith(prefix), lines
    return (lines[0].removeprefix(prefix), *lines[1:])


def _kwin_processes() -> int:
    """KWin compositors running in this container (process census)."""
    count = 0
    for comm in Path("/proc").glob("[0-9]*/comm"):
        try:
            if comm.read_text(encoding="utf-8").strip() == "kwin_wayland":
                count += 1
        except OSError:
            continue
    return count


async def _assert_no_session(client: McpTestClient) -> None:
    structured, _ = await client.call_structured("session_info")
    assert structured["session"] is None, structured


async def _probe_session(client: McpTestClient, log_path: Path) -> None:
    """A 1280x800 virtual session with EIS input running the interaction probe."""
    info, _ = await client.call_structured(
        "session_start", {"screen_width": 1280, "screen_height": 800}
    )
    assert info["input_backend"] == "eis", info
    await client.call_structured(
        "app_launch", {"command": PROBE_COMMAND, "env": probe_env(log_path)}
    )
    found, _ = await client.call_structured(
        "element_query",
        {"locator": {"app": PROBE_APP, "query": "Keyboard Target"}, "wait": APP_WAIT},
    )
    assert found["wait"]["satisfied"], found


async def _settled_events(log_path: Path) -> list[dict[str, Any]]:
    """The probe's event log once it stops growing."""
    events = read_events(log_path)
    while True:
        await anyio.sleep(QUIET_SECONDS)
        again = read_events(log_path)
        if len(again) == len(events):
            return again
        events = again


def _input_schema_objects(schema: object, where: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every object schema (one with ``properties``) inside an input schema."""
    if isinstance(schema, dict):
        if "properties" in schema:
            yield where, schema
        for key, value in schema.items():
            yield from _input_schema_objects(value, f"{where}.{key}")
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            yield from _input_schema_objects(value, f"{where}[{index}]")


def _type_cases() -> Iterator[tuple[str, dict[str, Any], str]]:
    """Wrong JSON types and missing/null required fields for each of the 29 tools."""
    snapshot = {tool["name"]: tool for tool in json.loads(SNAPSHOT.read_text(encoding="utf-8"))}
    assert list(snapshot) == list(TOOL_NAMES)
    for name in TOOL_NAMES:
        schema = snapshot[name]["inputSchema"]
        base = MINIMAL_ARGUMENTS[name]
        for field in schema.get("required", []):
            yield name, {k: v for k, v in base.items() if k != field}, field
            yield name, {**base, field: None}, field
        for field, prop in schema.get("properties", {}).items():
            if prop.get("type") == "integer":
                yield name, {**base, field: "abc"}, field
                yield name, {**base, field: True}, field
            elif prop.get("type") == "boolean":
                yield name, {**base, field: 2}, field
    # The rows QA-GATE-012 names explicitly.
    yield "pointer_click", {"target": {"x": "zero", "y": 0}}, "x"
    yield "keyboard_hold", {"phase": "hold", "keys": "a"}, "phase"
    yield "element_act", {"element": {}, "action": "Press"}, "element"
    yield "pointer_click", {"target": POINT, "count": 4}, "count"


@pytest.mark.qa("QA-GATE-009")
async def test_unknown_keys_are_tool_errors_with_migration_remedy(tmp_path: Path) -> None:
    log_path = tmp_path / "probe-events.jsonl"
    async with running_mcp_server() as client:
        # Without a session: the check runs before any gate, and starts nothing.
        for tool, arguments, key, remedy in UNKNOWN_KEY_CASES:
            lines = await client.call_tool_error(tool, arguments)
            assert _without_prefix(tool, lines) == (
                f"Unknown argument '{key}' for {tool}.",
                remedy,
            )
        await _assert_no_session(client)

        # With a session: the input tools inject nothing, and no app is launched.
        try:
            await _probe_session(client, log_path)
            before = await _settled_events(log_path)
            for tool, arguments, key, remedy in UNKNOWN_KEY_CASES:
                if tool == "session_start":
                    continue
                lines = await client.call_tool_error(tool, arguments)
                assert _without_prefix(tool, lines) == (
                    f"Unknown argument '{key}' for {tool}.",
                    remedy,
                )
            assert await _settled_events(log_path) == before
            info, _ = await client.call_structured("session_info")
            assert [app["command"] for app in info["apps"]] == [PROBE_COMMAND], info
        finally:
            await client.call_structured("session_stop")


@pytest.mark.qa("QA-GATE-010")
async def test_nested_extra_keys_rejected(tmp_path: Path) -> None:
    log_path = tmp_path / "probe-events.jsonl"
    async with running_mcp_server() as client:
        for tool in await client.list_tools():
            objects = list(_input_schema_objects(tool.input_schema, tool.name))
            assert objects, tool.name
            for where, schema in objects:
                assert schema.get("additionalProperties") is False, (where, schema)

        try:
            await _probe_session(client, log_path)
            before = await _settled_events(log_path)
            for tool, arguments, token in NESTED_CASES:
                lines = await client.call_tool_error(tool, arguments, lines=None)
                text = "\n".join(_without_prefix(tool, lines))
                assert "validation error" in text, (tool, arguments, text)
                assert _field_token(token).search(text), (tool, arguments, token, text)
            # {x, y, ref} is not silently taken as the point arm: nothing was clicked.
            assert await _settled_events(log_path) == before
        finally:
            await client.call_structured("session_stop")


@pytest.mark.qa("QA-GATE-012")
async def test_invalid_types_and_missing_required_parametrized() -> None:
    cases = list(_type_cases())
    assert {name for name, _, _ in cases} >= {
        name for name in TOOL_NAMES if MINIMAL_ARGUMENTS[name] or name == "session_start"
    }
    kwin_before = _kwin_processes()
    async with running_mcp_server() as client:
        failures: list[str] = []
        for tool, arguments, field in cases:
            result = await client.call_result(tool, arguments)
            text = "\n".join(c.text for c in result.content if c.type == "text")
            if not result.is_error:
                failures.append(f"{tool} {arguments!r} succeeded: {text!r}")
                if tool == "session_start":
                    await client.call_structured("session_stop")
                continue
            assert result.structured_content is None, (tool, arguments)
            if not _field_token(field).search(text):
                failures.append(f"{tool} {arguments!r} does not name {field!r}: {text!r}")
        assert failures == []
        await _assert_no_session(client)
        assert await client.session.send_ping() is not None
        assert [tool.name for tool in await client.list_tools()] == list(TOOL_NAMES)
    assert _kwin_processes() == kwin_before


@pytest.mark.qa("QA-ELEM-039")
async def test_malformed_ref_strings(tmp_path: Path) -> None:
    log_path = tmp_path / "probe-events.jsonl"
    async with running_mcp_server() as client:
        try:
            await _probe_session(client, log_path)
            before = await _settled_events(log_path)
            for ref in MALFORMED_REFS:
                lines = await client.call_tool_error(
                    "element_act", {"element": {"ref": ref}, "action": "Press"}, lines=None
                )
                reason = _without_prefix("element_act", lines)
                assert 1 <= len(reason) <= 2, (ref, reason)
                assert reason[0].startswith(("Malformed ref ", "Stale ref ")), (ref, reason)
                assert "element_query" in "\n".join(reason), (ref, reason)
            lines = await client.call_tool_error(
                "pointer_click", {"target": {"ref": STALE_REF, "dx": 1}}, lines=None
            )
            reason = _without_prefix("pointer_click", lines)
            assert 1 <= len(reason) <= 2, reason
            assert reason[0].startswith(f"Stale ref {STALE_REF} "), reason
            assert "element_query" in "\n".join(reason), reason

            assert await _settled_events(log_path) == before
            info, _ = await client.call_structured("session_info")
            assert info["session"] is not None, info
        finally:
            await client.call_structured("session_stop")
