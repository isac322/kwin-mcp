"""Locator matching of the AT-SPI worker's lenient walk, over a fake tree.

``app``, ``query`` and ``role`` compare case-insensitively (``app`` and
``query`` as substrings, ``role`` exactly); ``states`` are exact,
case-sensitive AT-SPI state nicks that must all hold; all fields are AND-ed
and ``{}`` selects every element.
"""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from kwin_mcp import accessibility
from kwin_mcp.accessibility import ElementInfo, _Criteria, _RefTable
from kwin_mcp.models import Locator


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


@dataclass(eq=False)
class _FakeNode:
    """Minimal ``Atspi.Accessible`` double; ``unreadable`` makes every read raise."""

    name: str
    role: str = "panel"
    children: list[_FakeNode] = field(default_factory=list)
    states: list[str] = field(default_factory=lambda: ["enabled"])
    unreadable: bool = False

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

    def get_action_iface(self) -> None:
        return None

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
