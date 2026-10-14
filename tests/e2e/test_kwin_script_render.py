"""Caller values reach KWin scripts only as JSON string literals, without a KWin session.

``focus_window`` used to fill ``{needle}`` and then ``{window_id}`` with two
sequential ``str.replace`` passes, so the second pass also rewrote the text the
first one inserted: ``app_name="{window_id}"`` pulled the id into the script
outside any string literal, and an id such as
``;workspace.activeWindow.closeWindow();//`` then ran as KWin script and closed
a real window in a live session. Each test captures the script that
``activate``/``close`` hands to KWin and checks that every placeholder line
holds exactly one JSON string literal decoding to the caller's value, while the
rest of the template is untouched.
"""

from __future__ import annotations

import json
import re

import pytest

from kwin_mcp import geometry

CLOSE_ACTIVE = ";workspace.activeWindow.closeWindow();//"
BREAK_OUT = '"; workspace.activeWindow.closeWindow(); var _x="'
PLACEHOLDER = re.compile(r"\{(needle|window_id)\}")


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    scripts: list[str] = []

    def _record(js_source: str, timeout: float = 5.0) -> str:
        scripts.append(js_source)
        return json.dumps({"found": False})

    monkeypatch.setattr(geometry, "run_kwin_script", _record)
    return scripts


def _assert_literals(template: str, script: str, values: dict[str, str]) -> None:
    """Each placeholder line must decode to its value; every other line is unchanged."""
    template_lines = template.splitlines()
    script_lines = script.splitlines()
    assert len(script_lines) == len(template_lines), script
    for template_line, script_line in zip(template_lines, script_lines, strict=True):
        placeholder = PLACEHOLDER.search(template_line)
        if placeholder is None:
            assert script_line == template_line, script
            continue
        prefix = template_line[: placeholder.start()]
        suffix = template_line[placeholder.end() :]
        assert script_line.startswith(prefix), script
        assert script_line.endswith(suffix), script
        literal = script_line[len(prefix) : len(script_line) - len(suffix)]
        # json.loads rejects anything past one complete string literal, so a
        # value that escaped its quotes fails here instead of decoding.
        assert json.loads(literal) == values[placeholder.group(1)], script


@pytest.mark.parametrize(
    ("app_name", "window_id"),
    [
        ("{window_id}", CLOSE_ACTIVE),
        ("{window_id}", BREAK_OUT),
        (BREAK_OUT, "{needle}"),
        ("{needle}{window_id}", "{window_id}{needle}" + CLOSE_ACTIVE),
    ],
)
def test_activate_keeps_each_value_inside_its_string_literal(
    monkeypatch: pytest.MonkeyPatch, app_name: str, window_id: str
) -> None:
    scripts = _capture(monkeypatch)

    geometry.activate(app_name=app_name, window_id=window_id)

    assert len(scripts) == 1
    _assert_literals(
        geometry._ACTIVATE_SCRIPT_TEMPLATE,
        scripts[0],
        {"needle": app_name.lower(), "window_id": window_id},
    )


@pytest.mark.parametrize("window_id", ["{window_id}", CLOSE_ACTIVE, BREAK_OUT])
def test_close_keeps_the_id_inside_its_string_literal(
    monkeypatch: pytest.MonkeyPatch, window_id: str
) -> None:
    scripts = _capture(monkeypatch)

    assert geometry.close(window_id) == {"found": False}

    assert len(scripts) == 1
    _assert_literals(geometry._CLOSE_SCRIPT_TEMPLATE, scripts[0], {"window_id": window_id})
