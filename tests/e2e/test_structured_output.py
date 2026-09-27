"""Typed structured output of the observation tools over the installed MCP stdio server.

``window_geometry``, ``find_ui_elements``, ``accessibility_tree``, and
``list_windows`` advertise a typed ``outputSchema`` and return matching
``structuredContent``, while their TextContent stays the human-readable text the
engine formatters produce (and ``kwin-mcp-cli`` prints).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import pytest
from jsonschema import Draft202012Validator
from mcp.types import TextContent
from mcp_harness import running_mcp_server

from kwin_mcp.results import (
    AccessibilityTreeResult,
    FindUIElementsResult,
    ListWindowsResult,
    WindowGeometryResult,
    format_find,
    format_list_windows,
    format_tree,
    format_window_geometry,
)

if TYPE_CHECKING:
    from typing import Any

    from mcp.types import CallToolResult
    from mcp_harness import McpTestClient

SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800
APP_TIMEOUT_SECONDS = 20
ACTIVE_TIMEOUT_SECONDS = 10
POLL_INTERVAL_SECONDS = 0.25
SESSION_REQUIRED_GUIDANCE = "Call session_start or session_connect first."
MISSING_NAME = "definitely-not-a-real-kwrite-element"

STRUCTURED_TOOL_PROPERTIES: dict[str, frozenset[str]] = {
    "accessibility_tree": frozenset({"elements"}),
    "find_ui_elements": frozenset({"query", "states", "elements"}),
    "list_windows": frozenset({"applications"}),
    "window_geometry": frozenset({"windows", "error"}),
}


@pytest.fixture
def anyio_backend() -> str:
    """The MCP stdio client is exercised on asyncio."""
    return "asyncio"


def _text(result: CallToolResult) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


async def _output_schemas(client: McpTestClient) -> dict[str, dict[str, Any]]:
    response = await client.session.list_tools()
    schemas: dict[str, dict[str, Any]] = {}
    for tool in response.tools:
        assert isinstance(tool.output_schema, dict), (tool.name, tool.output_schema)
        schemas[tool.name] = tool.output_schema
    return schemas


async def _call_structured(
    client: McpTestClient,
    schemas: dict[str, dict[str, Any]],
    name: str,
    arguments: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Call a typed tool and return its sole text block plus schema-valid structured content."""
    result = await client.call_result(name, arguments)
    text = _text(result)
    assert not result.is_error, f"{name} failed: {text}\n{client.stderr_text()[-4000:]}"
    assert len(result.content) == 1, (name, result.content)
    assert isinstance(result.content[0], TextContent), (name, result.content)
    structured = result.structured_content
    assert isinstance(structured, dict), (name, structured)
    Draft202012Validator(schemas[name]).validate(structured)
    return text, structured


@pytest.mark.anyio
async def test_typed_tools_advertise_object_output_schemas() -> None:
    async with running_mcp_server() as client:
        schemas = await _output_schemas(client)

    for name, expected_properties in STRUCTURED_TOOL_PROPERTIES.items():
        schema = schemas[name]
        Draft202012Validator.check_schema(schema)
        assert schema.get("type") == "object", (name, schema)
        properties = schema.get("properties")
        assert isinstance(properties, dict), (name, schema)
        assert frozenset(properties) == expected_properties, (name, schema)

    # Untyped tools keep the SDK's wrapped string result.
    stop_schema = schemas["session_stop"]
    assert stop_schema.get("type") == "object", stop_schema
    assert stop_schema.get("properties") == {"result": {"title": "Result", "type": "string"}}
    assert stop_schema.get("required") == ["result"], stop_schema


@pytest.mark.anyio
@pytest.mark.parametrize("name", sorted(STRUCTURED_TOOL_PROPERTIES))
async def test_typed_tools_without_session_are_errors_without_structured_content(
    name: str,
) -> None:
    arguments = {"query": ""} if name == "find_ui_elements" else {}
    async with running_mcp_server() as client:
        result = await client.call_result(name, arguments)

    assert result.is_error is True, _text(result)
    assert SESSION_REQUIRED_GUIDANCE in _text(result), _text(result)
    assert result.structured_content is None, result.structured_content


async def _wait_for_kwrite_application(
    client: McpTestClient, schemas: dict[str, dict[str, Any]]
) -> tuple[str, dict[str, Any]]:
    text, structured = "", {}
    with anyio.move_on_after(APP_TIMEOUT_SECONDS):
        while True:
            text, structured = await _call_structured(client, schemas, "list_windows")
            if any(
                "kwrite" in app["name"].lower() and app["windows"]
                for app in structured["applications"]
            ):
                return text, structured
            await anyio.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(f"kwrite never appeared in list_windows within {APP_TIMEOUT_SECONDS}s:\n{text}")


async def _wait_for_single_active_window(
    client: McpTestClient, schemas: dict[str, dict[str, Any]]
) -> tuple[str, dict[str, Any]]:
    text, structured = "", {}
    with anyio.move_on_after(ACTIVE_TIMEOUT_SECONDS):
        while True:
            text, structured = await _call_structured(client, schemas, "window_geometry")
            if sum(window["active"] for window in structured["windows"]) == 1:
                return text, structured
            await anyio.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(f"no single active window within {ACTIVE_TIMEOUT_SECONDS}s:\n{text}")


def _assert_list_windows(text: str, structured: dict[str, Any]) -> None:
    assert text == format_list_windows(ListWindowsResult.model_validate(structured))
    kwrite_apps = [app for app in structured["applications"] if "kwrite" in app["name"].lower()]
    assert len(kwrite_apps) == 1, structured
    kwrite = kwrite_apps[0]
    assert kwrite["window_count"] >= len(kwrite["windows"]) >= 1, kwrite
    assert f"- {kwrite['name']} ({kwrite['window_count']} windows)" in text.splitlines(), text
    for window in kwrite["windows"]:
        assert window["title"], kwrite
        assert any(line.startswith(f'    - "{window["title"]}"') for line in text.splitlines())


def _assert_window_geometry(text: str, structured: dict[str, Any]) -> None:
    assert text == format_window_geometry(WindowGeometryResult.model_validate(structured), "", "")
    assert structured["error"] is None, structured
    windows = structured["windows"]
    assert text.splitlines()[0] == f"Windows ({len(windows)}):", text
    assert any("kwrite" in window["app"].lower() for window in windows), structured
    assert [window["active"] for window in windows].count(True) == 1, structured
    for window in windows:
        for rect_name in ("frame", "client"):
            rect = window[rect_name]
            assert all(_is_int(rect[key]) for key in ("x", "y", "width", "height")), window
            assert rect["width"] > 0 and rect["height"] > 0, window
        assert f"    id:     {window['id']}" in text.splitlines(), text


def _assert_element(element: dict[str, Any]) -> None:
    assert isinstance(element["role"], str) and element["role"], element
    assert isinstance(element["name"], str), element
    assert _is_int(element["depth"]) and element["depth"] >= 0, element
    if element["rect"] is None:
        assert isinstance(element["unavailable"], str) and element["unavailable"], element
    else:
        assert element["unavailable"] is None, element
        assert all(_is_int(element["rect"][key]) for key in ("x", "y", "width", "height"))


@pytest.mark.anyio
async def test_typed_tools_return_schema_valid_structured_content_matching_text() -> None:
    async with running_mcp_server() as client:
        session_running = False
        try:
            schemas = await _output_schemas(client)
            start_output = await client.call_text(
                "session_start",
                {
                    "app_command": "kwrite",
                    "screen_width": SCREEN_WIDTH,
                    "screen_height": SCREEN_HEIGHT,
                },
            )
            session_running = True
            assert "App launched: kwrite" in start_output, start_output

            list_text, list_structured = await _wait_for_kwrite_application(client, schemas)
            _assert_list_windows(list_text, list_structured)

            focus_output = await client.call_text("focus_window", {"app_name": "kwrite"})
            assert focus_output.startswith("Focused:"), focus_output
            geometry_text, geometry = await _wait_for_single_active_window(client, schemas)
            _assert_window_geometry(geometry_text, geometry)

            missing_id = "{00000000-0000-0000-0000-000000000000}"
            missing_text, missing_geometry = await _call_structured(
                client, schemas, "window_geometry", {"window_id": missing_id}
            )
            assert missing_geometry == {"windows": [], "error": None}, missing_geometry
            assert missing_text == f"No window with id {missing_id!r}."

            tree_text, tree = await _call_structured(
                client, schemas, "accessibility_tree", {"app_name": "kwrite"}
            )
            assert tree_text == format_tree(AccessibilityTreeResult.model_validate(tree))
            elements = tree["elements"]
            assert elements, tree_text[:500]
            assert tree_text.startswith(f"# Accessibility Tree ({len(elements)} elements)\n\n")
            assert elements[0]["depth"] == 0, elements[0]
            assert elements[0]["role"] == "application", elements[0]
            assert "kwrite" in elements[0]["name"].lower(), elements[0]
            assert any(element["depth"] > 0 for element in elements), tree_text[:500]
            for element in elements:
                _assert_element(element)

            empty_tree_text, empty_tree = await _call_structured(
                client, schemas, "accessibility_tree", {"app_name": MISSING_NAME}
            )
            assert empty_tree == {"elements": []}, empty_tree
            assert empty_tree_text == "(no accessible applications found)"

            find_text, found = await _call_structured(
                client, schemas, "find_ui_elements", {"query": "KWrite", "app_name": "kwrite"}
            )
            assert find_text == format_find(FindUIElementsResult.model_validate(found))
            assert found["query"] == "KWrite", found
            assert found["states"] is None, found
            assert found["elements"], find_text[:500]
            assert find_text.startswith(
                f"Found {len(found['elements'])} elements matching query='KWrite':\n\n"
            ), find_text[:500]
            assert any("kwrite" in element["name"].lower() for element in found["elements"])
            for element in found["elements"]:
                _assert_element(element)

            states = ["enabled"]
            none_text, none_found = await _call_structured(
                client,
                schemas,
                "find_ui_elements",
                {"query": MISSING_NAME, "app_name": "kwrite", "states": states},
            )
            assert none_found == {"query": MISSING_NAME, "states": states, "elements": []}
            assert none_text == (
                f"No elements found matching query='{MISSING_NAME}', states={states}"
            )

            stop_output = await client.call_text("session_stop")
            session_running = False
            assert stop_output == "Session stopped.", stop_output
        finally:
            if session_running:
                await client.call_text("session_stop")
