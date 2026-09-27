"""Per-call progress and screenshot-attachment scope for engine code.

MCP-independent: the server installs a scope around each tool call on the tool
thread; outside a scope (e.g. kwin-mcp-cli or direct engine use) report() and
record_image() are no-ops.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

type ReportFn = Callable[[float, float | None, str | None], None]


@dataclass
class _Scope:
    """State of one tool call's scope."""

    report: ReportFn | None
    collect_images: bool
    images: list[Path] = field(default_factory=list)
    last_progress: float = float("-inf")


_scope: contextvars.ContextVar[_Scope | None] = contextvars.ContextVar(
    "kwin_mcp_call_scope", default=None
)


@contextmanager
def call_scope(report: ReportFn | None, *, collect_images: bool) -> Iterator[_Scope]:
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

    Values that do not exceed the last reported value are dropped: the MCP
    spec requires progress to increase.
    """
    s = _scope.get()
    if s is None or s.report is None or progress <= s.last_progress:
        return
    s.last_progress = progress
    s.report(progress, total, message)


def record_image(path: Path) -> None:
    """Record a screenshot PNG produced by this call for optional attachment."""
    s = _scope.get()
    if s is not None and s.collect_images:
        s.images.append(path)
