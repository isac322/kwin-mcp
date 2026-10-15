"""Window kinds and transient relations from stubbed KWin script output (QA-WIN-010).

The ``utility`` kind and X11 group transients have no E2E coverage (the
virtual session runs without Xwayland, e2e plan D10), so they are pinned here
on the JSON the geometry script reports, with ``run_kwin_script`` stubbed.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

import kwin_mcp.core
from kwin_mcp import geometry
from kwin_mcp.core import AutomationEngine
from kwin_mcp.session import LiveSession

from ._fakes import FAKE_DBUS_ADDRESS, FAKE_WAYLAND_SOCKET

if TYPE_CHECKING:
    from pathlib import Path

    from kwin_mcp.models import WindowKind


def _record(window_id: str, **overrides: object) -> dict[str, object]:
    """One windowList() entry as the geometry script reports it: a plain normal window."""
    record: dict[str, object] = {
        "pid": 100,
        "id": window_id,
        "resourceClass": "x11-app",
        "caption": window_id,
        "normal": False,
        "dialog": False,
        "utility": False,
        "popup": False,
        "managed": True,
        "deleted": False,
        "desktop": False,
        "dock": False,
        "notification": False,
        "active": False,
        "closeable": True,
        "moveable": True,
        "resizeable": True,
        "desktops": ["d1"],
        "onAllDesktops": False,
        "transient": False,
        "transientFor": None,
        "modal": False,
        "frame": [10.5, 20, 400, 300],
        "client": [10.5, 50, 400, 270],
        "buffer": [10.5, 20, 400, 300],
    }
    record.update(overrides)
    return record


# One window per KWin type flag; the expected kind follows S §2.
KIND_RECORDS: list[tuple[dict[str, object], WindowKind]] = [
    (_record("{main}", normal=True, active=True), "normal"),
    (_record("{dialog}", dialog=True, transient=True, transientFor="{main}", modal=True), "dialog"),
    (_record("{palette}", utility=True, transient=True, transientFor="{main}"), "utility"),
    (_record("{menu}", popup=True, managed=False, closeable=False), "popup"),
    (_record("{tooltip}", popup=True, managed=False, closeable=False), "popup"),
    (
        _record("{wallpaper}", desktop=True, moveable=False, onAllDesktops=True, desktops=[]),
        "shell",
    ),
    (_record("{panel}", dock=True, moveable=False, resizeable=False), "shell"),
    (_record("{toast}", notification=True), "shell"),
    (_record("{splash}"), "other"),
    # An X11 group transient: transient for its whole window group, so KWin
    # scripts see no transientFor.
    (_record("{group-transient}", dialog=True, transient=True), "dialog"),
]


@pytest.fixture
def scripts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the KWin round trip: every script gets the window list below as its reply."""
    sent: list[str] = []
    payload = json.dumps(
        [record for record, _ in KIND_RECORDS] + [_record("{closing}", normal=True, deleted=True)]
    )

    def _run(js_source: str, timeout: float = 5.0) -> str:
        del timeout
        sent.append(js_source)
        return payload

    monkeypatch.setattr(geometry, "run_kwin_script", _run)
    return sent


def _reported() -> list[dict[str, object]]:
    """The public windows as the engine reads them: the helper's JSON reply, converted."""
    raw = json.loads(json.dumps(geometry.windows()))
    return [geometry.window_from_kwin(w).model_dump(mode="json") for w in raw]


@pytest.mark.qa("QA-WIN-010")
def test_kind_mapping_and_x11_transients(scripts: list[str]) -> None:
    kwin = geometry.collect_windows()
    reported = _reported()

    # Kinds: every KWin type flag maps as S §2; deleted windows are not listed.
    assert [(w["id"], w["kind"]) for w in reported] == [
        (str(record["id"]), kind) for record, kind in KIND_RECORDS
    ]
    assert [geometry.window_kind(w) for w in kwin] == [kind for _, kind in KIND_RECORDS] + [
        "normal"
    ]

    # Transient relations come through for the desktop guard: a regular transient
    # names its main window, a modal dialog is flagged, an X11 group transient is
    # transient with no transientFor.
    by_id = {w["id"]: w for w in kwin}
    assert (by_id["{dialog}"]["transientFor"], by_id["{dialog}"]["modal"]) == ("{main}", True)
    assert (by_id["{palette}"]["transientFor"], by_id["{palette}"]["modal"]) == ("{main}", False)
    group = by_id["{group-transient}"]
    assert (group["transient"], group["transientFor"]) == (True, None)
    assert (by_id["{main}"]["transient"], by_id["{main}"]["transientFor"]) == (False, None)

    # The public fields: rounded ints, flags, desktops, all-desktops windows.
    main = reported[0]
    assert main["app_id"] == "x11-app"
    assert main["active"] is True
    assert main["frame"] == {"x": 10, "y": 20, "width": 400, "height": 300}
    assert main["client"] == {"x": 10, "y": 50, "width": 400, "height": 270}
    wallpaper = next(w for w in reported if w["id"] == "{wallpaper}")
    assert (wallpaper["desktops"], wallpaper["on_all_desktops"]) == ([], True)
    panel = next(w for w in reported if w["id"] == "{panel}")
    assert (panel["moveable"], panel["resizeable"], panel["closeable"]) == (False, False, True)
    menu = next(w for w in reported if w["id"] == "{menu}")
    assert menu["closeable"] is False

    # Reading is read-only: the only scripts sent are the window-list query.
    assert scripts == [geometry._SCRIPT_TEMPLATE, geometry._SCRIPT_TEMPLATE]


@pytest.mark.qa("QA-WIN-010")
def test_zero_sized_windows_are_clamped_to_one_pixel(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps([_record("{mapping}", normal=True, frame=[5, 5, 0, -3])])
    monkeypatch.setattr(geometry, "run_kwin_script", lambda _js, timeout=5.0: payload)

    (window,) = _reported()

    assert window["frame"] == {"x": 5, "y": 5, "width": 1, "height": 1}


@pytest.mark.qa("QA-WIN-010")
def test_records_missing_a_field_are_skipped() -> None:
    record = _record("{old}", normal=True)
    del record["utility"]

    assert geometry._parse_windows(json.dumps([record, _record("{ok}", normal=True)])) == [
        geometry._parse_windows(json.dumps([_record("{ok}", normal=True)]))[0]
    ]


@pytest.mark.qa("QA-WIN-009")
def test_kwin_query_timeout_is_tool_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Each KWin script waits 5 s for its reply; the subprocess that runs it is
    # bounded at 30 s, and a timeout surfaces as a tool error, not a hang.
    for script_op in (geometry.windows, geometry.activate, geometry.desktops, geometry.close):
        assert inspect.signature(script_op).parameters["timeout"].default == 5.0

    bounds: list[float | None] = []

    def _never_answers(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        timeout = kwargs.get("timeout")
        assert isinstance(timeout, int | float)
        bounds.append(timeout)
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr(kwin_mcp.core.subprocess, "run", _never_answers)
    engine = AutomationEngine()
    engine._session = LiveSession(
        dbus_address=FAKE_DBUS_ADDRESS, wayland_socket=FAKE_WAYLAND_SOCKET, screenshot_dir=tmp_path
    )
    try:
        with pytest.raises(RuntimeError) as excinfo:
            engine.window_list()
    finally:
        engine._session = None

    assert bounds == [30]
    assert str(excinfo.value) == "Window list unavailable: KWin query timed out after 30s"


@pytest.mark.qa("QA-WIN-009")
def test_geometry_helper_starts_without_pydantic() -> None:
    # The helper subprocess starts for every window query and every capture (twice
    # for an X11 capture). Importing the pydantic models there made each start
    # about 0.4 s slower, enough to push captures toward the 15 s topology bound
    # under load.
    probe = (
        "import sys, kwin_mcp.geometry; print(sorted(m for m in sys.modules if 'pydantic' in m))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=30
    )
    assert result.stdout.strip() == "[]", result.stdout
