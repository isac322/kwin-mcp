"""Invalid window selectors and window_place arguments are teaching tool errors.

Engine-level calls need no KWin session: an argument error must not depend on
session state, so the engine raises ``ValueError`` with its own message even
when no session is running. Over MCP the window tools are gated on a running
session first (contract §1), so the MCP cases start one and check that the
call comes back with ``isError`` and the engine's teaching text.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import pytest
from kwin_oracle import KWinOracle
from mcp_harness import McpTestClient, running_mcp_server

from kwin_mcp.core import AutomationEngine
from kwin_mcp.models import WindowSelector

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

EMPTY_SELECTOR = (
    "window needs at least one field (id, app_id, caption, active or kind).",
    "Use window_list to find a window id.",
)
PLACE_NEEDS_A_PART = (
    "window_place needs at least one of frame/desktop.",
    "Pass frame={x, y, width, height} and/or desktop=<id from desktop_list>.",
)
SCREEN = {"screen_width": 1280, "screen_height": 800}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def _server_with_session() -> AsyncGenerator[tuple[McpTestClient, dict[str, Any]]]:
    async with running_mcp_server() as client:
        info, _ = await client.call_structured("session_start", SCREEN)
        try:
            yield client, info
        finally:
            await client.call_structured("session_stop")


async def _engine_error(client: McpTestClient, name: str, arguments: dict[str, Any]) -> list[str]:
    """The engine's error lines, after the SDK's ``Error executing tool`` prefix."""
    lines = list(await client.call_tool_error(name, arguments, lines=None))
    prefix = f"Error executing tool {name}: "
    assert lines and lines[0].startswith(prefix), lines
    lines[0] = lines[0].removeprefix(prefix)
    return lines


# ── engine, no session ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "selector",
    [WindowSelector(), WindowSelector(id=None, app_id=None, caption=None)],
    ids=["no-arguments", "empty-name"],
)
def test_window_focus_without_a_selector_is_rejected_without_a_session(
    selector: WindowSelector,
) -> None:
    with pytest.raises(ValueError, match=re.escape("\n".join(EMPTY_SELECTOR))):
        AutomationEngine().window_focus(selector)


def test_window_close_without_an_id_is_rejected_without_a_session() -> None:
    with pytest.raises(ValueError, match=re.escape("\n".join(EMPTY_SELECTOR))):
        AutomationEngine().window_close(WindowSelector())


@pytest.mark.parametrize(
    ("selector", "desktop", "message"),
    [
        (WindowSelector(), "desktop", EMPTY_SELECTOR),
        (WindowSelector(id="{window}"), None, PLACE_NEEDS_A_PART),
    ],
    ids=["no-window-id", "no-desktop-id"],
)
def test_window_place_desktop_without_an_id_is_rejected_without_a_session(
    selector: WindowSelector, desktop: str | None, message: tuple[str, str]
) -> None:
    with pytest.raises(ValueError, match=f"^{re.escape(chr(10).join(message))}$"):
        AutomationEngine().window_place(selector, desktop=desktop)


# ── over MCP ─────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [({}, "window"), ({"window": {}}, EMPTY_SELECTOR[0])],
    ids=["no-arguments", "empty-name"],
)
async def test_window_focus_without_a_selector_is_a_tool_error_over_mcp(
    arguments: dict[str, Any], expected: str
) -> None:
    async with _server_with_session() as (client, _):
        lines = await _engine_error(client, "window_focus", arguments)
    # isError alone also holds for other failures; the text proves the
    # argument check is what refused the call.
    assert expected in "\n".join(lines), lines
    if arguments:
        assert tuple(lines) == EMPTY_SELECTOR, lines


@pytest.mark.anyio
async def test_window_place_zero_width_is_a_tool_error_over_mcp() -> None:
    async with _server_with_session() as (client, _):
        lines = await _engine_error(
            client, "window_place", {"window": {"id": "x"}, "frame": {"width": 0}}
        )
    # The published schema (exclusiveMinimum 0) rejects the value before the
    # engine runs, so only the field name is pinned, not the wording.
    assert "width" in "\n".join(lines), lines


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"window": {}, "desktop": "desktop"}, EMPTY_SELECTOR),
        ({"window": {"id": "{window}"}}, PLACE_NEEDS_A_PART),
    ],
    ids=["no-window-id", "no-desktop-id"],
)
async def test_window_place_desktop_without_an_id_is_a_tool_error_over_mcp(
    arguments: dict[str, Any], message: tuple[str, str]
) -> None:
    async with _server_with_session() as (client, _):
        lines = await _engine_error(client, "window_place", arguments)
    assert tuple(lines) == message, lines


def _kind_items(schema: dict[str, Any]) -> dict[str, Any]:
    """The array arm of ``kind`` in the WindowSelector of a tool's input schema."""
    defs = schema.get("$defs", {})

    def resolve(node: dict[str, Any]) -> dict[str, Any]:
        if "$ref" in node:
            return resolve(defs[node["$ref"].rsplit("/", 1)[-1]])
        arms = [resolve(arm) for arm in node.get("anyOf", [])]
        objects = [arm for arm in arms if arm.get("type") in ("object", "array")]
        return objects[0] if arms else node

    properties = schema["properties"]
    selector = resolve(properties.get("window") or properties["filter"])
    return resolve(selector["properties"]["kind"])


@pytest.mark.qa("QA-WIN-008")
@pytest.mark.anyio
async def test_selector_validation() -> None:
    async with _server_with_session() as (client, info):
        await client.call_structured("app_launch", {"command": "kcalc"})
        oracle = KWinOracle(info["dbus_address"])
        windows = oracle.wait_for_windows(
            lambda ws: any("kcalc" in w.resource_class.lower() for w in ws)
        )
        kcalc = next(w for w in windows if "kcalc" in w.resource_class.lower())

        # {} on a mutator teaches; it never means "every window".
        for tool in ("window_focus", "window_close"):
            lines = await _engine_error(client, tool, {"window": {}})
            assert tuple(lines) == EMPTY_SELECTOR, (tool, lines)
        lines = await _engine_error(client, "window_place", {"window": {}, "frame": {"x": 0}})
        assert tuple(lines) == EMPTY_SELECTOR, lines

        # As a filter, {}, null and an omitted filter all list every window.
        listings = [
            await client.call_structured("window_list", arguments)
            for arguments in ({"filter": {}}, {"filter": None}, {})
        ]
        id_lists = [[w["id"] for w in structured["windows"]] for structured, _ in listings]
        assert id_lists[0] == id_lists[1] == id_lists[2], id_lists
        assert id_lists[0] == [w.id for w in oracle.windows()], id_lists
        assert kcalc.id in id_lists[0]

        # A kind outside the enum is rejected by the schema.
        lines = await _engine_error(client, "window_list", {"filter": {"kind": ["toolbar"]}})
        assert "kind" in "\n".join(lines), lines
        assert "toolbar" in "\n".join(lines), lines

        # kind:[] would mean "nothing" or "all"; it is refused instead (minItems 1).
        for tool, arguments in (
            ("window_list", {"filter": {"kind": []}}),
            ("window_close", {"window": {"kind": []}}),
        ):
            lines = await _engine_error(client, tool, arguments)
            assert "kind" in "\n".join(lines), (tool, lines)
            assert "validation error" in lines[0], (tool, lines)
        assert oracle.window(kcalc.id) is not None

        for tool in ("window_list", "window_focus", "window_place", "window_close"):
            schema = next(t for t in await client.list_tools() if t.name == tool).input_schema
            items = _kind_items(schema)
            assert items["minItems"] == 1, (tool, items)
            assert items["items"]["enum"] == [
                "normal",
                "dialog",
                "utility",
                "popup",
                "shell",
                "other",
            ], (tool, items)
