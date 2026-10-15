"""Locator matching of the AT-SPI worker's lenient walk, over a fake tree.

``app`` compares as a case-insensitive substring, ``role`` case-insensitively
and exactly; ``query`` uses substring, exact-name or spoken-name matching.
``states`` are exact AT-SPI state nicks that must all hold; all fields are
AND-ed and ``{}`` selects every element.
"""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from kwin_mcp import accessibility
from kwin_mcp.accessibility import ElementInfo, _Criteria, _RefTable
from kwin_mcp.models import Locator, RefAddr, StrictAddr

if TYPE_CHECKING:
    from ._fakes import FakeEngine


def _info(
    *,
    name: str = "Seven",
    role: str = "push button",
    description: str = "",
    states: tuple[str, ...] = ("enabled", "focusable", "focused"),
) -> ElementInfo:
    return ElementInfo(
        role=role,
        name=name,
        description=description,
        states=list(states),
        x=0,
        y=0,
        width=0,
        height=0,
        actions=[],
        text="",
        value=None,
        value_max=None,
        children_count=0,
        depth=2,
        has_extents=False,
        mapped=False,
        unavailable="",
    )


@pytest.mark.qa("QA-ELEM-041")
@pytest.mark.parametrize(
    ("locator", "info", "expected"),
    [
        pytest.param(Locator(), _info(), True, id="empty-locator-matches-all"),
        pytest.param(Locator(query="", role="", states=[]), _info(), True, id="empty-values"),
        pytest.param(Locator(query="seven"), _info(), True, id="query-name-ci"),
        pytest.param(Locator(query="EVE"), _info(), True, id="query-name-substring"),
        pytest.param(Locator(query="BUTTON"), _info(), True, id="query-role-ci"),
        pytest.param(
            Locator(query="digit"), _info(description="Digit 7"), True, id="query-description"
        ),
        pytest.param(Locator(query="eight"), _info(), False, id="query-miss"),
        pytest.param(Locator(role="Push Button"), _info(), True, id="role-exact-ci"),
        pytest.param(Locator(role="button"), _info(), False, id="role-not-substring"),
        pytest.param(Locator(states=["focused"]), _info(), True, id="states-subset"),
        pytest.param(
            Locator(states=["focused", "enabled"]), _info(), True, id="states-all-order-free"
        ),
        pytest.param(Locator(states=["Focused"]), _info(), False, id="states-case-sensitive"),
        pytest.param(Locator(states=["focus"]), _info(), False, id="states-not-substring"),
        pytest.param(
            Locator(states=["focused", "checked"]), _info(), False, id="states-all-must-hold"
        ),
        pytest.param(
            Locator(query="seven", role="push button", states=["focused"]),
            _info(),
            True,
            id="and-all-hold",
        ),
        pytest.param(
            Locator(query="seven", role="label"), _info(), False, id="and-one-field-fails"
        ),
    ],
)
def test_locator_matching_rules(locator: Locator, info: ElementInfo, expected: bool) -> None:
    assert _Criteria.from_locator(locator).matches(info) is expected


@pytest.mark.qa("QA-ELEM-041")
@pytest.mark.parametrize(
    ("app", "app_name", "expected"),
    [
        pytest.param(None, "kcalc", True, id="unset"),
        pytest.param("", "kcalc", True, id="empty"),
        pytest.param("KCalc", "kcalc", True, id="ci"),
        pytest.param("calc", "kcalc", True, id="substring"),
        pytest.param("kwrite", "kcalc", False, id="miss"),
    ],
)
def test_locator_app_is_a_case_insensitive_substring(
    app: str | None, app_name: str, expected: bool
) -> None:
    assert _Criteria.from_locator(Locator(app=app)).matches_app(app_name) is expected


@dataclass(frozen=True)
class _FakeState:
    value_nick: str


class _FakeStateSet:
    def __init__(self, nicks: list[str]) -> None:
        self._nicks = nicks

    def get_states(self) -> list[_FakeState]:
        return [_FakeState(nick) for nick in self._nicks]


class _FakeActions:
    def __init__(self, names: list[str]) -> None:
        self.names = names

    def get_n_actions(self) -> int:
        return len(self.names)

    def get_action_name(self, index: int) -> str:
        return self.names[index]


@dataclass(eq=False)
class _FakeNode:
    """Minimal ``Atspi.Accessible`` double; ``unreadable`` makes every read raise."""

    name: str
    role: str = "panel"
    children: list[_FakeNode] = field(default_factory=list)
    states: list[str] = field(default_factory=lambda: ["enabled"])
    unreadable: bool = False
    actions: list[str] = field(default_factory=list)

    def _check(self) -> None:
        if self.unreadable:
            msg = "injected read failure"
            raise RuntimeError(msg)

    def get_name(self) -> str:
        self._check()
        return self.name

    def get_role_name(self) -> str:
        self._check()
        return self.role

    def get_description(self) -> str:
        return ""

    def get_state_set(self) -> _FakeStateSet:
        return _FakeStateSet(self.states)

    def get_component_iface(self) -> None:
        return None

    def get_action_iface(self) -> _FakeActions | None:
        return _FakeActions(self.actions) if self.actions else None

    def get_text_iface(self) -> None:
        return None

    def get_value_iface(self) -> None:
        return None

    def get_child_count(self) -> int:
        self._check()
        return len(self.children)

    def get_child_at_index(self, index: int) -> _FakeNode:
        return self.children[index]

    def get_process_id(self) -> int:
        return 0


def _desktop() -> list[_FakeNode]:
    """kcalc (frame > panel > two buttons, one unreadable panel), kwrite, a childless app."""
    kcalc = _FakeNode(
        "kcalc",
        "application",
        [
            _FakeNode(
                "KCalc",
                "frame",
                [
                    _FakeNode(
                        "",
                        "panel",
                        [
                            _FakeNode("Seven", "push button", states=["enabled", "focused"]),
                            _FakeNode("Eight", "push button"),
                        ],
                    ),
                    _FakeNode("Broken", unreadable=True, children=[_FakeNode("Hidden")]),
                    _FakeNode("Result", "label"),
                ],
            )
        ],
    )
    kwrite = _FakeNode(
        "kwrite", "application", [_FakeNode("Untitled — KWrite", "frame", [_FakeNode("Seven")])]
    )
    return [kcalc, kwrite, _FakeNode("xdg-desktop-portal-kde", "application")]


@pytest.fixture
def desktop(monkeypatch: pytest.MonkeyPatch) -> list[_FakeNode]:
    apps = _desktop()
    monkeypatch.setattr(
        accessibility,
        "Atspi",
        types.SimpleNamespace(
            get_desktop=lambda _index: _FakeNode("desktop", "desktop frame", apps)
        ),
    )
    monkeypatch.setattr(accessibility, "_kwin_windows", lambda: ([], "no KWin in unit tests"))
    monkeypatch.setattr(accessibility, "_REFS", _RefTable(generation=1))
    return apps


def _names(locator: dict[str, Any], max_depth: int = 15) -> list[tuple[int, str, str]]:
    response = accessibility._handle_request(
        {"op": "query", "locator": locator, "max_depth": max_depth}
    )
    assert response["ok"] is True
    return [(e["depth"], e["app"], e["name"]) for e in response["result"]]


@pytest.mark.qa("QA-ELEM-041")
def test_walk_is_one_filtered_pre_order_walk(desktop: list[_FakeNode]) -> None:
    del desktop
    # {} = every readable element in pre-order; the unreadable panel and its
    # subtree are skipped; the childless application is listed.
    assert _names({}) == [
        (0, "kcalc", "kcalc"),
        (1, "kcalc", "KCalc"),
        (2, "kcalc", ""),
        (3, "kcalc", "Seven"),
        (3, "kcalc", "Eight"),
        (2, "kcalc", "Result"),
        (0, "kwrite", "kwrite"),
        (1, "kwrite", "Untitled — KWrite"),
        (2, "kwrite", "Seven"),
        (0, "xdg-desktop-portal-kde", "xdg-desktop-portal-kde"),
    ]
    # Non-matching elements are hidden but their children are still searched.
    assert _names({"role": "PUSH BUTTON"}) == [(3, "kcalc", "Seven"), (3, "kcalc", "Eight")]
    assert _names({"query": "seven", "app": "KCALC"}) == [(3, "kcalc", "Seven")]
    assert _names({"query": "seven", "states": ["focused"]}) == [(3, "kcalc", "Seven")]
    assert _names({"query": "seven", "states": ["FOCUSED"]}) == []


@pytest.mark.qa("QA-ELEM-041")
@pytest.mark.parametrize(
    ("max_depth", "deepest"),
    [
        pytest.param(-3, 0, id="negative-apps-only"),
        pytest.param(0, 0, id="zero-apps-only"),
        pytest.param(1, 1, id="top-levels"),
        pytest.param(2, 2, id="two"),
    ],
)
def test_max_depth_bounds_the_walk(desktop: list[_FakeNode], max_depth: int, deepest: int) -> None:
    del desktop
    depths = [depth for depth, _app, _name in _names({}, max_depth=max_depth)]
    assert max(depths) == deepest
    assert depths.count(0) == 3


@pytest.mark.qa("QA-ELEM-041")
def test_wait_until_present_absent_and_timeout_as_data(desktop: list[_FakeNode]) -> None:
    def wait(**kwargs: Any) -> dict[str, Any]:
        response = accessibility._handle_request({"op": "wait", "max_depth": 15, **kwargs})
        assert response["ok"] is True
        return response["result"]

    present = wait(locator={"query": "Eight"}, until="present", timeout_ms=0)
    assert present["satisfied"] is True
    assert [e["name"] for e in present["elements"]] == ["Eight"]

    # timeout_ms <= 0 polls once; a negative poll interval is clamped to 0.
    missing = wait(locator={"query": "Nine"}, until="present", timeout_ms=-5, poll_interval_ms=-1)
    assert missing["satisfied"] is False
    assert missing["elements"] == []

    absent = wait(locator={"query": "Nine"}, until="absent", timeout_ms=0)
    assert absent["satisfied"] is True

    # Timed out waiting for absence: the elements are the last poll's matches.
    still_there = wait(locator={"query": "Eight"}, until="absent", timeout_ms=30)
    assert still_there["satisfied"] is False
    assert [e["name"] for e in still_there["elements"]] == ["Eight"]
    assert still_there["elapsed_ms"] >= 30

    # The element disappears: absence is satisfied.
    del desktop[0].children[0].children[0].children[1]
    assert wait(locator={"query": "Eight"}, until="absent", timeout_ms=0)["satisfied"] is True


@pytest.mark.parametrize(
    ("locator", "name", "expected"),
    [
        (Locator(query="save as", match="exact"), " SAVE\tAS ", True),
        (Locator(query="STRASSE", match="exact"), "Straße", True),
        (Locator(query="save", match="exact"), "Save As", False),
        (Locator(query="save as", match="exact"), "Save As...", False),
        (Locator(query="save as", match="spoken"), "SaveAs...", True),
        (Locator(query="save as", match="spoken"), "Save As?!", True),
        (Locator(query="résumé", match="spoken"), "Re\u0301sume\u0301", True),
        (Locator(query="saveas", match="spoken"), "Save As", False),
        (Locator(query="save as", match="spoken"), "Save-As", False),
        (Locator(query="save as", match="spoken"), "Saves A", False),
        (Locator(query="...", match="exact"), "...", True),
    ],
)
def test_accessible_name_matching_modes(locator: Locator, name: str, expected: bool) -> None:
    assert _Criteria.from_locator(locator).matches(_info(name=name)) is expected


@pytest.mark.parametrize("match", ["exact", "spoken"])
def test_name_modes_do_not_match_role_or_description(match: str) -> None:
    locator = Locator.model_validate({"query": "push button", "match": match})
    assert not _Criteria.from_locator(locator).matches(
        _info(name="Other", description="push button")
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"match": "exact", "query": None},
        {"match": "spoken", "query": None},
        {"match": "exact", "query": ""},
        {"match": "spoken", "query": ""},
        {"match": "exact", "query": " \t\n"},
        {"match": "spoken", "query": " \t\n"},
        {"match": "spoken", "query": "..."},
        {"match": "spoken", "query": "?! \u2026"},
        {"match": "unknown", "query": "Seven"},
    ],
)
def test_name_matching_rejects_invalid_locators(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Locator.model_validate(fields)


@pytest.mark.parametrize("match", ["exact", "spoken"])
def test_name_modes_filter_the_fake_tree(desktop: list[_FakeNode], match: str) -> None:
    button = desktop[0].children[0].children[0].children[0]
    button.name = " Save\tAs " if match == "exact" else "SaveAs..."
    assert _names({"query": "save as", "app": "kcalc", "match": match}) == [
        (3, "kcalc", button.name)
    ]


@pytest.mark.parametrize(
    ("available", "wanted", "chosen"),
    [
        (["Press", "Press"], ["Press"], "Press"),
        (["press", "PRESS"], ["Press"], "press"),
        (["click", "Press", "click"], ["Press", "click"], "Press"),
        (["click", "click"], ["Press", "click"], "click"),
    ],
)
def test_strict_candidates_deduplicate_per_element(
    desktop: list[_FakeNode], available: list[str], wanted: list[str], chosen: str
) -> None:
    button = _FakeNode("Save As", "push button", actions=available)
    desktop[0].children[:] = [button]
    candidates, exposed = accessibility._strict_candidates(
        _Criteria.from_locator(Locator(query="Save As", match="exact", app="kcalc")), wanted
    )
    assert len(candidates) == 1
    assert candidates[0].entry.accessible is button
    assert candidates[0].action == chosen
    assert exposed == list(dict.fromkeys(available))

    # Deduplicate action indexes, never distinct elements with the same label.
    desktop[0].children.append(_FakeNode("Save As", "push button", actions=available))
    candidates, _ = accessibility._strict_candidates(
        _Criteria.from_locator(Locator(query="Save As", match="exact", app="kcalc")), wanted
    )
    assert len(candidates) == 2


@pytest.mark.parametrize("by_ref", [False, True], ids=["strict", "ref"])
@pytest.mark.parametrize(
    ("available", "chosen"), [(["click", "click"], "click"), (["click", "Press"], "Press")]
)
@pytest.mark.parametrize("dispatch", ["confirmed", "unconfirmed", "failed"])
def test_element_act_fallback_reports_the_invoked_action(
    desktop: list[_FakeNode],
    fake_engine: FakeEngine,
    monkeypatch: pytest.MonkeyPatch,
    by_ref: bool,
    available: list[str],
    chosen: str,
    dispatch: str,
) -> None:
    button = _FakeNode("Save As...", "push button", actions=available)
    desktop[0].children[:] = [button]
    dispatched: list[str] = []

    def do_action(iface: _FakeActions, index: int) -> bool:
        dispatched.append(iface.names[index])
        if dispatch == "unconfirmed":
            raise RuntimeError("Timeout was reached")
        return dispatch != "failed"

    monkeypatch.setattr(
        accessibility.Atspi, "Action", types.SimpleNamespace(do_action=do_action), raising=False
    )
    monkeypatch.setattr(fake_engine.engine, "_atspi_generation", 1)
    fake_engine.atspi.respond(
        "action", lambda arguments: accessibility._handle_request({"op": "action", **arguments})
    )
    locator = Locator(query="save as", match="spoken", app="kcalc")
    element: RefAddr | StrictAddr
    if by_ref:
        response = accessibility._handle_request({"op": "query", "locator": locator.model_dump()})
        element = RefAddr(ref=response["result"][0]["ref"])
    else:
        element = StrictAddr(strict=locator)
    if dispatch == "failed":
        with pytest.raises(RuntimeError, match=f"AT-SPI action '{chosen}' matched one element"):
            fake_engine.engine.element_act(element=element, action=["Press", "click"])
    else:
        result = fake_engine.engine.element_act(element=element, action=["Press", "click"])
        assert result.action == chosen
        assert result.dispatch == ("sent_unconfirmed" if dispatch == "unconfirmed" else "confirmed")
        assert f"action: {chosen}" in result.render() or f"action '{chosen}'" in result.render()
    assert dispatched == [chosen]
    assert fake_engine.atspi.calls[-1].kwargs["action_name"] == ["Press", "click"]
