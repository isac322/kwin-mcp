"""Matching AT-SPI top-levels to KWin windows, without a KWin session.

The windows and top-levels below are the ones observed on Fedora 44 (KWin
6.7.5): Chromium 154 with its client-side decoration shadow and its hidden
omnibox popups, and a Flatpak app whose AT-SPI pid is its sandbox's
xdg-dbus-proxy. The AT-SPI side is faked with the few methods the matcher
calls; ``/proc`` is a temporary tree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from kwin_mcp import accessibility, geometry
from kwin_mcp.accessibility import _apply_stability, _resolve_app

if TYPE_CHECKING:
    from pathlib import Path

    from kwin_mcp.geometry import KWinWindow

BETTERBIRD_SCOPE = (
    "/user.slice/user-1000.slice/user@1000.service/app.slice/"
    "app-flatpak-eu.betterbird.Betterbird-4142079893.scope"
)
OTHER_SCOPE = (
    "/user.slice/user-1000.slice/user@1000.service/app.slice/"
    "app-flatpak-org.mozilla.Thunderbird-1234567890.scope"
)


@dataclass
class _Extents:
    x: int
    y: int
    width: int
    height: int


@dataclass
class _Component:
    extents: _Extents

    def get_extents(self, _coord_type: object) -> _Extents:
        return self.extents


@dataclass
class _TopLevel:
    name: str
    size: tuple[int, int]

    def get_name(self) -> str:
        return self.name

    def get_component_iface(self) -> _Component:
        return _Component(_Extents(0, 0, *self.size))


@dataclass
class _App:
    pid: int
    children: list[_TopLevel] = field(default_factory=list)

    def get_process_id(self) -> int:
        return self.pid

    def get_child_count(self) -> int:
        return len(self.children)

    def get_child_at_index(self, index: int) -> _TopLevel:
        return self.children[index]


def _window(
    pid: int,
    window_id: str,
    caption: str,
    client: list[int],
    buffer: list[int] | None = None,
) -> KWinWindow:
    return {
        "pid": pid,
        "id": window_id,
        "app": "app",
        "caption": caption,
        "normal": True,
        "popup": False,
        "managed": True,
        "deleted": False,
        "desktop": False,
        "dock": False,
        "notification": False,
        "active": False,
        "frame": client,
        "client": client,
        "buffer": buffer if buffer is not None else client,
    }


# Chromium 154, windowed: KWin's client rect excludes the shadow the client
# draws inside its surface; AT-SPI reports the whole surface.
def _chromium_window(window_id: str, caption: str) -> KWinWindow:
    return _window(3675, window_id, caption, [131, 8, 1018, 738], [115, -2, 1050, 780])


def _offsets(app: _App, kwin: list[KWinWindow]) -> list[tuple[tuple[int, int] | None, str]]:
    _, mappings, _ = _resolve_app(app, kwin, "")  # type: ignore[arg-type]
    return [(mapping.offset, mapping.reason) for mapping in mappings]


def test_chromium_frame_maps_through_its_buffer_and_phantom_popups_stay_alone() -> None:
    app = _App(
        3675,
        [
            _TopLevel("Hello Probe - Chromium", (1050, 780)),
            _TopLevel("", (856, 88)),
            _TopLevel("", (856, 89)),
        ],
    )

    assert _offsets(app, [_chromium_window("w1", "Hello Probe - Chromium")]) == [
        ((115, -2), ""),
        (None, "no-kwin-window"),
        (None, "no-kwin-window"),
    ]


def test_two_chromium_windows_of_one_size_are_told_apart_by_caption() -> None:
    app = _App(
        3675,
        [
            _TopLevel("example.org - Chromium", (1050, 780)),
            _TopLevel("Hello Probe - Chromium", (1050, 780)),
        ],
    )
    kwin = [
        _chromium_window("w1", "example.org - Chromium"),
        _window(3675, "w2", "Hello Probe - Chromium", [400, 300, 1018, 738], [384, 290, 1050, 780]),
    ]

    assert _offsets(app, kwin) == [((115, -2), ""), ((384, 290), "")]


def test_same_caption_and_size_still_unmaps_the_whole_app() -> None:
    app = _App(
        3675,
        [_TopLevel("New Tab - Chromium", (1050, 780)), _TopLevel("", (856, 88))],
    )
    kwin = [
        _chromium_window("w1", "New Tab - Chromium"),
        _chromium_window("w2", "New Tab - Chromium"),
    ]

    assert _offsets(app, kwin) == [(None, "ambiguous"), (None, "no-kwin-window")]


def test_a_caption_that_differs_is_not_matched_through_the_buffer() -> None:
    """Chromium's error page names its frame differently from its KWin caption."""
    app = _App(3675, [_TopLevel("example.invalid - Network error - Chromium", (1050, 780))])

    assert _offsets(app, [_chromium_window("w1", "example.invalid - Chromium")]) == [
        (None, "no-kwin-window")
    ]


def test_client_geometry_wins_when_it_matches() -> None:
    """A server-side decorated window keeps the client origin, as before."""
    app = _App(42, [_TopLevel("Calculator", (400, 500))])
    kwin = [_window(42, "w1", "Calculator", [100, 200, 400, 500], [90, 190, 420, 520])]

    assert _offsets(app, kwin) == [((100, 200), "")]


def test_windows_matching_through_client_and_buffer_are_ambiguous() -> None:
    """A client-size match must not hide another window that matches through its buffer."""
    app = _App(
        3675,
        [
            _TopLevel("example.org - Chromium", (1050, 780)),
            _TopLevel("example.org - Network error - Chromium", (1082, 822)),
        ],
    )
    kwin = [
        _chromium_window("loaded", "example.org - Chromium"),
        # Its client rect has the loaded frame's size; the error page's frame
        # name fails the caption check, so only the geometry tells them apart.
        _window(
            3675, "error", "example.org - Chromium", [800, 200, 1050, 780], [784, 190, 1082, 822]
        ),
    ]

    assert _offsets(app, kwin) == [(None, "ambiguous"), (None, "no-kwin-window")]


def test_an_ambiguous_top_level_unmaps_a_mapped_sibling() -> None:
    app = _App(
        42,
        [_TopLevel("Calculator", (400, 500)), _TopLevel("Untitled", (300, 200))],
    )
    kwin = [
        _window(42, "w1", "Calculator", [100, 200, 400, 500]),
        _window(42, "w2", "Untitled", [600, 200, 300, 200]),
        _window(42, "w3", "Untitled", [900, 200, 300, 200]),
    ]

    assert _offsets(app, kwin) == [(None, "ambiguous-window-match"), (None, "ambiguous")]


def _stable_offsets(
    app: _App, before: list[KWinWindow], after: list[KWinWindow], monkeypatch: pytest.MonkeyPatch
) -> list[tuple[tuple[int, int] | None, str]]:
    pids, mappings, _ = _resolve_app(app, before, "")  # type: ignore[arg-type]
    monkeypatch.setattr(accessibility, "_kwin_windows", lambda: (after, ""))
    _apply_stability({pids: mappings}, before, "")
    return [(mapping.offset, mapping.reason) for mapping in mappings]


def test_a_buffer_change_during_the_walk_unmaps_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _App(3675, [_TopLevel("Hello Probe - Chromium", (1050, 780))])
    before = [_chromium_window("w1", "Hello Probe - Chromium")]
    after = [
        _window(3675, "w1", "Hello Probe - Chromium", [131, 8, 1018, 738], [110, -7, 1060, 790])
    ]

    assert _stable_offsets(app, before, before, monkeypatch) == [((115, -2), "")]
    assert _stable_offsets(app, before, after, monkeypatch) == [(None, "windows-changed")]


def _fake_proc(root: Path, pid: int, comm: str, cgroup: str) -> None:
    directory = root / str(pid)
    directory.mkdir(parents=True)
    (directory / "comm").write_text(f"{comm}\n", encoding="utf-8")
    (directory / "cgroup").write_text(f"0::{cgroup}\n", encoding="utf-8")


@pytest.fixture
def proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(accessibility, "_PROC", tmp_path)
    return tmp_path


def test_flatpak_proxy_pid_maps_to_the_windows_of_its_own_instance(proc: Path) -> None:
    _fake_proc(proc, 287882, "xdg-dbus-proxy", BETTERBIRD_SCOPE)
    _fake_proc(proc, 287894, "betterbird", BETTERBIRD_SCOPE)
    _fake_proc(proc, 300, "thunderbird", OTHER_SCOPE)
    app = _App(287882, [_TopLevel("Inbox - Betterbird", (1200, 800))])
    kwin = [
        _window(287894, "w1", "Inbox - Betterbird", [10, 20, 1200, 800]),
        # Same caption and size in another Flatpak instance: never a candidate.
        _window(300, "w2", "Inbox - Betterbird", [500, 20, 1200, 800]),
    ]

    pids, mappings, _ = _resolve_app(app, kwin, "")  # type: ignore[arg-type]

    assert pids == frozenset({287882, 287894})
    assert [(m.offset, m.reason) for m in mappings] == [((10, 20), "")]


@pytest.mark.parametrize(
    ("comm", "scope"),
    [
        # Same cgroup but not the sandbox's proxy.
        ("helper", BETTERBIRD_SCOPE),
        # A proxy outside a Flatpak instance scope (e.g. a terminal's scope).
        ("xdg-dbus-proxy", "/user.slice/user-1000.slice/session-2.scope"),
    ],
)
def test_other_pids_without_windows_get_no_alias(proc: Path, comm: str, scope: str) -> None:
    _fake_proc(proc, 100, comm, scope)
    _fake_proc(proc, 200, "app", scope)
    app = _App(100, [_TopLevel("Inbox", (1200, 800))])
    kwin = [_window(200, "w1", "Inbox", [10, 20, 1200, 800])]

    pids, mappings, _ = _resolve_app(app, kwin, "")  # type: ignore[arg-type]

    assert pids == frozenset({100})
    assert [(m.offset, m.reason) for m in mappings] == [(None, "no-kwin-window")]


def test_geometry_report_carries_the_buffer_rect() -> None:
    payload = (
        '[{"pid": 1, "id": "w", "resourceClass": "c", "caption": "t", "normal": true,'
        ' "popup": false, "managed": true, "deleted": false, "desktop": false, "dock": false,'
        ' "notification": false, "active": true, "frame": [131, 8, 1018, 738],'
        ' "client": [131, 8, 1018, 738], "buffer": [114.6, -2.4, 1050, 780]}]'
    )

    (window,) = geometry._parse_windows(payload)

    assert window["buffer"] == [115, -2, 1050, 780]
    assert "buffer" not in geometry._report(window)


def test_a_flatpak_window_change_during_the_walk_unmaps_it(
    proc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stability check follows the window owner's pid, not the proxy's."""
    _fake_proc(proc, 287882, "xdg-dbus-proxy", BETTERBIRD_SCOPE)
    _fake_proc(proc, 287894, "betterbird", BETTERBIRD_SCOPE)
    app = _App(287882, [_TopLevel("Inbox - Betterbird", (1200, 800))])
    before = [_window(287894, "w1", "Inbox - Betterbird", [10, 20, 1200, 800])]
    after = [_window(287894, "w1", "Inbox - Betterbird", [40, 20, 1200, 800])]

    assert _stable_offsets(app, before, after, monkeypatch) == [(None, "windows-changed")]


@pytest.mark.parametrize(
    ("owner", "scope", "expected"),
    [
        # Another process of the same Flatpak instance opens a twin window.
        (287895, BETTERBIRD_SCOPE, [(None, "windows-changed")]),
        # A window of another instance does not concern this app.
        (300, OTHER_SCOPE, [((10, 20), "")]),
    ],
)
def test_a_window_from_a_new_flatpak_owner_during_the_walk(
    proc: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner: int,
    scope: str,
    expected: list[tuple[tuple[int, int] | None, str]],
) -> None:
    """Owners are resolved again on the second snapshot, not taken from the first."""
    _fake_proc(proc, 287882, "xdg-dbus-proxy", BETTERBIRD_SCOPE)
    _fake_proc(proc, 287894, "betterbird", BETTERBIRD_SCOPE)
    _fake_proc(proc, owner, "betterbird", scope)
    app = _App(287882, [_TopLevel("Inbox - Betterbird", (1200, 800))])
    before = [_window(287894, "w1", "Inbox - Betterbird", [10, 20, 1200, 800])]
    after = [*before, _window(owner, "w2", "Inbox - Betterbird", [500, 20, 1200, 800])]

    assert _stable_offsets(app, before, after, monkeypatch) == expected
