"""Type conversion of ``key=value`` arguments in ``kwin-mcp-cli``, without a KWin session.

The CLI turns a command line into kwargs via ``_parse_args``/``_parse_value``, which
resolve parameter annotations and convert the string values. Optional parameters
annotated ``T | None`` (a ``types.UnionType``) have no ``__origin__`` attribute, so
``_parse_value`` must unwrap them with ``typing.get_origin``/``typing.get_args``;
these tests would leave the value a raw string on Python 3.12-3.14 otherwise.
"""

from __future__ import annotations

import pytest

import kwin_mcp.cli as cli
from kwin_mcp.core import AutomationEngine


def _bool_or_none_probe(flag: bool | None = None) -> None:
    """Probe callable for Optional params; no public engine method takes ``bool | None``."""


def test_screenshot_optional_args_convert_to_their_inner_types() -> None:
    engine = AutomationEngine()
    kwargs = cli._parse_args(engine.screenshot, "max_edge=640 region=[0,0,100,100]")

    assert kwargs["max_edge"] == 640
    assert type(kwargs["max_edge"]) is int
    assert kwargs["region"] == [0, 0, 100, 100]
    assert type(kwargs["region"]) is list


def test_launch_app_optional_dict_arg_converts_to_dict() -> None:
    engine = AutomationEngine()
    kwargs = cli._parse_args(engine.launch_app, 'command=kcalc env=\'{"A":"1"}\'')

    assert kwargs["command"] == "kcalc"
    assert kwargs["env"] == {"A": "1"}
    assert type(kwargs["env"]) is dict


def test_optional_bool_arg_converts_to_bool() -> None:
    kwargs = cli._parse_args(_bool_or_none_probe, "flag=true")

    assert kwargs["flag"] is True
    assert type(kwargs["flag"]) is bool


def test_plain_int_annotation_still_converts() -> None:
    """Regression: non-Optional annotations keep working through the same path."""
    engine = AutomationEngine()
    kwargs = cli._parse_args(engine.accessibility_tree, "max_depth=3")

    assert kwargs["max_depth"] == 3
    assert type(kwargs["max_depth"]) is int


def test_quoted_json_list_arg_converts_to_list() -> None:
    engine = AutomationEngine()
    kwargs = cli._parse_args(engine.find_ui_elements, 'query=x states=\'["focused"]\'')

    assert kwargs["states"] == ["focused"]


def test_unquoted_list_arg_is_rejected() -> None:
    """``states=[focused]`` is not JSON: it fails loudly instead of being mis-parsed.

    v0.10.0 silently turned it into a wrong value; the CLI prints the ValueError as a
    one-line ``Error: ...`` (see test_cli_errors.py).
    """
    engine = AutomationEngine()
    with pytest.raises(ValueError):
        cli._parse_args(engine.find_ui_elements, "query=x states=[focused]")
