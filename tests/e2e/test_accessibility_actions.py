"""End-to-end coverage for native AT-SPI action invocation."""

from __future__ import annotations

import re
import time
import types
from typing import TYPE_CHECKING

import pytest

from kwin_mcp import accessibility, core

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

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
    with pytest.raises(RuntimeError, match="^Refused ambiguous AT-SPI action:"):
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
