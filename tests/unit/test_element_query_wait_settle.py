"""element_query waits: windows-changed settle, worker deadline and retry (KWin-free).

A match reported ``unavailable (windows-changed)`` usually belongs to a window
that is still opening; the worker's wait re-polls briefly so the caller gets
screen coordinates. Permanent reasons return at once, and the settle window is
bounded so a match that never maps still returns well before the timeout.

On the engine side every AT-SPI request runs in the worker with a per-attempt
deadline (``timeout_ms`` + 30 s for a wait); a worker that misses it, exits or
reports a bus error is abandoned and the request is retried once on a fresh
worker. Those tests drive the real ``_run_atspi``/``_atspi_exchange`` over a
fake worker process, a fake selector and a fake clock.
"""

from __future__ import annotations

import io
import time
import types
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from kwin_mcp import accessibility, core
from kwin_mcp.accessibility import _Found, _RefEntry, _RefTable
from kwin_mcp.models import Locator, UIElement, Wait

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from ._fakes import FakeEngine

POLL_INTERVAL_MS = 50
TIMEOUT_MS = 5000
# The settle window is 1 s; allow scheduling slack but stay far below the timeout.
SETTLE_BOUND_S = 2.0


# ── worker side: the windows-changed settle ──────────────────────────────


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


def _scripted_walk(
    monkeypatch: pytest.MonkeyPatch, answers: list[str], *, repeat_last: bool
) -> list[str]:
    """Answer each walk of the wait with one element per scripted reason."""
    calls: list[str] = []
    monkeypatch.setattr(accessibility, "_REFS", _RefTable(generation=1))

    def walk(_criteria: object, _max_depth: int) -> list[_Found]:
        index = len(calls)
        if index >= len(answers):
            assert repeat_last, calls
            index = len(answers) - 1
        calls.append(answers[index])
        node = object()
        entry = _RefEntry(node, "push button", "kcalc", 2, node, node)
        return [_Found(_element(answers[index]), accessibility._UNMAPPED, entry)]

    monkeypatch.setattr(accessibility, "_walk", walk)
    return calls


def _wait() -> tuple[list[UIElement], dict[str, Any]]:
    response = accessibility._handle_request(
        {
            "op": "wait",
            "locator": {"query": "Seven"},
            "until": "present",
            "timeout_ms": TIMEOUT_MS,
            "poll_interval_ms": POLL_INTERVAL_MS,
        }
    )
    assert response["ok"] is True, response
    result = response["result"]
    return [UIElement.from_worker(el) for el in result["elements"]], result


def test_windows_changed_match_is_repolled_until_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scripted_walk(monkeypatch, ["windows-changed", ""], repeat_last=False)

    elements, result = _wait()

    assert calls == ["windows-changed", ""], calls
    assert result["satisfied"] is True, result
    assert [(e.rect is not None, e.unavailable) for e in elements] == [(True, None)], elements


def test_persistent_windows_changed_returns_within_the_settle_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _scripted_walk(monkeypatch, ["windows-changed"], repeat_last=True)

    started = time.monotonic()
    elements, result = _wait()
    elapsed = time.monotonic() - started

    assert len(calls) >= 2, calls
    assert elapsed >= accessibility._WINDOWS_CHANGED_SETTLE_S * 0.9, elapsed
    assert elapsed < SETTLE_BOUND_S, elapsed
    assert result["satisfied"] is True, result
    assert [(e.rect, e.unavailable) for e in elements] == [(None, "windows-changed")], elements


def test_permanent_unavailable_reason_returns_on_the_first_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _scripted_walk(monkeypatch, ["no-extents"], repeat_last=False)

    elements, result = _wait()

    assert calls == ["no-extents"], calls
    assert result["satisfied"] is True, result
    assert [(e.rect, e.unavailable) for e in elements] == [(None, "no-extents")], elements


# ── engine side: worker deadline and retry ───────────────────────────────


class _Clock:
    """Monotonic fake time: sleeping and waiting on the selector advance it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _NeverReadySelector:
    """``selectors.DefaultSelector`` whose fd never becomes readable: the worker never answers."""

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    def __enter__(self) -> _NeverReadySelector:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def register(self, _fd: int, _events: int) -> None:
        return None

    def select(self, timeout: float) -> list[object]:
        self._clock.now += timeout
        return []


@dataclass
class _FakeWorkers:
    """Worker spawns and teardowns as the engine performs them, on the fake clock."""

    clock: _Clock
    spawned: list[tuple[int, float]] = field(default_factory=list)
    torn_down: list[tuple[int, float]] = field(default_factory=list)
    requests: list[bytes] = field(default_factory=list)


@pytest.fixture
def workers(fake_engine: FakeEngine, monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeWorkers]:
    """The engine's real ``_run_atspi`` over fake worker processes and a fake clock."""
    engine = fake_engine.engine
    clock = _Clock()
    fakes = _FakeWorkers(clock)
    # fake_engine replaces _run_atspi; these tests exercise the real one.
    monkeypatch.delattr(engine, "_run_atspi")
    monkeypatch.setattr(
        core, "time", types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
    )
    monkeypatch.setattr(
        core,
        "selectors",
        types.SimpleNamespace(DefaultSelector=lambda: _NeverReadySelector(clock), EVENT_READ=1),
    )

    class _Stdin(io.BytesIO):
        def flush(self) -> None:
            fakes.requests.append(self.getvalue())
            self.seek(0)
            self.truncate()

    def ensure() -> object:
        proc = engine._atspi_proc
        if proc is None:
            engine._atspi_generation += 1
            pid = 4000 + engine._atspi_generation
            proc = types.SimpleNamespace(
                pid=pid, stdin=_Stdin(), stdout=types.SimpleNamespace(fileno=lambda: -1)
            )
            # A stand-in for the worker's Popen: the engine reads only pid, stdin, stdout.
            vars(engine)["_atspi_proc"] = proc
            engine._atspi_buffer = b""
            fakes.spawned.append((pid, clock.now))
        return proc

    def teardown() -> None:
        proc = engine._atspi_proc
        engine._atspi_proc = None
        if proc is not None:
            fakes.torn_down.append((proc.pid, clock.now))

    monkeypatch.setattr(engine, "_ensure_atspi_worker", ensure)
    monkeypatch.setattr(engine, "_teardown_atspi_worker", teardown)
    try:
        yield fakes
    finally:
        engine._atspi_proc = None


def _worker_element(name: str) -> dict[str, Any]:
    info = _element("")
    info.name = name
    info.ref = "e2.1"
    info.app = "kcalc"
    return asdict(info)


@pytest.mark.qa("QA-ELEM-019")
def test_worker_deadline_timeout_plus_30s(fake_engine: FakeEngine, workers: _FakeWorkers) -> None:
    start = workers.clock.now
    with pytest.raises(RuntimeError) as failure:
        fake_engine.engine.element_query(Locator(query="zzz"), wait=Wait(timeout_ms=100))

    deadline_s = 0.1 + core._ATSPI_TIMEOUT_S
    assert str(failure.value) == (
        f"AT-SPI2 query timed out after {deadline_s:g}s (op=wait). "
        "Retried once but still failed — the AT-SPI2 bus may be unstable."
    )
    # Abandoned at the per-attempt deadline, respawned after the 0.5 s retry pause.
    (first, first_at), (second, second_at) = workers.spawned
    assert first != second
    assert first_at == start
    assert [pid for pid, _ in workers.torn_down] == [first, second]
    first_abandoned = workers.torn_down[0][1] - start
    assert first_abandoned == pytest.approx(deadline_s, abs=1e-6)
    assert second_at - start == pytest.approx(deadline_s + 0.5, abs=1e-6)
    assert workers.torn_down[1][1] - second_at == pytest.approx(deadline_s, abs=1e-6)
    assert workers.clock.sleeps == [0.5]
    # Both attempts carried the wait itself to the worker.
    assert len(workers.requests) == 2
    for line in workers.requests:
        assert b'"op": "wait"' in line and b'"timeout_ms": 100,' in line, line
    assert fake_engine.engine._atspi_generation == 2


def _scripted_exchange(
    monkeypatch: pytest.MonkeyPatch,
    fake_engine: FakeEngine,
    workers: _FakeWorkers,
    answers: Callable[[int], dict[str, Any]],
) -> list[int]:
    """Answer each exchange from ``answers(attempt)``; record which worker pid got it."""
    pids: list[int] = []
    engine = fake_engine.engine

    def exchange(
        proc: Any, line: bytes, _heartbeat: object = None, _timeout_s: float = 0.0
    ) -> dict[str, Any]:
        workers.requests.append(line)
        pids.append(proc.pid)
        return answers(len(pids) - 1)

    monkeypatch.setattr(engine, "_atspi_exchange", exchange)
    return pids


BUS_ERROR = "GLib.GError: g-dbus-error-quark: Message recipient disconnected from message bus"


@pytest.mark.qa("QA-ELEM-021")
def test_atspi_retry_once(
    fake_engine: FakeEngine, workers: _FakeWorkers, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (a) A transient bus error once: the request succeeds on a fresh worker after 0.5 s.
    def once(attempt: int) -> dict[str, Any]:
        if attempt == 0:
            return {"worker_error": BUS_ERROR}
        return {"ok": True, "result": [_worker_element("Seven")]}

    pids = _scripted_exchange(monkeypatch, fake_engine, workers, once)
    start = workers.clock.now
    found = fake_engine.engine.element_query()
    assert [e.name for e in found.elements] == ["Seven"]
    assert workers.clock.now - start == pytest.approx(0.5)
    assert workers.clock.sleeps == [0.5]
    assert len(pids) == 2 and pids[0] != pids[1], pids
    assert [pid for pid, _ in workers.torn_down] == [pids[0]]

    # (b) The bus keeps failing: one retry, then a ToolError saying so.
    workers.clock.sleeps.clear()
    pids = _scripted_exchange(
        monkeypatch, fake_engine, workers, lambda _attempt: {"worker_error": BUS_ERROR}
    )
    with pytest.raises(RuntimeError) as failure:
        fake_engine.engine.element_query()
    assert str(failure.value) == (
        f"AT-SPI2 query failed: {BUS_ERROR}. "
        "Retried once but still failed — the AT-SPI2 bus may be unstable."
    )
    assert len(pids) == 2 and pids[0] != pids[1], pids
    assert workers.clock.sleeps == [0.5]
