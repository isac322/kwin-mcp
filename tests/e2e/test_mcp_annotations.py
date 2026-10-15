"""Per-tool listing metadata over the installed server: hints, titles, _meta and icons.

The expected profiles are the design's annotation table (S §2 / §3.7), written out
here; titles come from the independent S tool table in ``tests/unit/fixtures``.
"""

from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from mcp_harness import TOOL_NAMES, running_mcp_server

if TYPE_CHECKING:
    from mcp.types import Tool

pytestmark = pytest.mark.anyio

S_TOOLS: list[dict[str, Any]] = json.loads(
    (Path(__file__).parents[1] / "unit" / "fixtures" / "s_tools.json").read_text(encoding="utf-8")
)["tools"]

SCREENSHOT_IMAGES_FLAG = "--screenshot-images"
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

# (readOnly, destructive, idempotent, openWorld) per profile.
PROFILE_HINTS: dict[str, tuple[bool, bool, bool, bool]] = {
    "RO": (True, False, True, False),
    "D": (False, True, False, False),
    "DI": (False, True, True, False),
    "NDI": (False, False, True, False),
    "ND": (False, False, False, False),
    "ND+openWorld": (False, False, False, True),
    "D+openWorld": (False, True, False, True),
}
PROFILES: dict[str, tuple[str, ...]] = {
    "RO": (
        "session_info",
        "app_log",
        "screen_capture",
        "element_query",
        "window_list",
        "desktop_list",
        "clipboard_read",
        "wayland_list",
    ),
    "D": (
        "element_act",
        "window_place",
        "window_close",
        "pointer_click",
        "pointer_scroll",
        "pointer_drag",
        "keyboard_press",
        "keyboard_type",
        "touch_tap",
        "touch_swipe",
        "touch_pinch",
    ),
    # window_place and window_close re-resolve their selector per call: repeating
    # one with window={active:true} acts on whichever window is active by then.
    "DI": (
        "session_stop",
        "pointer_hold",
        "keyboard_hold",
        "clipboard_write",
    ),
    "NDI": ("window_focus", "pointer_move"),
    "ND": ("session_start",),
    "ND+openWorld": ("session_connect",),
    "D+openWorld": ("app_launch", "dbus_call"),
}
EXPECTED_COUNTS = {
    "RO": 8,
    "D": 11,
    "DI": 4,
    "NDI": 2,
    "ND": 1,
    "ND+openWorld": 1,
    "D+openWorld": 2,
}
EXPECTED_HINTS: dict[str, dict[str, bool]] = {
    name: dict(zip(HINTS, PROFILE_HINTS[profile], strict=True))
    for profile, names in PROFILES.items()
    for name in names
}
# Tools whose large results carry the client size hint.
SIZE_HINT_KEY = "anthropic/maxResultSizeChars"
SIZE_HINT_TOOLS = frozenset({"element_query", "app_log"})


@pytest.fixture
def anyio_backend() -> str:
    """The installed MCP client runs on asyncio."""
    return "asyncio"


def _tool_table(tools: list[Tool]) -> dict[str, dict[str, object]]:
    """Wire form of everything a client sees per tool, keyed by tool name."""
    return {tool.name: tool.model_dump(by_alias=True, mode="json") for tool in tools}


def _hints(tool: Tool) -> dict[str, bool | None]:
    assert tool.annotations is not None, tool.name
    published = tool.annotations.model_dump(by_alias=True)
    return {hint: published[hint] for hint in HINTS}


async def test_tools_publish_annotations_and_keep_schemas_with_and_without_images_flag() -> None:
    """Every tool carries its hint table; the images flag changes no tool definition."""
    assert frozenset(EXPECTED_HINTS) == frozenset(TOOL_NAMES)

    async with running_mcp_server() as client:
        plain = await client.list_tools()
    async with running_mcp_server(SCREENSHOT_IMAGES_FLAG) as client:
        flagged = await client.list_tools()
        # main() parses the flag (the SDK never reads argv), so the server stays usable.
        assert await client.session.send_ping() is not None

    plain_table = _tool_table(plain)
    assert frozenset(plain_table) == frozenset(TOOL_NAMES)
    assert _tool_table(flagged) == plain_table

    for tool in plain:
        assert _hints(tool) == EXPECTED_HINTS[tool.name], (tool.name, _hints(tool))

        # Context injection and resolver parameters stay invisible in the input schema.
        properties = tool.input_schema.get("properties", {})
        for hidden in ("ctx", "gate", "window_target", "consent"):
            assert hidden not in properties, (tool.name, tool.input_schema)
            assert hidden not in tool.input_schema.get("required", []), (tool.name, hidden)

        # Every tool is typed now: no SDK `{"result": string}` wrapper.
        output_schema = tool.output_schema
        assert isinstance(output_schema, dict), (tool.name, output_schema)
        assert output_schema.get("type") == "object", (tool.name, output_schema)
        output_properties = output_schema.get("properties")
        assert isinstance(output_properties, dict), (tool.name, output_schema)
        assert list(output_properties) != ["result"], (tool.name, output_schema)


@pytest.mark.qa("QA-SURF-004")
async def test_annotation_profile_per_tool() -> None:
    assert {profile: len(names) for profile, names in PROFILES.items()} == EXPECTED_COUNTS
    assert sum(EXPECTED_COUNTS.values()) == len(TOOL_NAMES) == len(EXPECTED_HINTS)

    async with running_mcp_server() as client:
        tools = await client.list_tools()

    published = {tool.name: _hints(tool) for tool in tools}
    mismatched = {
        name: (hints, EXPECTED_HINTS[name])
        for name, hints in published.items()
        if hints != EXPECTED_HINTS[name]
    }
    assert not mismatched, mismatched
    open_world = {name for name, hints in published.items() if hints["openWorldHint"]}
    assert open_world == {"session_connect", "app_launch", "dbus_call"}
    # element_query is one read-only tool with or without wait.
    assert published["element_query"]["readOnlyHint"] is True
    assert "wait" in next(t for t in tools if t.name == "element_query").input_schema["properties"]


@pytest.mark.qa("QA-SURF-003")
async def test_titles_and_server_icon() -> None:
    async with running_mcp_server() as client:
        tools = await client.list_tools()
        server_info = client.initialize_result.server_info

    assert [tool.name for tool in tools] == [tool["name"] for tool in S_TOOLS]
    titles = {tool["name"]: tool["title"] for tool in S_TOOLS}
    assert titles["pointer_hold"] == "Hold or release mouse button"
    for tool in tools:
        assert tool.title == titles[tool.name], (tool.name, tool.title)
        if tool.annotations is not None and tool.annotations.title is not None:
            assert tool.annotations.title == titles[tool.name], tool.name
        assert not tool.icons, (tool.name, tool.icons)

    assert server_info.icons is not None
    assert len(server_info.icons) == 1, server_info.icons
    (icon,) = server_info.icons
    assert icon.mime_type == "image/svg+xml", icon
    assert icon.src.startswith("data:image/svg+xml;base64,"), icon.src[:40]


@pytest.mark.qa("QA-MCP-012")
async def test_meta_and_server_info() -> None:
    async with running_mcp_server() as client:
        tools = await client.list_tools()
        server_info = client.initialize_result.server_info

    for tool in tools:
        meta = tool.meta or {}
        if tool.name in SIZE_HINT_TOOLS:
            size = meta.get(SIZE_HINT_KEY)
            assert isinstance(size, int), (tool.name, meta)
            assert not isinstance(size, bool), (tool.name, meta)
            assert size > 0, (tool.name, meta)
        else:
            assert SIZE_HINT_KEY not in meta, (tool.name, meta)

    assert server_info.name == "kwin-mcp"
    assert server_info.version == "1.0.0"
    assert server_info.version == importlib.metadata.version("kwin-mcp")
    assert server_info.icons is not None
    assert len(server_info.icons) == 1
    assert all(not tool.icons for tool in tools)
