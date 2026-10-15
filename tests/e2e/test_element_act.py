"""End-to-end coverage for element_act: native AT-SPI actions by ref or strict locator.

The strict resolver is the 0.11 ``invoke_ui_action`` one, unchanged (G11): a
fail-closed, depth-64 walk that acts only on exactly one (element, action)
candidate. The KCalc and window-zoo tests drive the real path end to end and
read the outcome through independent oracles (``atspi_oracle``, the zoo's
press log, ``kwin_oracle``); the fake-AT-SPI tests pin the worker's discovery
refusals and the engine's dispatch reporting without a session.
"""

from __future__ import annotations

import os
import signal
import time
import types
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from atspi_oracle import AtspiNode, AtspiOracle
from kwin_oracle import KWinOracle
from pydantic import TypeAdapter
from window_zoo import APP_ID, MAIN_CAPTION, ZooProcess

from kwin_mcp import accessibility, core
from kwin_mcp.accessibility import _RefTable
from kwin_mcp.models import (
    ElementAddress,
    Locator,
    RefAddr,
    StrictAddr,
    Wait,
    WindowSelector,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import AppLaunchResult, ElementList, SessionInfo

# How long KCalc gets to show the result of an action in its display.
DISPLAY_TIMEOUT_S = 10.0
# libatspi gives an application it learned about less than this long ago the
# startup timeout (15 s) instead of the method-call timeout (0.8 s); a stopped
# application is only probed after this, so each unreadable read fails fast.
ATSPI_APP_STARTUP_S = 16.0


# ── KCalc display through the independent AT-SPI oracle ─────────────────


def _session_dbus(engine: AutomationEngine) -> str:
    info = engine.session_info().session
    assert info is not None
    return info.dbus_address


def _kcalc_binary(nodes: list[AtspiNode]) -> str | None:
    """KCalc's binary display: the label right before the base label ``2`` (pre-order)."""
    for index, node in enumerate(nodes):
        if index == 0 or node.role != "label" or node.name != "2":
            continue
        previous = nodes[index - 1]
        if previous.role == "label" and previous.name is not None:
            return previous.name.replace(" ", "")
    return None


def _wait_for_binary(oracle: AtspiOracle, expected: str) -> None:
    oracle.wait_for(
        lambda nodes: _kcalc_binary(nodes) == expected, app="kcalc", timeout_s=DISPLAY_TIMEOUT_S
    )


def _assert_binary_stays(oracle: AtspiOracle, expected: str) -> None:
    """The display keeps ``expected`` (read twice, so a late action would show)."""
    for _ in range(2):
        actual = _kcalc_binary(oracle.walk("kcalc"))
        assert actual == expected, actual
        time.sleep(0.5)


def _kcalc_seven(engine: AutomationEngine) -> ElementList:
    found = engine.element_query(Locator(app="kcalc", query="Seven"))
    sevens = [e for e in found.elements if e.name == "Seven"]
    assert len(sevens) == 1, found.render()
    return found


def _press_seven(engine: AutomationEngine) -> StrictAddr:
    return StrictAddr(strict=Locator(query="Seven", app="kcalc"))


# ── KCalc (real AT-SPI, ported 0.11 rows) ───────────────────────────────


def test_element_act_works_on_background_window(
    kcalc_session: AutomationEngine,
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
) -> None:
    dbus = _session_dbus(kcalc_session)
    launch("kwrite /tmp/kwin-mcp-action-background.txt")
    wait_for_app("kwrite")

    # Make the second app definitively foreground; KCalc must remain actionable
    # through its semantic accessibility interface without pointer coordinates.
    focused = kcalc_session.window_focus(WindowSelector(app_id="kwrite"))
    assert focused.window is not None and focused.window.active, focused.render()
    kwin = KWinOracle(dbus)
    active = kwin.active_window()
    assert active is not None and "kwrite" in active.resource_class.lower(), active

    result = kcalc_session.element_act(_press_seven(kcalc_session), "Press")
    assert result.dispatch == "confirmed", result.render()
    assert result.summary().startswith("Invoked AT-SPI action: Press on ["), result.render()

    _wait_for_binary(AtspiOracle(dbus), "111")

    # Invoking the background control must not require focusing KCalc first.
    active = kwin.active_window()
    assert active is not None and "kwrite" in active.resource_class.lower(), active


def test_element_act_fails_closed_on_ambiguous_match(
    kcalc_session: AutomationEngine,
) -> None:
    with pytest.raises(RuntimeError, match=r"^Refused ambiguous AT-SPI action:"):
        kcalc_session.element_act(StrictAddr(strict=Locator(query="button", app="kcalc")), "Press")


def test_element_act_respects_application_filter(
    kcalc_session: AutomationEngine,
) -> None:
    expected = (
        "No element matching query='Seven', app='definitely-not-kcalc' exposes AT-SPI "
        "action 'Press'. Matching elements expose: (none).\n"
    )
    with pytest.raises(RuntimeError) as failure:
        kcalc_session.element_act(
            StrictAddr(strict=Locator(query="Seven", app="definitely-not-kcalc")), "Press"
        )
    assert str(failure.value).startswith(expected), str(failure.value)


@pytest.mark.qa("QA-ELEM-046")
def test_element_act_exact_match_on_kcalc(kcalc_session: AutomationEngine) -> None:
    """Exact accessible names disambiguate Clear from KCalc's other clear buttons."""
    with pytest.raises(RuntimeError, match=r"^Refused ambiguous AT-SPI action:"):
        kcalc_session.element_act(StrictAddr(strict=Locator(query="Clear", app="kcalc")), "Press")

    cleared = kcalc_session.element_act(
        StrictAddr(strict=Locator(query="clear", app="kcalc", match="exact")), "Press"
    )
    assert cleared.action == "Press", cleared
    assert cleared.dispatch == "confirmed", cleared
    assert cleared.element is not None and cleared.element.name == "Clear", cleared
    assert cleared.summary().startswith("Invoked AT-SPI action: Press on "), cleared.render()

    # Neither a partial accessible name nor a role is an exact name.
    for query in ("Seve", "button"):
        with pytest.raises(
            RuntimeError,
            match=(
                rf"^No element matching query='{query}', match='exact', app='kcalc' "
                r"exposes AT-SPI action 'Press'\."
            ),
        ):
            kcalc_session.element_act(
                StrictAddr(strict=Locator(query=query, app="kcalc", match="exact")), "Press"
            )

    result = kcalc_session.element_act(
        StrictAddr(strict=Locator(query="seven", app="kcalc", match="exact")), "Press"
    )
    assert result.action == "Press" and result.dispatch == "confirmed", result
    assert result.element is not None and result.element.name == "Seven", result
    _wait_for_binary(AtspiOracle(_session_dbus(kcalc_session)), "111")


def test_element_act_spoken_match_on_kcalc(kcalc_session: AutomationEngine) -> None:
    """A dictated SEVEN. matches Seven; the preferred listed action is reported."""
    result = kcalc_session.element_act(
        StrictAddr(strict=Locator(query="SEVEN.", app="kcalc", match="spoken")),
        ["Press", "click"],
    )
    assert result.action == "Press" and result.dispatch == "confirmed", result
    assert result.element is not None and result.element.name == "Seven", result
    assert result.summary().startswith("Invoked AT-SPI action: Press on "), result.render()
    _wait_for_binary(AtspiOracle(_session_dbus(kcalc_session)), "111")


@pytest.mark.qa("QA-ELEM-046")
def test_element_act_fallback_actions_on_kcalc(kcalc_session: AutomationEngine) -> None:
    """An unavailable first action falls back to Press and reports the invoked action."""
    for fallback in ("click|Press", ["click", "Press"]):
        result = kcalc_session.element_act(
            StrictAddr(strict=Locator(query="seven", app="kcalc", match="exact")),
            fallback,
        )
        assert result.action == "Press", result
        assert result.dispatch == "confirmed", result
        assert result.element is not None and result.element.name == "Seven", result
        assert result.summary().startswith("Invoked AT-SPI action: Press on "), result.render()
    _wait_for_binary(AtspiOracle(_session_dbus(kcalc_session)), "1001101")


# ── Incomplete-discovery refusal (fake AT-SPI, no session needed) ────────
#
# The strict search must prove its search of the scope was complete before it
# triggers anything: a single unreadable node could hide the element that
# makes the match ambiguous — or the real unique match. The tests below
# replace the Atspi module with fakes so they run without KWin; the KCalc
# tests above keep covering the real path end to end. The strict path ends by
# resolving the single candidate's ref, so the fakes also answer those reads.


@dataclass(frozen=True)
class _FakeState:
    value_nick: str


class _FakeStateSet:
    def get_states(self) -> list[_FakeState]:
        return [_FakeState("enabled"), _FakeState("visible")]


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
        self.action = _FakeAction(self, actions) if actions is not None else None

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

    def get_state_set(self) -> _FakeStateSet:
        return _FakeStateSet()

    def get_component_iface(self) -> None:
        return None

    def get_text_iface(self) -> None:
        return None

    def get_value_iface(self) -> None:
        return None

    def get_action_iface(self) -> _FakeAction | None:
        self.check("action_iface")
        return self.action

    def get_child_count(self) -> int:
        self.check("child_count")
        return len(self.children)

    def get_child_at_index(self, index: int) -> _FakeNode:
        self.check("child")
        return self.children[index]

    def get_process_id(self) -> int:
        return 0


class _FakeAction:
    """Fake ``Atspi.Action`` whose reads delegate fault injection to the node."""

    def __init__(self, owner: _FakeNode, names: list[str]) -> None:
        self.owner = owner
        self.names = names

    def get_n_actions(self) -> int:
        self.owner.check("action_count")
        return len(self.names)

    def get_action_name(self, index: int) -> str:
        self.owner.check("action_name")
        return self.names[index]


# The desktop the injected Atspi module reports; rebuilt per scenario.
_DESKTOP: list[_FakeNode] = []


def _install_fake_atspi(
    monkeypatch: pytest.MonkeyPatch, do_action: Callable[[_FakeAction, int], bool]
) -> None:
    """Point the worker at ``_DESKTOP``, an unavailable KWin and a generation-1 ref table."""
    monkeypatch.setattr(
        accessibility,
        "Atspi",
        types.SimpleNamespace(
            get_desktop=lambda _index: _DESKTOP[0],
            Action=types.SimpleNamespace(do_action=do_action),
        ),
    )
    monkeypatch.setattr(accessibility, "_kwin_windows", lambda: ([], "no KWin in fake tests"))
    monkeypatch.setattr(accessibility, "_REFS", _RefTable(generation=1))


@pytest.fixture
def action_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace ``kwin_mcp.accessibility.Atspi`` with fakes and log do_action calls."""
    calls: list[str] = []

    def _do_action(iface: _FakeAction, index: int) -> bool:
        calls.append(iface.owner.name)
        return True

    _install_fake_atspi(monkeypatch, _do_action)
    return calls


def _action_button(fault: str = "") -> _FakeNode:
    """A node matching the default query and exposing the 'Press' action."""
    return _FakeNode("Seven", fault=fault, actions=["Press"])


def _run_action_request(
    children: list[_FakeNode],
    *,
    query: str = "Seven",
    action_name: str = "Press",
    app_name: str = "kcalc",
    app_fault: str = "",
) -> dict:
    """Send one strict 'action' request through the worker dispatch against the fakes."""
    _DESKTOP.clear()
    app = _FakeNode("kcalc", children=children, fault=app_fault)
    _DESKTOP.append(_FakeNode("desktop", children=[app]))
    locator = {"query": query, **({"app": app_name} if app_name else {})}
    return accessibility._handle_request(
        {"op": "action", "address": {"strict": locator}, "action_name": action_name}
    )


_SCOPED_HINT = "\nFind the element with element_query, then act on its ref."


def _assert_discovery_refusal(response: dict, calls: list[str], reason: str) -> None:
    """Incomplete discovery must answer ok:false, name kcalc, and invoke nothing."""
    assert response == {
        "ok": False,
        "error": f"Incomplete discovery in application 'kcalc': {reason}.{_SCOPED_HINT}",
    }
    assert calls == []


def test_element_act_invokes_the_unique_match(action_calls: list[str]) -> None:
    response = _run_action_request([_action_button()])
    assert response["ok"] is True, response
    result = dict(response["result"])
    element = result.pop("element")
    assert result == {
        "matches": 1,
        "invoked": True,
        "app": "kcalc",
        "role": "push button",
        "name": "Seven",
        "action": "Press",
    }
    assert element["name"] == "Seven" and element["role"] == "push button", element
    assert element["app"] == "kcalc" and element["depth"] == 1, element
    assert str(element["ref"]).startswith("e1."), element
    assert action_calls == ["Seven"]


def test_element_act_refuses_ambiguous_match(action_calls: list[str]) -> None:
    response = _run_action_request([_action_button(), _action_button()])
    assert response["ok"] is False, response
    error = response["error"]
    assert error.startswith(
        "Refused ambiguous AT-SPI action: 2 elements matching query='Seven', app='kcalc' "
        "expose 'Press': "
    ), error
    assert error.count('[push button] "Seven" in kcalc (e1.') == 2, error
    assert error.endswith("\nNarrow the locator (role, states, app) or act on one ref."), error
    assert action_calls == []


def test_element_act_ignores_a_match_without_an_action_interface(
    action_calls: list[str],
) -> None:
    """Missing Action interface is ordinary non-eligibility, not a read fault."""
    response = _run_action_request([_action_button(), _FakeNode("Seven label")])
    assert response["ok"] is True, response
    assert response["result"]["matches"] == 1
    assert response["result"]["invoked"] is True
    assert action_calls == ["Seven"]


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("name", "could not read element name: injected name read failure"),
        ("role", "could not read element role: injected role read failure"),
        (
            "description",
            "could not read element description: injected description read failure",
        ),
        ("action_iface", "could not read action interface: injected action_iface read failure"),
        ("action_count", "could not read action count: injected action_count read failure"),
        ("action_name", "could not read action name: injected action_name read failure"),
    ],
    ids=["name", "role", "description", "action_iface", "action_count", "action_name"],
)
def test_element_act_refuses_when_a_candidate_is_unreadable(
    fault: str, reason: str, action_calls: list[str]
) -> None:
    """A candidate that cannot be read could hide a second matching action."""
    response = _run_action_request([_action_button(), _action_button(fault=fault)])
    _assert_discovery_refusal(response, action_calls, reason)


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("child_count", "could not read child count: injected child_count read failure"),
        ("child", "could not read child: injected child read failure"),
    ],
    ids=["child_count", "child"],
)
def test_element_act_refuses_when_a_subtree_is_unreadable(
    fault: str, reason: str, action_calls: list[str]
) -> None:
    """A container whose children cannot be walked may hide a second match."""
    subtree = _FakeNode("panel", children=[_action_button()], fault=fault)
    response = _run_action_request([_action_button(), subtree])
    _assert_discovery_refusal(response, action_calls, reason)


def test_element_act_refuses_on_an_unreadable_non_candidate(
    action_calls: list[str],
) -> None:
    """Even a non-matching element must be readable; the fault could hide a match."""
    response = _run_action_request([_action_button(), _FakeNode("Status bar", fault="name")])
    _assert_discovery_refusal(
        response, action_calls, "could not read element name: injected name read failure"
    )


def test_element_act_refuses_when_the_app_name_is_unreadable(
    action_calls: list[str],
) -> None:
    """The app-name read decides the search scope, so its failure also refuses."""
    response = _run_action_request([_action_button()], app_fault="name")
    assert response == {
        "ok": False,
        "error": "Incomplete discovery: could not read application name: "
        "injected name read failure",
    }
    assert action_calls == []


def test_element_act_refuses_a_match_beyond_the_depth_limit(
    action_calls: list[str],
) -> None:
    """A subtree deeper than the limit cannot be searched, so nothing is invoked."""
    buried = _action_button()
    for _ in range(accessibility._ACTION_SEARCH_MAX_DEPTH):
        buried = _FakeNode("panel", children=[buried])
    response = _run_action_request([_action_button(), buried])
    _assert_discovery_refusal(
        response,
        action_calls,
        f"accessibility subtree deeper than {accessibility._ACTION_SEARCH_MAX_DEPTH} levels",
    )


def test_element_act_invokes_a_match_at_the_depth_limit(
    action_calls: list[str],
) -> None:
    """A match exactly at the depth limit is still found and invoked."""
    buried = _action_button()
    for _ in range(accessibility._ACTION_SEARCH_MAX_DEPTH - 1):
        buried = _FakeNode("panel", children=[buried])
    response = _run_action_request([buried])
    assert response["ok"] is True, response
    assert response["result"]["element"]["depth"] == accessibility._ACTION_SEARCH_MAX_DEPTH
    assert action_calls == ["Seven"]


# ── Engine results over the fake worker (no session needed) ──────────────


def _engine_over_fake_worker(monkeypatch: pytest.MonkeyPatch) -> core.AutomationEngine:
    """Engine whose AT-SPI requests are answered by the worker dispatch over the fakes."""
    engine = core.AutomationEngine()
    monkeypatch.setattr(engine, "_require_session", lambda: object())

    def run_atspi(op: str, *, retry: bool = True, **kwargs: object) -> dict:
        # State-changing requests must never be retried.
        assert retry is False
        return accessibility._handle_request({"op": op, **kwargs})

    monkeypatch.setattr(engine, "_run_atspi", run_atspi)
    return engine


@pytest.mark.parametrize(
    ("address", "action", "message"),
    [
        pytest.param(
            {},
            "Press",
            r"(?s)ref\n\s+Field required.*strict\n\s+Field required",
            id="-Press-needs a query",
        ),
        pytest.param(
            {"strict": {"query": "Seven"}},
            "",
            r'^element_act needs an action name, e\.g\. "Press"\.$',
            id="Seven--needs an action name",
        ),
    ],
)
def test_element_act_rejects_empty_address_without_a_session(
    address: dict, action: str, message: str
) -> None:
    """Argument errors are ValueErrors raised before the session is looked up."""
    engine = core.AutomationEngine()
    with pytest.raises(ValueError, match=message):
        element = TypeAdapter(ElementAddress).validate_python(address)
        engine.element_act(element, action)
    assert engine._session is None


def test_element_act_failed_request_warns_the_action_may_have_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out or dead worker may still finish the walk and invoke the action."""
    engine = core.AutomationEngine()
    monkeypatch.setattr(engine, "_require_session", lambda: object())

    def time_out(op: str, **_kwargs: object) -> dict:
        msg = f"AT-SPI2 query timed out after 30s (op={op})"
        raise RuntimeError(msg)

    monkeypatch.setattr(engine, "_run_atspi", time_out)
    with pytest.raises(RuntimeError) as failure:
        engine.element_act(StrictAddr(strict=Locator(query="Seven")), "Press")
    assert str(failure.value) == (
        "AT-SPI2 query timed out after 30s (op=action). "
        "The action may still have run; check the UI before retrying."
    )


def test_element_act_reports_an_unconfirmed_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatch that raises (e.g. a modal dialog blocks the reply) is sent, not failed."""
    calls: list[str] = []

    def _do_action(iface: _FakeAction, index: int) -> bool:
        calls.append(iface.owner.name)
        msg = "Timeout was reached"
        raise RuntimeError(msg)

    _install_fake_atspi(monkeypatch, _do_action)
    response = _run_action_request([_action_button()])
    assert response["ok"] is True, response
    result = dict(response["result"])
    assert result.pop("element")["name"] == "Seven"
    assert result == {
        "matches": 1,
        "invoked": None,
        "error": "Timeout was reached",
        "app": "kcalc",
        "role": "push button",
        "name": "Seven",
        "action": "Press",
    }

    act = _engine_over_fake_worker(monkeypatch).element_act(
        StrictAddr(strict=Locator(query="Seven", app="kcalc")), "Press"
    )
    assert act.dispatch == "sent_unconfirmed"
    assert act.dispatch_error == "Timeout was reached"
    assert act.render().splitlines()[0] == (
        "Sent AT-SPI action 'Press' to [push button] \"Seven\" in kcalc, "
        "but the application did not confirm (Timeout was reached); "
        "the action may have run — check the UI before retrying."
    )
    assert "did not confirm" in act.render()
    assert calls == ["Seven", "Seven"]


def test_incomplete_discovery_names_the_application_and_suggests_app_name(
    action_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal names the unreadable application; scoping with app recovers."""
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
    with pytest.raises(RuntimeError) as failure:
        engine.element_act(StrictAddr(strict=Locator(query="Seven")), "Press")
    assert str(failure.value) == (
        "Incomplete discovery in application 'deep': accessibility subtree deeper than "
        f"{accessibility._ACTION_SEARCH_MAX_DEPTH} levels.\nSet app='deep' to scope the search."
    )
    assert action_calls == []

    result = engine.element_act(StrictAddr(strict=Locator(query="Seven", app="kcalc")), "Press")
    assert result.summary() == 'Invoked AT-SPI action: Press on [push button] "Seven" in kcalc'
    assert result.dispatch == "confirmed"
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
    monkeypatch.setattr(engine, "_require_session", lambda: object())
    monkeypatch.setattr(engine, "_ensure_atspi_worker", spawn)
    monkeypatch.setattr(engine, "_teardown_atspi_worker", lambda: None)
    return engine


def test_element_act_unsent_request_has_no_may_have_run_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that exited before the request was written cannot have run the action."""
    worker = types.SimpleNamespace(stdin=_BrokenStdin(), stdout=object())
    engine = _engine_with_worker(monkeypatch, lambda: worker)
    with pytest.raises(core._AtspiRequestNotSentError) as failure:
        engine.element_act(StrictAddr(strict=Locator(query="Seven")), "Press")
    message = str(failure.value)
    assert message == "AT-SPI2 query failed: AT-SPI2 worker exited before the request was sent"
    assert "may still have run" not in message, message


def test_element_act_refused_worker_spawn_has_no_may_have_run_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker the process registry refused to start never received the request."""
    refusal = "the process registry is closed: refusing to spawn a new process"

    def refuse() -> object:
        raise RuntimeError(refusal)

    engine = _engine_with_worker(monkeypatch, refuse)
    with pytest.raises(core._AtspiRequestNotSentError) as failure:
        engine.element_act(StrictAddr(strict=Locator(query="Seven")), "Press")
    assert str(failure.value) == refusal


def test_element_act_worker_death_after_send_warns_the_action_may_have_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that took the request and then exited may have invoked the action."""
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as stdout:
        worker = types.SimpleNamespace(stdin=_AcceptingStdin(), stdout=stdout, poll=lambda: 1)
        engine = _engine_with_worker(monkeypatch, lambda: worker)
        with pytest.raises(RuntimeError, match="may still have run") as failure:
            engine.element_act(StrictAddr(strict=Locator(query="Seven")), "Press")
    assert not isinstance(failure.value, core._AtspiRequestNotSentError)
    assert str(failure.value) == (
        "AT-SPI2 query failed: AT-SPI2 worker exited (status 1). "
        "The action may still have run; check the UI before retrying."
    )


# ── QA items (real sessions) ─────────────────────────────────────────────


@pytest.fixture
def zoo_in_session(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    wait_for_app: Callable[[str], ElementList],
) -> Iterator[ZooProcess]:
    """A window zoo in a fresh virtual session, queryable over AT-SPI."""
    info = start_session()
    with ZooProcess(wayland_display=info.wayland_socket, dbus_address=info.dbus_address) as zoo:
        zoo.expect(f"window: {MAIN_CAPTION}")
        wait_for_app(APP_ID)
        yield zoo


def _zoo_presses(zoo: ZooProcess, name: str) -> int:
    return zoo.lines.count(f"pressed: {name}")


def _zoo_ok_button(engine: AutomationEngine) -> list[str]:
    """The actions the zoo's OK button exposes, read by the independent AT-SPI oracle."""
    nodes = AtspiOracle(_session_dbus(engine)).find(app=APP_ID, name="OK")
    buttons = [n for n in nodes if n.actions]
    assert len(buttons) == 1, nodes
    return list(buttons[0].actions)


@pytest.mark.qa("QA-ELEM-023")
def test_strict_address_unchanged_resolver_depth64_fail_closed(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
) -> None:
    info = start_session()
    # The zoo registers first, so KCalc is a later sibling of the subtree that
    # becomes unreadable once the zoo is stopped.
    with ZooProcess(wayland_display=info.wayland_socket, dbus_address=info.dbus_address) as zoo:
        zoo.expect(f"window: {MAIN_CAPTION}")
        wait_for_app(APP_ID)
        launch("kcalc")
        wait_for_app("kcalc")
        _kcalc_seven(engine)
        time.sleep(ATSPI_APP_STARTUP_S)

        os.kill(zoo.pid, signal.SIGSTOP)
        try:
            # The lenient walk skips the unreadable zoo and still lists KCalc.
            found = engine.element_query(Locator(query="Seven"))
            assert [(e.app, e.name) for e in found.elements if e.name == "Seven"] == [
                ("kcalc", "Seven")
            ], found.render()

            # The strict walk fails closed: it names the zoo and how to scope around it.
            with pytest.raises(RuntimeError) as failure:
                engine.element_act(StrictAddr(strict=Locator(query="Seven")), "Press")
            message = str(failure.value)
            assert message.startswith(f"Incomplete discovery in application '{APP_ID}': "), message
            assert message.endswith(f"\nSet app='{APP_ID}' to scope the search."), message

            # Scoped to KCalc, the zoo is never read and the press goes through.
            result = engine.element_act(
                StrictAddr(strict=Locator(query="Seven", app="kcalc")), "Press"
            )
            assert result.dispatch == "confirmed", result.render()
            assert result.element is not None and result.element.app == "kcalc", result.render()
        finally:
            os.kill(zoo.pid, signal.SIGCONT)

        # Exactly one press reached KCalc: the refused act pressed nothing.
        oracle = AtspiOracle(info.dbus_address)
        _wait_for_binary(oracle, "111")
        _assert_binary_stays(oracle, "111")


@pytest.mark.qa("QA-ELEM-026")
def test_act_by_ref_presses_kcalc_seven(kcalc_session: AutomationEngine) -> None:
    # KCalc names its '7' key "Seven" (an independent oracle pins this).
    found = _kcalc_seven(kcalc_session)
    seven = next(e for e in found.elements if e.name == "Seven")
    role_found = kcalc_session.element_query(Locator(app="kcalc", query="Seven", role=seven.role))
    assert [e.name for e in role_found.elements] == ["Seven"], role_found.render()
    ref = role_found.elements[0].ref

    result = kcalc_session.element_act(RefAddr(ref=ref), "Press")
    assert result.tool == "element_act"
    assert result.dispatch == "confirmed" and result.dispatch_error is None, result.render()
    assert result.element is not None, result.render()
    assert result.element.name == "Seven" and result.element.ref == ref, result.render()
    assert result.summary() == (
        f'Invoked AT-SPI action: Press on [{seven.role}] "Seven" in {seven.app}'
    )
    _wait_for_binary(AtspiOracle(_session_dbus(kcalc_session)), "111")


@pytest.mark.qa("QA-ELEM-027")
def test_zero_candidates_error_lists_exposed_actions(
    engine: AutomationEngine, zoo_in_session: ZooProcess
) -> None:
    zoo = zoo_in_session
    exposed = _zoo_ok_button(engine)
    strict = StrictAddr(strict=Locator(app=APP_ID, query="OK"))
    # 'OK?' is a label without actions: it matches the query but is never a candidate.
    if "Press" not in exposed:
        with pytest.raises(RuntimeError) as failure:
            engine.element_act(strict, "Press")
        assert str(failure.value) == (
            f"No element matching query='OK', app='{APP_ID}' exposes AT-SPI action 'Press'. "
            f"Matching elements expose: {', '.join(exposed)}.\n"
            "Use one of those action names, or check the locator with element_query."
        )
        assert _zoo_presses(zoo, "OK") == 0

    result = engine.element_act(strict, exposed[0])
    assert result.dispatch == "confirmed", result.render()
    assert result.element is not None and result.element.name == "OK", result.render()
    zoo.expect("pressed: OK")
    time.sleep(0.5)
    assert _zoo_presses(zoo, "OK") == 1, zoo.lines


@pytest.mark.qa("QA-ELEM-028")
def test_action_name_ci_and_candidate_errors(
    kcalc_session: AutomationEngine,
) -> None:
    engine = kcalc_session
    dbus = _session_dbus(engine)
    oracle = AtspiOracle(dbus)
    seven = _press_seven(engine)

    # Action names compare case-insensitively.
    assert engine.element_act(seven, "press").dispatch == "confirmed"
    _wait_for_binary(oracle, "111")
    assert engine.element_act(seven, "PRESS").dispatch == "confirmed"
    _wait_for_binary(oracle, "1001101")  # 77

    seven_element = next(e for e in _kcalc_seven(engine).elements if e.name == "Seven")
    exposed = ", ".join(seven_element.actions)

    with pytest.raises(RuntimeError) as failure:
        engine.element_act(seven, "Pre")
    assert str(failure.value) == (
        "No element matching query='Seven', app='kcalc' exposes AT-SPI action 'Pre'. "
        f"Matching elements expose: {exposed}.\n"
        "Use one of those action names, or check the locator with element_query."
    )

    with pytest.raises(RuntimeError) as failure:
        engine.element_act(StrictAddr(strict=Locator(app="kcalc", query="zzz")), "Press")
    assert str(failure.value) == (
        "No element matching query='zzz', app='kcalc' exposes AT-SPI action 'Press'. "
        "Matching elements expose: (none).\n"
        "Use one of those action names, or check the locator with element_query."
    )

    with pytest.raises(RuntimeError) as failure:
        engine.element_act(
            StrictAddr(strict=Locator(app="kcalc", role=seven_element.role)), "Press"
        )
    message = str(failure.value)
    assert message.startswith("Refused ambiguous AT-SPI action: "), message
    assert f"elements matching role='{seven_element.role}', app='kcalc' expose 'Press': " in (
        message
    ), message
    assert f'[{seven_element.role}] "Seven" in kcalc (e' in message, message
    assert message.endswith("\nNarrow the locator (role, states, app) or act on one ref."), message

    # No refusal touched the display.
    _assert_binary_stays(oracle, "1001101")

    # Two 'Apply' buttons in the zoo: an unknown action lists what they expose,
    # an exposed one is refused as ambiguous with both candidates; none is pressed.
    session = engine.session_info().session
    assert session is not None
    with ZooProcess(
        wayland_display=session.wayland_socket,
        dbus_address=dbus,
    ) as zoo:
        zoo.expect(f"window: {MAIN_CAPTION}")
        engine.element_query(
            Locator(app=APP_ID, query="Apply"),
            wait=Wait(timeout_ms=15000),
        )
        apply = StrictAddr(strict=Locator(app=APP_ID, query="Apply"))
        with pytest.raises(RuntimeError) as failure:
            engine.element_act(apply, "Frobnicate")
        message = str(failure.value)
        assert message.startswith(
            f"No element matching query='Apply', app='{APP_ID}' exposes AT-SPI action "
            "'Frobnicate'. Matching elements expose: "
        ), message
        assert "(none)" not in message, message

        action = _zoo_apply_action(dbus)
        with pytest.raises(RuntimeError) as failure:
            engine.element_act(apply, action)
        message = str(failure.value)
        assert message.startswith(
            "Refused ambiguous AT-SPI action: 2 elements matching query='Apply', "
            f"app='{APP_ID}' expose '{action}': "
        ), message
        assert message.count('"Apply" in window-zoo (e') == 2, message
        time.sleep(0.5)
        assert _zoo_presses(zoo, "Apply") == 0, zoo.lines

    _assert_binary_stays(oracle, "1001101")


def _zoo_apply_action(dbus: str) -> str:
    nodes = AtspiOracle(dbus).find(app=APP_ID, name="Apply")
    assert len(nodes) == 2 and all(n.actions for n in nodes), nodes
    return nodes[0].actions[0]


def _kwrite_open_item(engine: AutomationEngine) -> str:
    """Ref of KWrite's File > Open… menu item (its QAction opens a modal file dialog)."""
    found = engine.element_query(Locator(app="kwrite", query="Open"), max_depth=30)
    items = [e for e in found.elements if e.role == "menu item" and e.name.rstrip(".…") == "Open"]
    assert len(items) == 1, found.render()
    assert "Press" in items[0].actions, found.render()
    return items[0].ref


@pytest.mark.qa("QA-ELEM-029")
def test_dispatch_confirmed_vs_sent_unconfirmed(
    kcalc_session: AutomationEngine,
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
) -> None:
    engine = kcalc_session
    dbus = _session_dbus(engine)

    # A button press is answered at once: the dispatch is confirmed.
    confirmed = engine.element_act(_press_seven(engine), "Press")
    assert confirmed.dispatch == "confirmed" and confirmed.dispatch_error is None
    _wait_for_binary(AtspiOracle(dbus), "111")

    # Qt triggers a menu item's QAction inside the DoAction call, and KWrite's
    # File > Open… runs a modal file dialog there: the application cannot answer
    # the call until the dialog closes, so the dispatch stays unconfirmed.
    launch("kwrite")
    wait_for_app("kwrite")
    ref = _kwrite_open_item(engine)
    kwin = KWinOracle(dbus)
    before = {w.id for w in kwin.windows()}

    modal = engine.element_act(RefAddr(ref=ref), "Press")
    assert modal.dispatch == "sent_unconfirmed", modal.render()
    assert modal.dispatch_error, modal.render()
    element = modal.element
    assert element is not None and element.ref == ref, modal.render()
    assert modal.render().splitlines()[0] == (
        f"Sent AT-SPI action 'Press' to [{element.role}] \"{element.name}\" in "
        f"{element.app}, but the application did not confirm ({modal.dispatch_error}); "
        "the action may have run — check the UI before retrying."
    )

    # Exactly one modal: the unconfirmed action ran once and was not retried.
    kwin.wait_for_windows(lambda ws: any(w.id not in before for w in ws))
    time.sleep(2.0)
    added = [w for w in kwin.windows() if w.id not in before]
    assert len(added) == 1, added
    assert added[0].transient_for is not None or added[0].modal, added


@pytest.mark.qa("QA-ELEM-030")
def test_app_reported_failure_is_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An application that answers DoAction with false is a ToolError, not a result.

    No application in the image can answer false: at-spi2-atk replies true
    before it runs a GTK action (even on an insensitive button) and Qt replies
    true unconditionally, so the application's answer is injected here.
    """
    calls: list[str] = []

    def _do_action(iface: _FakeAction, index: int) -> bool:
        calls.append(iface.owner.name)
        return False

    _install_fake_atspi(monkeypatch, _do_action)
    _DESKTOP.clear()
    _DESKTOP.append(_FakeNode("desktop", children=[_FakeNode("kcalc", [_action_button()])]))
    engine = _engine_over_fake_worker(monkeypatch)
    with pytest.raises(RuntimeError) as failure:
        engine.element_act(StrictAddr(strict=Locator(query="Seven", app="kcalc")), "Press")
    assert str(failure.value) == (
        "AT-SPI action 'Press' matched one element but the application reported failure."
    )
    assert "may still have run" not in str(failure.value)
    # The application saw exactly one attempt; a reported failure is not retried.
    assert calls == ["Seven"]
