"""The ``tools/list`` bytes a client receives over stdio.

``KwinServer.list_tools()`` post-processes the SDK listing (compacted schemas);
these tests prove the post-processed listing is what reaches the wire, unchanged
and stable, and that the release-gate budget and forbidden strings hold on the
wire bytes, not only in-process.
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from mcp_harness import TOOL_NAMES, raw_mcp_client, running_mcp_server

from kwin_mcp.server import build_server

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

pytestmark = pytest.mark.anyio

SNAPSHOT = Path(__file__).parents[1] / "unit" / "snapshots" / "tool_surface.json"
LISTINGS = 3
V1_BUDGET = 9_510
V1_KEYS = ("name", "title", "description", "inputSchema", "annotations")
_FLAGS = ("--default-live-session", "--screenshot-images", "--live-consent=refuse")
FLAG_COMBINATIONS: list[list[str]] = [
    [flag for flag, on in zip(_FLAGS, switches, strict=True) if on]
    for switches in itertools.product((False, True), repeat=len(_FLAGS))
] + [["--screenshot-max-edge", "640"]]
# A 0.11 or PR reference, a source location, an implementation note about clamping.
FORBIDDEN = re.compile(r"0\.11|PR #|\.py:|(?i:clamp)|\b[\w./-]+\.\w+:\d+\b")


@pytest.fixture
def anyio_backend() -> str:
    """The raw JSON-RPC client runs on asyncio."""
    return "asyncio"


async def _wire_listing(*flags: str) -> list[dict[str, Any]]:
    """The tools of one raw ``tools/list`` result, asserted to be a single page."""
    async with raw_mcp_client({}, *flags) as client:
        result = await client.request("tools/list", {})
    assert result.get("nextCursor") is None, result
    return result["tools"]


async def _in_process(argv: Sequence[str] = ()) -> list[dict[str, Any]]:
    tools = await build_server(list(argv)).list_tools()
    return [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]


def _v1(tools: list[dict[str, Any]]) -> int:
    view = [{key: tool[key] for key in V1_KEYS if key in tool} for tool in tools]
    return round(len(json.dumps({"tools": view}, separators=(",", ":"), ensure_ascii=False)) / 4)


def _strings(value: object, where: str) -> Iterator[tuple[str, str]]:
    """Every shipped ``title``/``description`` string inside a listing value."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("title", "description") and isinstance(item, str):
                yield f"{where}.{key}", item
            else:
                yield from _strings(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{where}[{index}]")


@pytest.mark.qa("QA-SURF-017")
async def test_stdio_listing_equals_snapshot_and_in_process() -> None:
    async with raw_mcp_client({}) as client:
        raw_listings: list[str] = []
        for _ in range(LISTINGS):
            message = await client.request_message("tools/list", {})
            assert "error" not in message, message
            assert message["result"].get("nextCursor") is None
            # Key order survives json.loads, so dumps without sort_keys compares it too.
            raw_listings.append(json.dumps(message["result"]["tools"], ensure_ascii=False))

    assert len(set(raw_listings)) == 1, "consecutive tools/list results differ"
    wire = json.loads(raw_listings[0])
    assert [tool["name"] for tool in wire] == list(TOOL_NAMES)

    in_process = await _in_process()
    assert _canonical(wire) == _canonical(in_process)

    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    # The snapshot records the model- and client-visible fields of each tool.
    keys = set().union(*(tool.keys() for tool in snapshot))
    assert {key for tool in wire for key in tool} >= keys
    wire_view = [{key: tool[key] for key in tool if key in keys} for tool in wire]
    assert _canonical(wire_view) == _canonical(snapshot)


def _canonical(listing: list[dict[str, Any]]) -> str:
    """One encoder for every side: sorted keys, so only content can differ."""
    return json.dumps(listing, ensure_ascii=False, sort_keys=True, indent=1)


@pytest.mark.qa("QA-SURF-018")
@pytest.mark.parametrize("flags", FLAG_COMBINATIONS, ids=lambda flags: " ".join(flags) or "none")
async def test_wire_listing_within_budget(
    flags: list[str], record_property: Callable[[str, object], None]
) -> None:
    wire = await _wire_listing(*flags)
    assert [tool["name"] for tool in wire] == list(TOOL_NAMES)
    v1 = _v1(wire)
    record_property("wire_v1_tokens", v1)
    assert v1 <= V1_BUDGET, f"model-visible wire listing is {v1} chars/4, budget {V1_BUDGET}"
    # The measure counts every visible character: 40 more description characters is +10.
    padded = [{**wire[0], "description": wire[0]["description"] + "x" * 40}, *wire[1:]]
    assert _v1(padded) == v1 + 10


@pytest.mark.parametrize("flags", FLAG_COMBINATIONS, ids=lambda flags: " ".join(flags) or "none")
async def test_wire_shipped_strings_have_no_forbidden_fragments(flags: list[str]) -> None:
    tools = await _wire_listing(*flags)
    async with running_mcp_server(*flags) as client:
        instructions = client.initialize_result.instructions
    assert isinstance(instructions, str), instructions
    texts = [*_strings(tools, "tools"), ("instructions", instructions)]
    offenders = [
        f"{where}: {match.group(0)!r}"
        for where, text in texts
        for match in FORBIDDEN.finditer(text)
    ]
    assert offenders == []
