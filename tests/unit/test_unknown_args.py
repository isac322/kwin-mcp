"""``KwinServer.call_tool()``: removed tools, unknown argument keys, explicit null, unions.

Every call here runs in-process against a server whose engine methods are spies, so
a rejected call is proven to reach neither the engine nor a resolver.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from mcp.client.client import Client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ElicitRequestFormParams, ElicitResult, TextContent
from pydantic import TypeAdapter, ValidationError
from tool_surface import REMOVED_TOOL_NAMES, TOOL_NAMES

from kwin_mcp.models import ElementAddress, Target
from kwin_mcp.server import (
    KwinServer,
    ServerFlags,
    removed_tool_text,
    unknown_argument_text,
)

if TYPE_CHECKING:
    from mcp.client.session import ClientRequestContext
    from mcp.types import ElicitRequestParams, ErrorData

    from kwin_mcp.core import AutomationEngine

    from ._fakes import FakeElicitation, FakeEngine

# Every engine entry point a tool body or resolver can reach.
_ENGINE_ENTRY_POINTS = (*TOOL_NAMES, "_close_target", "_close")


@dataclass
class _Spy:
    """Replaces engine methods with recorders that fail after recording the call."""

    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch, engine: AutomationEngine) -> None:
        for name in _ENGINE_ENTRY_POINTS:
            monkeypatch.setattr(engine, name, self._recorder(name))

    def _recorder(self, name: str) -> Any:
        def method(*args: object, **kwargs: object) -> object:
            self.calls.append((name, {**{str(i): a for i, a in enumerate(args)}, **kwargs}))
            msg = f"spy: {name}"
            raise RuntimeError(msg)

        return method


@pytest.fixture
def spied(monkeypatch: pytest.MonkeyPatch, fake_engine: FakeEngine) -> tuple[KwinServer, _Spy]:
    """A server over the fake live engine whose every tool entry point is a spy."""
    spy = _Spy()
    spy.install(monkeypatch, fake_engine.engine)
    fake_engine.engine._clipboard_enabled = True
    return KwinServer(fake_engine.engine, ServerFlags()), spy


def _error(server: KwinServer, name: str, arguments: dict[str, Any]) -> str:
    with pytest.raises(ToolError) as exc_info:
        asyncio.run(server.call_tool(name, arguments))
    return str(exc_info.value)


# stale-name-scan: allow-begin (expected migration remedy per removed tool name)
_REMOVED_TOOL_REMEDIES = {
    "screenshot": "screen_capture(region, max_edge, include_cursor)",
    "accessibility_tree": "element_query(locator={app, role}, max_depth)",
    "find_ui_elements": "element_query(locator={query, app, states})",
    "wait_for_element": (
        "element_query(locator={query, app, states}, wait={timeout_ms, poll_interval_ms})"
    ),
    "list_windows": "element_query(max_depth=1)",
    "invoke_ui_action": "element_act(element={strict:{query, app}}, action)",
    "mouse_click": "pointer_click(target={x,y}, ...)",
    "mouse_move": "pointer_move(target={x,y})",
    "mouse_scroll": "pointer_scroll(target={x,y}, delta, axis, unit, steps)",
    "mouse_drag": "pointer_drag(from={x,y}, to={x,y}, ...)",
    "mouse_button_down": "pointer_hold(phase='press', button, target={x,y})",
    "mouse_button_up": "pointer_hold(phase='release', button, target={x,y})",
    "keyboard_type_unicode": "keyboard_type(text, method='unicode')",
    "keyboard_key": "keyboard_press(keys)",
    "keyboard_key_down": "keyboard_hold(phase='press', keys)",
    "keyboard_key_up": "keyboard_hold(phase='release', keys)",
    "touch_multi_swipe": "touch_swipe(from={x,y}, to={x,y}, fingers=3)",
    "clipboard_get": "clipboard_read()",
    "clipboard_set": "clipboard_write(text)",
    "launch_app": "app_launch(command, env)",
    "read_app_log": "app_log(pid, last_n_lines)",
    "focus_window": "window_focus(window={id} or {app_id, caption})",
    "window_geometry": "window_list(filter={app_id} or {id})",
    "active_window": "window_list(filter={active:true})",
    "window_set_geometry": "window_place(window={id}, frame={x, y, width, height})",
    "window_set_desktop": "window_place(window={id}, desktop)",
    "virtual_desktops": "desktop_list()",
    "wayland_info": "wayland_list(filter)",
}
# stale-name-scan: allow-end

# Every argument the previous surface had and 1.0 dropped or renamed, with the tool
# it is sent to and the remedy line (contract section 3.2, S section 5.2).
_REMOVED_ARGUMENT_ROWS: list[tuple[str, str, str]] = [
    ("session_start", "app_command", "Call app_launch(command) after session_start instead."),
    ("session_start", "env", "Pass env to app_launch(command, env) instead."),
    ("session_start", "screenshot_after_ms", "Use capture={delays_ms:[...]} instead."),
    ("pointer_click", "screenshot_after_ms", "Use capture={delays_ms:[...]} instead."),
    ("session_start", "keep_screenshots", "Use retention={artifacts:true} instead."),
    ("session_connect", "keep_screenshots", "Use retention={artifacts:true} instead."),
    ("session_start", "isolate_home", "Use home='isolated' instead."),
    ("session_start", "keep_home", "Use home='isolated' with retention={home:true} instead."),
    ("pointer_click", "x", "Use target={x,y} instead."),
    ("pointer_move", "y", "Use target={x,y} instead."),
    ("pointer_scroll", "x", "Use target={x,y} instead."),
    ("pointer_hold", "y", "Use target={x,y} instead."),
    ("touch_tap", "x", "Use target={x,y} instead."),
    ("window_place", "x", "Use frame={x, y, width, height} instead."),
    ("window_place", "y", "Use frame={x, y, width, height} instead."),
    ("window_place", "width", "Use frame={x, y, width, height} instead."),
    ("window_place", "height", "Use frame={x, y, width, height} instead."),
    ("pointer_drag", "from_x", "Use from={x,y} instead."),
    ("touch_swipe", "from_y", "Use from={x,y} instead."),
    ("pointer_drag", "to_x", "Use to={x,y} instead."),
    ("touch_swipe", "to_y", "Use to={x,y} instead."),
    ("touch_pinch", "center_x", "Use center={x,y} instead."),
    ("touch_pinch", "center_y", "Use center={x,y} instead."),
    ("window_focus", "window_id", "Use window={id} instead."),
    ("window_close", "window_id", "Use window={id} instead."),
    ("element_act", "window_id", "Use window={id} instead."),
    ("window_place", "desktop_id", "Use desktop=<id from desktop_list> instead."),
    ("element_query", "app_name", "Use locator={app} instead."),
    ("element_act", "app_name", "Use locator={app} instead."),
    ("window_focus", "app_name", "Use window={app_id} (or filter={app_id}) instead."),
    ("window_list", "app_name", "Use window={app_id} (or filter={app_id}) instead."),
    ("element_query", "query", "Use locator={query, role, states} instead."),
    ("element_query", "expected_states", "Use locator={query, role, states} instead."),
    ("element_query", "states", "Use locator={query, role, states} instead."),
    ("element_query", "role", "Use locator={query, role, states} instead."),
    ("element_query", "timeout_ms", "Use wait={timeout_ms, poll_interval_ms} instead."),
    ("element_query", "poll_interval_ms", "Use wait={timeout_ms, poll_interval_ms} instead."),
    ("element_act", "query", "Use element={strict:{query}} and action instead."),
    ("element_act", "action_name", "Use element={strict:{query}} and action instead."),
    ("pointer_click", "double", "Use count=2 or count=3 instead."),
    ("pointer_click", "triple", "Use count=2 or count=3 instead."),
    ("pointer_scroll", "horizontal", "Use axis='horizontal' instead."),
    ("pointer_scroll", "discrete", "Use unit='detent' instead."),
    ("keyboard_press", "key", "Use keys instead."),
    ("keyboard_hold", "key", "Use keys instead."),
    ("wayland_list", "filter_protocol", "Use filter instead."),
]


@pytest.mark.qa("QA-GATE-009")
def test_remedy_table_and_difflib(spied: tuple[KwinServer, _Spy]) -> None:
    """Unknown keys are ToolErrors naming the key and its replacement, before anything runs."""
    server, spy = spied
    # The table: every removed argument has a remedy row, not a guess.
    for tool, key, remedy in _REMOVED_ARGUMENT_ROWS:
        properties = server._properties[tool]
        assert key not in properties, (tool, key)
        assert unknown_argument_text(tool, key, properties) == (
            f"Unknown argument '{key}' for {tool}.\n{remedy}"
        )

    # The QA steps, end to end through call_tool().
    cases: list[tuple[str, dict[str, Any], str]] = [
        (
            "session_start",
            {"app_command": "kcalc"},
            "Unknown argument 'app_command' for session_start.\n"
            "Call app_launch(command) after session_start instead.",
        ),
        (
            "pointer_click",
            {"x": 10, "y": 10},
            "Unknown argument 'x' for pointer_click.\nUse target={x,y} instead.",
        ),
        (
            "screen_capture",
            {"screenshot_after_ms": [100]},
            "Unknown argument 'screenshot_after_ms' for screen_capture.\n"
            "Use capture={delays_ms:[...]} instead.",
        ),
        (
            "element_act",
            {"window_id": "w1", "element": {"ref": "e1.1"}, "action": "Press"},
            "Unknown argument 'window_id' for element_act.\nUse window={id} instead.",
        ),
        (
            "session_start",
            {"keep_screenshots": True},
            "Unknown argument 'keep_screenshots' for session_start.\n"
            "Use retention={artifacts:true} instead.",
        ),
        (
            "pointer_scroll",
            {"target": {"x": 1, "y": 1}, "delta": 3, "discrete": True, "horizontal": True},
            "Unknown argument 'discrete' for pointer_scroll.\nUse unit='detent' instead.",
        ),
        (
            "pointer_scroll",
            {"target": {"x": 1, "y": 1}, "delta": 3, "horizontal": True},
            "Unknown argument 'horizontal' for pointer_scroll.\nUse axis='horizontal' instead.",
        ),
        (
            "keyboard_type",
            {"text": "a", "unicode": True},
            "Unknown argument 'unicode' for keyboard_type.\n"
            "Valid arguments: text, method, capture.",
        ),
        (
            "pointer_click",
            {"target": {"x": 1, "y": 1}, "modifers": "ctrl"},
            "Unknown argument 'modifers' for pointer_click.\nDid you mean 'modifiers'?",
        ),
        (
            "session_stop",
            {"force": True},
            "Unknown argument 'force' for session_stop.\nsession_stop takes no arguments.",
        ),
    ]
    for tool, arguments, text in cases:
        assert _error(server, tool, arguments) == f"Error executing tool {tool}: {text}"
    assert spy.calls == []


@pytest.mark.qa("QA-GATE-008")
def test_removed_tool_names_point_to_replacement(spied: tuple[KwinServer, _Spy]) -> None:
    """Each of the 28 removed names is a one-line ToolError naming its replacement."""
    server, spy = spied
    assert set(_REMOVED_TOOL_REMEDIES) == set(REMOVED_TOOL_NAMES)
    for name in REMOVED_TOOL_NAMES:
        expected = (
            f"Unknown tool: {name}. Removed in kwin-mcp 1.0; use {_REMOVED_TOOL_REMEDIES[name]}."
        )
        assert removed_tool_text(name) == expected
        assert _error(server, name, {}) == expected
        assert "\n" not in expected
    # Any other unknown name keeps the SDK text.
    assert removed_tool_text("no_such_tool") is None
    assert _error(server, "no_such_tool", {}) == "Unknown tool: no_such_tool"
    assert spy.calls == []


def _elicitation_callback(fake: FakeElicitation) -> Any:
    async def callback(
        _context: ClientRequestContext, params: ElicitRequestParams
    ) -> ElicitResult | ErrorData:
        assert isinstance(params, ElicitRequestFormParams)
        action, content = fake.answer(params.message, dict(params.requested_schema))
        return ElicitResult.model_validate({"action": action, "content": content})

    return callback


@pytest.mark.qa("QA-GATE-011")
def test_unknown_key_check_runs_before_resolvers(
    spied: tuple[KwinServer, _Spy], fake_elicitation: FakeElicitation
) -> None:
    """An injected resolver name is an unknown key: no lookup, no prompt, no close."""
    server, spy = spied

    async def run() -> list[str]:
        texts: list[str] = []
        async with Client(
            server, elicitation_callback=_elicitation_callback(fake_elicitation), mode="legacy"
        ) as client:
            for arguments in (
                {"window": {"id": "w1"}, "consent": {"action": "accept"}},
                {"window": {"id": "w1"}, "window_target": {"window_id": "w1"}},
                {"window": {"id": "w1"}, "gate": None},
            ):
                result = await client.call_tool("window_close", arguments)
                assert result.is_error
                assert result.structured_content is None
                block = result.content[0]
                assert isinstance(block, TextContent)
                texts.append(block.text)
        return texts

    texts = asyncio.run(run())
    prefix = "Error executing tool window_close: Unknown argument"
    assert texts[0].startswith(f"{prefix} 'consent' ")
    assert texts[1].startswith(f"{prefix} 'window_target' ")
    assert texts[2].startswith(f"{prefix} 'gate' ")
    assert spy.calls == []
    assert fake_elicitation.requests == []


# (tool, arguments without the nullable ones, the nullable ones set to null)
_NULL_CASES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    ("pointer_move", {"target": {"x": 1, "y": 2}}, {"capture": None}),
    ("element_query", {}, {"wait": None}),
    ("window_list", {}, {"filter": None}),
    ("app_launch", {"command": "true"}, {"env": None}),
    ("screen_capture", {}, {"region": None, "max_edge": None}),
    ("pointer_click", {"target": {"x": 1, "y": 2}}, {"modifiers": None, "capture": None}),
]


@pytest.mark.qa("QA-SURF-016")
def test_explicit_null_equals_omission_kwargs(spied: tuple[KwinServer, _Spy]) -> None:
    """An explicit null on an optional parameter reaches the engine as the omitted default."""
    server, spy = spied
    for tool, base, nulls in _NULL_CASES:
        spy.calls.clear()
        _error(server, tool, base)
        _error(server, tool, {**base, **nulls})
        assert [name for name, _ in spy.calls] == [tool, tool]
        omitted, explicit = (kwargs for _, kwargs in spy.calls)
        assert omitted == explicit, tool


@pytest.mark.qa("QA-GATE-013")
def test_union_arm_overlap_rejected(spied: tuple[KwinServer, _Spy]) -> None:
    """A value matching both arms' keys matches neither: the error names both arms."""
    server, spy = spied
    for union, value, arms in (
        (Target, {"x": 1, "y": 2, "ref": "e1.1"}, ("Point", "ElementPoint")),
        (Target, {}, ("Point", "ElementPoint")),
        (ElementAddress, {"ref": "e1.1", "strict": {"query": "OK"}}, ("RefAddr", "StrictAddr")),
        (ElementAddress, {}, ("RefAddr", "StrictAddr")),
    ):
        with pytest.raises(ValidationError) as exc_info:
            TypeAdapter[object](union).validate_python(value)
        locations = {str(error["loc"][0]) for error in exc_info.value.errors()}
        assert set(arms) <= locations, (value, locations)

    for tool, arguments, arms in (
        ("pointer_click", {"target": {"x": 1, "y": 2, "ref": "e1.1"}}, ("Point", "ElementPoint")),
        ("pointer_click", {"target": {}}, ("Point", "ElementPoint")),
        (
            "element_act",
            {"element": {"ref": "e1.1", "strict": {"query": "OK"}}, "action": "Press"},
            ("RefAddr", "StrictAddr"),
        ),
        ("element_act", {"element": {}, "action": "Press"}, ("RefAddr", "StrictAddr")),
    ):
        text = _error(server, tool, arguments)
        assert text.startswith(f"Error executing tool {tool}: "), text
        for arm in arms:
            assert arm in text, (tool, arm, text)
    assert spy.calls == []
