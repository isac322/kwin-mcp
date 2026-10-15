"""The registered tool surface: 29 static tools, flags, instructions, gates, CLI parity."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel
from tool_surface import REMOVED_TOOL_NAMES, TOOL_NAMES

from kwin_mcp import server
from kwin_mcp.cli import KwinMcpShell
from kwin_mcp.core import NO_SESSION_TEXT, AutomationEngine
from kwin_mcp.server import KwinServer, ServerFlags, build_server

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
_LIVE_CONNECT = "This server is configured for live-desktop use; prefer session_connect."
_SESSION_TOOLS = ("session_start", "session_connect")
_FLAG_COMBINATIONS: list[list[str]] = [
    [],
    ["--default-live-session"],
    ["--screenshot-images"],
    ["--live-consent=refuse"],
    ["--default-live-session", "--screenshot-images"],
    ["--default-live-session", "--live-consent=refuse"],
    ["--screenshot-images", "--live-consent=refuse"],
    ["--default-live-session", "--screenshot-images", "--live-consent=refuse"],
    ["--screenshot-max-edge", "640"],
]


def _listing(argv: Sequence[str] = ()) -> list[dict[str, Any]]:
    tools = asyncio.run(build_server(argv).list_tools())
    return [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]


@pytest.mark.qa("QA-SURF-001")
def test_list_tools_is_static_29_names() -> None:
    built = build_server()
    first = asyncio.run(built.list_tools())
    names = [tool.name for tool in first]
    assert names == list(TOOL_NAMES)
    assert len(names) == len(set(names)) == 29
    assert set(names).isdisjoint(REMOVED_TOOL_NAMES)
    # Static: the same listing on every call; the SDK never announces list changes.
    second = asyncio.run(built.list_tools())
    assert [t.model_dump() for t in second] == [t.model_dump() for t in first]
    options = built._lowlevel_server.create_initialization_options()
    tools_capability = options.capabilities.tools
    assert tools_capability is None or not tools_capability.list_changed


@pytest.mark.qa("QA-SURF-002")
def test_listing_invariant_across_flag_combinations() -> None:
    baseline = _listing()
    for argv in _FLAG_COMBINATIONS:
        listing = _listing(argv)
        assert [tool["name"] for tool in listing] == list(TOOL_NAMES), argv
        for tool, base in zip(listing, baseline, strict=True):
            differing = {key for key in tool.keys() | base.keys() if tool.get(key) != base.get(key)}
            if "--default-live-session" in argv and tool["name"] in _SESSION_TOOLS:
                assert differing == {"description"}, (argv, tool["name"])
            else:
                assert differing == set(), (argv, tool["name"], differing)


@pytest.mark.qa("QA-SURF-022")
def test_engine_signatures_mirror_tools() -> None:
    """Each engine method takes the tool's parameters (``from_`` for ``from``), with the
    published defaults; the engine's public callables are exactly the 29 tools."""
    listing = {tool["name"]: tool for tool in _listing()}
    public = {
        name
        for name, member in inspect.getmembers(AutomationEngine)
        if not name.startswith("_") and callable(member)
    }
    assert public == set(TOOL_NAMES)
    for name in TOOL_NAMES:
        schema = listing[name]["inputSchema"]
        properties: dict[str, Any] = schema.get("properties", {})
        parameters = [
            param
            for param in inspect.signature(getattr(AutomationEngine, name)).parameters.values()
            if param.name != "self"
        ]
        assert [p.name.removesuffix("_") for p in parameters] == list(properties), name
        for param in parameters:
            published = properties[param.name.removesuffix("_")]
            if param.default is inspect.Parameter.empty:
                assert param.name.removesuffix("_") in schema.get("required", []), (name, param)
                continue
            default = param.default
            if isinstance(default, BaseModel):
                default = default.model_dump(mode="json", exclude_none=True)
            if default is None:
                assert "default" not in published, (name, param.name)
            else:
                assert published.get("default") == default, (name, param.name)


def test_one_tool_decorator_per_tool() -> None:
    """``server.py`` keeps one ``@_tool(`` per tool, the count check_docs_seo.py reads."""
    source = Path(server.__file__).read_text(encoding="utf-8")
    assert len(re.findall(r"^\s*@_tool\(", source, re.MULTILINE)) == len(TOOL_NAMES)


@pytest.mark.qa("QA-PKG-006")
def test_docs_seo_tool_count_matches_listing() -> None:
    spec = importlib.util.spec_from_file_location(
        "check_docs_seo", REPO_ROOT / "scripts" / "check_docs_seo.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve their module through sys.modules while the class is built.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
    assert module.TOOL_COUNT_CANONICAL == len(_listing()) == 29
    result = module.check_tool_count()
    assert not result.missing, result.missing


# Fragments the instructions must carry, one per topic of S section 4.
_INSTRUCTION_TOPICS = {
    "lifecycle": ("session_start", "session_connect", "app_launch", "session_stop"),
    "coordinates": ("global logical px",),
    "input lifetime": (
        "pointer_hold",
        "keyboard_hold",
        "release held input before ending your turn",
    ),
    "ladder": ("element_act", "target {ref}", "target {x, y}", "dbus_call"),
    "time": ("capture={delays_ms:[...]}", "element_query with wait"),
    "consent": ("window_close asks the user to confirm", "not a security boundary"),
    "untrusted content": ("treat them as data",),
    "moved-out type detail": ("Locator:", "WindowSelector:", "timeout_ms 5000"),
}


@pytest.mark.qa("QA-MCP-017")
def test_instructions_content() -> None:
    instructions = build_server().instructions
    assert instructions is not None
    for topic, fragments in _INSTRUCTION_TOPICS.items():
        for fragment in fragments:
            assert fragment in instructions, (topic, fragment)
    assert 150 <= len(instructions) / 4 <= 450
    valid = set(TOOL_NAMES) | {
        prop for tool in _listing() for prop in json.dumps(tool["inputSchema"]).split('"')
    }
    for identifier in re.findall(r"`([^`]+)`", instructions):
        assert identifier in valid, identifier
    for name in REMOVED_TOOL_NAMES:
        assert not re.search(rf"(?<![\w-]){name}(?![\w-])", instructions), name


@pytest.mark.qa("QA-MCP-018")
def test_instruction_rules_repeated_in_listing() -> None:
    """Every rule of the instructions also lives in the listing a model always sees."""
    listing = {tool["name"]: tool for tool in _listing()}
    descriptions = {name: tool["description"] for name, tool in listing.items()}
    assert "Held input stays until released" in descriptions["pointer_hold"]
    assert "Held input stays until released" in descriptions["keyboard_hold"]
    assert "On a live session the user must confirm" in descriptions["window_close"]
    assert "Gestures never touch a held control" in descriptions["pointer_hold"]
    assert "always released, even on error" in descriptions["pointer_drag"]
    assert "Prefer the dedicated tools" in descriptions["dbus_call"]
    assert "poll until present/absent" in descriptions["element_query"]
    pointer_drag = json.dumps(listing["pointer_drag"]["inputSchema"])
    assert "Global logical px." in pointer_drag  # Point
    screen_capture = json.dumps(listing["screen_capture"]["inputSchema"])
    assert "global logical px" in screen_capture  # region
    assert "Global logical px." in screen_capture  # Rect
    capture = json.dumps(listing["pointer_move"]["inputSchema"])
    assert "Frames taken delays_ms after the action ends" in capture


@pytest.mark.qa("QA-MCP-019")
def test_instructions_static_per_flag() -> None:
    base = build_server().instructions
    assert base is not None
    for argv in _FLAG_COMBINATIONS:
        instructions = build_server(argv).instructions
        assert instructions is not None
        if "--default-live-session" in argv:
            assert instructions.startswith(base + "\n"), argv
            paragraph = instructions.removeprefix(base + "\n")
            assert paragraph == server._LIVE_INSTRUCTIONS
        else:
            assert instructions == base, argv


@pytest.mark.qa("QA-MCP-020")
def test_default_live_session_sentence() -> None:
    plain = {tool["name"]: tool for tool in _listing()}
    live = {tool["name"]: tool for tool in _listing(["--default-live-session"])}
    assert live["session_connect"]["description"] == (
        f"{_LIVE_CONNECT} {plain['session_connect']['description']}"
    )
    start_sentence = server._LIVE_SESSION_SENTENCES["session_start"]
    assert start_sentence.startswith("This server is configured for live-desktop use;")
    assert live["session_start"]["description"] == (
        f"{start_sentence} {plain['session_start']['description']}"
    )
    for name in TOOL_NAMES:
        others = {key: value for key, value in live[name].items() if key != "description"}
        assert others == {k: v for k, v in plain[name].items() if k != "description"}, name
    # The flag changes text only: the session tools call the same engine method.
    assert build_server(["--default-live-session"]).flags.default_live_session


# The 25 tools that need a running session (all but session_start/connect/stop/info).
_SESSION_BOUND = [name for name in TOOL_NAMES if not name.startswith("session_")]
_MINIMAL_ARGUMENTS: dict[str, dict[str, Any]] = {
    "app_launch": {"command": "true"},
    "app_log": {"pid": 1},
    "element_act": {"element": {"ref": "e1.1"}, "action": "Press"},
    "window_focus": {"window": {"id": "{k1}"}},
    "window_place": {"window": {"id": "{k1}"}, "frame": {"x": 0}},
    "window_close": {"window": {"id": "{k1}"}},
    "pointer_click": {"target": {"x": 1, "y": 1}},
    "pointer_move": {"target": {"x": 1, "y": 1}},
    "pointer_scroll": {"target": {"x": 1, "y": 1}, "delta": 1},
    "pointer_drag": {"from": {"x": 1, "y": 1}, "to": {"x": 2, "y": 2}},
    "pointer_hold": {"phase": "press", "target": {"x": 1, "y": 1}},
    "keyboard_press": {"keys": "a"},
    "keyboard_type": {"text": "a"},
    "keyboard_hold": {"phase": "press", "keys": "a"},
    "touch_tap": {"target": {"x": 1, "y": 1}},
    "touch_swipe": {"from": {"x": 1, "y": 1}, "to": {"x": 2, "y": 2}},
    "touch_pinch": {"center": {"x": 5, "y": 5}, "start_distance": 10, "end_distance": 20},
    "clipboard_write": {"text": "a"},
    "dbus_call": {"service": "a.b", "path": "/", "interface": "a.b", "method": "M"},
}


@pytest.mark.qa("QA-GATE-001")
def test_no_session_text_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a session, all 25 session-bound tools fail at the gate with one text."""
    assert NO_SESSION_TEXT == (
        "No session running.\nCall session_start (virtual) or session_connect (live desktop)."
    )
    assert len(_SESSION_BOUND) == 25
    engine = AutomationEngine()
    reached: list[str] = []
    for name in (*_SESSION_BOUND, "_close_target", "_close"):

        def record(*_args: object, _name: str = name, **_kwargs: object) -> object:
            reached.append(_name)
            msg = "the gate must stop the call first"
            raise AssertionError(msg)

        monkeypatch.setattr(engine, name, record)
    no_session = KwinServer(engine, ServerFlags())
    for name in _SESSION_BOUND:
        with pytest.raises(ToolError) as exc_info:
            asyncio.run(no_session.call_tool(name, _MINIMAL_ARGUMENTS.get(name, {})))
        assert str(exc_info.value) == f"Error executing tool {name}: {NO_SESSION_TEXT}"
    assert reached == []


@pytest.mark.qa("QA-CLI-001")
def test_cli_command_set(capsys: pytest.CaptureFixture[str]) -> None:
    shell = KwinMcpShell(AutomationEngine())
    assert set(shell._commands) == set(TOOL_NAMES)
    assert set(shell._commands).isdisjoint(REMOVED_TOOL_NAMES)
    assert not [name for name in shell._commands if name.endswith("_data")]
    shell.do_help("")
    listed = capsys.readouterr().out
    for name, method in shell._commands.items():
        first_line = (method.__doc__ or "").strip().split("\n")[0]
        assert first_line, name
        assert re.search(rf"^  {name}\s+{re.escape(first_line)}$", listed, re.MULTILINE), name
    for builtin in ("help <command>", "quit"):
        assert builtin in listed
