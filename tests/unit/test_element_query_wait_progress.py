"""element_query wait progress stays within its total and strictly increases (KWin-free).

The AT-SPI worker answers a wait only after its own deadline, so heartbeats
keep firing past ``timeout_ms`` and can repeat an elapsed value. Each emitted
value must still be at most the total and greater than the previous one.
"""

from __future__ import annotations

import itertools
import types
from typing import TYPE_CHECKING

from kwin_mcp import core, progress
from kwin_mcp.models import Locator, Wait

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

    from ._fakes import FakeEngine

TIMEOUT_MS = 5000
# Seconds after the wait started at which the fake worker fires heartbeats:
# a repeated value, the deadline itself and two heartbeats past it.
HEARTBEAT_OFFSETS = [0.5, 1.0, 1.0, 2.0, 5.0, 6.0, 7.5]


def test_element_query_wait_progress_never_exceeds_total(
    fake_engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [100.0]
    monkeypatch.setattr(core, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))

    engine = fake_engine.engine
    requests: list[dict[str, object]] = []

    def run_atspi(
        op: str, *, heartbeat: Callable[[], None] | None = None, **kwargs: object
    ) -> dict:
        requests.append({"op": op, **kwargs})
        assert heartbeat is not None
        start = clock[0]
        for offset in HEARTBEAT_OFFSETS:
            clock[0] = start + offset
            heartbeat()
        return {
            "ok": True,
            "result": {"elements": [], "satisfied": False, "elapsed_ms": TIMEOUT_MS},
        }

    monkeypatch.setattr(engine, "_run_atspi", run_atspi)

    events: list[tuple[float, float | None, str | None]] = []

    def recorder(value: float, total: float | None, message: str | None) -> None:
        events.append((value, total, message))

    with progress.call_scope(recorder, collect_images=False):
        result = engine.element_query(Locator(query="zzz"), wait=Wait(timeout_ms=TIMEOUT_MS))

    assert result.wait is not None and result.wait.satisfied is False, result
    assert result.render().startswith(f"NOT SATISFIED: Timeout after {TIMEOUT_MS} ms"), (
        result.render()
    )
    assert requests[0]["op"] == "wait"
    assert events, events
    values = [value for value, _, _ in events]
    assert all(later > earlier for earlier, later in itertools.pairwise(values)), events
    assert all(total == TIMEOUT_MS and value <= total for value, total, _ in events), events
    # The worker request itself may run past the wait's own deadline.
    assert requests[0]["timeout_s"] == TIMEOUT_MS / 1000 + core._ATSPI_TIMEOUT_S
