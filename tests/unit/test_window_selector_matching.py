"""WindowSelector matching over a fake window pool (QA-WIN-049), without KWin.

``geometry.select_windows`` is the read filter of window_list; ``select_one``
is the exactly-one resolution every window mutator shares, with the selector
error texts of contract §3.3.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from kwin_mcp import geometry
from kwin_mcp.models import Rect, Window, WindowSelector

if TYPE_CHECKING:
    from kwin_mcp.models import WindowKind


def _window(
    window_id: str,
    app_id: str,
    caption: str,
    kind: WindowKind = "normal",
    *,
    active: bool = False,
) -> Window:
    return Window(
        id=window_id,
        app_id=app_id,
        caption=caption,
        kind=kind,
        active=active,
        frame=Rect(x=0, y=0, width=400, height=300),
        client=Rect(x=0, y=30, width=400, height=270),
        moveable=kind != "shell",
        resizeable=kind != "shell",
        closeable=kind != "shell",
        desktops=["d1"],
        on_all_desktops=False,
    )


# One pool with every kind: a zoo main window with an open popup menu and a
# utility palette, a kcalc window with its About dialog, two same-caption
# windows, a panel and a splash screen.
POOL = [
    _window("{zoo-main}", "window-zoo", "Zoo", active=True),
    _window("{zoo-menu}", "window-zoo", "", "popup"),
    _window("{zoo-tools}", "window-zoo", "Tools", "utility"),
    _window("{kcalc}", "org.kde.kcalc", "KCalc"),
    _window("{kcalc-about}", "org.kde.kcalc", "About KCalc", "dialog"),
    _window("{twin-a}", "twin", "Same Caption"),
    _window("{twin-b}", "twin", "Same Caption"),
    _window("{panel}", "plasmashell", "Panel", "shell"),
    _window("{splash}", "splash", "Loading", "other"),
]


def _ids(windows: list[Window]) -> list[str]:
    return [window.id for window in windows]


@pytest.mark.qa("QA-WIN-049")
@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        pytest.param(None, _ids(POOL), id="no-selector-every-kind"),
        pytest.param(WindowSelector(), _ids(POOL), id="empty-selector-every-kind"),
        pytest.param(WindowSelector(id="{kcalc-about}"), ["{kcalc-about}"], id="id-any-kind"),
        pytest.param(WindowSelector(id="{KCALC}"), [], id="id-is-exact"),
        pytest.param(WindowSelector(id="kcalc"), [], id="id-is-not-a-substring"),
        pytest.param(
            WindowSelector(app_id="KCALC"), ["{kcalc}", "{kcalc-about}"], id="app-id-ci-substring"
        ),
        pytest.param(
            WindowSelector(caption="same"), ["{twin-a}", "{twin-b}"], id="caption-ci-substring"
        ),
        pytest.param(WindowSelector(active=True), ["{zoo-main}"], id="active-true-any-kind"),
        pytest.param(
            WindowSelector(active=False),
            [w.id for w in POOL if not w.active],
            id="active-false",
        ),
        pytest.param(
            WindowSelector(kind=["normal"]),
            ["{zoo-main}", "{kcalc}", "{twin-a}", "{twin-b}"],
            id="kind-normal",
        ),
        pytest.param(
            WindowSelector(kind=["dialog", "utility"]),
            ["{zoo-tools}", "{kcalc-about}"],
            id="kind-membership",
        ),
        pytest.param(WindowSelector(kind=["shell"]), ["{panel}"], id="kind-shell"),
        pytest.param(WindowSelector(kind=["other"]), ["{splash}"], id="kind-other"),
        pytest.param(
            WindowSelector(app_id="window-zoo", kind=["normal"]),
            ["{zoo-main}"],
            id="and-app-id-kind",
        ),
        pytest.param(
            WindowSelector(app_id="kcalc", caption="about"),
            ["{kcalc-about}"],
            id="and-app-id-caption",
        ),
        pytest.param(WindowSelector(id="{kcalc}", kind=["dialog"]), [], id="and-id-kind-excludes"),
        pytest.param(
            WindowSelector(app_id="window-zoo", active=False),
            ["{zoo-menu}", "{zoo-tools}"],
            id="and-app-id-inactive",
        ),
        pytest.param(WindowSelector(app_id="zz-none"), [], id="no-match"),
    ],
)
def test_selector_matching_rules(selector: WindowSelector | None, expected: list[str]) -> None:
    assert _ids(geometry.select_windows(POOL, selector)) == expected


@pytest.mark.qa("QA-WIN-049")
@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        pytest.param(WindowSelector(id="{kcalc-about}"), "{kcalc-about}", id="id"),
        pytest.param(WindowSelector(app_id="window-zoo", kind=["normal"]), "{zoo-main}", id="kind"),
        pytest.param(WindowSelector(active=True), "{zoo-main}", id="active"),
        pytest.param(WindowSelector(kind=["utility"]), "{zoo-tools}", id="utility"),
    ],
)
def test_select_one_resolves_exactly_one(selector: WindowSelector, expected: str) -> None:
    assert geometry.select_one(POOL, selector).id == expected


@pytest.mark.qa("QA-WIN-049")
def test_ambiguity_lists_ids_kinds_and_the_hint() -> None:
    with pytest.raises(ValueError) as excinfo:
        geometry.select_one(POOL, WindowSelector(app_id="window-zoo"))

    assert str(excinfo.value) == (
        "3 windows match {app_id='window-zoo'}: "
        '{zoo-main} window-zoo "Zoo" (normal); '
        '{zoo-menu} window-zoo "" (popup); '
        '{zoo-tools} window-zoo "Tools" (utility)'
        '\nNarrow the selector: add kind:["normal"] or use id.'
    )


@pytest.mark.qa("QA-WIN-049")
def test_same_caption_normal_windows_stay_ambiguous() -> None:
    selector = WindowSelector(app_id="twin", caption="Same Caption", kind=["normal"])

    with pytest.raises(ValueError) as excinfo:
        geometry.select_one(POOL, selector)

    assert str(excinfo.value) == (
        "2 windows match {app_id='twin', caption='Same Caption', kind=['normal']}: "
        '{twin-a} twin "Same Caption" (normal); {twin-b} twin "Same Caption" (normal)'
        '\nNarrow the selector: add kind:["normal"] or use id.'
    )


@pytest.mark.qa("QA-WIN-049")
@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        pytest.param(
            WindowSelector(id="{nope}"),
            "No window with id '{nope}'.\nList windows with window_list.",
            id="id-only",
        ),
        pytest.param(
            WindowSelector(id="{kcalc}", kind=["dialog"]),
            "No window matches {id='{kcalc}', kind=['dialog']}.\nList windows with window_list.",
            id="id-and-kind",
        ),
        pytest.param(
            WindowSelector(app_id="zz", caption="x", active=False),
            "No window matches {app_id='zz', caption='x', active=False}."
            "\nList windows with window_list.",
            id="field-order",
        ),
    ],
)
def test_zero_matches_texts(selector: WindowSelector, expected: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        geometry.select_one(POOL, selector)

    assert str(excinfo.value) == expected


@pytest.mark.qa("QA-WIN-049")
def test_empty_selector_is_refused_by_mutators() -> None:
    with pytest.raises(ValueError) as excinfo:
        geometry.select_one(POOL, WindowSelector())

    assert str(excinfo.value) == (
        "window needs at least one field (id, app_id, caption, active or kind)."
        "\nUse window_list to find a window id."
    )


@pytest.mark.qa("QA-WIN-049")
def test_empty_kind_list_is_refused() -> None:
    # MCP rejects kind:[] in the schema (minItems); an unvalidated selector (the
    # CLI's or the engine's own) still gets the teaching text.
    selector = WindowSelector.model_construct(kind=[])
    expected = (
        "kind must name at least one window kind (normal, dialog, utility, popup, shell, other)."
        "\nOmit kind to match every kind."
    )

    for call in (geometry.select_windows, geometry.select_one):
        with pytest.raises(ValueError) as excinfo:
            call(POOL, selector)
        assert str(excinfo.value) == expected


@pytest.mark.qa("QA-WIN-049")
def test_describe_selector_lists_set_fields_in_order() -> None:
    selector = WindowSelector(kind=["normal"], active=True, caption="c", app_id="a", id="i")

    assert geometry.describe_selector(selector) == (
        "{id='i', app_id='a', caption='c', active=True, kind=['normal']}"
    )
    assert geometry.describe_selector(WindowSelector()) == "{}"
