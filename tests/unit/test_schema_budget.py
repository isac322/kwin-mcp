"""Release gate 1: the model-visible ``tools/list`` stays within the 0.11-era budget.

Sizes are characters of compact JSON divided by four, the measure the design used
(not a tokenizer). V1 is what a model sees of each tool (name, title, description,
inputSchema, annotations); V2 drops title and annotations; "full" adds outputSchema.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import TYPE_CHECKING, Any

import pytest

from kwin_mcp.server import build_server

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

V1_BUDGET = 9_510
V2_BUDGET = 8_711
_V1_KEYS = ("name", "title", "description", "inputSchema", "annotations")
_V2_KEYS = ("name", "description", "inputSchema")

# Every combination of the flags that change the server: 2^3 plus a frame size limit.
_FLAGS = ("--default-live-session", "--screenshot-images", "--live-consent=refuse")
FLAG_COMBINATIONS: list[list[str]] = [
    [flag for flag, on in zip(_FLAGS, switches, strict=True) if on]
    for switches in itertools.product((False, True), repeat=len(_FLAGS))
] + [["--screenshot-max-edge", "640"]]


def _listing(argv: Sequence[str]) -> list[dict[str, Any]]:
    tools = asyncio.run(build_server(argv).list_tools())
    return [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]


def _size(tools: list[dict[str, Any]], keys: Sequence[str] | None = None) -> int:
    view = [tool if keys is None else {k: tool[k] for k in keys if k in tool} for tool in tools]
    text = json.dumps({"tools": view}, separators=(",", ":"), ensure_ascii=False)
    return round(len(text) / 4)


@pytest.mark.qa("QA-SURF-018")
@pytest.mark.parametrize("argv", FLAG_COMBINATIONS, ids=lambda argv: " ".join(argv) or "none")
def test_model_visible_listing_within_budget(
    argv: list[str], record_property: Callable[[str, object], None]
) -> None:
    tools = _listing(argv)
    v1 = _size(tools, _V1_KEYS)
    record_property("v1_tokens", v1)
    assert v1 <= V1_BUDGET, f"model-visible listing is {v1} chars/4, budget {V1_BUDGET}"


@pytest.mark.qa("QA-SURF-018")
def test_budget_measure_counts_every_visible_character() -> None:
    """Calibration: 40 more description characters raise V1 by 10."""
    tools = _listing([])
    before = _size(tools, _V1_KEYS)
    tools[0] = {**tools[0], "description": tools[0]["description"] + "x" * 40}
    assert _size(tools, _V1_KEYS) == before + 10
    # Output schemas are not model-visible.
    tools[1] = {**tools[1], "outputSchema": {"description": "y" * 400}}
    assert _size(tools, _V1_KEYS) == before + 10


@pytest.mark.qa("QA-SURF-020")
def test_record_v2_and_output_schema_size(record_property: Callable[[str, object], None]) -> None:
    tools = _listing([])
    v2 = _size(tools, _V2_KEYS)
    full = _size(tools)
    record_property("v2_tokens", v2)
    record_property("full_tokens", full)
    assert v2 <= V2_BUDGET, f"V2 is {v2} chars/4, the previous surface's V2 was {V2_BUDGET}"
    assert all("outputSchema" in tool for tool in tools)
