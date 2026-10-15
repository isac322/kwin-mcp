"""AT-SPI worker ref table, by-ref resolution and the strict action search, over a fake tree.

``kwin_mcp.accessibility.Atspi`` is replaced by an in-process fake desktop, so
nothing here needs KWin, D-Bus or a real accessibility bus. KWin's window list
is reported unavailable, which leaves every element unmapped (``rect=null``)
without affecting refs or actions.
"""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from kwin_mcp import accessibility
from kwin_mcp.accessibility import _RefEntry, _RefTable
from kwin_mcp.models import Locator, StrictAddr

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
        return len(self.names)

    def get_action_name(self, index: int) -> str:
        return self.names[index]


@dataclass(eq=False)
class _FakeNode:
    """Minimal ``Atspi.Accessible`` double: mutable name, role, states and children."""

    name: str
    role: str = "panel"
    children: list[_FakeNode] = field(default_factory=list)
    states: list[str] = field(default_factory=lambda: ["enabled", "visible"])
    actions: list[str] | None = None

    def get_name(self) -> str:
        return self.name

    def get_role_name(self) -> str:
        return self.role

    def get_description(self) -> str:
        return ""

    def get_state_set(self) -> _FakeStateSet:
        return _FakeStateSet(self.states)

    def get_component_iface(self) -> None:
        return None

    def get_action_iface(self) -> _FakeActions | None:
        return None if self.actions is None else _FakeActions(self, self.actions)

    def get_text_iface(self) -> None:
        return None

    def get_value_iface(self) -> None:
        return None

    def get_child_count(self) -> int:
        return len(self.children)

    def get_child_at_index(self, index: int) -> _FakeNode:
        return self.children[index]

    def get_process_id(self) -> int:
        return 0


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
