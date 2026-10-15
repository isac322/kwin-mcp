"""Release gate 4: the published (compacted) schemas accept exactly what the raw
pydantic schemas accept.

Both schemas are checked with an independent validator (jsonschema, Draft 2020-12),
never with pydantic. The corpus is ``fixtures/schema_samples.json`` (hand-written valid
and invalid arguments per tool) plus mechanical mutations of every valid sample:
each key dropped, an unknown key added to every object, and every value swapped for
null, a string, an integer, a float, a boolean, a list and an object.

Two differences are by design and the only ones allowed:

- The raw argument model ignores unknown top-level keys, but ``KwinServer.call_tool``
  rejects them before validation; the raw side is therefore checked with its root
  closed (``additionalProperties: false``), which is what the server enforces.
- An explicit ``null`` on an optional field: the raw schema states ``X | null``, the
  published one just ``X`` (omission means null). The server still accepts the null
  (``test_unknown_args.py::test_explicit_null_equals_omission_kwargs``).
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from jsonschema import Draft202012Validator
from tool_surface import TOOL_NAMES

from kwin_mcp.server import build_server, raw_schemas

if TYPE_CHECKING:
    from collections.abc import Iterator

SAMPLES = json.loads(
    (Path(__file__).parent / "fixtures" / "schema_samples.json").read_text(encoding="utf-8")
)
_REPLACEMENTS: tuple[object, ...] = (None, "s", 1, 1.5, True, [], {})

type Path_ = tuple[str | int, ...]


def _leaves(value: object, prefix: Path_ = ()) -> Iterator[Path_]:
    """The path of every value nested in ``value`` (not ``value`` itself)."""
    if isinstance(value, dict):
        for key, sub in value.items():
            yield (*prefix, key)
            yield from _leaves(sub, (*prefix, key))
    elif isinstance(value, list):
        for index, sub in enumerate(value):
            yield (*prefix, index)
            yield from _leaves(sub, (*prefix, index))


def _objects(value: object, prefix: Path_ = ()) -> Iterator[Path_]:
    """The path of every object in ``value``, itself included."""
    if isinstance(value, dict):
        yield prefix
        for key, sub in value.items():
            yield from _objects(sub, (*prefix, key))
    elif isinstance(value, list):
        for index, sub in enumerate(value):
            yield from _objects(sub, (*prefix, index))


def _at(value: Any, path: Path_) -> Any:
    for step in path:
        value = value[step]
    return value


def _mutations(sample: dict[str, Any]) -> Iterator[tuple[dict[str, Any], str | None]]:
    """Mutated copies of ``sample``; the tag names the field set to null, if any."""
    for key in sample:
        yield {k: v for k, v in sample.items() if k != key}, None
    for path in _objects(sample):
        mutated = copy.deepcopy(sample)
        _at(mutated, path)["bogus_key"] = 1
        yield mutated, None
    for path in _leaves(sample):
        for replacement in _REPLACEMENTS:
            mutated = copy.deepcopy(sample)
            _at(mutated, path[:-1])[path[-1]] = copy.deepcopy(replacement)
            tag = str(path[-1]) if replacement is None else None
            yield mutated, tag


def _nullable_optional_fields(schema: dict[str, Any]) -> set[str]:
    """Names of the properties the raw schema declares as ``X | null`` defaulting to null."""
    names: set[str] = set()
    pending: list[object] = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for name, sub in node.get("properties", {}).items():
                arms = sub.get("anyOf", []) if isinstance(sub, dict) else []
                if {"type": "null"} in arms and sub.get("default", 0) is None:
                    names.add(name)
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return names


@pytest.fixture(scope="module")
def schema_pairs() -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    raw = raw_schemas()
    compact = {tool.name: tool.input_schema for tool in asyncio.run(build_server().list_tools())}
    assert set(raw) == set(compact) == set(TOOL_NAMES)
    return {name: ({**raw[name], "additionalProperties": False}, compact[name]) for name in raw}


@pytest.mark.qa("QA-SURF-015")
def test_raw_and_compact_schemas_accept_the_same_corpus(
    schema_pairs: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    assert set(SAMPLES["valid"]) == set(TOOL_NAMES)
    checked = 0
    for name, (raw, compact) in schema_pairs.items():
        assert list(raw.get("properties", {})) == list(compact.get("properties", {})), name
        assert raw.get("required", []) == compact.get("required", []), name
        raw_validator = Draft202012Validator(raw)
        compact_validator = Draft202012Validator(compact)
        nullable = _nullable_optional_fields(raw)

        for sample in SAMPLES["valid"][name]:
            assert raw_validator.is_valid(sample), (name, sample)
            assert compact_validator.is_valid(sample), (name, sample)
        for sample in SAMPLES["invalid"].get(name, []):
            assert not raw_validator.is_valid(sample), (name, sample)
            assert not compact_validator.is_valid(sample), (name, sample)

        for base in SAMPLES["valid"][name]:
            for sample, null_field in _mutations(base):
                checked += 1
                raw_ok = raw_validator.is_valid(sample)
                compact_ok = compact_validator.is_valid(sample)
                if raw_ok == compact_ok:
                    continue
                # The one allowed difference: null on an optional nullable field.
                assert (raw_ok, compact_ok) == (True, False), (name, sample)
                assert null_field in nullable, (name, sample)
    assert checked > 1_000
