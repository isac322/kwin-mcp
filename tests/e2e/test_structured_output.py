"""Structured output of every 1.0 tool over the installed MCP stdio server.

Every tool publishes an object-rooted ``outputSchema``. A successful call returns
``structuredContent`` that validates against it, and exactly one ``TextContent``
equal to the result model's ``render()`` that carries every id, ref, path, uri,
coordinate and size (S §3.8, plan row 37). Errors carry no ``structuredContent``.

The tours below make one successful call per tool: a virtual session for the
session, app, element, window, input, clipboard and escape-hatch tools, and a
live session on a nested visual KWin for ``session_connect``, ``screen_capture``
(the virtual backend cannot capture in the container) and ``window_close`` in
every consent outcome.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from atspi_oracle import AtspiOracle
from jsonschema import Draft202012Validator
from mcp.types import TextContent
from mcp_harness import (
    ERAS,
    MODELS,
    TOOL_NAMES,
    ScriptedUser,
    render_structured,
    running_client,
    running_mcp_server,
)
from session_harness import live_kwin
from visual_harness import nested_visual_kwin
from window_zoo import (
    APP_ID as ZOO_APP_ID,
)
from window_zoo import (
    INJECTION_TEXT,
    UNICODE_BUTTON,
    UNICODE_CAPTION,
    UNICODE_LABEL,
    ZooProcess,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from kwin_oracle import OracleWindow
    from mcp_harness import McpTestClient, ModernMcpTestClient
    from session_harness import LiveKWin
    from visual_harness import NestedVisualKWin

SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800
APP_TIMEOUT_MS = 15_000
POLL_SECONDS = 0.25
MISSING_NAME = "definitely-not-a-real-kwin-mcp-element"
MISSING_WINDOW_ID = "{00000000-0000-0000-0000-000000000000}"
CLIPBOARD_TEXT = "kwin-mcp structured clipboard"
NO_SESSION_LINES = (
    "No session running.",
    "Call session_start (virtual) or session_connect (live desktop).",
)
SESSION_FREE_TOOLS = ("session_start", "session_connect", "session_stop", "session_info")
SESSION_BOUND_TOOLS = tuple(name for name in TOOL_NAMES if name not in SESSION_FREE_TOOLS)
LIST_NAMES = {
    "service": "org.freedesktop.DBus",
    "path": "/",
    "interface": "org.freedesktop.DBus",
    "method": "ListNames",
}
_POINT = {"x": 40, "y": 40}
# Valid arguments for every session-bound tool, so the call reaches the session gate.
NO_SESSION_ARGUMENTS: dict[str, dict[str, Any]] = {
    "app_launch": {"command": "kcalc"},
    "app_log": {"pid": 1},
    "screen_capture": {},
    "element_query": {},
    "element_act": {"element": {"ref": "e1.1"}, "action": "Press"},
    "window_list": {},
    "desktop_list": {},
    "window_focus": {"window": {"app_id": "kcalc"}},
    "window_place": {"window": {"app_id": "kcalc"}, "frame": {"x": 0}},
    "window_close": {"window": {"app_id": "kcalc"}},
    "pointer_click": {"target": _POINT},
    "pointer_move": {"target": _POINT},
    "pointer_scroll": {"target": _POINT, "delta": 1},
    "pointer_drag": {"from": _POINT, "to": {"x": 80, "y": 80}},
    "pointer_hold": {"phase": "press", "target": _POINT},
    "keyboard_press": {"keys": "Return"},
    "keyboard_type": {"text": "a"},
    "keyboard_hold": {"phase": "press", "keys": "shift"},
    "touch_tap": {"target": _POINT},
    "touch_swipe": {"from": _POINT, "to": {"x": 80, "y": 80}},
    "touch_pinch": {"center": _POINT, "start_distance": 100, "end_distance": 50},
    "clipboard_read": {},
    "clipboard_write": {"text": "a"},
    "dbus_call": LIST_NAMES,
    "wayland_list": {},
}
assert tuple(NO_SESSION_ARGUMENTS) == SESSION_BOUND_TOOLS, set(NO_SESSION_ARGUMENTS) ^ set(
    SESSION_BOUND_TOOLS
)
# Leaves the text must carry verbatim (QA-MCP-002): identifiers, refs, paths and uris.
# app_id is an application name, not an identifier: the S consent refusal texts (PR #87,
# verbatim) name only the window id.
_TOKEN_KEYS = frozenset({"id", "ref", "path", "uri"})
_TOKEN_SUFFIXES = ("_id", "_path", "_uri")
_NOT_TOKEN_KEYS = frozenset({"app_id"})
_COORDINATE_KEYS = frozenset({"x", "y", "width", "height"})


@pytest.fixture
def anyio_backend() -> str:
    """The MCP stdio client is exercised on asyncio."""
    return "asyncio"


@dataclass(frozen=True)
class Call:
    """One successful tool call: its arguments, structured content and text."""

    tool: str
    arguments: dict[str, Any]
    structured: dict[str, Any]
    text: str


class Recorder:
    """``call_structured`` that keeps every call of a tour."""

    def __init__(self, client: McpTestClient | ModernMcpTestClient) -> None:
        self.client = client
        self.calls: list[Call] = []

    async def __call__(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = arguments or {}
        structured, text = await self.client.call_structured(tool, args)
        self.calls.append(Call(tool, args, structured, text))
        return structured


async def _wait_for_window(record: Recorder, app: str) -> dict[str, Any]:
    """Poll window_list until exactly one window whose app_id contains ``app`` is listed."""
    deadline = anyio.current_time() + APP_TIMEOUT_MS / 1000
    windows: list[dict[str, Any]] = []
    while anyio.current_time() < deadline:
        windows = (await record("window_list"))["windows"]
        matches = [window for window in windows if app in window["app_id"].lower()]
        if len(matches) == 1:
            return matches[0]
        await anyio.sleep(POLL_SECONDS)
    pytest.fail(f"{app} never appeared once in window_list: {windows}")


async def _wait_until_gone(record: Recorder, window_id: str) -> None:
    deadline = anyio.current_time() + 10
    while anyio.current_time() < deadline:
        if (await record("window_list", {"filter": {"id": window_id}}))["windows"] == []:
            return
        await anyio.sleep(POLL_SECONDS)
    pytest.fail(f"window {window_id} was not closed")


async def _wait_for_kcalc_seven(record: Recorder) -> str:
    """Launch-wait for KCalc's 7 key (AT-SPI name "Seven") and return its ref."""
    found = await record(
        "element_query",
        {"locator": {"app": "kcalc", "query": "Seven"}, "wait": {"timeout_ms": APP_TIMEOUT_MS}},
    )
    assert found["wait"]["satisfied"] is True, found
    return next(e["ref"] for e in found["elements"] if e["name"] == "Seven")


async def _virtual_tour(record: Recorder) -> None:
    """Every tool a virtual session serves, plus session_stop/session_info without one."""
    client = record.client
    no_session = await record("session_info")
    assert no_session["session"] is None, no_session
    not_stopped = await record("session_stop")
    assert not_stopped["stopped"] is False, not_stopped

    started = await record(
        "session_start",
        {
            "screen_width": SCREEN_WIDTH,
            "screen_height": SCREEN_HEIGHT,
            "enable_clipboard": True,
        },
    )
    try:
        assert started["kind"] == "virtual", started
        assert started["input_backend"] == "eis", started
        info = await record("session_info")
        assert info["session"]["kind"] == "virtual", info

        launched = await record("app_launch", {"command": "kcalc"})
        seven = await _wait_for_kcalc_seven(record)
        tree = await record("element_query", {"locator": {"app": "kcalc"}, "max_depth": 3})
        assert tree["wait"] is None, tree
        assert tree["elements"], tree
        timed_out = await record(
            "element_query",
            {
                "locator": {"app": "kcalc", "query": MISSING_NAME},
                "wait": {"timeout_ms": 300, "poll_interval_ms": 50},
            },
        )
        assert timed_out["wait"]["satisfied"] is False, timed_out
        await record("app_log", {"pid": launched["pid"]})
        await record("element_act", {"element": {"ref": seven}, "action": "Press"})

        window_id = (await _wait_for_window(record, "kcalc"))["id"]
        await record("desktop_list")
        await record("window_focus", {"window": {"id": window_id}})
        await record("window_place", {"window": {"id": window_id}, "frame": {"x": 40, "y": 40}})

        start, end = {"x": 600, "y": 500}, {"x": 700, "y": 560}
        await record("pointer_move", {"target": start})
        await record("pointer_click", {"target": {"ref": seven}})
        await record("pointer_scroll", {"target": start, "delta": 1})
        await record(
            "pointer_drag",
            {"from": start, "to": end, "waypoints": [{"x": 650, "y": 530, "dwell_ms": 10}]},
        )
        pressed = await record("pointer_hold", {"phase": "press", "target": start})
        assert pressed["held"]["buttons"] == ["left"], pressed
        await record("pointer_hold", {"phase": "move", "target": end})
        released = await record("pointer_hold", {"phase": "release", "target": end})
        assert released["held"] == {"buttons": [], "keys": []}, released
        await record("keyboard_press", {"keys": "Escape"})
        await record("keyboard_type", {"text": "12"})
        down = await record("keyboard_hold", {"phase": "press", "keys": "shift"})
        assert down["held"]["keys"] == ["shift"], down
        up = await record("keyboard_hold", {"phase": "release", "keys": "shift"})
        assert up["held"] == {"buttons": [], "keys": []}, up
        await record("touch_tap", {"target": start})
        await record("touch_swipe", {"from": start, "to": end, "duration_ms": 100})
        await record(
            "touch_pinch",
            {"center": start, "start_distance": 200, "end_distance": 100, "duration_ms": 100},
        )
        await record("clipboard_write", {"text": CLIPBOARD_TEXT})
        read = await record("clipboard_read")
        assert read["text"] == CLIPBOARD_TEXT, read
        await record("dbus_call", LIST_NAMES)
        await record("wayland_list", {"filter": "wl_seat"})

        closed = await record("window_close", {"window": {"id": window_id}})
        assert closed["requested"] is True, closed
        assert closed["consent"] == "not_required", closed
        await _wait_until_gone(record, window_id)
    except BaseException:
        await client.call_result("session_stop")
        raise
    stopped = await record("session_stop")
    assert stopped["stopped"] is True, stopped
    assert stopped["kind"] == "virtual", stopped
    assert (await record("session_info"))["session"] is None


async def _live_tour(record: Recorder, user: ScriptedUser, visual: NestedVisualKWin) -> None:
    """session_connect, screen_capture and window_close in every live consent outcome."""
    connected = await record(
        "session_connect",
        {"dbus_address": visual.dbus_address, "wayland_display": visual.wayland_display},
    )
    try:
        assert connected["kind"] == "live", connected
        await record("app_launch", {"command": "kcalc"})
        await _wait_for_kcalc_seven(record)
        window_id = (await _wait_for_window(record, "kcalc"))["id"]

        frame = await record("screen_capture")
        assert frame["width"] > 0 and frame["height"] > 0, frame
        moved = await record(
            "pointer_move", {"target": {"x": 100, "y": 100}, "capture": {"delays_ms": [0, 50]}}
        )
        assert [f["delay_ms"] for f in moved["frames"]] == [0, 50], moved

        for answer, consent in (
            ("unconfirmed", "not_confirmed"),
            ("decline", "declined"),
            ("cancel", "cancelled"),
        ):
            user.answer = answer
            refused = await record("window_close", {"window": {"id": window_id}})
            assert refused["requested"] is False, refused
            assert refused["consent"] == consent, refused
        user.answer = "confirm"
        closed = await record("window_close", {"window": {"id": window_id}})
        assert closed["requested"] is True, closed
        assert closed["consent"] == "accepted", closed
        assert len(user.requests) == 4, user.requests
        await _wait_until_gone(record, window_id)
    except BaseException:
        await record.client.call_result("session_stop")
        raise
    stopped = await record("session_stop")
    assert stopped["kind"] == "live", stopped


async def _tour(era: str) -> list[Call]:
    """The virtual tour and the live tour, each over its own server in ``era``."""
    async with running_client(era) as client:
        virtual = Recorder(client)
        await _virtual_tour(virtual)
    user = ScriptedUser()
    with nested_visual_kwin() as visual:
        env = {"DISPLAY": visual.x_display, "KWIN_MCP_X11_SCREENSHOT": "1"}
        async with running_client(era, env=env, elicitation_callback=user) as client:
            live = Recorder(client)
            await _live_tour(live, user, visual)
    calls = [*virtual.calls, *live.calls]
    assert {call.tool for call in calls} == set(TOOL_NAMES), set(TOOL_NAMES) - {
        call.tool for call in calls
    }
    return calls


def _tokens(value: object, key: str = "") -> list[tuple[str, str]]:
    """(key, token) for every id/ref/path/uri, integer coordinate and size_kb leaf."""
    if isinstance(value, dict):
        return [token for k, v in value.items() for token in _tokens(v, k)]
    if isinstance(value, list):
        return [token for item in value for token in _tokens(item, key)]
    if (
        isinstance(value, str)
        and value
        and key not in _NOT_TOKEN_KEYS
        and (key in _TOKEN_KEYS or key.endswith(_TOKEN_SUFFIXES))
    ):
        return [(key, value)]
    if key in _COORDINATE_KEYS and isinstance(value, int) and not isinstance(value, bool):
        return [(key, str(value))]
    if key == "size_kb" and isinstance(value, int | float):
        return [(key, f"{value:.1f}")]
    return []


# ── ported 0.11 checks ───────────────────────────────────────────────────


@pytest.mark.anyio
async def test_typed_tools_advertise_object_output_schemas() -> None:
    async with running_mcp_server() as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert set(tools) == set(TOOL_NAMES), set(tools) ^ set(TOOL_NAMES)
    for name in TOOL_NAMES:
        schema = tools[name].output_schema
        assert isinstance(schema, dict), (name, schema)
        Draft202012Validator.check_schema(schema)
        assert schema.get("type") == "object", (name, schema)
        properties = schema.get("properties")
        assert isinstance(properties, dict), (name, schema)
        model_properties = MODELS[name].model_json_schema(mode="serialization")["properties"]
        assert set(properties) == set(model_properties), (name, schema)


@pytest.mark.anyio
@pytest.mark.parametrize("name", SESSION_BOUND_TOOLS)
async def test_typed_tools_without_session_are_errors_without_structured_content(
    name: str,
) -> None:
    async with running_mcp_server() as client:
        lines = await client.call_tool_error(name, NO_SESSION_ARGUMENTS[name])
        # The session-free tools answer without a session, with structured content.
        info, info_text = await client.call_structured("session_info")
        stop, stop_text = await client.call_structured("session_stop")

    # The SDK keeps its "Error executing tool <name>: " prefix (contract, QA-MCP-016).
    what, remedy = NO_SESSION_LINES
    assert lines == (f"Error executing tool {name}: {what}", remedy), lines
    assert info["session"] is None, info
    assert info_text == "No session running.", info_text
    assert stop["stopped"] is False, stop
    assert stop_text == "No session running.", stop_text


def _assert_element(element: dict[str, Any]) -> None:
    assert isinstance(element["role"], str) and element["role"], element
    assert isinstance(element["name"], str), element
    assert isinstance(element["ref"], str) and element["ref"], element
    assert isinstance(element["depth"], int) and element["depth"] >= 0, element
    assert isinstance(element["states"], list), element
    if element["rect"] is None:
        assert element["unavailable"], element
    else:
        assert all(
            isinstance(element["rect"][key], int) for key in ("x", "y", "width", "height")
        ), element


@pytest.mark.anyio
async def test_typed_tools_return_schema_valid_structured_content_matching_text() -> None:
    async with running_mcp_server() as client:
        record = Recorder(client)
        no_session = await record("session_info")
        assert no_session == {
            "session": None,
            "apps": [],
            "held": {"buttons": [], "keys": []},
            "warnings": [],
        }, no_session
        await record(
            "session_start", {"screen_width": SCREEN_WIDTH, "screen_height": SCREEN_HEIGHT}
        )
        try:
            await record("app_launch", {"command": "kwrite"})
            kwrite = await _wait_for_window(record, "kwrite")
            list_call = record.calls[-1]
            assert f'- {kwrite["app_id"]} "{kwrite["caption"]}"' in list_call.text, list_call.text
            assert f"    id:     {kwrite['id']}" in list_call.text.splitlines(), list_call.text
            for key in ("frame", "client"):
                assert all(isinstance(kwrite[key][k], int) for k in _COORDINATE_KEYS), kwrite

            await record("window_focus", {"window": {"id": kwrite["id"]}})
            deadline = anyio.current_time() + 10
            while True:
                active = await record("window_list", {"filter": {"active": True}})
                if [window["id"] for window in active["windows"]] == [kwrite["id"]]:
                    break
                assert anyio.current_time() < deadline, active
                await anyio.sleep(POLL_SECONDS)
            assert record.calls[-1].text.startswith("Active window:\n"), record.calls[-1].text

            missing = await record("window_list", {"filter": {"id": MISSING_WINDOW_ID}})
            assert missing["windows"] == [], missing
            assert record.calls[-1].text == f"No window with id {MISSING_WINDOW_ID!r}."

            tree = await record(
                "element_query",
                {"locator": {"app": "kwrite"}, "wait": {"timeout_ms": APP_TIMEOUT_MS}},
            )
            tree_text = record.calls[-1].text
            elements = tree["elements"]
            assert elements, tree_text[:500]
            assert tree_text.startswith(f"Found {len(elements)} elements matching app='kwrite':\n")
            assert elements[0]["depth"] == 0, elements[0]
            assert elements[0]["role"] == "application", elements[0]
            assert "kwrite" in elements[0]["name"].lower(), elements[0]
            assert any(element["depth"] > 0 for element in elements), tree_text[:500]
            for element in elements:
                _assert_element(element)

            empty = await record("element_query", {"locator": {"app": MISSING_NAME}})
            assert empty == {
                "locator": {"app": MISSING_NAME, "query": None, "role": None, "states": None},
                "elements": [],
                "wait": None,
            }, empty
            assert record.calls[-1].text == f"No elements found matching app='{MISSING_NAME}'"

            found = await record("element_query", {"locator": {"query": "KWrite", "app": "kwrite"}})
            found_text = record.calls[-1].text
            assert found["locator"]["query"] == "KWrite", found
            assert found["locator"]["states"] is None, found
            assert found["elements"], found_text[:500]
            assert found_text.startswith(
                f"Found {len(found['elements'])} elements matching query='KWrite', app='kwrite':\n"
            ), found_text[:500]
            assert any("kwrite" in element["name"].lower() for element in found["elements"])
            for element in found["elements"]:
                _assert_element(element)

            states = ["enabled"]
            none_found = await record(
                "element_query",
                {"locator": {"query": MISSING_NAME, "app": "kwrite", "states": states}},
            )
            assert none_found["elements"] == [], none_found
            assert none_found["locator"]["states"] == states, none_found
            assert record.calls[-1].text == (
                f"No elements found matching query='{MISSING_NAME}', states={states}, app='kwrite'"
            )
        except BaseException:
            await client.call_result("session_stop")
            raise
        stopped = await record("session_stop")
        assert stopped["stopped"] is True, stopped
        assert record.calls[-1].text == "Session stopped.", record.calls[-1].text

    for call in record.calls:
        assert call.text == render_structured(call.tool, call.structured)


# ── QA items ─────────────────────────────────────────────────────────────


@pytest.mark.qa("QA-MCP-001")
@pytest.mark.timeout(600)
@pytest.mark.parametrize("era", ERAS)
@pytest.mark.anyio
async def test_every_tool_structured_content_validates(era: str) -> None:
    """Every call of the tours validates against the raw pydantic schema as well."""
    calls = await _tour(era)

    violations: list[str] = []
    for call in calls:
        schema = MODELS[call.tool].model_json_schema(mode="serialization")
        Draft202012Validator.check_schema(schema)
        violations.extend(
            f"{call.tool}({call.arguments}): {error.message}"
            for error in Draft202012Validator(schema).iter_errors(call.structured)
        )
    assert violations == [], violations

    def values(tool: str, read: Callable[[dict[str, Any]], object]) -> list[object]:
        return [read(call.structured) for call in calls if call.tool == tool]

    consents = values("window_close", lambda s: s["consent"])
    assert set(consents) == {
        "not_required",
        "accepted",
        "not_confirmed",
        "declined",
        "cancelled",
    }, consents
    waits = values("element_query", lambda s: None if s["wait"] is None else s["wait"]["satisfied"])
    assert {None, True, False} <= set(waits), waits
    assert set(values("session_info", lambda s: s["session"] is None)) == {True, False}
    assert set(values("session_stop", lambda s: s["stopped"])) == {True, False}


@pytest.mark.qa("QA-MCP-002")
@pytest.mark.timeout(600)
@pytest.mark.parametrize("era", ERAS)
@pytest.mark.anyio
async def test_text_equals_render_and_is_lossless(era: str) -> None:
    """The text is the shared render() and carries every identifying leaf."""
    calls = await _tour(era)

    missing: list[str] = []
    for call in calls:
        assert call.text == render_structured(call.tool, call.structured), call
        missing.extend(
            f"{call.tool}: {key}={token!r}"
            for key, token in _tokens(call.structured)
            if token not in call.text
        )
    assert missing == [], missing
    kinds = {key for call in calls for key, _ in _tokens(call.structured)}
    assert {"id", "ref", "path", "uri", "x", "y", "width", "height", "size_kb"} <= kinds, kinds


@pytest.mark.qa("QA-MCP-004")
@pytest.mark.timeout(300)
@pytest.mark.anyio
async def test_echo_and_size_fragments_in_text() -> None:
    with nested_visual_kwin() as visual:
        env = {"DISPLAY": visual.x_display, "KWIN_MCP_X11_SCREENSHOT": "1"}
        async with running_mcp_server(env=env) as client:
            await client.call_structured(
                "session_connect",
                {"dbus_address": visual.dbus_address, "wayland_display": visual.wayland_display},
            )
            try:
                for text in ("hi", "b" * 200, 'say "hi"\nnow'):
                    typed, typed_text = await client.call_structured(
                        "keyboard_type", {"text": text}
                    )
                    assert typed["echo"] == repr(text), typed
                    assert typed["chars"] == len(text), typed
                    assert typed_text.split("\n")[0] == f"Typed: {text!r}", typed_text
                long_text = "a" * 201
                typed, typed_text = await client.call_structured(
                    "keyboard_type", {"text": long_text}
                )
                assert typed["echo"] == "<201 characters>", typed
                assert typed_text.split("\n")[0] == "Typed: <201 characters>", typed_text
                assert "aaaa" not in typed_text, typed_text

                for text in ("c" * 200, 'quote " and\nnewline'):
                    written, written_text = await client.call_structured(
                        "clipboard_write", {"text": text}
                    )
                    assert written == {"length": len(text), "echo": repr(text)}, written
                    assert written_text == f"Clipboard set: {text!r}", written_text
                written, written_text = await client.call_structured(
                    "clipboard_write", {"text": "c" * 201}
                )
                assert written == {"length": 201, "echo": "<201 characters>"}, written
                assert written_text == "Clipboard set: <201 characters>", written_text

                frame, frame_text = await client.call_structured("screen_capture")
                lines = frame_text.split("\n")
                assert lines[0] == (
                    f"Screenshot saved: {frame['path']} ({frame['size_kb']:.1f} KB)"
                ), frame_text
                assert lines[1] == frame["coordinate_space"], frame_text
                assert lines[1].startswith("Coordinate space: "), frame_text

                moved, moved_text = await client.call_structured(
                    "pointer_move",
                    {"target": {"x": 100, "y": 100}, "capture": {"delays_ms": [0, 50]}},
                )
                assert len(moved["frames"]) == 2, moved
                moved_lines = moved_text.split("\n")
                assert "Captured 2 frames:" in moved_lines, moved_text
                for burst in moved["frames"]:
                    line = f"  {burst['delay_ms']}ms: {burst['path']} ({burst['size_kb']:.1f} KB)"
                    assert line in moved_lines, moved_text
                    index = moved_lines.index(line)
                    assert moved_lines[index + 1] == f"    {burst['coordinate_space']}", moved_text
                    assert moved_lines[index + 1].startswith("    Coordinate space: "), moved_text
            finally:
                await client.call_result("session_stop")


async def _live_zoo_calls(
    live: LiveKWin,
) -> tuple[tuple[dict[str, Any], str, int], tuple[dict[str, Any], str, int]]:
    """element_query of the zoo and window_list over stdio on a live session.

    Each item is (structuredContent, text, number of content blocks).
    """
    async with running_mcp_server() as client:
        await client.call_structured(
            "session_connect",
            {"dbus_address": live.dbus_address, "wayland_display": live.wayland_display},
        )
        try:
            out: list[tuple[dict[str, Any], str, int]] = []
            for tool, arguments in (
                ("element_query", {"locator": {"app": ZOO_APP_ID}}),
                ("window_list", {}),
            ):
                raw = await client.call_result(tool, arguments)
                assert not raw.is_error, raw
                assert len(raw.content) == 1, raw.content
                assert isinstance(raw.content[0], TextContent), raw.content
                structured, text = await client.call_structured(tool, arguments)
                out.append((structured, text, len(raw.content)))
        finally:
            await client.call_result("session_stop")
    return out[0], out[1]


def _oracle_names(live: LiveKWin, names: tuple[str, ...]) -> Counter[tuple[str | None, str]]:
    nodes = AtspiOracle(live.dbus_address).wait_for(
        lambda found: all(any(n.name == name for n in found) for name in names),
        app=ZOO_APP_ID,
    )
    return Counter((n.role, n.name) for n in nodes if n.name in names)


def _zoo_window(live: LiveKWin, test: Callable[[OracleWindow], bool]) -> OracleWindow:
    windows = live.wait_for_windows(lambda found: any(test(w) for w in found))
    return next(w for w in windows if test(w))


@pytest.mark.qa("QA-MCP-028")
@pytest.mark.slow
@pytest.mark.timeout(600)
@pytest.mark.anyio
async def test_unicode_round_trip() -> None:
    names = (UNICODE_BUTTON, UNICODE_LABEL)
    with (
        live_kwin() as live,
        ZooProcess(
            wayland_display=live.wayland_display,
            dbus_address=live.dbus_address,
            open_commands=("unicode",),
        ) as zoo,
    ):
        zoo.expect(f"window: {UNICODE_CAPTION}")
        oracle_window = _zoo_window(live, lambda w: w.caption == UNICODE_CAPTION)
        oracle_names = _oracle_names(live, names)
        (elements, elements_text, _), (windows, windows_text, _) = await _live_zoo_calls(live)

    listed = Counter((e["role"], e["name"]) for e in elements["elements"] if e["name"] in names)
    assert listed == oracle_names, (listed, oracle_names)
    for name in names:
        assert f'"{name}"' in elements_text, elements_text
    window = next(w for w in windows["windows"] if w["id"] == oracle_window.id)
    assert window["caption"] == oracle_window.caption == UNICODE_CAPTION, window
    assert f'- {window["app_id"]} "{UNICODE_CAPTION}"' in windows_text, windows_text
    for text in (elements_text, windows_text):
        assert "\\u202e" not in text and "\\U0001f" not in text, text
    assert "\u202e" in windows_text, windows_text


@pytest.mark.qa("QA-ELEM-043")
@pytest.mark.timeout(300)
@pytest.mark.anyio
async def test_injection_shaped_text_is_data() -> None:
    names = (INJECTION_TEXT,)
    with (
        live_kwin() as live,
        ZooProcess(
            wayland_display=live.wayland_display,
            dbus_address=live.dbus_address,
            open_commands=("injection",),
        ) as zoo,
    ):
        zoo.expect(f"window: {INJECTION_TEXT}")
        oracle_window = _zoo_window(live, lambda w: "ignore previous instructions" in w.caption)
        oracle_names = _oracle_names(live, names)
        (
            (elements, elements_text, blocks),
            (windows, windows_text, window_blocks),
        ) = await _live_zoo_calls(live)

    # The hostile name is data: one TextContent block, the exact string in structured
    # content, and in the text exactly once per element / window that carries it.
    assert blocks == window_blocks == 1
    injected = [e for e in elements["elements"] if e["name"] == INJECTION_TEXT]
    assert Counter((e["role"], e["name"]) for e in injected) == oracle_names, injected
    assert elements_text.count(INJECTION_TEXT) == len(injected), elements_text
    window = next(w for w in windows["windows"] if w["id"] == oracle_window.id)
    assert window["caption"] == oracle_window.caption == INJECTION_TEXT, window
    captioned = [w for w in windows["windows"] if w["caption"] == INJECTION_TEXT]
    assert windows_text.count(INJECTION_TEXT) == len(captioned) == 1, windows_text
