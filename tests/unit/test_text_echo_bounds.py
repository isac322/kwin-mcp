"""Bounded echo of caller text in keyboard_type results (F96).

The engine runs over the recording fake EIS client, and the unicode route is
replaced by a stub that reports success, so only the result is observed: text
up to 200 characters is echoed as its repr, longer text as its length only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import kwin_mcp.input
from kwin_mcp.core import AutomationEngine
from kwin_mcp.input import InputBackend
from kwin_mcp.models import TypeRoute

if TYPE_CHECKING:
    from kwin_mcp.models import TypeMethod

    from ._fakes import FakeEngine

_METHODS: list[tuple[TypeMethod, str, str]] = [
    ("keys", "a", "Typed: "),
    ("unicode", "é", "Typed unicode: "),
]


def _typed_through(monkeypatch: pytest.MonkeyPatch, fake_engine: FakeEngine) -> list[str]:
    """Record what reaches each typing route; the key route still sends real fake events."""
    typed: list[str] = []
    original = InputBackend.keyboard_type

    def keys(self: InputBackend, text: str) -> None:
        typed.append(text)
        original(self, text)

    def unicode(self: InputBackend, text: str, *_args: object, **_kwargs: object) -> TypeRoute:
        typed.append(text)
        return TypeRoute(kind="wtype")

    monkeypatch.setattr(kwin_mcp.input.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(InputBackend, "keyboard_type", keys)
    monkeypatch.setattr(InputBackend, "keyboard_type_unicode", unicode)
    monkeypatch.setattr(fake_engine.engine, "_session_env", dict)
    return typed


@pytest.mark.parametrize(("method", "char", "prefix"), _METHODS)
def test_text_at_the_limit_is_echoed_verbatim(
    monkeypatch: pytest.MonkeyPatch,
    fake_engine: FakeEngine,
    method: TypeMethod,
    char: str,
    prefix: str,
) -> None:
    typed = _typed_through(monkeypatch, fake_engine)
    text = char * 200

    result = fake_engine.engine.keyboard_type(text, method=method)

    assert result.echo == repr(text)
    assert result.chars == 200
    assert result.render().splitlines()[0] == f"{prefix}{text!r}"
    assert typed == [text]


@pytest.mark.parametrize(("method", "char", "prefix"), _METHODS)
def test_text_beyond_the_limit_is_echoed_as_its_length(
    monkeypatch: pytest.MonkeyPatch,
    fake_engine: FakeEngine,
    method: TypeMethod,
    char: str,
    prefix: str,
) -> None:
    typed = _typed_through(monkeypatch, fake_engine)
    text = char * 201

    result = fake_engine.engine.keyboard_type(text, method=method)

    assert result.echo == "<201 characters>"
    assert result.chars == 201
    assert result.render().splitlines()[0] == f"{prefix}<201 characters>"
    assert typed == [text]


def test_keyboard_type_rejects_unsupported_characters_without_a_session() -> None:
    """The key-map check runs before any session or input backend is looked up."""
    with pytest.raises(ValueError, match="'é', '€'") as excinfo:
        AutomationEngine().keyboard_type("héllo€!")
    assert str(excinfo.value) == (
        "keyboard_type cannot type 'é', '€'.\nRetry with method=\"unicode\"."
    )
