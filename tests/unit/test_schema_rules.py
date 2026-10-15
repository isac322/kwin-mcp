"""Release gate 5: structural rules of the published input and output schemas."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from jsonschema import Draft202012Validator
from mcp.server.mcpserver.exceptions import ToolError

from kwin_mcp.server import KwinServer, ServerFlags, build_server

from .schema_subset_rules import RULES, resolve, violations, walk

if TYPE_CHECKING:
    from ._fakes import FakeEngine
    from .schema_subset_rules import Schema

NEGATIVE_FIXTURES = Path(__file__).parent / "fixtures" / "schema_subset_negative"


def _listing() -> dict[str, dict[str, Any]]:
    tools = asyncio.run(build_server().list_tools())
    return {
        tool.name: tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools
    }


@pytest.fixture(scope="module")
def listing() -> dict[str, dict[str, Any]]:
    return _listing()


def _inputs(listing: dict[str, dict[str, Any]]) -> dict[str, Schema]:
    return {name: tool["inputSchema"] for name, tool in listing.items()}


def _schemas(listing: dict[str, dict[str, Any]]) -> list[tuple[str, Schema]]:
    pairs: list[tuple[str, Schema]] = []
    for name, tool in listing.items():
        pairs.append((f"{name}/inputSchema", tool["inputSchema"]))
        pairs.append((f"{name}/outputSchema", tool["outputSchema"]))
    return pairs


def _property(schema: Schema, *path: str) -> Schema:
    """``schema.properties[path[0]].properties[path[1]]...``, following local refs."""
    node = schema
    for name in path:
        target = resolve(schema, node["properties"][name])
        assert target is not None, path
        merged = {**node["properties"][name], **target}
        node = merged
    return node


_POINT_TOOLS = ("pointer_click", "pointer_move", "pointer_scroll", "pointer_hold", "touch_tap")
_WINDOW_MUTATORS = ("window_focus", "window_place", "window_close")


@pytest.mark.qa("QA-SURF-006")
def test_enum_value_sets(listing: dict[str, dict[str, Any]]) -> None:
    inputs = _inputs(listing)
    expected: list[tuple[str, tuple[str, ...], list[str]]] = [
        *[
            (tool, ("button",), ["left", "right", "middle"])
            for tool in ("pointer_click", "pointer_drag", "pointer_hold")
        ],
        ("pointer_scroll", ("axis",), ["vertical", "horizontal"]),
        ("pointer_scroll", ("unit",), ["smooth_step", "detent"]),
        ("pointer_hold", ("phase",), ["press", "move", "release"]),
        ("keyboard_hold", ("phase",), ["press", "release"]),
        ("keyboard_type", ("method",), ["keys", "unicode"]),
        ("session_start", ("home",), ["host", "isolated"]),
        ("element_query", ("wait", "until"), ["present", "absent"]),
    ]
    for tool, path, values in expected:
        assert _property(inputs[tool], *path)["enum"] == values, (tool, path)
    kinds = ["normal", "dialog", "utility", "popup", "shell", "other"]
    for tool in _WINDOW_MUTATORS:
        assert _property(inputs[tool], "window", "kind")["items"]["enum"] == kinds
    assert _property(inputs["window_list"], "filter", "kind")["items"]["enum"] == kinds

    close = listing["window_close"]["outputSchema"]["properties"]
    consent = [arm["enum"] for arm in close["consent"]["anyOf"] if "enum" in arm]
    refusal = [arm["enum"] for arm in close["refusal"]["anyOf"] if "enum" in arm]
    assert consent == [
        ["not_required", "accepted", "not_confirmed", "declined", "cancelled", "unsupported"]
    ]
    assert refusal == [["no_window", "not_closeable", "no_consent"]]
    for where, schema in _schemas(listing):
        assert not [pointer for pointer, sub in walk(schema) if "const" in sub], where


# (property name, keyword, value): the only numeric and array bounds S states.
_ALLOWED_BOUNDS = {
    ("screen_width", "minimum", 1),
    ("screen_height", "minimum", 1),
    ("last_n_lines", "minimum", 0),
    ("max_edge", "minimum", 0),
    ("count", "minimum", 1),
    ("count", "maximum", 3),
    ("fingers", "minimum", 1),
    ("width", "exclusiveMinimum", 0),
    ("height", "exclusiveMinimum", 0),
    ("kind", "minItems", 1),
}
_BOUND_KEYWORDS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minItems")


@pytest.mark.qa("QA-SURF-007")
def test_numeric_bounds_only_where_s_states(listing: dict[str, dict[str, Any]]) -> None:
    found: set[tuple[str, str, object]] = set()
    unexpected: list[str] = []
    for name, schema in _inputs(listing).items():
        for pointer, sub in walk(schema):
            for keyword in _BOUND_KEYWORDS:
                if keyword in sub:
                    bound = (pointer.rsplit("/", 1)[-1], keyword, sub[keyword])
                    found.add(bound)
                    if bound not in _ALLOWED_BOUNDS:
                        unexpected.append(f"{name}{pointer}: {keyword}={sub[keyword]}")
    assert unexpected == []
    assert found == _ALLOWED_BOUNDS
    # Values the engine accepts and adjusts carry no bound.
    unbounded = {
        "pointer_click": ("hold_ms",),
        "pointer_scroll": ("steps",),
        "pointer_drag": ("dwell_ms",),
        "touch_tap": ("hold_ms",),
        "touch_swipe": ("duration_ms",),
        "touch_pinch": ("start_distance", "end_distance", "duration_ms"),
        "element_query": ("max_depth",),
    }
    inputs = _inputs(listing)
    for tool, names in unbounded.items():
        for name in names:
            assert not set(_BOUND_KEYWORDS) & set(inputs[tool]["properties"][name]), (tool, name)
    wait = _property(inputs["element_query"], "wait")["properties"]
    for field in ("timeout_ms", "poll_interval_ms"):
        assert not set(_BOUND_KEYWORDS) & set(wait[field]), field
    delays = _property(inputs["pointer_move"], "capture")["properties"]["delays_ms"]
    assert "minItems" not in delays


_DI_NAME = re.compile(
    r"^(ctx|consent|target_resolution|close_target|close_consent|window_target|gate"
    r"|require_\w+|session_obj|engine)$"
)


@pytest.mark.qa("QA-SURF-009")
def test_no_di_params_in_input_schema(listing: dict[str, dict[str, Any]]) -> None:
    for name, schema in _inputs(listing).items():
        for _pointer, sub in walk(schema):
            for prop in sub.get("properties", {}):
                assert not _DI_NAME.match(prop), (name, prop)
    assert set(listing["window_close"]["inputSchema"]["properties"]) == {"window", "capture"}


def _union_pointers() -> set[tuple[str, str]]:
    allowed = {(tool, "/properties/target") for tool in _POINT_TOOLS}
    allowed |= {
        (tool, f"/properties/{end}")
        for tool in ("pointer_drag", "touch_swipe")
        for end in ("from", "to")
    }
    allowed |= {("touch_pinch", "/properties/center"), ("element_act", "/properties/element")}
    allowed.add(("dbus_call", "/properties/args/items"))
    return allowed


@pytest.mark.qa("QA-SURF-011")
def test_only_two_new_property_unions(listing: dict[str, dict[str, Any]]) -> None:
    """anyOf only at Target, ElementAddress and dbus_call.args items; no other combinator."""
    found: set[tuple[str, str]] = set()
    for name, schema in _inputs(listing).items():
        assert schema.get("type") == "object" and "anyOf" not in schema, name
        for pointer, sub in walk(schema):
            for keyword in ("oneOf", "allOf", "not", "discriminator", "const"):
                assert keyword not in sub, (name, pointer, keyword)
            assert sub.get("type") != "null", (name, pointer)
            if "anyOf" in sub:
                found.add((name, pointer))
                for arm in sub["anyOf"]:
                    target = resolve(schema, arm)
                    assert target is not None
                    assert not [p for p, inner in walk(target) if "anyOf" in inner], (name, pointer)
    assert found == _union_pointers()
    assert not violations(listing["pointer_click"]["inputSchema"]).get("object-anyof-arms")
    # Output schemas: a nullable field is the only union.
    for name, tool in listing.items():
        schema = tool["outputSchema"]
        assert schema.get("type") == "object" and "anyOf" not in schema, name
        for pointer, sub in walk(schema):
            for keyword in ("oneOf", "allOf", "not", "discriminator", "const"):
                assert keyword not in sub, (name, pointer, keyword)
            if "anyOf" in sub:
                arms = sub["anyOf"]
                assert len(arms) == 2 and {"type": "null"} in arms, (name, pointer)


@pytest.mark.qa("QA-SURF-012")
def test_no_auto_titles(listing: dict[str, dict[str, Any]]) -> None:
    for where, schema in _schemas(listing):
        assert [pointer for pointer, sub in walk(schema) if "title" in sub] == [], where
    assert all(isinstance(tool["title"], str) for tool in listing.values())


@pytest.mark.qa("QA-SURF-013")
def test_defs_referenced_twice_no_dangling_ref(listing: dict[str, dict[str, Any]]) -> None:
    for where, schema in _schemas(listing):
        text = json.dumps(schema)
        for pointer, sub in walk(schema):
            if "$ref" in sub:
                assert resolve(schema, sub) is not None, (where, pointer, sub["$ref"])
        for name in schema.get("$defs", {}):
            count = text.count(json.dumps(f"#/$defs/{name}"))
            assert count >= 2, (where, name, count)
    # Target is used twice in pointer_drag/touch_swipe, so its arms stay shared.
    for tool in ("pointer_drag", "touch_swipe"):
        assert set(listing[tool]["inputSchema"]["$defs"]) == {"Point", "ElementPoint"}


# The shipped one-line description and the property names of each input type.
_TYPES: dict[str, tuple[str, set[str]]] = {
    "Capture": (
        "Frames taken delays_ms after the action ends; same options as screen_capture.",
        {"delays_ms", "region", "max_edge", "include_cursor"},
    ),
    "Locator": (
        "AND-ed filters; app/query/role case-insensitive, states = exact AT-SPI state names; "
        "{} = all.",
        {"app", "query", "role", "states", "match"},
    ),
    "WindowSelector": (
        "AND-ed filters over all windows; mutators need exactly one match (errors list ids "
        "and kinds).",
        {"id", "app_id", "caption", "active", "kind"},
    ),
    "ElementPoint": ("Centre of a ref's live rect + offset.", {"ref", "dx", "dy"}),
    "Point": ("Global logical px.", {"x", "y"}),
    "Rect": ("Global logical px.", {"x", "y", "width", "height"}),
    "Wait": (
        "Poll until true; timeout returns satisfied=false.",
        {"until", "timeout_ms", "poll_interval_ms"},
    ),
    "Retention": ("Kept after session end. home needs home='isolated'.", {"artifacts", "home"}),
    "StrictAddr": ("Strict: depth 64, fail-closed, exactly one (element, action).", {"strict"}),
    "PartialRect": ("Omitted fields keep their value.", {"x", "y", "width", "height"}),
}


@pytest.mark.qa("QA-SURF-014")
def test_defs_type_shapes_and_descriptions(listing: dict[str, dict[str, Any]]) -> None:
    """Each type appears with its exact shape and its one-liner, inlined or in $defs."""
    inputs = _inputs(listing)
    drag_from = inputs["pointer_drag"]["properties"]["from"]["anyOf"]
    element_arms = inputs["element_act"]["properties"]["element"]["anyOf"]
    where: dict[str, Schema] = {
        "Capture": _property(inputs["pointer_move"], "capture"),
        "Locator": _property(inputs["element_query"], "locator"),
        "WindowSelector": _property(inputs["window_focus"], "window"),
        "ElementPoint": resolve(inputs["pointer_drag"], drag_from[1]) or {},
        "Point": resolve(inputs["pointer_drag"], drag_from[0]) or {},
        "Rect": _property(inputs["screen_capture"], "region"),
        "Wait": _property(inputs["element_query"], "wait"),
        "Retention": _property(inputs["session_start"], "retention"),
        "StrictAddr": element_arms[1],
        "PartialRect": _property(inputs["window_place"], "frame"),
    }
    for type_name, (one_liner, properties) in _TYPES.items():
        schema = where[type_name]
        assert set(schema["properties"]) == properties, type_name
        assert one_liner in schema["description"], (type_name, schema["description"])
    assert _property(element_arms[1], "strict")["description"] == _TYPES["Locator"][0]
    # The pre-round-5 wording is gone.
    text = json.dumps(listing)
    assert "{unique" not in text
    assert "case-insensitive; {} = all. states" not in text


_REJECTED_NESTED: list[tuple[str, dict[str, Any]]] = [
    ("pointer_click", {"target": {"x": 1, "y": 2, "ref": "e1.2"}}),
    ("pointer_click", {"target": {"x": 10}}),
    ("pointer_click", {"target": {}}),
    ("element_act", {"element": {"ref": "e1.1", "strict": {"query": "OK"}}, "action": "Press"}),
    ("element_query", {"locator": {"app": "x", "bogus": 1}}),
    ("pointer_move", {"target": {"x": 1, "y": 1}, "capture": {"delays_ms": [0], "foo": 1}}),
    ("element_query", {"wait": {"until": "present", "bogus": 1}}),
    ("session_start", {"retention": {"artifacts": True, "logs": True}}),
    ("window_focus", {"window": {"app_id": "x", "name": "y"}}),
]


@pytest.mark.qa("QA-GATE-010")
def test_additional_properties_false_everywhere(
    listing: dict[str, dict[str, Any]], fake_engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every input object is closed, and the validator itself rejects nested extra keys."""
    for name, schema in _inputs(listing).items():
        for pointer, sub in walk(schema):
            if sub.get("type") == "object" and "properties" in sub:
                assert sub.get("additionalProperties") is False, (name, pointer)
            elif sub.get("type") == "object":
                # The one open map: app_launch.env, string values only.
                assert (name, pointer) == ("app_launch", "/properties/env")
                assert sub["additionalProperties"] == {"type": "string"}

    calls: list[str] = []
    for tool in {tool for tool, _ in _REJECTED_NESTED}:

        def record(*_args: object, _tool: str = tool, **_kwargs: object) -> object:
            calls.append(_tool)
            msg = "the engine must not be reached"
            raise AssertionError(msg)

        monkeypatch.setattr(fake_engine.engine, tool, record)
    server = KwinServer(fake_engine.engine, ServerFlags())
    for tool, arguments in _REJECTED_NESTED:
        with pytest.raises(ToolError) as exc_info:
            asyncio.run(server.call_tool(tool, arguments))
        assert str(exc_info.value).startswith(f"Error executing tool {tool}: "), arguments
        assert "validation error" in str(exc_info.value), arguments
    assert calls == []
    assert fake_engine.eis.events == []


@pytest.mark.qa("QA-CLT-007")
def test_client_schema_subset_lint(listing: dict[str, dict[str, Any]]) -> None:
    """All 29 input schemas pass every subset rule and the 2020-12 metaschema; each
    negative fixture breaks exactly its own rule."""
    for name, schema in _inputs(listing).items():
        assert violations(schema) == {}, name
        Draft202012Validator.check_schema(schema)
    for tool in listing.values():
        Draft202012Validator.check_schema(tool["outputSchema"])

    fixtures = {path.stem: path for path in NEGATIVE_FIXTURES.glob("*.json")}
    assert set(fixtures) == {rule.id for rule in RULES}
    for rule in RULES:
        assert rule.source, rule.id
        schema = json.loads(fixtures[rule.id].read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        assert list(violations(schema)) == [rule.id], rule.id
