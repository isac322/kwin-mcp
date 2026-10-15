"""The engine's public surface is exactly the 29 tools (QA-CLI-001, engine side).

``kwin-mcp-cli`` lists the engine's public callables as its commands, so any
extra public method (a removed 0.11 name, a ``*_data`` twin) would leak into the
CLI. The annotations must also resolve at runtime: the CLI validates JSON
arguments against them with pydantic.
"""

from __future__ import annotations

import inspect
import typing

import pytest
from tool_surface import REMOVED_TOOL_NAMES, TOOL_NAMES

from kwin_mcp.core import AutomationEngine


def _public_callables() -> set[str]:
    engine = AutomationEngine()
    return {
        name for name in dir(engine) if not name.startswith("_") and callable(getattr(engine, name))
    }


@pytest.mark.qa("QA-CLI-001")
def test_engine_public_callables_are_exactly_the_29_tools() -> None:
    public = _public_callables()

    assert public == set(TOOL_NAMES)
    assert not public & set(REMOVED_TOOL_NAMES)
    assert not [name for name in public if name.endswith("_data")]


@pytest.mark.qa("QA-CLI-001")
def test_engine_has_no_public_data_attributes() -> None:
    engine = AutomationEngine()

    public = {name for name in vars(engine) if not name.startswith("_")}

    assert public == set()


@pytest.mark.qa("QA-CLI-001")
@pytest.mark.parametrize("name", TOOL_NAMES)
def test_engine_method_annotations_resolve_at_runtime(name: str) -> None:
    method = getattr(AutomationEngine, name)

    hints = typing.get_type_hints(method)

    parameters = [p for p in inspect.signature(method).parameters if p != "self"]
    assert set(parameters) <= set(hints)
    assert "return" in hints
    # Every tool body has a first docstring line for the CLI's help listing.
    assert (method.__doc__ or "").strip().splitlines()[0]
