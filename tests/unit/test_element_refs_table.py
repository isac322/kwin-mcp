"""AT-SPI worker ref table, by-ref resolution and the strict action search, over a fake tree.

``kwin_mcp.accessibility.Atspi`` is replaced by an in-process fake desktop, so
nothing here needs KWin, D-Bus or a real accessibility bus. KWin's window list
is reported unavailable, which leaves every element unmapped (``rect=null``)
without affecting refs or actions.
"""

from __future__ import annotations

import io
import json
import sys
import time
import types
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from kwin_mcp import accessibility
from kwin_mcp.accessibility import _RefEntry, _RefTable
from kwin_mcp.models import Locator, Rect, StrictAddr, UIElement, Window

from ._fakes import kwin_record

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from ._fakes import FakeEngine


@dataclass(frozen=True)
class _FakeState:
    value_nick: str


class _FakeStateSet:
    def __init__(self, nicks: list[str]) -> None:
        self._nicks = nicks

    def get_states(self) -> list[_FakeState]:
        return [_FakeState(nick) for nick in self._nicks]


class _FakeActions:
    """Fake ``Atspi.Action`` interface; do_action goes through the fake module."""

    def __init__(self, owner: _FakeNode, names: list[str]) -> None:
        self.owner = owner
        self.names = names

    def get_n_actions(self) -> int:
        return 0 if self.owner._busy("get_n_actions") else len(self.names)

    def get_action_name(self, index: int) -> str:
        if self.owner._busy("get_action_name"):
            raise RuntimeError(_NO_REPLY_TEXT)
        return self.names[index]


_NO_REPLY_TEXT = "atspi_error: Did not receive a reply."


@dataclass(eq=False)
class _FakeNode:
    """Minimal ``Atspi.Accessible`` double: mutable name, role, states and children.

    ``busy`` answers like libatspi when the app does not reply in time (after
    the call timeout, on the fake clock), ``gone`` like it does for an element
    the app destroyed (at once).
    """

    name: str
    role: str = "panel"
    children: list[_FakeNode] = field(default_factory=list)
    states: list[str] = field(default_factory=lambda: ["enabled", "visible"])
    actions: list[str] | None = None
    description: str = ""
    busy: bool = False
    gone: bool = False
    pid: int = 0
    # Called once per read of an app's element; True makes that read time out.
    clock: ClassVar[Callable[[], bool] | None] = None
    # Seconds the fake clock is ahead of time.monotonic().
    waited: ClassVar[float] = 0.0
    # Seconds the wall clock was stepped forward, on top of that.
    wall_step: ClassVar[float] = 0.0
    # Seconds an answered read takes, by method name.
    slow: dict[str, float] = field(default_factory=dict)
    slow_all: ClassVar[dict[str, float]] = {}

    def _busy(self, read: str) -> bool:
        """Advance the fake clock for one read; True when it times out."""
        _FakeNode.waited += self.slow.get(read, 0.0) + _FakeNode.slow_all.get(read, 0.0)
        clock = _FakeNode.clock
        busy = self.busy or (clock is not None and self.role != "desktop frame" and clock())
        if busy:
            _FakeNode.waited += accessibility._CALL_TIMEOUT_MS / 1000
        return busy

    def get_name(self) -> str:
        return "" if self._busy("get_name") or self.gone else self.name

    def get_role_name(self) -> str:
        if self._busy("get_role_name"):
            raise RuntimeError(_NO_REPLY_TEXT)
        if self.gone:
            msg = "atspi_error: Object does not exist at path"
            raise RuntimeError(msg)
        return self.role

    def get_description(self) -> str:
        return "" if self._busy("get_description") else self.description

    def get_state_set(self) -> _FakeStateSet:
        return _FakeStateSet(["defunct"] if self._busy("get_state_set") else self.states)

    def get_component_iface(self) -> None:
        # No extents either way, but a busy read takes the timeout.
        self._busy("get_component_iface")
        return None

    def get_action_iface(self) -> _FakeActions | None:
        if self._busy("get_action_iface") or self.actions is None:
            return None
        return _FakeActions(self, self.actions)

    def get_text_iface(self) -> None:
        return None

    def get_value_iface(self) -> None:
        return None

    def get_child_count(self) -> int:
        return -1 if self._busy("get_child_count") or self.gone else len(self.children)

    def get_child_at_index(self, index: int) -> _FakeNode | None:
        return None if self._busy("get_child_at_index") or self.gone else self.children[index]

    def get_process_id(self) -> int:
        return self.pid


@dataclass
class _FakeAtspi:
    """The fake desktop the worker sees and the actions it dispatched."""

    apps: list[_FakeNode] = field(default_factory=list)
    dispatched: list[tuple[str, str]] = field(default_factory=list)
    dispatch_error: Exception | None = None

    def do_action(self, iface: _FakeActions, index: int) -> bool:
        self.dispatched.append((iface.owner.name, iface.names[index]))
        if self.dispatch_error is not None:
            raise self.dispatch_error
        return True


@pytest.fixture
def atspi(monkeypatch: pytest.MonkeyPatch) -> _FakeAtspi:
    """Point the worker at a fake desktop and a fresh generation-1 ref table."""
    fake = _FakeAtspi()
    monkeypatch.setattr(
        accessibility,
        "Atspi",
        types.SimpleNamespace(
            get_desktop=lambda _index: _FakeNode("desktop", "desktop frame", fake.apps),
            Action=types.SimpleNamespace(do_action=fake.do_action),
        ),
    )
    monkeypatch.setattr(accessibility, "_kwin_windows", lambda: ([], "no KWin in unit tests"))
    monkeypatch.setattr(accessibility, "_REFS", _RefTable(generation=1))
    monkeypatch.setattr(accessibility, "_APP_NAMES", {})
    monkeypatch.setattr(_FakeNode, "waited", 0.0)
    monkeypatch.setattr(_FakeNode, "wall_step", 0.0)
    clock = types.SimpleNamespace(
        monotonic=lambda: time.monotonic() + _FakeNode.waited,
        time=lambda: time.time() + _FakeNode.waited + _FakeNode.wall_step,
    )
    monkeypatch.setattr(accessibility, "time", clock)
    _time_fake_reads(monkeypatch)
    return fake


def _time_fake_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the worker's ``_time_reads`` to the fakes; monkeypatch undoes it."""
    for cls in (_FakeNode, _FakeActions):
        for name, read in list(vars(cls).items()):
            if name.startswith("get_"):
                monkeypatch.setattr(cls, name, read)
    accessibility._time_reads(_FakeNode)
    accessibility._time_reads(_FakeActions)


def _request(op: str, **kwargs: Any) -> dict[str, Any]:
    return accessibility._handle_request({"op": op, **kwargs})


def _query(**locator: Any) -> list[dict[str, Any]]:
    response = _request("query", locator=locator, max_depth=100)
    assert response["ok"] is True
    return response["result"]


def _resolve(*refs: str) -> list[dict[str, Any]]:
    response = _request("resolve", refs=list(refs))
    assert response["ok"] is True
    return response["result"]


def _kcalc(*children: _FakeNode) -> _FakeNode:
    return _FakeNode("kcalc", "application", [_FakeNode("KCalc", "frame", list(children))])


def _button(name: str = "Seven", actions: Iterable[str] = ("Press",)) -> _FakeNode:
    return _FakeNode(name, "push button", actions=list(actions))


def _entry(name: str) -> _RefEntry:
    node = _FakeNode(name)
    return _RefEntry(node, "panel", "app", 1, node, node)


@pytest.mark.qa("QA-ELEM-040")
def test_ref_table_generation_role_lru(atspi: _FakeAtspi, monkeypatch: pytest.MonkeyPatch) -> None:
    button = _button()
    atspi.apps[:] = [_kcalc(button)]
    (element,) = _query(query="Seven")
    ref = element["ref"]
    assert ref.startswith("e1.")
    assert element["app"] == "kcalc"
    assert element["depth"] == 2

    # Re-minting the same object yields a new ref; both resolve.
    (again,) = _query(query="Seven")
    assert again["ref"] != ref
    assert [item["ok"] for item in _resolve(ref, again["ref"])] == [True, True]

    # A name change is tolerated (Play -> Pause).
    button.name = "Eight"
    (resolved,) = _resolve(ref)
    assert resolved["ok"] is True
    assert resolved["element"]["name"] == "Eight"
    assert resolved["element"]["ref"] == ref

    # A role change is not.
    button.role = "toggle button"
    assert _resolve(ref) == [
        {
            "ref": ref,
            "ok": False,
            "error": (
                f"Stale ref {ref} (role changed from 'push button' to 'toggle button'); "
                "re-run element_query."
            ),
        }
    ]
    button.role = "push button"

    # A defunct element is stale.
    button.states = ["defunct"]
    assert _resolve(ref)[0]["error"] == (
        f"Stale ref {ref} (element no longer exists); re-run element_query."
    )
    button.states = ["enabled"]

    # Malformed refs name the expected shape.
    assert _resolve("E1.2 ")[0]["error"] == (
        "Malformed ref 'E1.2 ': refs look like e<generation>.<n>, as element_query returns "
        "them.\nRe-run element_query and copy a ref."
    )

    # A replaced worker (new generation) rejects the old ref, even when n collides.
    monkeypatch.setattr(accessibility, "_REFS", _RefTable(generation=2))
    assert _resolve(ref)[0]["error"] == (
        f"Stale ref {ref} (AT-SPI worker restarted); re-run element_query."
    )
    (fresh,) = _query(query="Eight")
    assert fresh["ref"] == "e2.1"
    assert _resolve("e2.2")[0]["error"] == (
        "Stale ref e2.2 (evicted from the ref table or never issued); re-run element_query."
    )

    # LRU order: resolving a ref makes it most recent, so the next mint beyond
    # 4096 evicts the second-oldest ref instead of the oldest.
    table = _RefTable(generation=3)
    refs = [table.mint(_entry(str(index))) for index in range(4096)]
    assert len(table) == 4096
    table.resolve(refs[0])
    table.mint(_entry("4096"))
    assert len(table) == 4096
    table.resolve(refs[0])
    with pytest.raises(accessibility._StaleRefError, match="evicted from the ref table"):
        table.resolve(refs[1])
    table.resolve(refs[2])


@pytest.mark.qa("QA-ELEM-044")
def test_lru_evicts_least_recent(atspi: _FakeAtspi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(accessibility, "_REFS", _RefTable(generation=1, capacity=4))
    atspi.apps[:] = [_kcalc(*(_button(f"Button {index}") for index in range(3)))]

    elements = _query()  # application, frame and three buttons: five refs
    assert [element["ref"] for element in elements] == [f"e1.{n}" for n in range(1, 6)]

    first, last = _resolve(elements[0]["ref"], elements[-1]["ref"])
    assert first == {
        "ref": "e1.1",
        "ok": False,
        "error": "Stale ref e1.1 (evicted from the ref table or never issued); "
        "re-run element_query.",
    }
    assert last["ok"] is True
    assert last["element"]["name"] == "Button 2"


def _nested(levels: int) -> _FakeNode:
    """A pressable button ``levels`` below the application node."""
    node = _button()
    for _ in range(levels - 2):
        node = _FakeNode("", "filler", [node])
    return _FakeNode("kcalc", "application", [_FakeNode("KCalc", "frame", [node])])


@pytest.mark.qa("QA-ELEM-042")
def test_strict_walk_depth_64_bound(atspi: _FakeAtspi) -> None:
    strict = {"strict": {"query": "Seven"}}

    atspi.apps[:] = [_nested(64)]
    response = _request("action", address=strict, action_name="press")
    assert response["ok"] is True, response
    assert response["result"]["invoked"] is True
    assert response["result"]["element"]["depth"] == 64
    assert atspi.dispatched == [("Seven", "Press")]

    atspi.dispatched.clear()
    atspi.apps[:] = [_nested(65)]
    assert _request("action", address=strict, action_name="Press") == {
        "ok": False,
        "error": "Incomplete discovery in application 'kcalc': accessibility subtree deeper "
        "than 64 levels.\nSet app='kcalc' to scope the search.",
    }
    assert atspi.dispatched == []

    # The lenient walk is not bounded the same way: it reaches depth 65.
    assert [element["depth"] for element in _query(query="Seven")] == [65]


@pytest.mark.qa("QA-ELEM-040")
def test_ref_and_strict_refusals_list_exposed_actions(atspi: _FakeAtspi) -> None:
    seven = _button("Seven", ("Press", "SetFocus"))
    atspi.apps[:] = [_kcalc(seven, _button("Seventy"), _FakeNode("Seven", "label"))]
    (ref,) = [e["ref"] for e in _query(query="Seven", role="push button") if e["name"] == "Seven"]

    assert _request("action", address={"ref": ref}, action_name="Pre") == {
        "ok": False,
        "error": f"Element {ref} [push button] \"Seven\" has no AT-SPI action 'Pre'; "
        "it exposes: Press, SetFocus.\nUse one of those action names.",
    }

    response = _request(
        "action", address={"strict": {"query": "seven", "app": "kcalc"}}, action_name="Toggle"
    )
    assert response == {
        "ok": False,
        "error": "No element matching query='seven', app='kcalc' exposes AT-SPI action "
        "'Toggle'. Matching elements expose: Press, SetFocus.\n"
        "Use one of those action names, or check the locator with element_query.",
    }

    response = _request("action", address={"strict": {"query": "seven"}}, action_name="PRESS")
    assert response["ok"] is False
    error = response["error"]
    assert error.startswith(
        "Refused ambiguous AT-SPI action: 2 elements matching query='seven' expose 'PRESS': "
        '[push button] "Seven" in kcalc (e1.'
    )
    assert '; [push button] "Seventy" in kcalc (e1.' in error
    assert error.endswith(")\nNarrow the locator (role, states, app) or act on one ref.")

    # The refusal's refs are minted and usable; actions match case-insensitively.
    candidate_ref = error.split("(", 1)[1].split(")", 1)[0]
    response = _request("action", address={"ref": candidate_ref}, action_name="press")
    assert response["ok"] is True
    assert response["result"]["action"] == "Press"
    assert atspi.dispatched == [("Seven", "Press")]

    # A label without actions is never a candidate.
    assert _request("action", address={"strict": {"role": "label"}}, action_name="Press") == {
        "ok": False,
        "error": "No element matching role='label' exposes AT-SPI action 'Press'. "
        "Matching elements expose: (none).\n"
        "Use one of those action names, or check the locator with element_query.",
    }


def _recording_run_atspi(
    calls: list[tuple[str, bool]],
) -> Callable[..., dict[str, object]]:
    def run_atspi(
        op: str,
        *,
        heartbeat: Callable[[], None] | None = None,
        retry: bool = True,
        timeout_s: float | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        del heartbeat, timeout_s, kwargs
        calls.append((op, retry))
        msg = "AT-SPI2 request failed: worker exited after the request was sent"
        raise RuntimeError(msg)

    return run_atspi


@pytest.mark.qa("QA-ELEM-031")
def test_invoke_ipc_failure_after_send_not_retried(
    atspi: _FakeAtspi, fake_engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Worker side: a dispatch that raises after sending is reported, not repeated.
    atspi.apps[:] = [_kcalc(_button())]
    atspi.dispatch_error = RuntimeError("Timeout was reached")
    response = _request("action", address={"strict": {"query": "Seven"}}, action_name="Press")
    assert response["ok"] is True
    assert response["result"]["invoked"] is None
    assert response["result"]["error"] == "Timeout was reached"
    assert atspi.dispatched == [("Seven", "Press")]

    # Engine side: the worker request fails after it was sent; it is sent once.
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(fake_engine.engine, "_run_atspi", _recording_run_atspi(calls))
    with pytest.raises(RuntimeError) as excinfo:
        fake_engine.engine.element_act(
            element=StrictAddr(strict=Locator(query="Seven", app="kcalc")), action="Press"
        )
    assert "The action may still have run; check the UI before retrying." in str(excinfo.value)
    assert calls == [("action", False)]


@pytest.mark.parametrize("hidden", [True, False])
def test_query_and_old_ref_resolution_share_click_geometry_policy(
    atspi: _FakeAtspi, monkeypatch: pytest.MonkeyPatch, hidden: bool
) -> None:
    """A toolkit may retain positive extents for a closed menu or off-desktop widget."""
    button = _button()
    button.states = ["enabled", "visible", "showing"]
    atspi.apps[:] = [_kcalc(button)]
    window = kwin_record(
        Window(
            id="w1",
            app_id="kcalc",
            caption="KCalc",
            kind="normal",
            active=True,
            frame=Rect(x=150, y=120, width=400, height=500),
            client=Rect(x=150, y=120, width=400, height=500),
            moveable=True,
            resizeable=True,
            closeable=True,
            desktops=["d1"],
            on_all_desktops=False,
        )
    )
    monkeypatch.setattr(_FakeNode, "get_process_id", lambda _self: 100)
    monkeypatch.setattr(
        _FakeNode,
        "get_component_iface",
        lambda _self: types.SimpleNamespace(
            get_extents=lambda _coord: types.SimpleNamespace(x=0, y=0, width=400, height=500)
        ),
    )
    monkeypatch.setattr(
        accessibility.Atspi, "CoordType", types.SimpleNamespace(WINDOW="window"), raising=False
    )
    monkeypatch.setattr(accessibility, "_kwin_windows", lambda: ([window], ""))
    (original,) = _query(query="Seven")
    old_ref = original["ref"]
    assert UIElement.from_worker(original).rect is not None

    if hidden:
        button.states = ["enabled", "sensitive"]
        reason = "not-showing"
    else:
        window["desktops"] = ["d2"]
        reason = "inactive-desktop"

    (queried,) = _query(query="Seven")
    (resolved,) = _resolve(old_ref)
    for payload in (queried, resolved["element"]):
        element = UIElement.from_worker(payload)
        assert element.rect is None and element.unavailable == reason
        assert element.actions == ["Press"]
        assert element.window_id == "w1"
        assert element.states == button.states
    assert queried["ref"] != old_ref
    assert resolved["element"]["ref"] == old_ref

    acted = _request("action", address={"ref": old_ref}, action_name="Press")
    assert acted["ok"] is True and acted["result"]["invoked"] is True
    assert atspi.dispatched == [("Seven", "Press")]
    assert acted["result"]["element"]["unavailable"] == reason

    button.states = ["enabled", "visible", "showing"]
    window["desktops"] = ["d1"]
    (restored,) = _resolve(old_ref)
    assert UIElement.from_worker(restored["element"]).rect is not None


def _set_busy(node: _FakeNode, busy: bool) -> None:
    node.busy = busy
    for child in node.children:
        _set_busy(child, busy)


@pytest.mark.qa("QA-ELEM-023")
def test_an_app_that_does_not_answer_is_not_read_as_gone(atspi: _FakeAtspi) -> None:
    app = _kcalc(_button())
    atspi.apps[:] = [app]
    (ref,) = [element["ref"] for element in _query(query="Seven")]

    _set_busy(app, True)
    for locator in ({"query": "Seven", "app": "kcalc"}, {"query": "Seven"}):
        response = _request("wait", locator=locator, max_depth=15, until="absent", timeout_ms=0)
        assert response["result"]["satisfied"] is False, locator
        assert response["result"]["complete"] is False, locator
        # A plain query cannot list the busy app's elements, but says so.
        response = _request("query", locator=locator, max_depth=15)
        assert (response["result"], response["complete"]) == ([], False), locator
    strict = {"strict": {"query": "Seven", "app": "kcalc"}}
    response = _request("action", address=strict, action_name="Press")
    assert response["error"].startswith("Incomplete discovery in application 'kcalc'")
    assert response["error"].endswith(
        "The application is not answering; retry when it is idle, "
        "or find the element with element_query and act on its ref."
    )
    (resolved,) = _resolve(ref)
    assert resolved["error"].startswith(f"Ref {ref} did not answer")
    response = _request("action", address={"ref": ref}, action_name="Press")
    assert response["error"].startswith(f"Ref {ref} did not answer")
    assert atspi.dispatched == []

    _set_busy(app, False)
    assert _request("query", locator={"query": "Seven"}, max_depth=15)["complete"] is True
    response = _request("action", address=strict, action_name="Press")
    assert response["ok"] is True, response
    assert atspi.dispatched == [("Seven", "Press")]


@pytest.mark.qa("QA-ELEM-023")
@pytest.mark.parametrize("op", ["absent", "ref", "strict"])
def test_no_busy_spell_reads_as_a_wrong_result(
    atspi: _FakeAtspi, monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    # Every read of the app from the k-th to the m-th times out, then it answers
    # again: a busy spell of any length starting at any read. libatspi answers
    # most of those reads with plausible values, so only the time they took
    # shows them.
    seven = _button("Seven")
    seven.description = "digit"
    app = _kcalc(seven, _button("Eight"))
    app.pid = 4242
    atspi.apps[:] = [app]
    # With KWin answering, window mapping reads every top-level's name and extents.
    monkeypatch.setattr(accessibility, "_kwin_windows", lambda: ([], ""))
    monkeypatch.setattr(accessibility, "_window_pids", lambda pid, _kwin: frozenset({pid}))
    (element,) = _query(query="Seven")
    # The fake has no Atspi.StateType to check state names against.
    monkeypatch.setattr(accessibility, "_validate_state_nicks", lambda _states: None)
    reads = 0

    def run() -> dict[str, Any]:
        if op == "absent":
            locator = {"query": "Seven", "app": "kcalc"}
            return _request("wait", locator=locator, max_depth=15, until="absent", timeout_ms=0)
        if op == "ref":
            return _request("resolve", refs=[element["ref"]])
        # Matched by description and states: those reads time out as well.
        strict = {"strict": {"query": "digit", "states": ["enabled"], "app": "kcalc"}}
        return _request("action", address=strict, action_name="Press")

    def count() -> bool:
        nonlocal reads
        reads += 1
        return False

    monkeypatch.setattr(_FakeNode, "clock", count)
    run()
    total = reads
    for first in range(1, total + 1):
        for last in range(first, total + 1):
            reads = 0
            atspi.dispatched.clear()

            def spell(first: int = first, last: int = last) -> bool:
                nonlocal reads
                reads += 1
                return first <= reads <= last

            monkeypatch.setattr(_FakeNode, "clock", spell)
            response = run()
            where = f"silent from read {first} to {last} of {total}: {response}"
            if op == "absent":
                assert response["result"]["satisfied"] is False, where
            elif op == "ref":
                (resolved,) = response["result"]
                if resolved["ok"]:
                    assert resolved["element"] == element, where
                else:
                    assert "did not answer" in resolved["error"], where
            elif response["ok"]:
                assert atspi.dispatched == [("Seven", "Press")], where
            else:
                assert atspi.dispatched == [], where
                assert "not answering" in response["error"] or (
                    "did not answer" in response["error"]
                ), where


@pytest.mark.qa("QA-ELEM-023")
def test_a_ref_whose_app_does_not_answer_for_its_window_did_not_answer(
    atspi: _FakeAtspi,
) -> None:
    # The element answers, the reads that map its window to a KWin window do not.
    app = _kcalc(_button())
    atspi.apps[:] = [app]
    (ref,) = [element["ref"] for element in _query(query="Seven")]
    app.busy = True
    (resolved,) = _resolve(ref)
    assert resolved["error"].startswith(f"Ref {ref} did not answer"), resolved


@pytest.mark.qa("QA-ELEM-023")
def test_a_slow_answer_reads_as_no_reply(
    atspi: _FakeAtspi, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bound is the call's time, not its value: a value that came as late as
    # a timeout may be one.
    app = _kcalc(_button())
    atspi.apps[:] = [app]
    app.children[0].children[0].slow["get_name"] = accessibility._NO_REPLY_S
    locator = {"query": "Seven", "app": "kcalc"}
    response = _request("wait", locator=locator, max_depth=15, until="absent", timeout_ms=0)
    assert response["result"]["satisfied"] is False
    assert (response["result"]["elements"], response["result"]["complete"]) == ([], False)


@pytest.mark.qa("QA-ELEM-023")
@pytest.mark.parametrize(
    "error", ["atspi_error: Did not receive a reply.", "atspi_error: timeout from dbind (1)"]
)
def test_a_timeout_error_is_no_reply_however_soon_it_comes(
    atspi: _FakeAtspi, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    # dbind times its loop on the wall clock: a clock step can end it early.
    app = _kcalc(_button())
    atspi.apps[:] = [app]
    (element,) = _query(query="Seven")
    button = app.children[0].children[0]

    def timeout_at_once() -> str:
        raise RuntimeError(error)

    # Through the worker's wrapper, as the class method it stands in for.
    monkeypatch.setattr(button, "get_role_name", accessibility._timed(timeout_at_once))
    locator = {"query": "Seven", "app": "kcalc"}
    response = _request("wait", locator=locator, max_depth=15, until="absent", timeout_ms=0)
    assert response["result"]["satisfied"] is False
    assert response["result"]["complete"] is False
    (resolved,) = _resolve(element["ref"])
    assert resolved["error"].startswith(f"Ref {element['ref']} did not answer"), resolved


@pytest.mark.qa("QA-ELEM-023")
def test_an_error_that_comes_as_late_as_a_timeout_is_no_reply(atspi: _FakeAtspi) -> None:
    # Only the time tells a timeout whose error text is not libatspi's own.
    app = _kcalc(_button())
    atspi.apps[:] = [app]
    button = app.children[0].children[0]
    button.gone = True
    button.slow["get_role_name"] = accessibility._NO_REPLY_S
    locator = {"query": "Seven", "app": "kcalc"}
    response = _request("wait", locator=locator, max_depth=15, until="absent", timeout_ms=0)
    assert (response["result"]["satisfied"], response["result"]["complete"]) == (False, False)


@pytest.mark.qa("QA-ELEM-023")
def test_a_placeholder_after_a_wall_clock_step_is_no_reply(
    atspi: _FakeAtspi, monkeypatch: pytest.MonkeyPatch
) -> None:
    # dbind gives up on the wall clock: stepped forward, it returns a property
    # read's placeholder at once, while the monotonic clock barely moves.
    app = _kcalc(_button())
    atspi.apps[:] = [app]
    button = app.children[0].children[0]

    def stepped(node: _FakeNode) -> str:
        if node is not button:
            return node.name
        _FakeNode.wall_step += accessibility._CALL_TIMEOUT_MS / 1000
        return ""

    monkeypatch.setattr(_FakeNode, "get_name", accessibility._timed(stepped))
    locator = {"query": "Seven", "app": "kcalc"}
    response = _request("wait", locator=locator, max_depth=15, until="absent", timeout_ms=0)
    assert (response["result"]["satisfied"], response["result"]["complete"]) == (False, False)


@pytest.mark.qa("QA-ELEM-023")
def test_many_actions_that_answer_are_read_in_full(
    atspi: _FakeAtspi, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each action name answers well within the timeout; together they take longer.
    names = [f"action {index}" for index in range(20)] + ["Press"]
    app = _kcalc(_button(actions=names))
    atspi.apps[:] = [app]
    monkeypatch.setattr(_FakeNode, "slow_all", {"get_action_name": accessibility._NO_REPLY_S / 10})
    (element,) = _query(query="Seven")
    assert element["actions"] == names
    response = _request("query", locator={"query": "Seven"}, max_depth=15)
    assert response["complete"] is True
    strict = {"strict": {"query": "Seven", "app": "kcalc"}}
    assert _request("action", address=strict, action_name="Press")["ok"] is True
    assert _request("action", address={"ref": element["ref"]}, action_name="Press")["ok"] is True
    assert atspi.dispatched == [("Seven", "Press"), ("Seven", "Press")]


@pytest.mark.qa("QA-ELEM-023")
def test_a_long_list_that_answers_is_read_in_full(
    atspi: _FakeAtspi, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each child read answers quickly, but together they take longer than a
    # call timeout: that is not a silent read.
    rows = [_FakeNode(f"row {index}", "table cell") for index in range(100)]
    app = _kcalc(_FakeNode("files", "table", rows), _button())
    atspi.apps[:] = [app]
    slow = {"get_child_at_index": accessibility._NO_REPLY_S / 50}
    monkeypatch.setattr(_FakeNode, "slow_all", slow)
    assert [element["name"] for element in _query(query="Seven")] == ["Seven"]
    locator = {"query": "Nine", "app": "kcalc"}
    response = _request("wait", locator=locator, max_depth=15, until="absent", timeout_ms=0)
    assert response["result"]["satisfied"] is True
    strict = {"strict": {"query": "Seven", "app": "kcalc"}}
    response = _request("action", address=strict, action_name="Press")
    assert response["ok"] is True, response


@pytest.mark.qa("QA-ELEM-023")
def test_an_app_with_many_windows_that_answers_is_read_in_full(
    atspi: _FakeAtspi, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mapping top-levels to KWin windows reads each one: many quick answers
    # take longer than a call timeout together.
    menus = [_FakeNode(f"menu {index}", "window") for index in range(40)]
    app = _kcalc(_button())
    app.children += menus
    app.pid = 4242
    atspi.apps[:] = [app]
    monkeypatch.setattr(accessibility, "_kwin_windows", lambda: ([], ""))
    monkeypatch.setattr(accessibility, "_window_pids", lambda pid, _kwin: frozenset({pid}))
    monkeypatch.setattr(_FakeNode, "slow_all", {"get_name": accessibility._NO_REPLY_S / 20})
    (element,) = _query(query="Seven")
    response = _request("query", locator={"query": "Seven"}, max_depth=15)
    assert response["complete"] is True
    (resolved,) = _resolve(element["ref"])
    assert resolved["ok"] is True, resolved


@pytest.mark.qa("QA-ELEM-023")
def test_a_registry_that_does_not_answer_is_not_an_empty_desktop(
    atspi: _FakeAtspi, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The registry lists the applications; it can time out like an app.
    atspi.apps[:] = [_kcalc(_button())]
    desktop = _FakeNode("desktop", "desktop frame", atspi.apps, busy=True)
    namespace = types.SimpleNamespace(**vars(accessibility.Atspi))
    namespace.get_desktop = lambda _index: desktop
    monkeypatch.setattr(accessibility, "Atspi", namespace)
    locator = {"query": "Seven", "app": "kcalc"}
    response = _request("wait", locator=locator, max_depth=15, until="absent", timeout_ms=0)
    assert response["result"]["satisfied"] is False
    response = _request("action", address={"strict": locator}, action_name="Press")
    assert response["error"].startswith("Incomplete discovery: could not read desktop child")
    assert "application is not answering" not in response["error"]


@pytest.mark.qa("QA-ELEM-036")
def test_a_destroyed_element_is_gone_not_busy(
    atspi: _FakeAtspi, monkeypatch: pytest.MonkeyPatch
) -> None:
    # libatspi reads a destroyed element's child count as -1 too, like a busy
    # app's, but at once, and its role read fails with an ordinary error.
    seven, eight = _button("Seven"), _button("Eight")
    exited = _FakeNode("exited", "application")

    def no_longer_exists() -> str:
        msg = "atspi_error: The application no longer exists (0)"
        raise RuntimeError(msg)

    monkeypatch.setattr(exited, "get_name", no_longer_exists)
    atspi.apps[:] = [exited, _kcalc(seven, eight)]
    (ref,) = [element["ref"] for element in _query(query="Seven")]
    seven.gone = True

    assert [element["name"] for element in _query(query="Eight")] == ["Eight"]
    response = _request("wait", locator={"query": "Seven"}, max_depth=15, until="absent")
    assert response["result"]["satisfied"] is True
    (resolved,) = _resolve(ref)
    assert resolved["error"].startswith(f"Stale ref {ref}")


@pytest.mark.qa("QA-ELEM-023")
def test_a_busy_app_outside_the_scope_stays_out_of_the_search(
    atspi: _FakeAtspi, monkeypatch: pytest.MonkeyPatch
) -> None:
    zoo = _FakeNode("window-zoo", "application", [_FakeNode("Zoo", "frame")])
    kcalc = _kcalc(_button())
    atspi.apps[:] = [zoo, kcalc]
    scoped = {"strict": {"query": "Seven", "app": "kcalc"}}
    unscoped = {"strict": {"query": "Seven"}}

    # Never read, a busy app's name is unknown: it might be the scope, and
    # scoping cannot get around it, so neither refusal suggests app=.
    _set_busy(zoo, True)
    monkeypatch.setattr(accessibility, "_process_name", lambda _app: "zoo-process")
    for address in (scoped, unscoped):
        response = _request("action", address=address, action_name="Press")
        assert response["error"].startswith("Incomplete discovery in application 'zoo-process'")
        assert "retry when it is idle" in response["error"], response
        assert "Set app=" not in response["error"], response
    assert atspi.dispatched == []

    # Once read, its name keeps it out of a search scoped to another app.
    _set_busy(zoo, False)
    assert {element["app"] for element in _query()} == {"window-zoo", "kcalc"}
    _set_busy(zoo, True)
    absent = _request(
        "wait",
        locator={"query": "Nine", "app": "kcalc"},
        max_depth=15,
        until="absent",
        timeout_ms=0,
    )
    assert absent["result"]["satisfied"] is True
    response = _request("action", address=scoped, action_name="Press")
    assert response["ok"] is True, response
    assert atspi.dispatched == [("Seven", "Press")]
    response = _request("action", address=unscoped, action_name="Press")
    assert response["error"].startswith("Incomplete discovery in application 'window-zoo'")
    assert response["error"].endswith("\nSet app='window-zoo' to scope the search.")


@pytest.mark.qa("QA-ELEM-023")
def test_a_new_app_with_a_reused_pid_starts_unknown(atspi: _FakeAtspi) -> None:
    other = _FakeNode("otherapp", "application", [_FakeNode("Other", "frame")], pid=5)
    atspi.apps[:] = [other]
    assert {element["app"] for element in _query()} == {"otherapp"}

    # otherapp exits; a new process gets the same pid and is busy from the start.
    target = _FakeNode(
        "targetapp", "application", [_FakeNode("Target", "frame", [_button()])], pid=5
    )
    _set_busy(target, True)
    atspi.apps[:] = [target]
    locator = {"query": "Seven", "app": "targetapp"}
    response = _request("wait", locator=locator, max_depth=15, until="absent", timeout_ms=0)
    assert response["result"]["satisfied"] is False
    response = _request("action", address={"strict": locator}, action_name="Press")
    assert response["error"].startswith("Incomplete discovery"), response


@pytest.mark.qa("QA-ELEM-023")
def test_an_app_really_named_empty_stays_out_of_another_apps_scope(
    atspi: _FakeAtspi, monkeypatch: pytest.MonkeyPatch
) -> None:
    nameless = _FakeNode("", "application", [_FakeNode("Nameless", "frame", [_button()])])
    atspi.apps[:] = [nameless, _kcalc(_button("Eight"))]
    _query()
    _set_busy(nameless, True)
    locator = {"query": "Nine", "app": "kcalc"}
    response = _request("wait", locator=locator, max_depth=15, until="absent", timeout_ms=0)
    assert response["result"]["satisfied"] is True
    strict = {"strict": {"query": "Eight", "app": "kcalc"}}
    response = _request("action", address=strict, action_name="Press")
    assert response["ok"] is True, response
    # Unscoped, the refusal names it by its process: its own name is "".
    monkeypatch.setattr(accessibility, "_process_name", lambda _app: "nameless-process")
    response = _request("action", address={"strict": {"query": "Eight"}}, action_name="Press")
    assert response["error"].startswith("Incomplete discovery in application 'nameless-process'")


def test_an_unreadable_app_name_does_not_suggest_scoping(
    atspi: _FakeAtspi, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every app's name is read before the filter applies, so app= cannot help.
    broken = _FakeNode("broken", "application")

    def name_error() -> str:
        msg = "atspi_error: org.freedesktop.DBus.Error.Failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(broken, "get_name", name_error)
    monkeypatch.setattr(accessibility, "_process_name", lambda _app: "broken-process")
    atspi.apps[:] = [broken, _kcalc(_button())]
    response = _request("action", address={"strict": {"query": "Seven"}}, action_name="Press")
    assert response["error"].startswith("Incomplete discovery in application 'broken-process'")
    assert response["error"].endswith("\nFind the element with element_query, then act on its ref.")


@pytest.mark.qa("QA-ELEM-023")
def test_the_worker_times_every_read_against_the_timeout_it_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The bound assumes libatspi's timeout, so the worker sets it instead of
    # relying on the library's default, and wraps the library's reads itself.
    pinned: list[tuple[int, int]] = []
    # GObject's own methods (get_property, get_data) are not the library's reads.
    gobject = type("Object", (), {"get_data": lambda _self: None, "__module__": "gi.GObject"})
    action = type("Action", (), {"get_n_actions": lambda _self: 0, "do_action": None})
    accessible = type(
        "Accessible",
        (gobject, action),
        {"get_name": lambda _self: "", "get_process_id": lambda _self: 1},
    )
    namespace = types.SimpleNamespace(
        set_timeout=lambda *a: pinned.append(a), Accessible=accessible
    )
    monkeypatch.setattr(accessibility, "Atspi", namespace)
    # Keep the worker off the test run's own file descriptors.
    fake_os = types.SimpleNamespace(
        dup=lambda fd: fd, fdopen=lambda *_a, **_k: io.StringIO(), dup2=lambda *_a: None
    )
    monkeypatch.setattr(accessibility, "os", fake_os)
    stream = types.SimpleNamespace(fileno=lambda: 1)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.setattr(accessibility, "_REFS", _RefTable(generation=1))
    with warnings.catch_warnings():
        accessibility._serve(1)
    assert pinned == [(accessibility._CALL_TIMEOUT_MS, accessibility._STARTUP_MS)]
    # Reads defined on an interface in the MRO are timed too.
    assert hasattr(accessible.get_name, "__wrapped__")
    assert hasattr(action.get_n_actions, "__wrapped__")
    assert not hasattr(action.do_action, "__wrapped__")
    assert not hasattr(gobject.get_data, "__wrapped__")
    # The bus daemon answers it, not the application.
    assert not hasattr(accessible.get_process_id, "__wrapped__")


@pytest.mark.qa("QA-ELEM-023")
def test_a_no_reply_that_escapes_an_operation_is_a_worker_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The signal passes ``except Exception``; the worker still answers the request.
    namespace = types.SimpleNamespace(set_timeout=lambda *_a: None, Accessible=object)
    monkeypatch.setattr(accessibility, "Atspi", namespace)
    responses = io.StringIO()
    fake_os = types.SimpleNamespace(
        dup=lambda fd: fd, fdopen=lambda *_a, **_k: responses, dup2=lambda *_a: None
    )
    monkeypatch.setattr(accessibility, "os", fake_os)
    stream = types.SimpleNamespace(fileno=lambda: 1)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"op": "query"}\n'))
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.setattr(accessibility, "_REFS", _RefTable(generation=1))

    def no_reply(_request: object) -> dict[str, Any]:
        raise accessibility._NoReplyError(accessibility._NO_REPLY)

    monkeypatch.setattr(accessibility, "_handle_request", no_reply)
    with warnings.catch_warnings():
        accessibility._serve(1)
    assert json.loads(responses.getvalue())["worker_error"].startswith("_NoReplyError")


@pytest.mark.qa("QA-ELEM-023")
def test_the_real_atspi_reads_are_timed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Atspi.Accessible comes from gi.overrides, its interfaces from gi.repository.
    real = accessibility.Atspi
    for owner in real.Accessible.__mro__:
        if not owner.__module__.endswith(".Atspi"):
            continue
        for name, read in list(vars(owner).items()):
            if name.startswith("get_"):
                monkeypatch.setattr(owner, name, read)  # undone after the test
    accessibility._time_reads(real.Accessible)
    for read in (
        real.Accessible.get_name,
        real.Accessible.get_child_at_index,
        real.Component.get_extents,
        real.Action.get_action_name,
        real.Text.get_character_count,
        real.Value.get_current_value,
    ):
        assert hasattr(read, "__wrapped__"), read
    for kept in (
        real.Accessible.get_process_id,
        real.Action.do_action,
        real.Accessible.get_property,
    ):
        assert not hasattr(kept, "__wrapped__"), kept
