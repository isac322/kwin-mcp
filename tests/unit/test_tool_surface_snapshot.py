"""Release gate 1 (surface): the listing against S's tool table and the committed snapshot.

``fixtures/s_tools.json`` restates S's table (titles, annotations, parameters,
shipped descriptions, output fields) independently of the server code.
``snapshots/tool_surface.json`` is the normalized ``list_tools()`` of a server without
flags; ``KWIN_MCP_UPDATE_SNAPSHOT=1`` rewrites it after an intended change.
"""

from __future__ import annotations

import asyncio
import collections
import difflib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from kwin_mcp.server import build_server

HERE = Path(__file__).parent
SNAPSHOT = HERE / "snapshots" / "tool_surface.json"
S_TOOLS: list[dict[str, Any]] = json.loads(
    (HERE / "fixtures" / "s_tools.json").read_text(encoding="utf-8")
)["tools"]
_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def _listing() -> list[dict[str, Any]]:
    tools = asyncio.run(build_server().list_tools())
    return [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]


def _serialize(listing: list[dict[str, Any]]) -> str:
    return json.dumps(listing, indent=1, ensure_ascii=False) + "\n"


@pytest.fixture(scope="module")
def listing() -> dict[str, dict[str, Any]]:
    return {tool["name"]: tool for tool in _listing()}


def test_listing_equals_committed_snapshot() -> None:
    current = _serialize(_listing())
    if os.environ.get("KWIN_MCP_UPDATE_SNAPSHOT") == "1":
        SNAPSHOT.write_text(current, encoding="utf-8")
    committed = SNAPSHOT.read_text(encoding="utf-8")
    diff = "".join(
        difflib.unified_diff(
            committed.splitlines(keepends=True),
            current.splitlines(keepends=True),
            "snapshots/tool_surface.json",
            "list_tools()",
        )
    )
    assert current == committed, (
        "tools/list changed; review the diff and rerun with KWIN_MCP_UPDATE_SNAPSHOT=1:\n" + diff
    )


@pytest.mark.qa("QA-SURF-003")
def test_titles_match_s_table(listing: dict[str, dict[str, Any]]) -> None:
    assert [tool["name"] for tool in S_TOOLS] == list(listing)
    for expected in S_TOOLS:
        tool = listing[expected["name"]]
        assert tool["title"] == expected["title"], expected["name"]
        assert tool["annotations"].get("title", expected["title"]) == expected["title"]


def _profile(hints: dict[str, bool]) -> str:
    if hints["readOnlyHint"]:
        return "RO"
    profile = ("D" if hints["destructiveHint"] else "ND") + ("I" if hints["idempotentHint"] else "")
    return profile + ("+openWorld" if hints["openWorldHint"] else "")


@pytest.mark.qa("QA-SURF-004")
def test_annotation_counts(listing: dict[str, dict[str, Any]]) -> None:
    for expected in S_TOOLS:
        annotations = listing[expected["name"]]["annotations"]
        assert {hint: annotations[hint] for hint in _HINTS} == expected["annotations"], expected[
            "name"
        ]
    profiles = collections.Counter(
        _profile({hint: tool["annotations"][hint] for hint in _HINTS}) for tool in listing.values()
    )
    assert profiles == {
        "RO": 8,
        "D": 11,
        "DI": 4,
        "NDI": 2,
        "ND": 1,
        "ND+openWorld": 1,
        "D+openWorld": 2,
    }
    open_world = {name for name, tool in listing.items() if tool["annotations"]["openWorldHint"]}
    assert open_world == {"session_connect", "app_launch", "dbus_call"}


@pytest.mark.qa("QA-SURF-005")
def test_input_schema_params_required_defaults(listing: dict[str, dict[str, Any]]) -> None:
    for expected in S_TOOLS:
        name = expected["name"]
        schema = listing[name]["inputSchema"]
        properties = schema.get("properties", {})
        # Sets: Python puts pointer_hold's required target before the defaulted button.
        assert set(properties) == {param["name"] for param in expected["params"]}, name
        assert set(schema.get("required", [])) == {
            param["name"] for param in expected["params"] if param["required"]
        }, name
        for param in expected["params"]:
            published = properties[param["name"]]
            if param["required"] or param["default"] is None:
                # Required, or optional with omission meaning null: no default.
                assert "default" not in published, (name, param["name"])
            else:
                assert published["default"] == param["default"], (name, param["name"])
    assert listing["pointer_hold"]["inputSchema"]["required"] == ["phase", "target"]
    for name in ("pointer_drag", "touch_swipe"):
        assert "from" in listing[name]["inputSchema"]["properties"]
        assert "from_" not in json.dumps(listing[name]["inputSchema"])


@pytest.mark.qa("QA-SURF-008")
def test_output_schemas_match_s_models(listing: dict[str, dict[str, Any]]) -> None:
    for expected in S_TOOLS:
        schema = listing[expected["name"]]["outputSchema"]
        assert schema["type"] == "object", expected["name"]
        assert "anyOf" not in schema, expected["name"]
        assert list(schema["properties"]) == expected["output"], expected["name"]
        assert list(schema["properties"]) != ["result"], expected["name"]


@pytest.mark.qa("QA-SURF-010")
def test_descriptions_equal_shipped_text(listing: dict[str, dict[str, Any]]) -> None:
    for expected in S_TOOLS:
        name = expected["name"]
        tool = listing[name]
        assert tool["description"] == expected["description"], name
        properties = tool["inputSchema"].get("properties", {})
        for param in expected["params"]:
            published = properties[param["name"]]
            if param["description"] is not None:
                # The field's shipped text; an inlined type's one-liner may follow it.
                assert published["description"].startswith(param["description"]), (
                    name,
                    param["name"],
                )
            elif "properties" in published:
                # A dropped restatement of an object type: the type's one-liner remains.
                assert published.get("description"), (name, param["name"])
