"""wait_for_element progress stays within its total and strictly increases (KWin-free).

The AT-SPI worker answers a wait only after its own deadline, so heartbeats
keep firing past ``timeout_ms`` and can repeat an elapsed value. Each emitted
value must still be at most the total and greater than the previous one.
"""

from __future__ import annotations

import types
from typing import TYPE_CHECKING

from kwin_mcp import core, progress

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

TIMEOUT_MS = 5000
# Seconds after the wait started at which the fake worker fires heartbeats:
# a repeated value, the deadline itself and two heartbeats past it.
HEARTBEAT_OFFSETS = [0.5, 1.0, 1.0, 2.0, 5.0, 6.0, 7.5]


def test_wait_for_element_progress_never_exceeds_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(core, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))

    engine = core.AutomationEngine()
    monkeypatch.setattr(engine, "_get_session", lambda: object())
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
        return {"ok": False, "error": f"Timeout after {TIMEOUT_MS}ms: no elements matching 'zzz'"}

    monkeypatch.setattr(engine, "_run_atspi", run_atspi)

    events: list[tuple[float, float | None, str | None]] = []

    def recorder(value: float, total: float | None, message: str | None) -> None:
        events.append((value, total, message))

    with progress.call_scope(recorder, collect_images=False):
        result = engine.wait_for_element("zzz", timeout_ms=TIMEOUT_MS)

    assert result.startswith(f"Timeout after {TIMEOUT_MS}ms"), result
    # The worker request itself may run past the wait's own deadline.
    assert requests[0]["op"] == "wait"
    assert requests[0]["timeout_s"] == TIMEOUT_MS / 1000 + core._ATSPI_TIMEOUT_S
    assert events, events
    values = [value for value, _, _ in events]
    assert all(later > earlier for earlier, later in zip(values, values[1:], strict=False)), events
    assert all(total == TIMEOUT_MS and value <= total for value, total, _ in events), events
