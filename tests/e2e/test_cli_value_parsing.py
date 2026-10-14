"""Type conversion of ``key=value`` arguments in ``kwin-mcp-cli``, without a KWin session.

The CLI turns a command line into kwargs via ``_parse_args``/``_parse_value``, which
resolve parameter annotations and convert the string values. Optional parameters
annotated ``T | None`` (a ``types.UnionType``) have no ``__origin__`` attribute, so
``_parse_value`` must unwrap them with ``typing.get_origin``/``typing.get_args``;
these tests would leave the value a raw string on Python 3.12-3.14 otherwise.

Booleans accept only true/false, yes/no and 1/0 in any case; any other literal raises
instead of silently becoming False.
"""

from __future__ import annotations

import shlex

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


@pytest.mark.parametrize("literal", ["true", "TRUE", "True", "1", "yes", "YES", "Yes"])
def test_true_literals_convert_to_true(literal: str) -> None:
    assert cli._parse_value(literal, bool) is True
    assert cli._parse_args(_bool_or_none_probe, f"flag={literal}")["flag"] is True


@pytest.mark.parametrize("literal", ["false", "FALSE", "False", "0", "no", "NO", "No"])
def test_false_literals_convert_to_false(literal: str) -> None:
    assert cli._parse_value(literal, bool) is False
    assert cli._parse_args(_bool_or_none_probe, f"flag={literal}")["flag"] is False


@pytest.mark.parametrize(
    "literal", ["banana", "", "2", "y", "n", "on", "off", "t", "f", " true", "false "]
)
def test_unknown_bool_literals_are_rejected(literal: str) -> None:
    """An unrecognised literal fails loudly; before, it silently became False."""
    with pytest.raises(ValueError, match="Invalid boolean value"):
        cli._parse_value(literal, bool)
    with pytest.raises(ValueError, match="Invalid boolean value"):
        cli._parse_args(_bool_or_none_probe, f"flag={shlex.quote(literal)}")


def test_plain_int_annotation_still_converts() -> None:
    """Regression: non-Optional annotations keep working through the same path."""
    engine = AutomationEngine()
    kwargs = cli._parse_args(engine.accessibility_tree, "max_depth=3")

    assert kwargs["max_depth"] == 3
    assert type(kwargs["max_depth"]) is int


def test_invoke_ui_action_exact_options_convert_to_bool_and_list() -> None:
    engine = AutomationEngine()
    kwargs = cli._parse_args(
        engine.invoke_ui_action,
        "query=save action_name=Press exact_name=true fallback_action_names='[\"click\"]'",
    )

    assert kwargs["exact_name"] is True
    assert kwargs["fallback_action_names"] == ["click"]
    assert type(kwargs["fallback_action_names"]) is list


def test_invoke_ui_action_exact_name_false_converts_to_false() -> None:
    engine = AutomationEngine()
    kwargs = cli._parse_args(
        engine.invoke_ui_action, "query=save action_name=Press exact_name=false"
    )

    assert kwargs["exact_name"] is False


def test_invoke_ui_action_rejects_an_unknown_exact_name_literal() -> None:
    """exact_name=banana must not quietly become False and fall back to substring matching."""
    engine = AutomationEngine()

    with pytest.raises(ValueError, match="Invalid boolean value: 'banana'"):
        cli._parse_args(engine.invoke_ui_action, "query=Save action_name=Press exact_name=banana")


def test_invoke_ui_action_spoken_name_converts_to_bool() -> None:
    engine = AutomationEngine()
    kwargs = cli._parse_args(
        engine.invoke_ui_action, "query='save as' action_name=Press spoken_name=true"
    )

    assert kwargs["spoken_name"] is True


def test_invoke_ui_action_rejects_an_unknown_spoken_name_literal() -> None:
    """spoken_name=banana must not quietly become False and fall back to substring matching."""
    engine = AutomationEngine()

    with pytest.raises(ValueError, match="Invalid boolean value: 'banana'"):
        cli._parse_args(
            engine.invoke_ui_action, "query='save as' action_name=Press spoken_name=banana"
        )


def test_quoted_json_list_arg_converts_to_list() -> None:
    engine = AutomationEngine()
    kwargs = cli._parse_args(engine.find_ui_elements, "query=x states='[\"focused\"]'")

    assert kwargs["states"] == ["focused"]


def test_unquoted_list_arg_is_rejected() -> None:
    """``states=[focused]`` is not JSON: it fails loudly instead of being mis-parsed.

    v0.10.0 silently turned it into a wrong value; the CLI prints the ValueError as a
    one-line ``Error: ...`` (see test_cli_errors.py).
    """
    engine = AutomationEngine()
    with pytest.raises(ValueError):
        cli._parse_args(engine.find_ui_elements, "query=x states=[focused]")
