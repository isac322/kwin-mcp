"""keyboard_type progress reporting, driven through a key-swallowing EIS client.

KWin-free: the backend is built without connecting, and its client accepts key
events without sending them, so only the progress sequence is observed.
"""

from __future__ import annotations

import itertools
import types
from typing import TYPE_CHECKING

from kwin_mcp import input as kwin_input
from kwin_mcp import progress

if TYPE_CHECKING:
    import pytest

TEXT_LENGTH = 200


class _SilentClient:
    """Accepts every key event and sends nothing."""

    def keyboard_key(self, _keycode: int, _state: int) -> None:
        return None


def _backend(monkeypatch: pytest.MonkeyPatch) -> kwin_input.InputBackend:
    """An InputBackend that never connected, typing into ``_SilentClient``."""
    backend = object.__new__(kwin_input.InputBackend)
    monkeypatch.setattr(backend, "_client", _SilentClient(), raising=False)
    return backend


def test_keyboard_type_progress_ends_at_total(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kwin_input.time, "sleep", lambda _seconds: None)
    events: list[tuple[float, float | None, str | None]] = []

    def report(value: float, total: float | None, message: str | None) -> None:
        events.append((value, total, message))

    with progress.call_scope(report, collect_images=False):
        _backend(monkeypatch).keyboard_type("a" * TEXT_LENGTH)

    assert events, "keyboard_type reported no progress"
    assert events[-1][:2] == (TEXT_LENGTH, TEXT_LENGTH), events
    values = [value for value, _total, _message in events]
    assert values == sorted(set(values)), events
    assert all(value <= TEXT_LENGTH for value in values), events


def _type_with_clock(
    monkeypatch: pytest.MonkeyPatch, text: str, step_s: float
) -> list[tuple[float, float | None, str | None]]:
    """Type ``text`` while every ``time.monotonic`` read advances the clock by ``step_s``."""
    ticks = itertools.count()
    monkeypatch.setattr(
        kwin_input,
        "time",
        types.SimpleNamespace(
            sleep=lambda _seconds: None, monotonic=lambda: 100.0 + next(ticks) * step_s
        ),
    )
    events: list[tuple[float, float | None, str | None]] = []

    def report(value: float, total: float | None, message: str | None) -> None:
        events.append((value, total, message))

    with progress.call_scope(report, collect_images=False):
        _backend(monkeypatch).keyboard_type(text)
    return events


def test_keyboard_type_reports_every_character_once_the_interval_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clock step of exactly the interval reports each index; the last one is not repeated."""
    events = _type_with_clock(monkeypatch, "abcde", kwin_input._TYPE_REPORT_INTERVAL_S)

    assert events == [(index, 5, f"Typed {index}/5 characters") for index in range(1, 6)]


def test_keyboard_type_reports_intermediate_values_then_the_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Half-interval steps report every second index, then a final report at the total."""
    events = _type_with_clock(monkeypatch, "abcde", kwin_input._TYPE_REPORT_INTERVAL_S / 2)

    assert events == [
        (2, 5, "Typed 2/5 characters"),
        (4, 5, "Typed 4/5 characters"),
        (5, 5, "Typed 5/5 characters"),
    ]
