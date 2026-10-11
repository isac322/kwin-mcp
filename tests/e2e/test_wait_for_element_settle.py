"""wait_for_elements settles matches whose window list was changing (KWin-free).

A match reported ``unavailable (windows-changed)`` usually belongs to a window
that is still opening; the wait re-polls briefly so the caller gets screen
coordinates. Permanent reasons return at once, and the settle window is
bounded so a match that never maps still returns well before the timeout.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from kwin_mcp import accessibility

if TYPE_CHECKING:
    import pytest

POLL_INTERVAL_MS = 50
TIMEOUT_MS = 5000
# The settle window is 1 s; allow scheduling slack but stay far below the timeout.
SETTLE_BOUND_S = 2.0


def _element(unavailable: str) -> accessibility.ElementInfo:
    mapped = not unavailable
    return accessibility.ElementInfo(
        role="push button",
        name="Seven",
        description="",
        states=["enabled"],
        x=10,
        y=20,
        width=30,
        height=40,
        actions=["Press"],
        text="",
        value=None,
        value_max=None,
        children_count=0,
        depth=2,
        has_extents=True,
        mapped=mapped,
        unavailable=unavailable,
    )


def _scripted_find(
    monkeypatch: pytest.MonkeyPatch, answers: list[str], *, repeat_last: bool
) -> list[str]:
    """Answer each find_elements call with one element per scripted reason."""
    calls: list[str] = []

    def find(
        query: str, app_name: str = "", states: list[str] | None = None
    ) -> list[accessibility.ElementInfo]:
        index = len(calls)
        if index >= len(answers):
            assert repeat_last, calls
            index = len(answers) - 1
        calls.append(answers[index])
        return [_element(answers[index])]

    monkeypatch.setattr(accessibility, "find_elements", find)
    return calls


def _wait() -> list[accessibility.ElementInfo]:
    return accessibility.wait_for_elements(
        "Seven", timeout_ms=TIMEOUT_MS, poll_interval_ms=POLL_INTERVAL_MS
    )


def test_windows_changed_match_is_repolled_until_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scripted_find(monkeypatch, ["windows-changed", ""], repeat_last=False)

    elements = _wait()

    assert calls == ["windows-changed", ""], calls
    assert [(e.mapped, e.unavailable) for e in elements] == [(True, "")], elements


def test_persistent_windows_changed_returns_within_the_settle_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _scripted_find(monkeypatch, ["windows-changed"], repeat_last=True)

    started = time.monotonic()
    elements = _wait()
    elapsed = time.monotonic() - started

    assert len(calls) >= 2, calls
    assert elapsed < SETTLE_BOUND_S, elapsed
    assert [e.unavailable for e in elements] == ["windows-changed"], elements


def test_permanent_unavailable_reason_returns_on_the_first_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _scripted_find(monkeypatch, ["no-extents"], repeat_last=False)

    elements = _wait()

    assert calls == ["no-extents"], calls
    assert [e.unavailable for e in elements] == ["no-extents"], elements
