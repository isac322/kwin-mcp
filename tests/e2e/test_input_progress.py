"""keyboard_type progress reporting, driven through a key-swallowing EIS client.

KWin-free: the backend is built without connecting, and its client accepts key
events without sending them, so only the progress sequence is observed.
"""

from __future__ import annotations

import pytest

from kwin_mcp import input as kwin_input
from kwin_mcp import progress

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
