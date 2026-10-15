"""K1 agent tasks T1-T8 as the design's call sequences over the installed stdio server.

Each test replays one task of proposal S §6.4 with exactly the calls the table
lists (plus arrange steps the task presupposes, such as launching the app) and
checks the end state through an oracle the test owns: ``atspi_oracle`` for the
accessibility tree, ``kwin_oracle`` for windows and desktops, and the
interaction probe's raw event log (PEL) for injected input. A tool's own
report is never the evidence that the task succeeded.

Deviations from the §6.4 table, forced by the image or by AT-SPI facts:

- T1: KCalc's 7 key is named ``Seven`` and Qt 6 reports its role as
  ``button`` (the table's ``"7"`` / ``"push button"`` match nothing).
- T2: KWrite also exposes a hidden, unshown text field, so the locator adds
  ``states: ["showing"]``.
- T4: the image ships no Dolphin, so the drag runs between the interaction
  probe's drag source (``Drag Target``) and a drop target (``Click Target``),
  and the probe's event log is the drop oracle.
- T7: the ambiguity comes from an open Kate menu (a popup with the same
  app_id). window_place refuses to move a window whose transients would move
  too, so the menu is dismissed before the ``kind: ["normal"]`` retry.
"""

from __future__ import annotations

import shlex
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

import anyio
import pytest
from atspi_oracle import AtspiOracle
from kwin_oracle import KWinOracle, OracleWindow
from mcp_harness import ScriptedUser, running_mcp_server
from probe_session import (
    BUTTON_KINDS,
    KEY_KINDS,
    PROBE_APP,
    PROBE_COMMAND,
    assert_interval,
    assert_matches_golden,
    load_golden,
    of_kind,
    probe_env,
    read_events,
)
from session_harness import live_kwin

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from atspi_oracle import AtspiNode
    from mcp_harness import McpTestClient

    from kwin_mcp.models import (
        AppLaunchResult,
        DesktopList,
        ElementActResult,
        ElementList,
        KeyboardPressResult,
        KeyboardTypeResult,
        PointerDragResult,
        SessionInfo,
        TouchPinchResult,
        UIElement,
        WindowCloseResult,
        WindowPlaceResult,
    )

SCREEN = {"screen_width": 1280, "screen_height": 800}
KATE_CLASS = "org.kde.kate"
KIND_HINT = 'Narrow the selector: add kind:["normal"] or use id.'
OWN_UI_TEXT = (
    "window_close is disabled in live sessions: it could discard unsaved work "
    "on the real desktop. Close the window through the app's own UI instead."
)


@pytest.fixture
def anyio_backend() -> str:
    """The MCP stdio client is exercised on asyncio."""
    return "asyncio"


@asynccontextmanager
async def _virtual_session(
    client: McpTestClient, *, clipboard: bool = False
) -> AsyncGenerator[SessionInfo]:
    try:
        info = cast(
            "SessionInfo",
            await client.call_model(
                "session_start", {**SCREEN, "enable_clipboard": clipboard, "home": "isolated"}
            ),
        )
        assert info.input_backend == "eis", info
        yield info
    finally:
        await client.call_result("session_stop")


async def _launch(client: McpTestClient, command: str, env: dict[str, str] | None = None) -> int:
    args: dict[str, Any] = {"command": command}
    if env is not None:
        args["env"] = env
    launched = cast("AppLaunchResult", await client.call_model("app_launch", args))
    return launched.pid


def _status(nodes: list[AtspiNode], prefix: str) -> str:
    names = [n.name for n in nodes if n.name is not None and n.name.startswith(prefix)]
    assert len(names) == 1, names
    return names[0]


async def _launch_probe(
    client: McpTestClient, info: SessionInfo, log_path: Path
) -> tuple[OracleWindow, AtspiOracle]:
    """Start the interaction probe with its event log; return its active window."""
    pid = await _launch(client, PROBE_COMMAND, probe_env(log_path))
    windows = KWinOracle(info.dbus_address).wait_for_windows(
        lambda ws: any(w.pid == pid and w.active for w in ws)
    )
    atspi = AtspiOracle(info.dbus_address)
    atspi.wait_for(
        lambda ns: any(n.name and n.name.startswith("raw_events:") for n in ns), app=PROBE_APP
    )
    return next(w for w in windows if w.pid == pid), atspi


def _local(window: OracleWindow, x: float, y: float) -> tuple[int, int]:
    """Global logical px -> probe surface-local px (what the event log records)."""
    assert window.client is not None, window
    return round(x - window.client.x), round(y - window.client.y)


def _inside(box: tuple[int, int, int, int], x: int, y: int) -> bool:
    bx, by, width, height = box
    return bx <= x < bx + width and by <= y < by + height


def _kate_window(oracle: KWinOracle, document: str) -> OracleWindow:
    """Kate's main window once it shows ``document`` (Kate may hand off to another pid)."""

    def showing(window: OracleWindow) -> bool:
        return window.resource_class == KATE_CLASS and window.normal and document in window.caption

    return next(w for w in oracle.wait_for_windows(lambda ws: any(map(showing, ws))) if showing(w))


def _one(found: ElementList) -> UIElement:
    assert len(found.elements) == 1, found.render()
    return found.elements[0]


# ── T1 ───────────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.qa("QA-SCEN-001")
async def test_t1_kcalc_click_seven() -> None:
    async with running_mcp_server() as client, _virtual_session(client) as info:
        await _launch(client, "kcalc")
        found = cast(
            "ElementList",
            await client.call_model(
                "element_query",
                {
                    "locator": {"app": "kcalc", "query": "Seven", "role": "button"},
                    "wait": {"timeout_ms": 5000},
                },
            ),
        )
        assert found.wait is not None and found.wait.satisfied, found.render()
        seven = _one(found)
        acted = cast(
            "ElementActResult",
            await client.call_model(
                "element_act", {"element": {"ref": seven.ref}, "action": "Press"}
            ),
        )
        assert acted.dispatch == "confirmed", acted.render()

        AtspiOracle(info.dbus_address).wait_for(
            lambda ns: any(n.role == "label" and n.name == "7" for n in ns), app="kcalc"
        )


# ── T2 ───────────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.qa("QA-SCEN-002")
async def test_t2_type_korean(tmp_path: Path) -> None:
    document = tmp_path / "t2-korean.txt"
    document.write_text("", encoding="utf-8")
    async with running_mcp_server() as client, _virtual_session(client, clipboard=True) as info:
        await _launch(client, f"kwrite {shlex.quote(str(document))}")
        found = cast(
            "ElementList",
            await client.call_model(
                "element_query",
                {
                    "locator": {"app": "kwrite", "role": "text", "states": ["showing"]},
                    "wait": {"timeout_ms": 10000},
                },
            ),
        )
        editor = _one(found)
        await client.call_model("pointer_click", {"target": {"ref": editor.ref}})
        typed = cast(
            "KeyboardTypeResult",
            await client.call_model("keyboard_type", {"text": "안녕하세요", "method": "unicode"}),
        )
        assert typed.route, typed.render()

        nodes = AtspiOracle(info.dbus_address).wait_for(
            lambda ns: any(n.role == "text" and n.text == "안녕하세요" for n in ns), app="kwrite"
        )
        target = [n for n in nodes if n.role == "text" and n.text == "안녕하세요"]
        assert len(target) == 1 and "focused" in target[0].states, target


# ── T3 ───────────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.qa("QA-SCEN-003")
async def test_t3_ctrl_shift_t(tmp_path: Path) -> None:
    log_path = tmp_path / "probe-events.jsonl"
    async with running_mcp_server() as client, _virtual_session(client) as info:
        _, atspi = await _launch_probe(client, info, log_path)

        pressed = cast(
            "KeyboardPressResult",
            await client.call_model("keyboard_press", {"keys": "ctrl+shift+t"}),
        )
        assert pressed.keys == "ctrl+shift+t", pressed.render()

        def settled(nodes: list[AtspiNode]) -> bool:
            raw = [n.name for n in nodes if n.name and n.name.startswith("raw_events:")]
            return bool(raw) and " kr=3 " in raw[0]

        nodes = atspi.wait_for(settled, app=PROBE_APP)

    raw = _status(nodes, "raw_events:")
    assert " kp=3 kr=3 " in raw and raw.endswith("held_mods=none"), raw
    keys = of_kind(read_events(log_path), *KEY_KINDS)
    assert_matches_golden(keys, load_golden("key_chord_ctrl_shift_t"))
    assert keys[-1]["held_mods"] == "none", keys


# ── T4 ───────────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.qa("QA-SCEN-004")
async def test_t4_drag_file_to_editor(tmp_path: Path) -> None:
    log_path = tmp_path / "probe-events.jsonl"
    async with running_mcp_server() as client, _virtual_session(client) as info:
        _, atspi = await _launch_probe(client, info, log_path)

        source = _one(
            cast(
                "ElementList",
                await client.call_model(
                    "element_query", {"locator": {"app": PROBE_APP, "query": "Drag Target"}}
                ),
            )
        )
        target = _one(
            cast(
                "ElementList",
                await client.call_model(
                    "element_query", {"locator": {"app": PROBE_APP, "query": "Click Target"}}
                ),
            )
        )
        dragged = cast(
            "PointerDragResult",
            await client.call_model(
                "pointer_drag",
                {"from": {"ref": source.ref}, "to": {"ref": target.ref}, "dwell_ms": 300},
            ),
        )
        assert dragged.dwell_ms == 300, dragged.render()

        nodes = atspi.wait_for(
            lambda ns: any(
                n.name and n.name.startswith("drag_status: source=mouse press=left release=left")
                for n in ns
            ),
            app=PROBE_APP,
        )

    boxes = {n.name: n.window for n in nodes if n.name in {"Drag Target", "Click Target"}}
    assert boxes.keys() == {"Drag Target", "Click Target"}, boxes
    events = read_events(log_path)
    buttons = of_kind(events, *BUTTON_KINDS)
    assert [(e["kind"], e["button"]) for e in buttons] == [
        ("button_press", "left"),
        ("button_release", "left"),
    ], buttons
    press, release = buttons
    drag_box, drop_box = boxes["Drag Target"], boxes["Click Target"]
    assert drag_box is not None and drop_box is not None, boxes
    # The probe window's AT-SPI window coordinates are its surface coordinates.
    assert _inside(drag_box, press["x"], press["y"]), (press, drag_box)
    assert _inside(drop_box, release["x"], release["y"]), (release, drop_box)
    moved = [e for e in of_kind(events, "motion") if e["held"] == ["left"]]
    assert moved, events
    assert _inside(drop_box, moved[-1]["x"], moved[-1]["y"]), (moved[-1], drop_box)
    # Amendment A15: the dwell at the drop target is at least 300 ms - 10 %, at most 3x.
    assert_interval(moved[-1], release, at_least_ms=300)


# ── T5 ───────────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.qa("QA-SCEN-005")
async def test_t5_pinch(tmp_path: Path) -> None:
    log_path = tmp_path / "probe-events.jsonl"
    async with running_mcp_server() as client, _virtual_session(client) as info:
        window, _ = await _launch_probe(client, info, log_path)
        assert window.client is not None
        x, y, width, height = window.client.as_ints()
        # Every contact of the gesture lands on the probe surface.
        assert x <= 400 and x + width > 600 and y <= 400 < y + height, window.client

        pinched = cast(
            "TouchPinchResult",
            await client.call_model(
                "touch_pinch",
                {"center": {"x": 500, "y": 400}, "start_distance": 200, "end_distance": 50},
            ),
        )
        assert (pinched.start_distance, pinched.end_distance) == (200, 50), pinched.render()

        oracle_window = KWinOracle(info.dbus_address).window(window.id)
        assert oracle_window is not None and oracle_window.client == window.client

    touches = of_kind(read_events(log_path), "touch_begin", "touch_update", "touch_end")
    begins = sorted((e["x"], e["y"]) for e in of_kind(touches, "touch_begin"))
    ends = sorted((e["x"], e["y"]) for e in of_kind(touches, "touch_end"))
    assert begins == [_local(window, 400, 400), _local(window, 600, 400)], touches[:4]
    assert ends == [_local(window, 475, 400), _local(window, 525, 400)], touches[-4:]


# ── T6 ───────────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.qa("QA-SCEN-006")
async def test_t6_wait_dialog_close(tmp_path: Path) -> None:
    document = tmp_path / "t6-unsaved.txt"
    async with running_mcp_server() as client, _virtual_session(client) as info:
        oracle = KWinOracle(info.dbus_address)
        await _launch(client, f"kate {shlex.quote(str(document))}")
        main = _kate_window(oracle, document.name)
        # Arrange: unsaved text, then a titlebar close, so Kate asks about saving.
        await client.call_model("keyboard_type", {"text": "unsaved"})
        AtspiOracle(info.dbus_address).wait_for(
            lambda ns: any(n.role == "text" and n.text == "unsaved" for n in ns), app="kate"
        )
        assert oracle.close(main.id)

        found = cast(
            "ElementList",
            await client.call_model(
                "element_query",
                {"locator": {"app": "kate", "role": "dialog"}, "wait": {"timeout_ms": 10000}},
            ),
        )
        assert found.wait is not None and found.wait.satisfied, found.render()
        dialog = _one(found)
        assert dialog.window_id is not None, dialog
        assert dialog.window_id != main.id, dialog
        closed = cast(
            "WindowCloseResult",
            await client.call_model("window_close", {"window": {"id": dialog.window_id}}),
        )
        assert closed.requested and closed.consent == "not_required", closed.render()

        remaining = oracle.wait_for_windows(lambda ws: all(w.id != dialog.window_id for w in ws))
        # Dismissing the save question cancels the close: the editor stays.
        assert any(w.id == main.id for w in remaining), remaining


# ── T7 ───────────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.qa("QA-SCEN-007")
async def test_t7_place_on_desktop_two(tmp_path: Path) -> None:
    document = tmp_path / "t7-place.txt"
    async with running_mcp_server() as client, _virtual_session(client) as info:
        oracle = KWinOracle(info.dbus_address)
        await _launch(client, f"kate {shlex.quote(str(document))}")
        main = _kate_window(oracle, document.name)
        # Arrange: a second virtual desktop, and an open Kate menu (a popup with
        # the same app_id) so the name-only selector is ambiguous.
        oracle.run("workspace.createDesktop(1, 'Desktop 2');\nreport(true);")
        desktops_before = oracle.desktops()
        assert len(desktops_before) == 2, desktops_before
        current_before = oracle.current_desktop()
        await client.call_model("keyboard_press", {"keys": "alt+f"})
        oracle.wait_for_windows(
            lambda ws: any("kate" in w.resource_class and w.kind == "popup" for w in ws)
        )

        listed = cast("DesktopList", await client.call_model("desktop_list"))
        assert [d.id for d in listed.desktops] == [d.id for d in desktops_before], listed
        desktop_two = listed.desktops[1].id
        frame = {"x": 100, "y": 100, "width": 800, "height": 600}
        ambiguous = await client.call_tool_error(
            "window_place",
            {"window": {"app_id": "kate"}, "frame": frame, "desktop": desktop_two},
        )
        assert ambiguous[1] == KIND_HINT, ambiguous
        assert main.id in ambiguous[0] and "(popup)" in ambiguous[0], ambiguous
        # KWin would move the open menu (a transient) along with the window, which
        # window_place refuses, so the menu is dismissed before the retry.
        await client.call_model("keyboard_press", {"keys": "Escape"})
        oracle.wait_for_windows(lambda ws: not any(w.kind == "popup" for w in ws))
        placed = cast(
            "WindowPlaceResult",
            await client.call_model(
                "window_place",
                {
                    "window": {"app_id": "kate", "kind": ["normal"]},
                    "frame": frame,
                    "desktop": desktop_two,
                },
            ),
        )
        assert placed.window_id == main.id, placed.render()

        def placed_on_two(window: OracleWindow | None) -> bool:
            return (
                window is not None
                and window.frame is not None
                and window.frame.as_ints() == (100, 100, 800, 600)
                and window.desktops == (desktop_two,)
            )

        deadline = time.monotonic() + 10.0
        after = oracle.window(main.id)
        while not placed_on_two(after) and time.monotonic() < deadline:
            await anyio.sleep(0.2)
            after = oracle.window(main.id)
        assert after is not None, main.id
        assert placed_on_two(after), (
            f"frame={after.frame} desktops={after.desktops} wanted {desktop_two}\n{placed.render()}"
        )
        assert oracle.current_desktop() == current_before, after


# ── T8 ───────────────────────────────────────────────────────────────────


async def _connect_and_open_notes(client: McpTestClient, live: Any, notes: Path) -> OracleWindow:
    await client.call_model(
        "session_connect",
        {"dbus_address": live.dbus_address, "wayland_display": live.wayland_display},
    )
    await _launch(client, f"kate {shlex.quote(str(notes))}")
    return _kate_window(live.oracle(), notes.name)


@pytest.mark.anyio
@pytest.mark.qa("QA-SCEN-008")
async def test_t8_live_close_with_consent(tmp_path: Path) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("live notes\n", encoding="utf-8")
    selector = {"window": {"app_id": "kate", "caption": "notes.txt"}}
    with live_kwin() as live:
        user = ScriptedUser("confirm")
        async with running_mcp_server(elicitation_callback=user) as client:
            try:
                window = await _connect_and_open_notes(client, live, notes)
                closed = cast(
                    "WindowCloseResult", await client.call_model("window_close", selector)
                )
                assert closed.consent == "accepted" and closed.requested, closed.render()
                assert closed.window_id == window.id, closed.render()
                assert len(user.requests) == 1, user.requests
                live.wait_for_windows(lambda ws: all(w.id != window.id for w in ws))
            finally:
                await client.call_result("session_stop")

        # Variant: a client without the elicitation capability cannot consent.
        async with running_mcp_server() as client:
            try:
                window = await _connect_and_open_notes(client, live, notes)
                refused = cast(
                    "WindowCloseResult", await client.call_model("window_close", selector)
                )
                assert refused.consent == "unsupported", refused.render()
                assert not refused.requested, refused.render()
                assert refused.render() == OWN_UI_TEXT
                assert live.oracle().window(window.id) is not None
            finally:
                await client.call_result("session_stop")
