"""kwin-mcp-cli argument parsing: JSON for model, union, list and dict parameters."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from kwin_mcp.cli import KwinMcpShell, _parse_args, _parse_value
from kwin_mcp.core import AutomationEngine
from kwin_mcp.models import (
    ElementPoint,
    Locator,
    PartialRect,
    Point,
    PointerMoveResult,
    Rect,
    RefAddr,
    StrictAddr,
    Target,
    TypedArg,
    Wait,
    WindowSelector,
)
from kwin_mcp.server import KwinServer, ServerFlags

if TYPE_CHECKING:
    from ._fakes import FakeEngine


@pytest.mark.qa("QA-CLI-002")
@pytest.mark.parametrize(
    ("command", "arguments", "expected"),
    [
        (
            "pointer_click",
            """target='{"x":10,"y":20}' button=right count=2 modifiers=ctrl hold_ms=30""",
            {
                "target": Point(x=10, y=20),
                "button": "right",
                "count": 2,
                "modifiers": "ctrl",
                "hold_ms": 30,
            },
        ),
        (
            "pointer_click",
            """target='{"ref":"e1.3","dx":0,"dy":0}'""",
            {"target": ElementPoint(ref="e1.3", dx=0, dy=0)},
        ),
        (
            "element_act",
            """element='{"strict":{"query":"OK","app":"kcalc"}}' action=Press""",
            {
                "element": StrictAddr(strict=Locator(query="OK", app="kcalc")),
                "action": "Press",
            },
        ),
        (
            "element_act",
            """element='{"ref":"e2.7"}' action=Press""",
            {"element": RefAddr(ref="e2.7"), "action": "Press"},
        ),
        (
            "window_list",
            """filter='{"kind":["normal"]}'""",
            {"filter": WindowSelector(kind=["normal"])},
        ),
        (
            "element_query",
            """locator='{"app":"kcalc","states":["focused"]}' max_depth=3 """
            """wait='{"until":"absent","timeout_ms":100}'""",
            {
                "locator": Locator(app="kcalc", states=["focused"]),
                "max_depth": 3,
                "wait": Wait(until="absent", timeout_ms=100),
            },
        ),
        (
            "dbus_call",
            """service=org.kde.KWin path=/KWin interface=org.kde.KWin method=reconfigure """
            """args='["string:a", {"type": "int32", "value": 5}]'""",
            {
                "service": "org.kde.KWin",
                "path": "/KWin",
                "interface": "org.kde.KWin",
                "method": "reconfigure",
                "args": ["string:a", TypedArg(type="int32", value=5)],
            },
        ),
        (
            "app_launch",
            """command=true env='{"A":"1"}'""",
            {"command": "true", "env": {"A": "1"}},
        ),
        (
            "screen_capture",
            """region='{"x":0,"y":0,"width":10,"height":10}' max_edge=0 include_cursor=true""",
            {"region": Rect(x=0, y=0, width=10, height=10), "max_edge": 0, "include_cursor": True},
        ),
        (
            "window_place",
            """window='{"id":"{k1}"}' frame='{"x":5}' desktop=d2""",
            {"window": WindowSelector(id="{k1}"), "frame": PartialRect(x=5), "desktop": "d2"},
        ),
        (
            "pointer_drag",
            """from='{"x":1,"y":2}' to='{"ref":"e1.4"}'""",
            {"from_": Point(x=1, y=2), "to": ElementPoint(ref="e1.4")},
        ),
        (
            "pointer_click",
            """{"target": {"x": 1, "y": 2}, "button": "left"}""",
            {"target": Point(x=1, y=2), "button": "left"},
        ),
        (
            "touch_swipe",
            """{"from": {"x": 1, "y": 2}, "to": {"x": 3, "y": 4}, "fingers": 2}""",
            {"from_": Point(x=1, y=2), "to": Point(x=3, y=4), "fingers": 2},
        ),
    ],
)
def test_parse_value_model_and_union_params(
    command: str, arguments: str, expected: dict[str, object]
) -> None:
    """Object parameters arrive as validated model instances, never as strings."""
    method = getattr(AutomationEngine(), command)
    parsed = _parse_args(method, arguments)
    assert parsed == expected
    for name, value in expected.items():
        assert type(parsed[name]) is type(value), name


@pytest.mark.qa("QA-CLI-002")
@pytest.mark.parametrize(
    "value",
    [
        """{"x": 1}""",
        """{"x": 1, "y": 2, "ref": "e1.1"}""",
        """{}""",
        "not json",
    ],
)
def test_parse_value_rejects_invalid_targets(value: str) -> None:
    """A malformed object is a ValueError (pydantic's), printed as an Error: line."""
    with pytest.raises(ValidationError):
        _parse_value(value, Target)


def test_scalars_keep_their_conversions() -> None:
    assert _parse_value("true", bool) is True
    assert _parse_value("no", bool) is False
    assert _parse_value("7", int) == 7
    assert _parse_value("7", int | None) == 7
    assert _parse_value("ctrl+c", str | None) == "ctrl+c"
    assert _parse_value("left", Any) == "left"


@pytest.mark.parametrize(
    ("command", "arguments"),
    [
        ("pointer_hold", """phase=movee target='{"x":100,"y":100}'"""),
        ("pointer_hold", '{"phase": "movee", "target": {"x": 100, "y": 100}}'),
        ("keyboard_hold", "phase=relase keys=shift"),
        ("keyboard_type", "text=hi method=unicod"),
        ("pointer_scroll", """target='{"x":1,"y":2}' delta=1 axis=diagonal"""),
    ],
)
def test_invalid_enum_is_refused_before_the_engine_runs(command: str, arguments: str) -> None:
    """A misspelled choice fails parsing: 0.11-style fallthrough would send input."""
    with pytest.raises(ValidationError):
        _parse_args(getattr(AutomationEngine(), command), arguments)


@pytest.mark.qa("QA-CLI-002")
def test_cli_output_equals_mcp_text(
    monkeypatch: pytest.MonkeyPatch,
    fake_engine: FakeEngine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI prints exactly the text the MCP server returns for the same call."""
    engine = fake_engine.engine
    received: list[object] = []

    def pointer_move(target: Target, capture: object = None) -> PointerMoveResult:
        received.append(target)
        return PointerMoveResult(at=Point(x=10, y=20), warnings=["example warning"])

    monkeypatch.setattr(engine, "pointer_move", pointer_move)
    shell = KwinMcpShell(engine)
    shell.default("""pointer_move target='{"x":10,"y":20}'""")
    cli_text = capsys.readouterr().out

    result = asyncio.run(
        KwinServer(engine, ServerFlags()).call_tool("pointer_move", {"target": {"x": 10, "y": 20}})
    )
    assert isinstance(result, CallToolResult)
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert cli_text == block.text + "\n"
    assert received == [Point(x=10, y=20), Point(x=10, y=20)]
