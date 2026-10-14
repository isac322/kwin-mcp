"""End-to-end coverage for native AT-SPI action invocation."""

from __future__ import annotations

import re
import time
import types
from typing import TYPE_CHECKING, Any

import pytest

from kwin_mcp import accessibility, core

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from kwin_mcp.core import AutomationEngine

_DYNAMIC_LABEL = re.compile(
    r'^\s*- \[label] "([^"]*)" \(enabled, sensitive, read-only\) '
    r"@ (?:screen \(-?\d+, -?\d+, \d+x\d+\)|unavailable \([^)]+\))$"
)
POLL_INTERVAL_SECONDS = 0.1
ACTION_TIMEOUT_SECONDS = 3.0


def _binary_value(tree: str) -> str | None:
    lines = tree.splitlines()
    for index, line in enumerate(lines):
        if index == 0 or not re.search(r'\[label] "2" \(enabled, sensitive, read-only\)', line):
            continue
        match = _DYNAMIC_LABEL.match(lines[index - 1])
        assert match is not None, tree[:500]
        return match.group(1).replace(" ", "")
    return None


def _wait_for_binary(engine: AutomationEngine, expected: str) -> None:
    deadline = time.monotonic() + ACTION_TIMEOUT_SECONDS
    actual: str | None = None
    tree = ""
    while time.monotonic() < deadline:
        tree = engine.accessibility_tree(app_name="kcalc")
        actual = _binary_value(tree)
        if actual == expected:
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    assert actual == expected, tree[:500]


def test_invoke_ui_action_works_on_background_window(
    kcalc_session: AutomationEngine,
    wait_for_app: Callable[[str], str],
) -> None:
    kcalc_session.launch_app(command="kwrite /tmp/kwin-mcp-action-background.txt")
    wait_for_app("kwrite")

    # Make the second app definitively foreground; KCalc must remain actionable
    # through its semantic accessibility interface without pointer coordinates.
    focused = kcalc_session.focus_window(app_name="kwrite")
    assert focused.startswith("Focused:"), focused
    assert "kwrite" in kcalc_session.active_window().lower()

    result = kcalc_session.invoke_ui_action(
        query="Seven",
        action_name="Press",
        app_name="kcalc",
    )
    assert "Invoked" in result, result

    _wait_for_binary(kcalc_session, "111")

    # Invoking the background control must not require focusing KCalc first.
    assert "kwrite" in kcalc_session.active_window().lower()


def test_invoke_ui_action_fails_closed_on_ambiguous_match(
    kcalc_session: AutomationEngine,
) -> None:
    with pytest.raises(RuntimeError, match=r"^Refused ambiguous AT-SPI action:"):
        kcalc_session.invoke_ui_action(
            query="button",
            action_name="Press",
            app_name="kcalc",
        )


def test_invoke_ui_action_respects_application_filter(
    kcalc_session: AutomationEngine,
) -> None:
    with pytest.raises(
        RuntimeError,
        match=re.escape("No element matching 'Seven' exposes AT-SPI action 'Press'."),
    ):
        kcalc_session.invoke_ui_action(
            query="Seven",
            action_name="Press",
            app_name="definitely-not-kcalc",
        )


def test_invoke_ui_action_exact_name_on_kcalc(kcalc_session: AutomationEngine) -> None:
    """KCalc's "Clear" and "Seven" buttons verify live exact-name disambiguation and fallback."""
    with pytest.raises(RuntimeError, match=r"^Refused ambiguous AT-SPI action:"):
        kcalc_session.invoke_ui_action(
            query="Clear",
            action_name="Press",
            app_name="kcalc",
        )

    cleared = kcalc_session.invoke_ui_action(
        query="clear",
        action_name="Press",
        app_name="kcalc",
        exact_name=True,
    )
    assert cleared.startswith("Invoked AT-SPI action: Press on "), cleared
    assert '"Clear"' in cleared, cleared

    result = kcalc_session.invoke_ui_action(
        query="seven",
        action_name="click",
        app_name="kcalc",
        exact_name=True,
        fallback_action_names=["Press"],
    )
    assert result.startswith("Invoked AT-SPI action: Press on "), result
    assert '"Seven"' in result, result

    _wait_for_binary(kcalc_session, "111")


@pytest.mark.parametrize("query", ["Seve", "button"])
def test_invoke_ui_action_exact_name_refuses_partial_or_role_matches_on_kcalc(
    kcalc_session: AutomationEngine, query: str
) -> None:
    """A name prefix or a role is no exact name: nothing is invoked."""
    with pytest.raises(
        RuntimeError,
        match=re.escape(f"No element named {query!r} exposes AT-SPI action 'Press'."),
    ):
        kcalc_session.invoke_ui_action(
            query=query, action_name="Press", app_name="kcalc", exact_name=True
        )


# ── Incomplete-discovery refusal (fake AT-SPI, no session needed) ────────
#
# invoke_action must prove its search of the scope was complete before it
# triggers anything: a single unreadable node could hide the element that
# makes the match ambiguous — or the real unique match. The tests below
# replace the Atspi module with fakes so they run without KWin; the KCalc
# tests above keep covering the real path end to end.


class _FakeNode:
    """Minimal ``Atspi.Accessible`` double with one injectable read fault."""

    def __init__(
        self,
        name: str,
        children: Iterable[_FakeNode] = (),
        fault: str = "",
        actions: list[str] | None = None,
    ) -> None:
        self.name = name
        self.children = list(children)
        self.fault = fault
        self.action = _FakeAction(self) if actions is not None else None

    def check(self, operation: str) -> None:
        if self.fault == operation:
            raise RuntimeError(f"injected {operation} read failure")

    def get_name(self) -> str:
        self.check("name")
        return self.name

    def get_role_name(self) -> str:
        self.check("role")
        return "push button" if self.action is not None else "panel"

    def get_description(self) -> str:
        self.check("description")
        return ""

    def get_action_iface(self) -> _FakeAction | None:
        self.check("action_iface")
        return self.action

    def get_child_count(self) -> int:
        self.check("child_count")
        return len(self.children)

    def get_child_at_index(self, index: int) -> _FakeNode:
        self.check("child")
        return self.children[index]


class _FakeAction:
    """Fake ``Atspi.Action`` whose reads delegate fault injection to the node."""

    def __init__(self, owner: _FakeNode) -> None:
        self.owner = owner

    def get_n_actions(self) -> int:
        self.owner.check("action_count")
        return 1

    def get_action_name(self, index: int) -> str:
        self.owner.check("action_name")
        return "Press"


# The desktop the injected Atspi module reports; rebuilt per scenario.
_DESKTOP: list[_FakeNode] = []


@pytest.fixture
def action_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace ``kwin_mcp.accessibility.Atspi`` with fakes and log do_action calls."""
    calls: list[str] = []

    def _do_action(iface: _FakeAction, index: int) -> bool:
        calls.append(iface.owner.name)
        return True

    monkeypatch.setattr(
        accessibility,
        "Atspi",
        types.SimpleNamespace(
            get_desktop=lambda _index: _DESKTOP[0],
            Action=types.SimpleNamespace(do_action=_do_action),
        ),
    )
    return calls


def _action_button(fault: str = "") -> _FakeNode:
    """A node matching the default query and exposing the 'Press' action."""
    return _FakeNode("Seven", fault=fault, actions=["press"])


def _run_action_request(
    children: list[_FakeNode],
    *,
    query: str = "Seven",
    action_name: str = "Press",
    app_name: str = "kcalc",
    app_fault: str = "",
) -> dict:
    """Send one 'action' request through the worker dispatch against the fakes."""
    _DESKTOP.clear()
    app = _FakeNode("kcalc", children=children, fault=app_fault)
    _DESKTOP.append(_FakeNode("desktop", children=[app]))
    return accessibility._handle_request(
        {"op": "action", "query": query, "action_name": action_name, "app_name": app_name}
    )


def _assert_discovery_refusal(response: dict, calls: list[str]) -> None:
    """Incomplete discovery must answer ok:false without invoking anything."""
    assert response["ok"] is False
    assert "incomplete discovery" in str(response["error"]).lower()
    assert calls == []


def test_invoke_ui_action_invokes_the_unique_match(action_calls: list[str]) -> None:
    response = _run_action_request([_action_button()])

    assert response == {
        "ok": True,
        "result": {
            "matches": 1,
            "invoked": True,
            "app": "kcalc",
            "role": "push button",
            "name": "Seven",
            "action": "Press",
        },
    }
    assert action_calls == ["Seven"]


def test_invoke_ui_action_refuses_ambiguous_match(action_calls: list[str]) -> None:
    response = _run_action_request([_action_button(), _action_button()])

    assert response == {"ok": True, "result": {"matches": 2, "invoked": False}}
    assert action_calls == []


def test_invoke_ui_action_ignores_a_match_without_an_action_interface(
    action_calls: list[str],
) -> None:
    """Missing Action interface is ordinary non-eligibility, not a read fault."""
    response = _run_action_request([_action_button(), _FakeNode("Seven label")])

    assert response["ok"] is True
    assert response["result"]["matches"] == 1
    assert action_calls == ["Seven"]


@pytest.mark.parametrize(
    "fault",
    ["name", "role", "description", "action_iface", "action_count", "action_name"],
)
def test_invoke_ui_action_refuses_when_a_candidate_is_unreadable(
    fault: str, action_calls: list[str]
) -> None:
    """A candidate that cannot be read could hide a second matching action."""
    response = _run_action_request([_action_button(), _action_button(fault=fault)])

    _assert_discovery_refusal(response, action_calls)


@pytest.mark.parametrize("fault", ["child_count", "child"])
def test_invoke_ui_action_refuses_when_a_subtree_is_unreadable(
    fault: str, action_calls: list[str]
) -> None:
    """A container whose children cannot be walked may hide a second match."""
    subtree = _FakeNode("panel", children=[_action_button()], fault=fault)

    response = _run_action_request([_action_button(), subtree])

    _assert_discovery_refusal(response, action_calls)


def test_invoke_ui_action_refuses_on_an_unreadable_non_candidate(
    action_calls: list[str],
) -> None:
    """Even a non-matching element must be readable; the fault could hide a match."""
    response = _run_action_request([_action_button(), _FakeNode("Status bar", fault="name")])

    _assert_discovery_refusal(response, action_calls)


def test_invoke_ui_action_refuses_when_the_app_name_is_unreadable(
    action_calls: list[str],
) -> None:
    """The app-name read decides the search scope, so its failure also refuses."""
    response = _run_action_request([_action_button()], app_fault="name")

    _assert_discovery_refusal(response, action_calls)


def test_invoke_ui_action_refuses_a_match_beyond_the_depth_limit(
    action_calls: list[str],
) -> None:
    """A subtree deeper than the limit cannot be searched, so nothing is invoked."""
    buried = _action_button()
    for _ in range(accessibility._ACTION_SEARCH_MAX_DEPTH):
        buried = _FakeNode("panel", children=[buried])

    response = _run_action_request([_action_button(), buried])

    _assert_discovery_refusal(response, action_calls)


def test_invoke_ui_action_invokes_a_match_at_the_depth_limit(
    action_calls: list[str],
) -> None:
    """A match exactly at the depth limit is still found and invoked."""
    buried = _action_button()
    for _ in range(accessibility._ACTION_SEARCH_MAX_DEPTH - 1):
        buried = _FakeNode("panel", children=[buried])

    response = _run_action_request([buried])

    assert response["ok"] is True
    assert action_calls == ["Seven"]


# ── Engine results over the fake worker (no session needed) ──────────────


def _engine_over_fake_worker(monkeypatch: pytest.MonkeyPatch) -> core.AutomationEngine:
    """Engine whose AT-SPI requests are answered by the worker dispatch over the fakes."""
    engine = core.AutomationEngine()
    monkeypatch.setattr(engine, "_get_session", lambda: object())

    def run_atspi(op: str, *, retry: bool = True, **kwargs: object) -> dict:
        # State-changing requests must never be retried.
        assert retry is False
        return accessibility._handle_request({"op": op, **kwargs})

    monkeypatch.setattr(engine, "_run_atspi", run_atspi)
    return engine


@pytest.mark.parametrize(
    ("query", "action_name", "message"),
    [("", "Press", "needs a query"), ("Seven", "", "needs an action_name")],
)
def test_invoke_ui_action_rejects_empty_arguments_without_a_session(
    query: str, action_name: str, message: str
) -> None:
    """Argument errors are ValueErrors raised before the session is looked up."""
    with pytest.raises(ValueError, match=message):
        core.AutomationEngine().invoke_ui_action(query=query, action_name=action_name)


def test_invoke_ui_action_failed_request_warns_the_action_may_have_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out or dead worker may still finish the walk and invoke the action."""
    engine = core.AutomationEngine()
    monkeypatch.setattr(engine, "_get_session", lambda: object())

    def time_out(op: str, **_kwargs: object) -> dict:
        msg = f"AT-SPI2 query timed out after 30s (op={op})"
        raise RuntimeError(msg)

    monkeypatch.setattr(engine, "_run_atspi", time_out)

    with pytest.raises(RuntimeError, match="may still have run") as failure:
        engine.invoke_ui_action(query="Seven", action_name="Press")
    assert str(failure.value).startswith("AT-SPI2 query timed out after 30s (op=action)")


def test_invoke_ui_action_reports_an_unconfirmed_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatch that raises (e.g. a modal dialog blocks the reply) is sent, not failed."""
    calls: list[str] = []

    def _do_action(iface: _FakeAction, index: int) -> bool:
        calls.append(iface.owner.name)
        msg = "Timeout was reached"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        accessibility,
        "Atspi",
        types.SimpleNamespace(
            get_desktop=lambda _index: _DESKTOP[0],
            Action=types.SimpleNamespace(do_action=_do_action),
        ),
    )

    response = _run_action_request([_action_button()])

    assert response == {
        "ok": True,
        "result": {
            "matches": 1,
            "invoked": None,
            "error": "Timeout was reached",
            "app": "kcalc",
            "role": "push button",
            "name": "Seven",
            "action": "Press",
        },
    }

    result = _engine_over_fake_worker(monkeypatch).invoke_ui_action(
        query="Seven", action_name="Press", app_name="kcalc"
    )

    assert result == (
        "Sent AT-SPI action 'Press' to [push button] \"Seven\" in kcalc, but the "
        "application did not confirm (Timeout was reached); the action may have run "
        "— check the UI before retrying."
    )
    assert "did not confirm" in result
    assert calls == ["Seven", "Seven"]


def test_incomplete_discovery_names_the_application_and_suggests_app_name(
    action_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal names the unreadable application; scoping with app_name recovers."""
    buried = _action_button()
    for _ in range(accessibility._ACTION_SEARCH_MAX_DEPTH):
        buried = _FakeNode("panel", children=[buried])
    _DESKTOP.clear()
    _DESKTOP.append(
        _FakeNode(
            "desktop",
            children=[
                _FakeNode("kcalc", children=[_action_button()]),
                _FakeNode("deep", children=[buried]),
            ],
        )
    )
    engine = _engine_over_fake_worker(monkeypatch)

    with pytest.raises(RuntimeError, match="Incomplete discovery") as failure:
        engine.invoke_ui_action(query="Seven", action_name="Press")
    message = str(failure.value)
    assert "'deep'" in message, message
    assert "app_name" in message, message
    assert action_calls == []

    result = engine.invoke_ui_action(query="Seven", action_name="Press", app_name="kcalc")

    assert result == 'Invoked AT-SPI action: Press on [push button] "Seven" in kcalc'
    assert action_calls == ["Seven"]


class _BrokenStdin:
    """Worker stdin whose reader is gone: the request line cannot be written."""

    def write(self, _data: bytes) -> int:
        raise BrokenPipeError

    def flush(self) -> None:
        return None


class _AcceptingStdin:
    def write(self, data: bytes) -> int:
        return len(data)

    def flush(self) -> None:
        return None


def _engine_with_worker(
    monkeypatch: pytest.MonkeyPatch, spawn: Callable[[], object]
) -> core.AutomationEngine:
    """Engine running the real ``_run_atspi`` against the worker ``spawn`` returns."""
    engine = core.AutomationEngine()
    monkeypatch.setattr(engine, "_get_session", lambda: object())
    monkeypatch.setattr(engine, "_ensure_atspi_worker", spawn)
    monkeypatch.setattr(engine, "_teardown_atspi_worker", lambda: None)
    return engine


def test_invoke_ui_action_unsent_request_has_no_may_have_run_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that exited before the request was written cannot have run the action."""
    worker = types.SimpleNamespace(stdin=_BrokenStdin(), stdout=object())
    engine = _engine_with_worker(monkeypatch, lambda: worker)

    with pytest.raises(core.AtspiRequestNotSentError) as failure:
        engine.invoke_ui_action(query="Seven", action_name="Press")
    message = str(failure.value)
    assert message == "AT-SPI2 query failed: AT-SPI2 worker exited before the request was sent"
    assert "may still have run" not in message, message


def test_invoke_ui_action_refused_worker_spawn_has_no_may_have_run_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker the process registry refused to start never received the request."""
    refusal = "the process registry is closed: refusing to spawn a new process"

    def refuse() -> object:
        raise RuntimeError(refusal)

    engine = _engine_with_worker(monkeypatch, refuse)

    with pytest.raises(core.AtspiRequestNotSentError) as failure:
        engine.invoke_ui_action(query="Seven", action_name="Press")
    assert str(failure.value) == refusal


def test_invoke_ui_action_worker_death_after_send_warns_the_action_may_have_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that took the request and then exited may have invoked the action."""
    import os

    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as stdout:
        worker = types.SimpleNamespace(stdin=_AcceptingStdin(), stdout=stdout, poll=lambda: 1)
        engine = _engine_with_worker(monkeypatch, lambda: worker)

        with pytest.raises(RuntimeError, match="may still have run") as failure:
            engine.invoke_ui_action(query="Seven", action_name="Press")
    assert not isinstance(failure.value, core.AtspiRequestNotSentError)
    assert str(failure.value).startswith("AT-SPI2 query failed: AT-SPI2 worker exited (status 1)")


# ── Exact names and fallback actions (fake AT-SPI, no session needed) ────
#
# exact_name compares the query with element names only (case-folded, whitespace
# collapsed, punctuation significant), and fallback_action_names lets one walk
# accept elements exposing "Press" or "click". The candidate is the element, so
# resolution and action choice happen in the same discovery that invokes.


class _ActionsNode(_FakeNode):
    """A node with an explicit action list, role and description.

    ``bad_action`` is an action index whose name cannot be read.
    """

    def __init__(
        self,
        name: str,
        actions: list[str],
        *,
        role: str = "push button",
        description: str = "",
        bad_action: int | None = None,
    ) -> None:
        super().__init__(name, actions=actions)
        self.action = _ListedActions(self, actions, bad_action)
        self.role = role
        self.description = description

    def get_role_name(self) -> str:
        return self.role

    def get_description(self) -> str:
        return self.description


class _ListedActions(_FakeAction):
    def __init__(self, owner: _FakeNode, names: list[str], bad_index: int | None) -> None:
        super().__init__(owner)
        self.names = names
        self.bad_index = bad_index

    def get_n_actions(self) -> int:
        return len(self.names)

    def get_action_name(self, index: int) -> str:
        if index == self.bad_index:
            msg = f"injected action name {index} read failure"
            raise RuntimeError(msg)
        return self.names[index]


@pytest.fixture
def invoked(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Replace ``Atspi`` with fakes; log (element name, action index) per do_action."""
    calls: list[tuple[str, int]] = []

    def _do_action(iface: _FakeAction, index: int) -> bool:
        calls.append((iface.owner.name, index))
        return True

    monkeypatch.setattr(
        accessibility,
        "Atspi",
        types.SimpleNamespace(
            get_desktop=lambda _index: _DESKTOP[0],
            Action=types.SimpleNamespace(do_action=_do_action),
        ),
    )
    return calls


def _request(
    apps: Mapping[str, Sequence[_FakeNode]],
    query: str,
    *,
    exact_name: bool = True,
    action_name: str = "Press",
    fallback_action_names: list[str] | None = None,
) -> dict:
    """One worker 'action' request over a desktop with the given applications."""
    _DESKTOP.clear()
    _DESKTOP.append(
        _FakeNode(
            "desktop",
            children=[_FakeNode(app, children=children) for app, children in apps.items()],
        )
    )
    # The new keys appear only when used, exactly as the engine sends them.
    request: dict[str, object] = {
        "op": "action",
        "query": query,
        "action_name": action_name,
        "app_name": "",
    }
    if exact_name:
        request["exact_name"] = True
    if fallback_action_names is not None:
        request["fallback_action_names"] = fallback_action_names
    return accessibility._handle_request(request)


_SAVE_FAMILY: list[_FakeNode] = [
    _ActionsNode("Save", ["Press"]),
    _ActionsNode("Save As…", ["Press"]),
    _ActionsNode("Autosave", ["Press"]),
]


def test_exact_name_ignores_substring_siblings(invoked: list[tuple[str, int]]) -> None:
    response = _request({"editor": _SAVE_FAMILY}, "save")

    assert response["result"] == {
        "matches": 1,
        "invoked": True,
        "app": "editor",
        "role": "push button",
        "name": "Save",
        "action": "Press",
    }
    assert invoked == [("Save", 0)]


def test_substring_mode_still_counts_every_sibling(invoked: list[tuple[str, int]]) -> None:
    """Without exact_name the long-standing substring semantics are unchanged."""
    response = _request({"editor": _SAVE_FAMILY}, "save", exact_name=False)

    assert response["result"] == {"matches": 3, "invoked": False}
    assert invoked == []


def test_exact_name_does_not_match_role_or_description(
    invoked: list[tuple[str, int]],
) -> None:
    nodes: list[_FakeNode] = [
        _ActionsNode("Store", ["Press"], description="save"),
        _ActionsNode("Keep", ["Press"], role="save"),
    ]

    response = _request({"editor": nodes}, "save")

    assert response["result"] == {"matches": 0, "invoked": False}
    assert invoked == []


@pytest.mark.parametrize("query", ["save all", "SAVE ALL", "  Save \t all "])
def test_exact_name_folds_case_and_whitespace(query: str, invoked: list[tuple[str, int]]) -> None:
    response = _request({"editor": [_ActionsNode("  Save   All ", ["Press"])]}, query)

    assert response["result"]["matches"] == 1
    assert invoked == [("  Save   All ", 0)]


@pytest.mark.parametrize(
    ("name", "query", "matches"), [("Save.", "save", 0), ("Save.", "save.", 1)]
)
def test_exact_name_keeps_punctuation_significant(
    name: str, query: str, matches: int, invoked: list[tuple[str, int]]
) -> None:
    response = _request({"editor": [_ActionsNode(name, ["Press"])]}, query)

    assert response["result"]["matches"] == matches
    assert len(invoked) == matches


def test_exact_name_refuses_two_exact_matches(invoked: list[tuple[str, int]]) -> None:
    nodes: list[_FakeNode] = [_ActionsNode("Save", ["Press"]), _ActionsNode("save", ["Press"])]

    response = _request({"editor": nodes}, "save")

    assert response["result"] == {"matches": 2, "invoked": False}
    assert invoked == []


@pytest.mark.parametrize("query", ["", "   ", "\t\n"])
def test_exact_name_empty_or_whitespace_query_never_matches_unnamed_elements(
    query: str, invoked: list[tuple[str, int]]
) -> None:
    """Unnamed controls must never match an empty or whitespace-only exact query."""
    nodes: list[_FakeNode] = [_ActionsNode("", ["Press"]), _ActionsNode("Save", ["Press"])]

    response = _request({"editor": nodes}, query)
    assert response["ok"] is False
    assert "no name left" in str(response["error"])
    assert invoked == []

    direct = accessibility.invoke_action(query, "Press", exact_name=True)
    assert direct == {"matches": 0, "invoked": False}
    assert invoked == []


def test_exact_name_without_a_listed_action_is_not_a_candidate(
    invoked: list[tuple[str, int]],
) -> None:
    nodes: list[_FakeNode] = [_ActionsNode("Save", ["SetFocus"]), _FakeNode("Save")]

    response = _request({"editor": nodes}, "save", fallback_action_names=["click"])

    assert response["result"] == {"matches": 0, "invoked": False}
    assert invoked == []


@pytest.mark.parametrize(
    ("actions", "expected"),
    [
        (["click", "Press"], ("Press", 1)),
        (["Press", "click"], ("Press", 0)),
        (["SetFocus", "click"], ("click", 1)),
        (["activate", "Click"], ("Click", 1)),
        (["press"], ("press", 0)),
    ],
    ids=[
        "press-preferred-later",
        "press-preferred-first",
        "click-fallback",
        "earlier-fallback-preferred-casefold",
        "own-spelling",
    ],
)
def test_fallback_actions_follow_preference_order(
    actions: list[str], expected: tuple[str, int], invoked: list[tuple[str, int]]
) -> None:
    response = _request(
        {"editor": [_ActionsNode("Save", actions)]},
        "save",
        fallback_action_names=["click", "activate"],
    )

    assert response["result"]["matches"] == 1
    assert response["result"]["action"] == expected[0]
    assert invoked == [("Save", expected[1])]


@pytest.mark.parametrize("actions", [["Press", "click"], ["Press", "Press"]])
def test_an_element_counts_once_however_many_listed_actions_it_exposes(
    actions: list[str], invoked: list[tuple[str, int]]
) -> None:
    response = _request(
        {"editor": [_ActionsNode("Save", actions)]}, "save", fallback_action_names=["click"]
    )

    assert response["result"]["matches"] == 1
    assert invoked == [("Save", 0)]


def test_legacy_mode_counts_each_duplicate_action_index(invoked: list[tuple[str, int]]) -> None:
    """Without the new options, two "Press" indices on one element still refuse."""
    response = _request(
        {"kcalc": [_ActionsNode("Seven", ["Press", "Press"])]}, "seven", exact_name=False
    )

    assert response == {"ok": True, "result": {"matches": 2, "invoked": False}}
    assert invoked == []


def test_exact_mode_counts_duplicate_action_indices_once(
    invoked: list[tuple[str, int]],
) -> None:
    """With exact_name the element is the candidate: its first "Press" index runs once."""
    response = _request({"kcalc": [_ActionsNode("Seven", ["Press", "Press"])]}, "seven")

    assert response["result"]["matches"] == 1
    assert response["result"]["invoked"] is True
    assert invoked == [("Seven", 0)]


def test_press_and_click_elements_in_different_apps_are_ambiguous(
    invoked: list[tuple[str, int]],
) -> None:
    """Action preference never breaks ambiguity: two elements stay two candidates."""
    response = _request(
        {"qt-app": [_ActionsNode("Save", ["Press"])], "gtk-app": [_ActionsNode("Save", ["click"])]},
        "save",
        fallback_action_names=["click"],
    )

    assert response["result"] == {"matches": 2, "invoked": False}
    assert invoked == []


def test_fallback_actions_also_work_in_substring_mode(invoked: list[tuple[str, int]]) -> None:
    response = _request(
        {"gtk-app": [_ActionsNode("Seven", ["click", "click"])]},
        "seven",
        exact_name=False,
        fallback_action_names=["click"],
    )

    assert response["result"]["matches"] == 1
    assert response["result"]["action"] == "click"
    assert invoked == [("Seven", 0)]


def test_exact_name_reads_every_action_name_before_choosing(
    invoked: list[tuple[str, int]],
) -> None:
    """An unreadable later action name refuses even though "Press" was already found."""
    response = _request(
        {"editor": [_ActionsNode("Save", ["Press", "click"], bad_action=1)]}, "save"
    )

    _assert_discovery_refusal(response, [])
    assert invoked == []


@pytest.mark.parametrize("fault", ["name", "role", "description", "child_count"])
def test_exact_name_still_refuses_incomplete_discovery(
    fault: str, invoked: list[tuple[str, int]]
) -> None:
    """A non-candidate that cannot be read could hide a second exact match."""
    nodes: list[_FakeNode] = [_ActionsNode("Save", ["Press"]), _FakeNode("Status", fault=fault)]

    response = _request({"editor": nodes}, "save")

    _assert_discovery_refusal(response, [])
    assert invoked == []


def test_exact_name_still_refuses_a_match_beyond_the_depth_limit(
    invoked: list[tuple[str, int]],
) -> None:
    buried: _FakeNode = _ActionsNode("Save", ["Press"])
    for _ in range(accessibility._ACTION_SEARCH_MAX_DEPTH):
        buried = _FakeNode("panel", children=[buried])

    response = _request({"editor": [_ActionsNode("Save", ["Press"]), buried]}, "save")

    _assert_discovery_refusal(response, [])
    assert invoked == []


# ── Engine: exact names and fallbacks (no session needed) ────────────────


def test_engine_exact_name_reports_the_invoked_fallback_action(
    invoked: list[tuple[str, int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _request({"gtk-app": [_ActionsNode("Save", ["click"])]}, "save")
    engine = _engine_over_fake_worker(monkeypatch)

    result = engine.invoke_ui_action(
        query="save", action_name="Press", exact_name=True, fallback_action_names=["click"]
    )

    assert result == 'Invoked AT-SPI action: click on [push button] "Save" in gtk-app'
    assert invoked == [("Save", 0)]


def test_engine_exact_name_not_found_message(
    invoked: list[tuple[str, int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _request({"editor": _SAVE_FAMILY}, "x")
    engine = _engine_over_fake_worker(monkeypatch)

    with pytest.raises(RuntimeError) as failure:
        engine.invoke_ui_action(
            query="Save all", action_name="Press", exact_name=True, fallback_action_names=["click"]
        )
    assert str(failure.value) == (
        "No element named 'Save all' exposes AT-SPI action 'Press' or 'click'."
    )
    assert invoked == []


def test_engine_exact_name_ambiguity_message(
    invoked: list[tuple[str, int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _request(
        {"qt-app": [_ActionsNode("Save", ["Press"])], "gtk-app": [_ActionsNode("Save", ["click"])]},
        "x",
    )
    engine = _engine_over_fake_worker(monkeypatch)

    with pytest.raises(RuntimeError) as failure:
        engine.invoke_ui_action(
            query="save", action_name="Press", exact_name=True, fallback_action_names=["click"]
        )
    assert str(failure.value) == (
        "Refused ambiguous AT-SPI action: 2 elements named 'save' expose 'Press' or 'click'."
    )
    assert invoked == []


def test_engine_failure_message_names_the_chosen_fallback_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        accessibility,
        "Atspi",
        types.SimpleNamespace(
            get_desktop=lambda _index: _DESKTOP[0],
            Action=types.SimpleNamespace(do_action=lambda _iface, _index: False),
        ),
    )
    _DESKTOP.clear()
    _DESKTOP.append(
        _FakeNode(
            "desktop", children=[_FakeNode("gtk-app", children=[_ActionsNode("Save", ["click"])])]
        )
    )
    engine = _engine_over_fake_worker(monkeypatch)

    with pytest.raises(RuntimeError) as failure:
        engine.invoke_ui_action(
            query="save", action_name="Press", exact_name=True, fallback_action_names=["click"]
        )
    assert str(failure.value) == (
        "AT-SPI action 'click' matched one element but the application reported failure."
    )


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        ([], "No element matching 'Seven' exposes AT-SPI action 'Press'."),
        (
            [_ActionsNode("Seven", ["Press", "Press"])],
            "Refused ambiguous AT-SPI action: 2 elements matching 'Seven' expose 'Press'.",
        ),
    ],
    ids=["not-found", "duplicate-action-indices"],
)
def test_engine_legacy_texts_are_unchanged(
    nodes: list[_FakeNode],
    message: str,
    invoked: list[tuple[str, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _request({"kcalc": nodes}, "x")
    engine = _engine_over_fake_worker(monkeypatch)

    with pytest.raises(RuntimeError) as failure:
        engine.invoke_ui_action(query="Seven", action_name="Press")
    assert str(failure.value) == message
    assert invoked == []


def test_engine_legacy_failure_message_names_the_requested_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        accessibility,
        "Atspi",
        types.SimpleNamespace(
            get_desktop=lambda _index: _DESKTOP[0],
            Action=types.SimpleNamespace(do_action=lambda _iface, _index: False),
        ),
    )
    _DESKTOP.clear()
    _DESKTOP.append(
        _FakeNode("desktop", children=[_FakeNode("kcalc", children=[_action_button()])])
    )
    engine = _engine_over_fake_worker(monkeypatch)

    with pytest.raises(RuntimeError) as failure:
        engine.invoke_ui_action(query="Seven", action_name="Press")
    assert str(failure.value) == (
        "AT-SPI action 'Press' matched one element but the application reported failure."
    )


def test_engine_sends_the_new_options_only_when_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default requests keep exactly their original shape."""
    engine = core.AutomationEngine()
    monkeypatch.setattr(engine, "_get_session", lambda: object())
    requests: list[dict[str, object]] = []

    def run_atspi(op: str, *, retry: bool = True, **kwargs: object) -> dict:
        requests.append({"op": op, **kwargs})
        return {"ok": True, "result": {"matches": 0, "invoked": False}}

    monkeypatch.setattr(engine, "_run_atspi", run_atspi)

    options: tuple[dict[str, Any], ...] = (
        {},
        {"exact_name": True},
        {"fallback_action_names": ["click"]},
        {"exact_name": True, "fallback_action_names": ["click"]},
    )
    for kwargs in options:
        with pytest.raises(RuntimeError):
            engine.invoke_ui_action("save", "Press", **kwargs)

    base = {"op": "action", "query": "save", "action_name": "Press", "app_name": ""}
    assert requests == [
        base,
        {**base, "exact_name": True},
        {**base, "fallback_action_names": ["click"]},
        {**base, "exact_name": True, "fallback_action_names": ["click"]},
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"exact_name": "yes"}, "exact_name must be true or false"),
        ({"exact_name": 1}, "exact_name must be true or false"),
        ({"fallback_action_names": "click"}, "fallback_action_names must be a list"),
        ({"fallback_action_names": [""]}, "fallback_action_names must be a list"),
        ({"fallback_action_names": ["   "]}, "fallback_action_names must be a list"),
        ({"fallback_action_names": ["click", 7]}, "fallback_action_names must be a list"),
    ],
)
def test_engine_rejects_invalid_options_without_a_session(
    kwargs: dict[str, Any], message: str
) -> None:
    """Option errors are ValueErrors raised before the session is looked up."""
    with pytest.raises(ValueError, match=message):
        core.AutomationEngine().invoke_ui_action("save", "Press", **kwargs)


@pytest.mark.parametrize(
    ("query", "action_name", "app_name", "exact_name", "message"),
    [
        ("", "Press", "", False, "invoke_ui_action needs a query"),
        ("   ", "Press", "", False, "invoke_ui_action needs a query"),
        ("\t\n", "Press", "", True, "invoke_ui_action needs a query"),
        (7, "Press", "", False, "invoke_ui_action needs a query"),
        ("save", "", "", False, "invoke_ui_action needs an action_name"),
        ("save", "   ", "", True, "invoke_ui_action needs an action_name"),
        ("save", 7, "", True, "invoke_ui_action needs an action_name"),
        ("save", "Press", 7, False, "invoke_ui_action app_name must be a string"),
    ],
)
def test_engine_rejects_empty_or_non_string_query_and_action_without_a_session(
    query: Any, action_name: Any, app_name: Any, exact_name: bool, message: str
) -> None:
    """Blank or non-string queries and action names fail before the session is looked up."""
    with pytest.raises(ValueError, match=message):
        core.AutomationEngine().invoke_ui_action(
            query=query, action_name=action_name, app_name=app_name, exact_name=exact_name
        )
