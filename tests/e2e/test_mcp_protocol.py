"""Installed-package MCP protocol coverage over the real stdio transport."""

from __future__ import annotations

import importlib.metadata
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from mcp_harness import (
    ERAS,
    LEGACY_PROTOCOL_VERSION,
    MAX_LINE_BYTES,
    TOOL_NAMES,
    McpTestClient,
    ModernMcpTestClient,
    raw_mcp_client,
    running_client,
    running_mcp_server,
    text_of,
)
from probe_session import (
    BUTTON_KINDS,
    PROBE_APP,
    PROBE_COMMAND,
    of_kind,
    probe_env,
    read_events,
)
from session_harness import live_kwin

if TYPE_CHECKING:
    from mcp.types import CallToolResult, Tool

pytestmark = pytest.mark.anyio

Client = McpTestClient | ModernMcpTestClient
HERE = Path(__file__).parent
SNAPSHOT = HERE.parent / "unit" / "snapshots" / "tool_surface.json"
S_TOOLS: list[dict[str, Any]] = json.loads(
    (HERE.parent / "unit" / "fixtures" / "s_tools.json").read_text(encoding="utf-8")
)["tools"]
BAD_BINARY = "definitely-not-a-real-binary"
SESSION_ARGUMENTS = {"screen_width": 1280, "screen_height": 800}
APP_WAIT = {"timeout_ms": 15_000}
NO_SESSION = (
    "No session running.",
    "Call session_start (virtual) or session_connect (live desktop).",
)
HOLD_MS = 1500
# A locator nothing in the session matches.
NEVER = {"query": "No Such Element Anywhere"}
LIVE_SENTENCES = {
    "session_connect": "This server is configured for live-desktop use; prefer session_connect.",
    "session_start": (
        "This server is configured for live-desktop use; use session_start only when an "
        "isolated virtual session is asked for."
    ),
}
LIST_CHANGED = "notifications/tools/list_changed"


@pytest.fixture
def anyio_backend() -> str:
    """The MCP stdio client is exercised on asyncio."""
    return "asyncio"


def _tool_error_lines(name: str, result: CallToolResult) -> tuple[str, ...]:
    assert result.is_error, (name, result)
    assert result.structured_content is None, (name, result)
    text = text_of(result)
    prefix = f"Error executing tool {name}: "
    assert text.startswith(prefix), text
    return tuple(text.removeprefix(prefix).split("\n"))


def _listing(tools: list[Tool]) -> str:
    return json.dumps(
        [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools],
        sort_keys=True,
    )


async def _probe_session(client: Client, log_path: Path) -> dict[str, Any]:
    """A virtual session with EIS running the probe; returns the Keyboard Target element."""
    info, _ = await client.call_structured("session_start", SESSION_ARGUMENTS)
    assert info["input_backend"] == "eis", info
    await client.call_structured(
        "app_launch", {"command": PROBE_COMMAND, "env": probe_env(log_path)}
    )
    return await _keyboard_target(client)


async def _keyboard_target(client: Client) -> dict[str, Any]:
    found, _ = await client.call_structured(
        "element_query",
        {"locator": {"app": PROBE_APP, "query": "Keyboard Target"}, "wait": APP_WAIT},
    )
    assert found["wait"]["satisfied"], found
    (entry,) = [e for e in found["elements"] if e["name"] == "Keyboard Target"]
    return entry


# ── Listing and schemas ───────────────────────────────────────────────────


async def test_installed_server_initializes_and_registers_exact_tool_schemas() -> None:
    snapshot = {tool["name"]: tool for tool in json.loads(SNAPSHOT.read_text(encoding="utf-8"))}
    async with running_mcp_server() as client:
        assert await client.session.send_ping() is not None
        tools = await client.list_tools()

    names = [tool.name for tool in tools]
    assert len(names) == len(set(names)) == 29
    assert names == list(TOOL_NAMES) == list(snapshot)
    for tool in tools:
        schema = tool.input_schema
        expected = snapshot[tool.name]["inputSchema"]
        assert schema.get("type") == "object", (tool.name, schema)
        assert schema == expected, tool.name
        required = schema.get("required", [])
        assert len(required) == len(set(required)), (tool.name, required)
        assert set(required) <= set(schema.get("properties", {})), (tool.name, schema)


@pytest.mark.qa("QA-SURF-005")
async def test_installed_server_registers_exact_tool_schemas() -> None:
    """Property sets, required lists and defaults against S §11's independent table."""
    async with running_mcp_server() as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert list(tools) == [tool["name"] for tool in S_TOOLS]
    for expected in S_TOOLS:
        schema = tools[expected["name"]].input_schema
        properties = schema.get("properties", {})
        params = expected["params"]
        assert set(properties) == {p["name"] for p in params}, expected["name"]
        assert sorted(schema.get("required", [])) == sorted(
            p["name"] for p in params if p["required"]
        ), expected["name"]
        for param in params:
            published = properties[param["name"]]
            if param["required"]:
                assert "default" not in published, (expected["name"], param)
            elif param.get("default") is None:
                assert published.get("default") is None, (expected["name"], param)
            else:
                assert published.get("default") == param["default"], (expected["name"], param)
        assert "from_" not in json.dumps(schema), expected["name"]

    assert tools["pointer_hold"].input_schema["required"] == ["phase", "target"]
    assert tools["element_query"].input_schema["properties"]["locator"]["default"] == {}
    assert tools["session_start"].input_schema["properties"]["retention"]["default"] == {
        "artifacts": False,
        "home": False,
    }
    assert tools["wayland_list"].input_schema["properties"]["filter"]["default"] == ""
    assert tools["app_log"].input_schema["properties"]["last_n_lines"]["default"] == 50
    assert "from" in tools["pointer_drag"].input_schema["properties"]


@pytest.mark.parametrize(
    ("arguments", "invalid_field"),
    [
        pytest.param({"button": "left"}, "target", id="missing-required-parameter"),
        pytest.param({"target": {"x": "an integer", "y": 0}}, "x", id="malformed-type"),
    ],
)
async def test_invalid_tool_arguments_are_rejected_over_stdio_json_rpc(
    arguments: dict[str, object], invalid_field: str
) -> None:
    async with running_mcp_server() as client:
        result = await client.call_result("pointer_click", arguments)
        assert await client.session.send_ping() is not None

    assert result.is_error is True
    assert result.structured_content is None
    error_text = text_of(result)
    assert error_text, result.content
    field_token = rf"(?<![A-Za-z0-9_]){re.escape(invalid_field)}(?![A-Za-z0-9_])"
    assert re.search(field_token, error_text), error_text


async def test_no_active_session_tool_errors_are_isolated_and_server_survives() -> None:
    calls = (
        ("screen_capture", {"include_cursor": False}),
        ("pointer_move", {"target": {"x": 0, "y": 0}}),
    )
    async with running_mcp_server() as client:
        for tool_name, arguments in calls:
            result = await client.call_result(tool_name, arguments)
            assert _tool_error_lines(tool_name, result) == NO_SESSION
        assert await client.session.send_ping() is not None
        assert [tool.name for tool in await client.list_tools()] == list(TOOL_NAMES)


async def test_default_live_session_switches_session_tool_recommendations() -> None:
    async with running_mcp_server("--default-live-session") as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert len(tools) == 29
    assert frozenset(tools) == frozenset(TOOL_NAMES)
    connect_description = tools["session_connect"].description or ""
    start_description = tools["session_start"].description or ""
    assert connect_description.startswith(LIVE_SENTENCES["session_connect"] + " ")
    assert start_description.startswith(LIVE_SENTENCES["session_start"] + " ")
    assert "isolated" in start_description
    assert "virtual" in start_description


async def test_engine_exception_is_a_tool_error_and_server_survives() -> None:
    async with running_mcp_server() as client:
        await client.call_structured("session_start", SESSION_ARGUMENTS)
        try:
            result = await client.call_result("app_launch", {"command": BAD_BINARY})
            lines = _tool_error_lines("app_launch", result)
            assert BAD_BINARY in lines[0], lines
            tools_after_error = await client.list_tools()
            assert [tool.name for tool in tools_after_error] == list(TOOL_NAMES)
        finally:
            stopped, text = await client.call_structured("session_stop")

        # The failed launch kept the session, so the stop ended it.
        assert stopped["stopped"] is True, stopped
        assert text == "Session stopped."
        again, text = await client.call_structured("session_stop")
        assert again["stopped"] is False, again
        assert text == "No session running."


async def test_initialize_reports_kwin_mcp_server_name_and_package_version() -> None:
    async with running_mcp_server() as client:
        server_info = client.initialize_result.server_info

    assert server_info.name == "kwin-mcp"
    assert server_info.version == importlib.metadata.version("kwin-mcp")


@pytest.mark.qa("QA-SURF-007")
async def test_unbounded_params_accept_negative_values(tmp_path: Path) -> None:
    """S keeps no lower bound on these: the engine clamps, the call succeeds."""
    async with running_mcp_server() as client:
        entry = await _probe_session(client, tmp_path / "probe-events.jsonl")
        try:
            target = {"ref": entry["ref"]}
            calls: list[tuple[str, dict[str, Any]]] = [
                ("pointer_click", {"target": target, "hold_ms": -5}),
                (
                    "pointer_drag",
                    {"from": target, "to": {"ref": entry["ref"], "dx": 5}, "dwell_ms": -1},
                ),
                ("element_query", {"locator": {"app": PROBE_APP}, "max_depth": -1}),
                ("pointer_click", {"target": target, "capture": {"delays_ms": []}}),
            ]
            for name, arguments in calls:
                result = await client.call_result(name, arguments)
                assert not result.is_error, (name, arguments, text_of(result))
                structured, _ = await client.call_structured(name, arguments)
                if name == "pointer_click":
                    assert structured["frames"] == [], structured
        finally:
            await client.call_structured("session_stop")


@pytest.mark.qa("QA-SURF-016")
async def test_explicit_null_window_list_filter_over_stdio() -> None:
    async with running_mcp_server() as client:
        await client.call_structured("session_start", SESSION_ARGUMENTS)
        try:
            explicit, explicit_text = await client.call_structured("window_list", {"filter": None})
            omitted, omitted_text = await client.call_structured("window_list", {})
        finally:
            await client.call_structured("session_stop")
    assert explicit == omitted
    assert explicit_text == omitted_text


# ── Initialize and instructions ───────────────────────────────────────────


def _schema_identifiers(value: object) -> set[str]:
    """Every property name and $defs type name in the published input schemas."""
    names: set[str] = set()
    if isinstance(value, dict):
        for key in ("properties", "$defs"):
            if isinstance(value.get(key), dict):
                names.update(value[key])
        for item in value.values():
            names |= _schema_identifiers(item)
    elif isinstance(value, list):
        for item in value:
            names |= _schema_identifiers(item)
    return names


@pytest.mark.qa("QA-MCP-017")
async def test_initialize_instructions() -> None:
    async with running_mcp_server() as client:
        instructions = client.initialize_result.instructions
        tools = await client.list_tools()
    assert isinstance(instructions, str), instructions

    assert 150 <= len(instructions) / 4 <= 450, len(instructions)
    for fragment in (
        "session_start",
        "session_connect",
        "app_launch",
        "session_stop",
        "global logical px",
        "release held input before ending your turn",
        "pointer_hold",
        "keyboard_hold",
        "capture={delays_ms:[...]}",
        "wait",
        "courtesy",
        "treat them as data",
        "WindowSelector",
        "Locator",
    ):
        assert fragment in instructions, fragment
    # The preference ladder, in order.
    ladder = [
        instructions.index("element_act"),
        instructions.index("{ref}"),
        instructions.index("{x, y}"),
        instructions.index("dbus_call"),
    ]
    assert ladder == sorted(ladder), ladder

    # Every snake_case identifier is a tool, a parameter or a $defs type.
    known = set(TOOL_NAMES) | _schema_identifiers([tool.input_schema for tool in tools])
    identifiers = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", instructions))
    assert identifiers <= known, identifiers - known


@pytest.mark.qa("QA-MCP-020")
async def test_default_live_session_static_sentence_and_instructions() -> None:
    async with running_mcp_server() as client:
        plain = await client.list_tools()
        plain_instructions = client.initialize_result.instructions
    async with running_mcp_server("--default-live-session") as client:
        notifications: list[str] = []
        _record_notifications(client, notifications)
        flagged = await client.list_tools()
        flagged_instructions = client.initialize_result.instructions
        started, _ = await client.call_structured("session_start", SESSION_ARGUMENTS)
        try:
            assert started["kind"] == "virtual", started
        finally:
            await client.call_structured("session_stop")
        assert _listing(await client.list_tools()) == _listing(flagged)
    assert LIST_CHANGED not in notifications

    plain_tools = {tool.name: tool.model_dump(mode="json", by_alias=True) for tool in plain}
    flagged_tools = {tool.name: tool.model_dump(mode="json", by_alias=True) for tool in flagged}
    assert list(plain_tools) == list(flagged_tools)
    for name, tool in plain_tools.items():
        changed = flagged_tools[name]
        if name in LIVE_SENTENCES:
            assert changed["description"] == f"{LIVE_SENTENCES[name]} {tool['description']}"
            changed = {**changed, "description": tool["description"]}
        assert changed == tool, name

    assert isinstance(plain_instructions, str)
    assert isinstance(flagged_instructions, str)
    assert flagged_instructions.startswith(plain_instructions + "\n"), flagged_instructions
    paragraph = flagged_instructions.removeprefix(plain_instructions + "\n")
    assert "session_connect" in paragraph, paragraph
    assert "isolated virtual session" in paragraph, paragraph


def _record_notifications(client: McpTestClient, sink: list[str]) -> None:
    """Record the method of every server notification the legacy session receives."""
    session = client.session
    forward = session._message_handler

    async def record(message: Any) -> None:
        root = getattr(message, "root", message)
        method = getattr(root, "method", None)
        if isinstance(method, str):
            sink.append(method)
        await forward(message)

    session._message_handler = record


# ── Static listing over a lifecycle ───────────────────────────────────────


@pytest.mark.qa("QA-SURF-021")
@pytest.mark.parametrize("era", ERAS)
async def test_static_listing_no_list_changed(era: str) -> None:
    notifications: list[str] = []
    events: list[object] = []
    with live_kwin() as live:
        async with running_client(era) as client, anyio.create_task_group() as group:
            if isinstance(client, McpTestClient):
                capabilities = client.initialize_result.capabilities
                _record_notifications(client, notifications)
            else:
                capabilities = client.client.server_capabilities

                async def listen() -> None:
                    async with client.client.listen(tools_list_changed=True) as stream:
                        async for event in stream:
                            events.append(event)

                group.start_soon(listen)
            # listChanged is false or absent (the modern era may advertise no tools entry).
            if capabilities.tools is not None:
                assert not capabilities.tools.list_changed, capabilities.tools
            if isinstance(client, McpTestClient):
                assert capabilities.tools is not None

            reference = _listing(await client.list_tools())
            listings: list[str] = []

            async def step(name: str, arguments: dict[str, Any] | None = None) -> None:
                await client.call_result(name, arguments or {})
                listings.append(_listing(await client.list_tools()))

            await step("session_start", SESSION_ARGUMENTS)
            await step("app_launch", {"command": "true"})
            clipboard = await client.call_result("clipboard_read")
            assert clipboard.is_error, text_of(clipboard)
            listings.append(_listing(await client.list_tools()))
            await step("session_stop")
            await step(
                "session_connect",
                {"dbus_address": live.dbus_address, "wayland_display": live.wayland_display},
            )
            await step("session_stop")
            await anyio.sleep(0.5)
            group.cancel_scope.cancel()

    assert listings == [reference] * 6
    names = [tool["name"] for tool in json.loads(reference)]
    assert "clipboard_read" in names
    assert "clipboard_write" in names
    assert LIST_CHANGED not in notifications
    assert events == []


# ── Serialization on the tool thread ──────────────────────────────────────


@pytest.mark.qa("QA-MCP-024")
async def test_concurrent_tool_calls_are_serialized(tmp_path: Path) -> None:
    """Blocking tool calls issued concurrently must run one after another, in order.

    pointer_click with hold_ms sleeps inside the input backend without taking any
    engine lock, so overlapping calls only serialize if the server itself does.
    """
    log_path = tmp_path / "probe-events.jsonl"
    async with running_mcp_server() as client:
        entry = await _probe_session(client, log_path)
        try:
            target = {"ref": entry["ref"]}
            # Focus the entry, then let the probe go quiet.
            await client.call_structured("pointer_click", {"target": target})
            await anyio.sleep(0.6)
            mark = len(read_events(log_path))

            outputs: list[str] = []

            async def click() -> None:
                _, text = await client.call_structured(
                    "pointer_click", {"target": target, "hold_ms": HOLD_MS}
                )
                outputs.append(text)

            started = anyio.current_time()
            with anyio.fail_after(30):
                async with anyio.create_task_group() as group:
                    group.start_soon(click)
                    await anyio.sleep(0.005)
                    group.start_soon(click)
            elapsed = anyio.current_time() - started
            await anyio.sleep(0.5)

            assert len(outputs) == 2
            assert all(text.startswith("Clicked left at ") for text in outputs), outputs
            assert all(f"held {HOLD_MS}ms" in text.splitlines()[0] for text in outputs), outputs
            buttons = of_kind(read_events(log_path)[mark:], *BUTTON_KINDS)
            assert [e["kind"] for e in buttons] == [
                "button_press",
                "button_release",
                "button_press",
                "button_release",
            ], buttons
            minimum = 2 * HOLD_MS / 1000
            assert elapsed >= minimum * 0.9, elapsed
            assert elapsed <= 3 * minimum, elapsed

            async def type_text(text: str) -> None:
                await client.call_structured("keyboard_type", {"text": text})

            with anyio.fail_after(30):
                async with anyio.create_task_group() as group:
                    group.start_soon(type_text, "aaaa")
                    await anyio.sleep(0.005)
                    group.start_soon(type_text, "bbbb")
            await anyio.sleep(0.5)
            assert (await _keyboard_target(client))["text"] == "aaaabbbb"

            finished: dict[str, float] = {}

            async def wait_never() -> None:
                result = await client.call_result(
                    "element_query", {"locator": NEVER, "wait": {"timeout_ms": 3000}}
                )
                assert not result.is_error, text_of(result)
                assert text_of(result).startswith("NOT SATISFIED:"), text_of(result)
                finished["wait"] = time.monotonic()

            async def capture() -> None:
                # Its outcome does not matter here (the container's virtual KWin may
                # not answer ScreenShot2); it must not run before the wait ends.
                await client.call_result("screen_capture")
                finished["capture"] = time.monotonic()

            submitted = time.monotonic()
            with anyio.fail_after(120):
                async with anyio.create_task_group() as group:
                    group.start_soon(wait_never)
                    await anyio.sleep(0.005)
                    group.start_soon(capture)
            assert finished["wait"] - submitted >= 3.0 * 0.9, finished
            assert finished["capture"] >= finished["wait"], finished
        finally:
            await client.call_structured("session_stop")


@pytest.mark.qa("QA-MCP-025")
async def test_stop_queued_between_calls(tmp_path: Path) -> None:
    async with running_mcp_server() as client:
        await _probe_session(client, tmp_path / "probe-events.jsonl")
        results: dict[str, CallToolResult] = {}
        finished: dict[str, float] = {}

        async def call(key: str, name: str, arguments: dict[str, Any]) -> None:
            results[key] = await client.call_result(name, arguments)
            finished[key] = time.monotonic()

        with anyio.fail_after(60):
            async with anyio.create_task_group() as group:
                group.start_soon(
                    call, "A", "element_query", {"locator": NEVER, "wait": {"timeout_ms": 2000}}
                )
                await anyio.sleep(0.2)
                group.start_soon(call, "B", "session_stop", {})
                await anyio.sleep(0.2)
                group.start_soon(call, "C", "keyboard_press", {"keys": "a"})
                await anyio.sleep(0.2)
                group.start_soon(
                    call, "D", "pointer_hold", {"phase": "press", "target": {"x": 5, "y": 5}}
                )

        a = results["A"]
        assert not a.is_error, text_of(a)
        assert a.structured_content is not None
        assert a.structured_content["wait"]["satisfied"] is False, a.structured_content
        assert text_of(a).startswith("NOT SATISFIED:")
        b = results["B"]
        assert not b.is_error, text_of(b)
        assert b.structured_content is not None
        assert b.structured_content["stopped"] is True, b.structured_content
        assert _tool_error_lines("keyboard_press", results["C"]) == NO_SESSION
        assert _tool_error_lines("pointer_hold", results["D"]) == NO_SESSION
        assert finished["A"] <= finished["B"] <= finished["C"] <= finished["D"], finished

        await client.call_structured("session_start", SESSION_ARGUMENTS)
        try:
            info, _ = await client.call_structured("session_info")
            assert info["held"] == {"buttons": [], "keys": []}, info
        finally:
            await client.call_structured("session_stop")


# ── Cancellation ──────────────────────────────────────────────────────────


class _Wire:
    """Raw send/receive on a ``RawJsonRpcClient``'s streams, keeping every message."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.messages: list[tuple[float, dict[str, Any]]] = []

    async def send(self, message: dict[str, Any]) -> None:
        await self._client._send(message)

    async def receive(self) -> dict[str, Any]:
        line = await self._client._stdout.receive_until(b"\n", MAX_LINE_BYTES)
        message = json.loads(line)
        self.messages.append((time.monotonic(), message))
        return message

    async def until_response(self, request_id: int) -> dict[str, Any]:
        while True:
            message = await self.receive()
            if "method" in message and "id" in message:
                await self.send(
                    {"jsonrpc": "2.0", "id": message["id"], "result": {}}
                    if message["method"] == "ping"
                    else {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                )
            elif message.get("id") == request_id:
                return message


@pytest.mark.qa("QA-MCP-027")
@pytest.mark.slow
@pytest.mark.timeout(600)
async def test_client_cancellation_leaves_server_healthy() -> None:
    async with raw_mcp_client({}, era=LEGACY_PROTOCOL_VERSION) as client:
        wire = _Wire(client)
        await client.call_text("session_start", SESSION_ARGUMENTS)
        try:
            wait_id, info_id = 9001, 9002
            submitted = time.monotonic()
            await wire.send(
                {
                    "jsonrpc": "2.0",
                    "id": wait_id,
                    "method": "tools/call",
                    "params": {
                        "name": "element_query",
                        "arguments": {"locator": NEVER, "wait": {"timeout_ms": 3000}},
                        "_meta": {"progressToken": "cancelled-wait"},
                    },
                }
            )
            await anyio.sleep(0.5)
            await wire.send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": wait_id, "reason": "test cancels the wait"},
                }
            )
            cancelled_at = time.monotonic()
            await wire.send(
                {
                    "jsonrpc": "2.0",
                    "id": info_id,
                    "method": "tools/call",
                    "params": {"name": "session_info", "arguments": {}},
                }
            )
            with anyio.fail_after(60):
                info = await wire.until_response(info_id)
            answered = time.monotonic()
            assert "error" not in info, info
            assert info["result"]["isError"] is False, info
            assert info["result"]["structuredContent"]["session"] is not None, info
            # session_info ran on the tool thread after the wait body finished.
            assert answered - submitted >= 3.0 * 0.9, answered - submitted
            assert answered - submitted <= 3 * 3.0, answered - submitted

            # Anything still in flight arrives within a second; nothing more may.
            with anyio.move_on_after(1.0):
                while True:
                    await wire.receive()

            for at, message in wire.messages:
                if message.get("id") == wait_id:
                    assert "error" in message, message
                if message.get("method") == "notifications/progress":
                    assert message["params"]["progressToken"] == "cancelled-wait", message
                    assert at <= cancelled_at + 0.5, (at - cancelled_at, message)
            # The server is healthy: a fresh call answers normally.
            assert (await client.call_text("session_info")).startswith("Session: virtual.")
        finally:
            await client.call_text("session_stop")
