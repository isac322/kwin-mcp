"""Installed-package MCP protocol coverage over the real stdio transport."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from mcp.types import TextContent
from mcp_harness import EXPECTED_TOOL_NAMES, running_mcp_server

if TYPE_CHECKING:
    from typing import Any

    from mcp.types import CallToolResult

BAD_BINARY = "definitely-not-a-real-binary"
SESSION_REQUIRED_GUIDANCE = "Call session_start or session_connect first."
NO_DEFAULT = object()
ARRAY_OF_STRINGS = ("array", "string")
ARRAY_OF_INTEGERS = ("array", "integer")
ARRAY_OF_INTEGER_ARRAYS = ("array", ARRAY_OF_INTEGERS)
STRING_OBJECT = ("object", "string")
NULLABLE_ARRAY_OF_STRINGS = ("nullable", ARRAY_OF_STRINGS)
NULLABLE_ARRAY_OF_INTEGERS = ("nullable", ARRAY_OF_INTEGERS)
NULLABLE_ARRAY_OF_INTEGER_ARRAYS = ("nullable", ARRAY_OF_INTEGER_ARRAYS)
NULLABLE_STRING_OBJECT = ("nullable", STRING_OBJECT)
# dbus_call args entries are dbus-send strings or typed-JSON objects.
STRING_OR_OBJECT = ("anyOf", ("string", "object"))
NULLABLE_ARRAY_OF_STRINGS_OR_OBJECTS = ("nullable", ("array", STRING_OR_OBJECT))

EXPECTED_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "accessibility_tree": frozenset(),
    "active_window": frozenset(),
    "clipboard_get": frozenset(),
    "clipboard_set": frozenset({"text"}),
    "dbus_call": frozenset({"service", "path", "interface", "method"}),
    "find_ui_elements": frozenset({"query"}),
    "focus_window": frozenset({"app_name"}),
    "keyboard_key": frozenset({"key"}),
    "keyboard_key_down": frozenset({"key"}),
    "keyboard_key_up": frozenset({"key"}),
    "keyboard_type": frozenset({"text"}),
    "keyboard_type_unicode": frozenset({"text"}),
    "launch_app": frozenset({"command"}),
    "list_windows": frozenset(),
    "mouse_button_down": frozenset({"x", "y"}),
    "mouse_button_up": frozenset({"x", "y"}),
    "mouse_click": frozenset({"x", "y"}),
    "mouse_drag": frozenset({"from_x", "from_y", "to_x", "to_y"}),
    "mouse_move": frozenset({"x", "y"}),
    "mouse_scroll": frozenset({"x", "y", "delta"}),
    "read_app_log": frozenset({"pid"}),
    "screenshot": frozenset(),
    "session_connect": frozenset(),
    "session_start": frozenset(),
    "session_stop": frozenset(),
    "touch_multi_swipe": frozenset({"from_x", "from_y", "to_x", "to_y"}),
    "touch_pinch": frozenset({"center_x", "center_y", "start_distance", "end_distance"}),
    "touch_swipe": frozenset({"from_x", "from_y", "to_x", "to_y"}),
    "touch_tap": frozenset({"x", "y"}),
    "wait_for_element": frozenset({"query"}),
    "wayland_info": frozenset(),
    "window_close": frozenset({"window_id"}),
    "window_geometry": frozenset(),
}

EXPECTED_TOOL_PROPERTIES: dict[str, dict[str, tuple[object, object]]] = {
    "accessibility_tree": {
        "app_name": ("string", ""),
        "max_depth": ("integer", 15),
        "role": ("string", ""),
    },
    "active_window": {},
    "clipboard_get": {},
    "clipboard_set": {"text": ("string", NO_DEFAULT)},
    "dbus_call": {
        "service": ("string", NO_DEFAULT),
        "path": ("string", NO_DEFAULT),
        "interface": ("string", NO_DEFAULT),
        "method": ("string", NO_DEFAULT),
        "args": (NULLABLE_ARRAY_OF_STRINGS_OR_OBJECTS, None),
    },
    "find_ui_elements": {
        "query": ("string", NO_DEFAULT),
        "app_name": ("string", ""),
        "states": (NULLABLE_ARRAY_OF_STRINGS, None),
    },
    "focus_window": {"app_name": ("string", NO_DEFAULT)},
    "keyboard_key": {
        "key": ("string", NO_DEFAULT),
        "screenshot_after_ms": (NULLABLE_ARRAY_OF_INTEGERS, None),
    },
    "keyboard_key_down": {"key": ("string", NO_DEFAULT)},
    "keyboard_key_up": {"key": ("string", NO_DEFAULT)},
    "keyboard_type": {
        "text": ("string", NO_DEFAULT),
        "screenshot_after_ms": (NULLABLE_ARRAY_OF_INTEGERS, None),
    },
    "keyboard_type_unicode": {
        "text": ("string", NO_DEFAULT),
        "screenshot_after_ms": (NULLABLE_ARRAY_OF_INTEGERS, None),
    },
    "launch_app": {
        "command": ("string", NO_DEFAULT),
        "env": (NULLABLE_STRING_OBJECT, None),
    },
    "list_windows": {},
    "mouse_button_down": {
        "x": ("integer", NO_DEFAULT),
        "y": ("integer", NO_DEFAULT),
        "button": ("string", "left"),
    },
    "mouse_button_up": {
        "x": ("integer", NO_DEFAULT),
        "y": ("integer", NO_DEFAULT),
        "button": ("string", "left"),
    },
    "mouse_click": {
        "x": ("integer", NO_DEFAULT),
        "y": ("integer", NO_DEFAULT),
        "button": ("string", "left"),
        "double": ("boolean", False),
        "triple": ("boolean", False),
        "modifiers": (NULLABLE_ARRAY_OF_STRINGS, None),
        "hold_ms": ("integer", 0),
        "screenshot_after_ms": (NULLABLE_ARRAY_OF_INTEGERS, None),
    },
    "mouse_drag": {
        "from_x": ("integer", NO_DEFAULT),
        "from_y": ("integer", NO_DEFAULT),
        "to_x": ("integer", NO_DEFAULT),
        "to_y": ("integer", NO_DEFAULT),
        "button": ("string", "left"),
        "modifiers": (NULLABLE_ARRAY_OF_STRINGS, None),
        "waypoints": (NULLABLE_ARRAY_OF_INTEGER_ARRAYS, None),
        "screenshot_after_ms": (NULLABLE_ARRAY_OF_INTEGERS, None),
    },
    "mouse_move": {
        "x": ("integer", NO_DEFAULT),
        "y": ("integer", NO_DEFAULT),
        "screenshot_after_ms": (NULLABLE_ARRAY_OF_INTEGERS, None),
    },
    "mouse_scroll": {
        "x": ("integer", NO_DEFAULT),
        "y": ("integer", NO_DEFAULT),
        "delta": ("integer", NO_DEFAULT),
        "horizontal": ("boolean", False),
        "discrete": ("boolean", False),
        "steps": ("integer", 1),
    },
    "read_app_log": {
        "pid": ("integer", NO_DEFAULT),
        "last_n_lines": ("integer", 50),
    },
    "screenshot": {"include_cursor": ("boolean", False)},
    "session_connect": {
        "dbus_address": ("string", ""),
        "wayland_display": ("string", ""),
        "keep_screenshots": ("boolean", False),
    },
    "session_start": {
        "app_command": ("string", ""),
        "screen_width": ("integer", 1920),
        "screen_height": ("integer", 1080),
        "enable_clipboard": ("boolean", False),
        "keep_screenshots": ("boolean", False),
        "isolate_home": ("boolean", False),
        "keep_home": ("boolean", False),
        "env": (NULLABLE_STRING_OBJECT, None),
    },
    "session_stop": {},
    "touch_multi_swipe": {
        "from_x": ("integer", NO_DEFAULT),
        "from_y": ("integer", NO_DEFAULT),
        "to_x": ("integer", NO_DEFAULT),
        "to_y": ("integer", NO_DEFAULT),
        "fingers": ("integer", 3),
        "duration_ms": ("integer", 300),
        "screenshot_after_ms": (NULLABLE_ARRAY_OF_INTEGERS, None),
    },
    "touch_pinch": {
        "center_x": ("integer", NO_DEFAULT),
        "center_y": ("integer", NO_DEFAULT),
        "start_distance": ("integer", NO_DEFAULT),
        "end_distance": ("integer", NO_DEFAULT),
        "duration_ms": ("integer", 500),
        "screenshot_after_ms": (NULLABLE_ARRAY_OF_INTEGERS, None),
    },
    "touch_swipe": {
        "from_x": ("integer", NO_DEFAULT),
        "from_y": ("integer", NO_DEFAULT),
        "to_x": ("integer", NO_DEFAULT),
        "to_y": ("integer", NO_DEFAULT),
        "duration_ms": ("integer", 300),
        "screenshot_after_ms": (NULLABLE_ARRAY_OF_INTEGERS, None),
    },
    "touch_tap": {
        "x": ("integer", NO_DEFAULT),
        "y": ("integer", NO_DEFAULT),
        "hold_ms": ("integer", 0),
        "screenshot_after_ms": (NULLABLE_ARRAY_OF_INTEGERS, None),
    },
    "wait_for_element": {
        "query": ("string", NO_DEFAULT),
        "app_name": ("string", ""),
        "timeout_ms": ("integer", 5000),
        "poll_interval_ms": ("integer", 200),
        "expected_states": (NULLABLE_ARRAY_OF_STRINGS, None),
    },
    "wayland_info": {"filter_protocol": ("string", "")},
    "window_close": {"window_id": ("string", NO_DEFAULT)},
    "window_geometry": {"app_name": ("string", ""), "window_id": ("string", "")},
}


def _assert_json_type(
    property_schema: dict[str, Any],
    expected_type: object,
    context: tuple[str, str],
) -> None:
    if isinstance(expected_type, str):
        assert property_schema.get("type") == expected_type, (context, property_schema)
        assert "anyOf" not in property_schema, (context, property_schema)
        return

    assert isinstance(expected_type, tuple) and len(expected_type) == 2, expected_type
    kind, nested_type = expected_type
    if kind == "nullable":
        variants = property_schema.get("anyOf")
        assert isinstance(variants, list) and len(variants) == 2, (context, property_schema)
        assert all(isinstance(variant, dict) for variant in variants), (context, property_schema)
        null_variants = [variant for variant in variants if variant.get("type") == "null"]
        assert len(null_variants) == 1, (context, property_schema)
        non_null_variants = [variant for variant in variants if variant.get("type") != "null"]
        assert len(non_null_variants) == 1, (context, property_schema)
        _assert_json_type(non_null_variants[0], nested_type, context)
        return

    if kind == "anyOf":
        assert isinstance(nested_type, tuple), expected_type
        variants = property_schema.get("anyOf")
        assert isinstance(variants, list), (context, property_schema)
        assert len(variants) == len(nested_type), (context, property_schema)
        for variant, variant_type in zip(variants, nested_type, strict=True):
            assert isinstance(variant, dict), (context, property_schema)
            _assert_json_type(variant, variant_type, context)
        return

    assert property_schema.get("type") == kind, (context, property_schema)
    assert "anyOf" not in property_schema, (context, property_schema)
    if kind == "array":
        nested_schema = property_schema.get("items")
    elif kind == "object":
        nested_schema = property_schema.get("additionalProperties")
    else:
        pytest.fail(f"Unsupported expected schema type: {expected_type!r}")
    assert isinstance(nested_schema, dict), (context, property_schema)
    _assert_json_type(nested_schema, nested_type, context)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _result_text(result: CallToolResult) -> str:
    return "\n".join(content.text for content in result.content if isinstance(content, TextContent))


@pytest.mark.anyio
async def test_installed_server_initializes_and_registers_exact_tool_schemas() -> None:
    assert len(EXPECTED_TOOL_NAMES) == 33
    assert frozenset(EXPECTED_REQUIRED_FIELDS) == EXPECTED_TOOL_NAMES
    assert frozenset(EXPECTED_TOOL_PROPERTIES) == EXPECTED_TOOL_NAMES

    async with running_mcp_server() as client:
        assert await client.session.send_ping() is not None
        response = await client.session.list_tools()

    names = [tool.name for tool in response.tools]
    assert len(names) == len(set(names)), names
    assert frozenset(names) == EXPECTED_TOOL_NAMES

    for tool in response.tools:
        schema = tool.inputSchema
        assert isinstance(schema, dict), tool.name
        assert schema.get("type") == "object", (tool.name, schema)

        properties = schema.get("properties")
        assert isinstance(properties, dict), (tool.name, schema)
        expected_properties = EXPECTED_TOOL_PROPERTIES[tool.name]
        assert frozenset(properties) == frozenset(expected_properties), (tool.name, schema)

        expected_required = frozenset(
            name for name, (_, default) in expected_properties.items() if default is NO_DEFAULT
        )
        assert expected_required == EXPECTED_REQUIRED_FIELDS[tool.name], tool.name

        required = schema.get("required", [])
        assert isinstance(required, list), (tool.name, schema)
        assert len(required) == len(set(required)), (tool.name, required)
        assert frozenset(required) == EXPECTED_REQUIRED_FIELDS[tool.name], (tool.name, schema)
        assert set(required) <= properties.keys(), (tool.name, schema)

        for property_name, (expected_type, expected_default) in expected_properties.items():
            property_schema = properties[property_name]
            assert isinstance(property_schema, dict), (tool.name, property_name, property_schema)
            context = (tool.name, property_name)
            _assert_json_type(property_schema, expected_type, context)
            if expected_default is NO_DEFAULT:
                assert "default" not in property_schema, (context, property_schema)
            else:
                assert property_schema.get("default", NO_DEFAULT) == expected_default, (
                    context,
                    property_schema,
                )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("arguments", "invalid_field"),
    [
        pytest.param({"x": 0}, "y", id="missing-required-parameter"),
        pytest.param({"x": {"not": "an integer"}, "y": 0}, "x", id="malformed-type"),
    ],
)
async def test_invalid_tool_arguments_are_rejected_over_stdio_json_rpc(
    arguments: dict[str, Any], invalid_field: str
) -> None:
    async with running_mcp_server() as client:
        result = await client.call_result("mouse_click", arguments)

    assert result.isError is True
    error_text = _result_text(result)
    assert error_text, result.content
    field_token = rf"(?<![A-Za-z0-9_]){re.escape(invalid_field)}(?![A-Za-z0-9_])"
    assert re.search(field_token, error_text), error_text


@pytest.mark.anyio
async def test_no_active_session_tool_errors_are_isolated_and_server_survives() -> None:
    calls = (
        ("screenshot", {"include_cursor": False}),
        ("mouse_move", {"x": 0, "y": 0}),
    )

    async with running_mcp_server() as client:
        for tool_name, arguments in calls:
            result = await client.call_result(tool_name, arguments)

            assert result.isError is True
            error_text = _result_text(result)
            assert SESSION_REQUIRED_GUIDANCE in error_text, (tool_name, error_text)
            assert await client.session.send_ping() is not None

        tools_after_errors = await client.session.list_tools()

    assert frozenset(tool.name for tool in tools_after_errors.tools) == EXPECTED_TOOL_NAMES


@pytest.mark.anyio
async def test_default_live_session_switches_session_tool_recommendations() -> None:
    async with running_mcp_server("--default-live-session") as client:
        response = await client.session.list_tools()

    names = [tool.name for tool in response.tools]
    assert len(names) == len(set(names)) == 33
    assert frozenset(names) == EXPECTED_TOOL_NAMES
    tools = {tool.name: tool for tool in response.tools}

    connect_description = (tools["session_connect"].description or "").lower()
    start_description = (tools["session_start"].description or "").lower()

    assert "default session tool" in connect_description
    assert "only use when explicitly asked" not in connect_description
    assert "only use when explicitly asked" in start_description
    assert "isolated" in start_description
    assert "virtual" in start_description
    assert "default session tool is session_connect" in start_description


@pytest.mark.anyio
async def test_engine_exception_is_a_tool_error_and_server_survives() -> None:
    cleanup_result: CallToolResult | None = None

    async with running_mcp_server() as client:
        try:
            result = await client.call_result(
                "session_start",
                {
                    "app_command": BAD_BINARY,
                    "screen_width": 1280,
                    "screen_height": 800,
                },
            )

            assert result.isError is True
            error_text = _result_text(result)
            assert BAD_BINARY in error_text, error_text

            tools_after_error = await client.session.list_tools()
            assert frozenset(tool.name for tool in tools_after_error.tools) == EXPECTED_TOOL_NAMES
        finally:
            cleanup_result = await client.call_result("session_stop")

        assert cleanup_result is not None
        assert cleanup_result.isError is False
        assert _result_text(cleanup_result) == "Session stopped."
        assert await client.call_text("session_stop") == "No session running."
