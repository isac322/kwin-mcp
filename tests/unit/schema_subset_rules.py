"""Static JSON Schema subset rules: an automated proxy for MCP client schema converters.

Clients translate each ``inputSchema`` into their model vendor's function-calling
dialect, and each dialect accepts only part of JSON Schema. Every rule below keeps
the published schemas inside the part all of them accept. Each rule cites the vendor
or specification text it follows, or is marked [INFERENCE] where the rule is a
conservative reading rather than a documented requirement.

``tests/unit/fixtures/schema_subset_negative/<rule id>.json`` holds one schema per rule
that breaks exactly that rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

type Schema = dict[str, Any]

_MAPS = ("properties", "$defs", "patternProperties")
_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SINGLES = ("items", "additionalProperties", "not")
_REF_PREFIX = "#/$defs/"
_PRIMITIVES = {"string", "integer", "number", "boolean"}
_BOUNDS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")


def walk(schema: Schema, pointer: str = "") -> Iterator[tuple[str, Schema]]:
    """Every subschema of ``schema`` (itself included) with its JSON pointer."""
    yield pointer, schema
    for key, value in schema.items():
        if key in _MAPS and isinstance(value, dict):
            for name, sub in value.items():
                if isinstance(sub, dict):
                    yield from walk(sub, f"{pointer}/{key}/{name}")
        elif key in _LISTS and isinstance(value, list):
            for index, sub in enumerate(value):
                if isinstance(sub, dict):
                    yield from walk(sub, f"{pointer}/{key}/{index}")
        elif key in _SINGLES and isinstance(value, dict):
            yield from walk(value, f"{pointer}/{key}")


def resolve(root: Schema, schema: Schema) -> Schema | None:
    """``schema`` itself, or the ``$defs`` entry its local ``$ref`` names (None if dangling)."""
    ref = schema.get("$ref")
    if ref is None:
        return schema
    if not isinstance(ref, str) or not ref.startswith(_REF_PREFIX):
        return None
    target = root.get("$defs", {}).get(ref.removeprefix(_REF_PREFIX))
    return target if isinstance(target, dict) else None


def _root_object(root: Schema) -> list[str]:
    combinators = [key for key in ("anyOf", "oneOf", "allOf", "not", "enum") if key in root]
    if root.get("type") != "object" or combinators:
        return [""]
    return []


def _additional_properties(root: Schema) -> list[str]:
    return [
        pointer
        for pointer, sub in walk(root)
        if "properties" in sub and sub.get("additionalProperties") is not False
    ]


def _object_anyof_arms(root: Schema) -> list[str]:
    bad: list[str] = []
    for pointer, sub in walk(root):
        arms = sub.get("anyOf")
        if not isinstance(arms, list):
            continue
        in_items = pointer.endswith("/items")
        required_sets: list[set[str]] = []
        for index, arm in enumerate(arms):
            target = resolve(root, arm) if isinstance(arm, dict) else None
            where = f"{pointer}/anyOf/{index}"
            if target is None:
                bad.append(where)
            elif target.get("type") == "object":
                required = target.get("required")
                nested = any("anyOf" in inner for _, inner in walk(target))
                if not isinstance(required, list) or not required or nested:
                    bad.append(where)
                else:
                    required_sets.append({str(name) for name in required})
            elif not (in_items and target.get("type") in _PRIMITIVES and len(target) == 1):
                bad.append(where)
        for i, first in enumerate(required_sets):
            if any(first & other for other in required_sets[i + 1 :]):
                bad.append(f"{pointer}/anyOf")
                break
    return bad


def _no_const(root: Schema) -> list[str]:
    return [pointer for pointer, sub in walk(root) if "const" in sub]


def _local_refs(root: Schema) -> list[str]:
    return [pointer for pointer, sub in walk(root) if "$ref" in sub and resolve(root, sub) is None]


def _min_items(root: Schema) -> list[str]:
    return [
        pointer
        for pointer, sub in walk(root)
        if "minItems" in sub
        and (
            sub.get("type") != "array"
            or not isinstance(sub["minItems"], int)
            or isinstance(sub["minItems"], bool)
            or sub["minItems"] < 0
        )
    ]


def _integer_bounds(root: Schema) -> list[str]:
    return [
        pointer
        for pointer, sub in walk(root)
        if any(key in sub for key in _BOUNDS)
        and (
            sub.get("type") not in ("integer", "number")
            or not all(
                isinstance(sub[key], int | float) and not isinstance(sub[key], bool)
                for key in _BOUNDS
                if key in sub
            )
        )
    ]


_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}


def _typed_enum(root: Schema) -> list[str]:
    bad: list[str] = []
    for pointer, sub in walk(root):
        values = sub.get("enum")
        if values is None:
            continue
        expected = _JSON_TYPES.get(str(sub.get("type")))
        if (
            expected is None
            or not isinstance(values, list)
            or not values
            or not all(isinstance(value, expected) for value in values)
        ):
            bad.append(pointer)
    return bad


def _required_declared(root: Schema) -> list[str]:
    return [
        pointer
        for pointer, sub in walk(root)
        if "required" in sub and not set(sub["required"]) <= set(sub.get("properties", {}))
    ]


def _no_null_type(root: Schema) -> list[str]:
    return [
        pointer
        for pointer, sub in walk(root)
        if "type" in sub and (sub["type"] == "null" or isinstance(sub["type"], list))
    ]


@dataclass(frozen=True)
class Rule:
    """One subset rule: an id (the negative fixture's file stem), its source, its check."""

    id: str
    source: str
    check: Callable[[Schema], list[str]]


RULES: tuple[Rule, ...] = (
    Rule(
        "root-object",
        "https://platform.openai.com/docs/guides/structured-outputs"
        "#root-objects-must-not-be-anyof-and-must-be-an-object",
        _root_object,
    ),
    Rule(
        "additional-properties",
        "https://platform.openai.com/docs/guides/structured-outputs"
        "#additionalproperties-false-must-always-be-set-in-objects",
        _additional_properties,
    ),
    Rule(
        "object-anyof-arms",
        "https://ai.google.dev/api/caching#Schema (anyOf of schemas); [INFERENCE] arms are "
        "objects with disjoint required keys so a converter that tries arms in order "
        "cannot pick the wrong one; a primitive arm only inside array items",
        _object_anyof_arms,
    ),
    Rule(
        "no-const",
        "https://ai.google.dev/api/caching#Schema (the Schema object has enum, no const)",
        _no_const,
    ),
    Rule(
        "local-refs",
        "https://json-schema.org/draft/2020-12/json-schema-core#name-schema-references; "
        "https://platform.openai.com/docs/guides/structured-outputs#definitions-are-supported",
        _local_refs,
    ),
    Rule(
        "min-items",
        "https://json-schema.org/draft/2020-12/json-schema-validation#name-minitems",
        _min_items,
    ),
    Rule(
        "integer-bounds",
        "https://json-schema.org/draft/2020-12/json-schema-validation"
        "#name-validation-keywords-for-num",
        _integer_bounds,
    ),
    Rule(
        "typed-enum",
        "https://ai.google.dev/api/caching#Schema (enum is declared with its type)",
        _typed_enum,
    ),
    Rule(
        "required-declared",
        "https://json-schema.org/draft/2020-12/json-schema-validation#name-required; "
        "[INFERENCE] converters that rebuild objects from properties drop undeclared names",
        _required_declared,
    ),
    Rule(
        "no-null-type",
        "[INFERENCE] null types and type lists are not portable across converters; "
        "omission means null in the published schemas",
        _no_null_type,
    ),
)


def violations(schema: Schema) -> dict[str, list[str]]:
    """Rule id -> JSON pointers of the violations, for every rule that ``schema`` breaks."""
    found = {rule.id: rule.check(schema) for rule in RULES}
    return {rule_id: pointers for rule_id, pointers in found.items() if pointers}
