"""AT-SPI worker ref table, by-ref resolution and the strict action search, over a fake tree.

``kwin_mcp.accessibility.Atspi`` is replaced by an in-process fake desktop, so
nothing here needs KWin, D-Bus or a real accessibility bus. KWin's window list
is reported unavailable, which leaves every element unmapped (``rect=null``)
without affecting refs or actions.
"""

from __future__ import annotations

import os
import subprocess
import types
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
        return 0 if self.owner._busy() else len(self.names)

    def get_action_name(self, index: int) -> str:
        if self.owner._busy():
            raise RuntimeError(self.owner.no_reply)
        return self.names[index]


@dataclass(eq=False)
class _FakeNode:
    """Minimal ``Atspi.Accessible`` double: mutable name, role, states and children.

    ``busy`` answers like libatspi when the app does not reply in time, ``gone``
    like it does for an element the app destroyed.
    """

    name: str
    role: str = "panel"
    children: list[_FakeNode] = field(default_factory=list)
    states: list[str] = field(default_factory=lambda: ["enabled", "visible"])
    actions: list[str] | None = None
    busy: bool = False
    gone: bool = False
    # libdbus's text on the bus; atk-bridge apps on a peer-to-peer link get dbind's.
    no_reply: ClassVar[str] = "atspi_error: Did not receive a reply."
    # Called once per read of an app's element; True makes that read time out.
    clock: ClassVar[Callable[[], bool] | None] = None

    def _busy(self) -> bool:
        clock = _FakeNode.clock
        return self.busy or (clock is not None and self.role != "desktop frame" and clock())

    def get_name(self) -> str:
        return "" if self._busy() or self.gone else self.name

    def get_role_name(self) -> str:
        if self._busy():
            raise RuntimeError(self.no_reply)
        if self.gone:
            msg = "atspi_error: Object does not exist at path"
            raise RuntimeError(msg)
        return self.role

    def get_description(self) -> str:
        return ""

    def get_state_set(self) -> _FakeStateSet:
        return _FakeStateSet(["defunct"] if self._busy() else self.states)

    def get_component_iface(self) -> None:
        return None

    def get_action_iface(self) -> _FakeActions | None:
        if self._busy() or self.actions is None:
            return None
        return _FakeActions(self, self.actions)

    def get_text_iface(self) -> None:
        return None

    def get_value_iface(self) -> None:
        return None

    def get_child_count(self) -> int:
        return -1 if self._busy() or self.gone else len(self.children)

    def get_child_at_index(self, index: int) -> _FakeNode | None:
        return None if self._busy() or self.gone else self.children[index]

    def get_process_id(self) -> int:
        return os.getpid()


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
    return fake


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
@pytest.mark.parametrize(
    "no_reply", ["atspi_error: Did not receive a reply.", "atspi_error: timeout from dbind (1)"]
)
def test_an_app_that_does_not_answer_is_not_read_as_gone(
    atspi: _FakeAtspi, monkeypatch: pytest.MonkeyPatch, no_reply: str
) -> None:
    monkeypatch.setattr(_FakeNode, "no_reply", no_reply)
    app = _kcalc(_button())
    atspi.apps[:] = [app]
    (ref,) = [element["ref"] for element in _query(query="Seven")]

    _set_busy(app, True)
    for locator in ({"query": "Seven", "app": "kcalc"}, {"query": "Seven"}):
        response = _request("wait", locator=locator, max_depth=15, until="absent", timeout_ms=0)
        assert response["result"]["satisfied"] is False, locator
    strict = {"strict": {"query": "Seven", "app": "kcalc"}}
    monkeypatch.setattr(accessibility, "_process_name", lambda _app: "kcalc")
    response = _request("action", address=strict, action_name="Press")
    assert response["ok"] is False
    assert response["error"].startswith("Incomplete discovery in application 'kcalc'")
    assert "no reply within the AT-SPI call timeout" in response["error"]
    assert response["error"].endswith(
        "retry when it is idle, or find the element with element_query and act on its ref."
    )
    (resolved,) = _resolve(ref)
    assert resolved["error"].startswith(f"Ref {ref} did not answer")
    assert atspi.dispatched == []

    # The name read times out, and the app answers right after it: still kcalc.
    def name_then_answer() -> str:
        timed_out = app.busy
        _set_busy(app, False)
        return "" if timed_out else app.name

    with monkeypatch.context() as patch:
        patch.setattr(app, "get_name", name_then_answer)
        scoped = {"query": "Seven", "app": "kcalc"}
        response = _request("wait", locator=scoped, max_depth=15, until="absent", timeout_ms=0)
    assert [element["name"] for element in response["result"]["elements"]] == ["Seven"]

    # The app turns busy after the button's count read: its name and children
    # read as "" and none, and only a later count check notices.
    button = app.children[0].children[0]

    def name_turns_busy() -> str:
        _set_busy(app, True)
        return ""

    with monkeypatch.context() as patch:
        patch.setattr(button, "get_name", name_turns_busy)
        response = _request("wait", locator=scoped, max_depth=15, until="absent", timeout_ms=0)
        assert response["result"]["satisfied"] is False
        _set_busy(app, False)
        response = _request("action", address=strict, action_name="Press")
        assert "The application is not answering; retry when it is idle" in response["error"]
    assert atspi.dispatched == []

    # Busy between two siblings: the second reads as None, and only the check
    # after the app's walk notices.
    _set_busy(app, False)
    app.children[0].children.append(_button("Eight"))

    def count_then_busy() -> int:
        _set_busy(app, True)
        return 0

    with monkeypatch.context() as patch:
        patch.setattr(button, "get_child_count", count_then_busy)
        response = _request("action", address={"strict": {"query": "Eight"}}, action_name="Press")
    assert "could not read the application again" in response["error"]
    _set_busy(app, False)

    # One count read times out and the app answers right after: not gone.
    def one_timeout() -> int:
        return next(replies, 1 if node is app else 0)

    for node, counts in ((button, [-1]), (app, [-1]), (app, [1, -1])):
        replies = iter(counts)
        with monkeypatch.context() as patch:
            patch.setattr(node, "get_child_count", one_timeout)
            response = _request("wait", locator=scoped, max_depth=15, until="absent", timeout_ms=0)
        assert response["result"]["satisfied"] is False, node.name
    assert atspi.dispatched == []
    app.children[0].children.pop()

    # The count read answers, then the role read times out: still not gone.
    def role_times_out() -> str:
        raise RuntimeError(no_reply)

    with monkeypatch.context() as patch:
        patch.setattr(button, "get_role_name", role_times_out)
        response = _request("wait", locator=scoped, max_depth=15, until="absent", timeout_ms=0)
        assert response["result"]["satisfied"] is False
        (resolved,) = _resolve(ref)
        assert resolved["error"].startswith(f"Ref {ref} did not answer")

    # The app exits right after the strict walk: a refusal, not a worker error.
    def no_longer_exists() -> str:
        msg = "atspi_error: The application no longer exists (0)"
        raise RuntimeError(msg)

    with monkeypatch.context() as patch:
        counts = iter([1])

        def count_then_exit() -> int:
            count = next(counts, -1)
            if count < 0:
                patch.setattr(app, "get_role_name", no_longer_exists)
            return count

        patch.setattr(app, "get_child_count", count_then_exit)
        response = _request("action", address=strict, action_name="Press")
    assert response["ok"] is False
    assert "could not read the application again" in response["error"]
    assert atspi.dispatched == []

    # Answering again, without the button: now it is gone.
    _set_busy(app, False)
    app.children[0].children.clear()
    response = _request("wait", locator={"query": "Seven"}, max_depth=15, until="absent")
    assert response["result"]["satisfied"] is True


@pytest.mark.qa("QA-ELEM-036")
def test_a_destroyed_element_is_gone_not_busy(
    atspi: _FakeAtspi, monkeypatch: pytest.MonkeyPatch
) -> None:
    # libatspi reads a destroyed element's child count as -1 too, like a busy
    # app's, but its role read fails with an ordinary error.
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

    # A GTK 3 app that exited: the count is -1, libatspi returns the last role,
    # and only the dead process tells it from a busy app.
    (ref,) = [element["ref"] for element in _query(query="Eight")]
    monkeypatch.setattr(eight, "get_child_count", lambda: -1)
    monkeypatch.setattr(accessibility, "_process_alive", lambda _element: False)
    (resolved,) = _resolve(ref)
    assert resolved["error"].startswith(f"Stale ref {ref}")


@pytest.mark.qa("QA-ELEM-023")
@pytest.mark.parametrize("op", ["absent", "ref", "strict"])
def test_an_app_that_turns_busy_at_any_read_is_not_read_as_gone(
    atspi: _FakeAtspi, monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    atspi.apps[:] = [_kcalc(_button("Seven"), _button("Eight"))]
    (ref,) = [element["ref"] for element in _query(query="Seven")]
    reads = 0

    def run() -> dict[str, Any]:
        if op == "absent":
            locator = {"query": "Seven", "app": "kcalc"}
            return _request("wait", locator=locator, max_depth=15, until="absent", timeout_ms=0)
        if op == "ref":
            return _request("resolve", refs=[ref])
        strict = {"strict": {"query": "Seven", "app": "kcalc"}}
        return _request("action", address=strict, action_name="Press")

    def count() -> bool:
        nonlocal reads
        reads += 1
        return False

    monkeypatch.setattr(_FakeNode, "clock", count)
    run()
    total = reads
    for first_silent in range(1, total + 1):
        reads = 0
        atspi.dispatched.clear()

        def busy_from_here(first_silent: int = first_silent) -> bool:
            nonlocal reads
            reads += 1
            return reads >= first_silent

        monkeypatch.setattr(_FakeNode, "clock", busy_from_here)
        response = run()
        where = f"busy from read {first_silent} of {total}: {response}"
        if op == "absent":
            assert response["result"]["satisfied"] is False, where
        elif op == "ref":
            assert not response["result"][0].get("error", "").startswith("Stale"), where
        elif not response["ok"]:
            # Refused during the search, or the chosen element stopped answering.
            error = response["error"]
            assert error.startswith("Incomplete discovery") or "did not answer" in error, where
        else:
            assert atspi.dispatched == [("Seven", "Press")], where


def test_process_alive_counts_a_zombie_as_dead() -> None:
    # An app kwin-mcp launched stays a zombie until kwin-mcp's process registry reaps it.
    child = subprocess.Popen(["true"])
    try:
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        zombie = types.SimpleNamespace(get_process_id=lambda: child.pid)
        alive = types.SimpleNamespace(get_process_id=os.getpid)
        assert accessibility._process_alive(zombie) is False
        assert accessibility._process_alive(alive) is True
    finally:
        child.wait()
