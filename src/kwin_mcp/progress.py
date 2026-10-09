"""Per-call progress and screenshot-attachment scope for engine code.

MCP-independent: the server installs a scope around each tool call on the tool
thread; outside a scope (e.g. kwin-mcp-cli or direct engine use) report() and
record_image() are no-ops.

One call can report more than one progress sequence: an action counts its own
steps (e.g. touch_swipe 1..30), then the screenshot_after_ms burst restarts at
frame 1. When a report's raw value does not exceed the previous raw value, a
new phase begins: report() offsets the raw values by the last emitted value so
emitted progress (and totals) always increase, as the MCP spec requires.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

type ReportFn = Callable[[float, float | None, str | None], None]


@dataclass
class _Scope:
    """State of one tool call's scope."""

    report: ReportFn | None
    collect_images: bool
    images: list[Path] = field(default_factory=list)
    # Raw progress of the previous report; a non-increasing value opens a phase.
    last_raw: float = float("-inf")
    # Emitted value the current phase's raw values are offset by.
    phase_base: float = 0.0
    # Last value handed to report(); emissions must exceed it.
    last_emitted: float = float("-inf")


_scope: contextvars.ContextVar[_Scope | None] = contextvars.ContextVar(
    "kwin_mcp_call_scope", default=None
)


@contextmanager
def call_scope(report: ReportFn | None, *, collect_images: bool) -> Generator[_Scope]:
    """Install the scope for one tool call around the engine body invocation.

    Set/reset on the calling (tool) thread so nothing leaks into the next call
    on the reused worker thread or into helper threads.
    """
    scope = _Scope(report, collect_images)
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


def report(progress: float, total: float | None = None, message: str | None = None) -> None:
    """Emit progress for the current tool call; a no-op outside a scope.

    When ``progress`` does not exceed the previous raw value it marks a new
    phase and is emitted offset by the last emitted value; totals are offset
    the same way (``None`` stays ``None``). No emitted value is ever less than
    or equal to the last emitted one.
    """
    s = _scope.get()
    if s is None or s.report is None:
        return
    if progress <= s.last_raw:
        s.phase_base = max(s.last_emitted, 0.0)
    s.last_raw = progress
    value = s.phase_base + progress
    if value <= s.last_emitted:
        return
    s.last_emitted = value
    s.report(value, None if total is None else s.phase_base + total, message)


def record_image(path: Path) -> None:
    """Record a screenshot PNG produced by this call for optional attachment."""
    s = _scope.get()
    if s is not None and s.collect_images:
        s.images.append(path)
