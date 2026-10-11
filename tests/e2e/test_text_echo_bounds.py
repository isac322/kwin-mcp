"""Bounded echo of caller text in keyboard_type results (KWin-free).

The engine's input backend is replaced by a stub that accepts any text, so only
the result string is observed: text up to 200 characters is echoed as its
repr, longer text as its length only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from kwin_mcp import core

if TYPE_CHECKING:
    from collections.abc import Callable


class _AcceptingInput:
    """Input backend stub that types nothing and reports success."""

    def __init__(self) -> None:
        self.typed: list[str] = []

    def keyboard_type(self, text: str) -> None:
        self.typed.append(text)

    def keyboard_type_unicode(
        self,
        text: str,
        env: dict[str, str] | None = None,
        focused_class: Callable[[], str | None] | None = None,
    ) -> bool:
        self.typed.append(text)
        return True


def _engine(monkeypatch: pytest.MonkeyPatch) -> tuple[core.AutomationEngine, _AcceptingInput]:
    engine = core.AutomationEngine()
    backend = _AcceptingInput()
    monkeypatch.setattr(engine, "_get_input", lambda: backend)
    monkeypatch.setattr(engine, "_session_env", lambda: {})
    return engine, backend


@pytest.mark.parametrize(
    ("method", "char", "prefix"),
    [("keyboard_type", "a", "Typed: "), ("keyboard_type_unicode", "é", "Typed unicode: ")],
)
def test_text_at_the_limit_is_echoed_verbatim(
    monkeypatch: pytest.MonkeyPatch, method: str, char: str, prefix: str
) -> None:
    engine, backend = _engine(monkeypatch)
    text = char * 200

    result = getattr(engine, method)(text)

    assert result == f"{prefix}{text!r}", result[:300]
    assert backend.typed == [text]


@pytest.mark.parametrize(
    ("method", "char", "prefix"),
    [("keyboard_type", "a", "Typed: "), ("keyboard_type_unicode", "é", "Typed unicode: ")],
)
def test_text_beyond_the_limit_is_echoed_as_its_length(
    monkeypatch: pytest.MonkeyPatch, method: str, char: str, prefix: str
) -> None:
    engine, backend = _engine(monkeypatch)
    text = char * 201

    result = getattr(engine, method)(text)

    assert result == f"{prefix}<201 characters>", result[:300]
    assert backend.typed == [text]


def test_keyboard_type_rejects_unsupported_characters_without_a_session() -> None:
    """The key-map check runs before the input backend is looked up."""
    with pytest.raises(ValueError, match="cannot type 'é', '€'; use keyboard_type_unicode"):
        core.AutomationEngine().keyboard_type("héllo€!")
