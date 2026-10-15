"""End-to-end contracts for keyboard_press: the KeyChord grammar as the probe sees it.

The oracle is the interaction probe's raw event log (PEL): every key event GTK
receives, with its keyval name, hardware keycode (evdev + 8) and the modifiers
held before it. KWrite's document is read through the independent AT-SPI oracle.
"""

from __future__ import annotations

import shlex
import time
from typing import TYPE_CHECKING

import pytest
from atspi_oracle import AtspiOracle
from probe_session import (
    KEY_KINDS,
    Probe,
    assert_matches_golden,
    load_golden,
    of_kind,
    start_probe_session,
)

from kwin_mcp.models import HeldInput, KeyboardPressResult, WindowSelector

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import AppLaunchResult, ElementList, SessionInfo

WAIT_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.1
# X keycode = evdev keycode + 8 (the probe logs the hardware keycode GDK reports).
_X = 8
CTRL, SHIFT, SUPER = (37, "Control_L"), (50, "Shift_L"), (133, "Super_L")


@pytest.fixture
def probe(engine: AutomationEngine, tmp_path: Path) -> Probe:
    return start_probe_session(engine, tmp_path)


def _press(probe: Probe, keys: str) -> tuple[KeyboardPressResult, list[dict[str, object]]]:
    """Press a chord and return the result with the key events it produced."""
    mark = probe.mark()
    result = probe.engine.keyboard_press(keys)
    events = of_kind(probe.settle(since=mark), *KEY_KINDS)
    return result, events


def _keys(events: list[dict[str, object]]) -> list[tuple[str, str, int]]:
    return [(str(e["kind"]), str(e["key"]), int(str(e["keycode"]))) for e in events]


def _tap(*keys: tuple[int, str]) -> list[tuple[str, str, int]]:
    """Presses in order, releases in reverse order."""
    downs = [("key_press", name, code) for code, name in keys]
    ups = [("key_release", name, code) for code, name in reversed(keys)]
    return downs + ups


def _wait_until(predicate: Callable[[], bool], description: str) -> None:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out waiting for {description}")
        time.sleep(POLL_INTERVAL_SECONDS)


def _focused_text(atspi: AtspiOracle) -> str:
    """Text of KWrite's focused document, read by the test's own AT-SPI walk."""
    nodes = [
        n
        for n in atspi.find(app="kwrite", role="text")
        if "focused" in n.states and n.error is None
    ]
    assert len(nodes) == 1, [(n.role, n.name, sorted(n.states)) for n in nodes]
    return nodes[0].text or ""


def test_keyboard_press_types_then_clears_text(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
    tmp_path: Path,
) -> None:
    info = start_session()
    assert info.input_backend == "eis", info
    launch(f"kwrite {shlex.quote(str(tmp_path / 'kwin-mcp-key.txt'))}")
    wait_for_app("kwrite")
    engine.window_focus(WindowSelector(app_id="org.kde.kwrite"))
    atspi = AtspiOracle(info.dbus_address)

    assert engine.keyboard_press("7").render() == "Pressed: 7"
    _wait_until(lambda: _focused_text(atspi) == "7", "document text '7'")

    engine.keyboard_press("ctrl+a")
    engine.keyboard_press("Delete")
    _wait_until(lambda: _focused_text(atspi) == "", "an empty document")


@pytest.mark.qa("QA-INP-051")
def test_keychord_grammar_ctrl_shift_t(probe: Probe) -> None:
    result, events = _press(probe, "ctrl+shift+t")

    assert result == KeyboardPressResult(keys="ctrl+shift+t", held=HeldInput())
    assert result.render() == "Pressed: ctrl+shift+t"
    # Same sequence and A15 timing as 0.11's keyboard_key("ctrl+shift+t").
    assert_matches_golden(events, load_golden("key_chord_ctrl_shift_t"))
    assert _keys(events) == _tap(CTRL, SHIFT, (20 + _X, "T"))
    assert [e["held_mods"] for e in events] == [
        "ctrl",
        "ctrl+shift",
        "ctrl+shift",
        "ctrl+shift",
        "ctrl",
        "none",
    ]


# The 0.11 key map (F101): names are case-insensitive, aliases share a keycode,
# a single character is its unshifted key, modifier-only chords press the
# modifier alone.
_CHORDS: list[tuple[str, list[tuple[int, str]]]] = [
    ("Return", [(28 + _X, "Return")]),
    ("enter", [(28 + _X, "Return")]),
    ("Tab", [(15 + _X, "Tab")]),
    ("F5", [(63 + _X, "F5")]),
    ("page_up", [(104 + _X, "Page_Up")]),
    ("pageup", [(104 + _X, "Page_Up")]),
    ("CONTROL+a", [CTRL, (30 + _X, "a")]),
    ("meta", [SUPER]),
    ("super", [SUPER]),
    ("A", [(30 + _X, "a")]),
    ("!", [(2 + _X, "1")]),
    ("a", [(30 + _X, "a")]),
    ("CTRL+Shift+T", [CTRL, SHIFT, (20 + _X, "T")]),
    ("shift", [SHIFT]),
]


@pytest.mark.qa("QA-INP-052")
def test_return_tab_real_keys(probe: Probe) -> None:
    """Every name reaches the probe as the 0.11 keysym; malformed chords press nothing."""
    for keys, expected in _CHORDS:
        result, events = _press(probe, keys)
        assert result.render() == f"Pressed: {keys}"
        assert _keys(events) == _tap(*expected), keys
        assert not any(e["repeat"] for e in events), (keys, events)

    # Return is a real key press, timed as 0.11's keyboard_key("Return").
    _, events = _press(probe, "Return")
    assert_matches_golden(events, load_golden("key_return"))

    # Case-insensitive modifier aliases in one chord. meta and super name the same
    # key, so KWin delivers one Super_L press; KWin keeps Meta+<key> combinations
    # for its own shortcuts, so the client must not count on seeing the "a".
    result, events = _press(probe, "control+meta+super+a")
    assert result.render() == "Pressed: control+meta+super+a"
    assert _keys(events)[:2] == [
        ("key_press", "Control_L", CTRL[0]),
        ("key_press", "Super_L", SUPER[0]),
    ]
    assert _keys(events)[-1] == ("key_release", "Control_L", CTRL[0])
    for _code, name in (CTRL, SUPER):
        downs = [e for e in events if e["key"] == name and e["kind"] == "key_press"]
        ups = [e for e in events if e["key"] == name and e["kind"] == "key_release"]
        assert (len(downs), len(ups)) == (1, 1), (name, events)
    assert events[-1]["held_mods"] == "none"

    for malformed in ("ctrl++", "ctrl+", "+a", "F13"):
        mark = probe.mark()
        with pytest.raises(ValueError) as excinfo:
            probe.engine.keyboard_press(malformed)
        assert str(excinfo.value) == f"Unknown key: {malformed}"
        # No partial press: not even the leading ctrl.
        assert probe.settle(since=mark) == [], malformed
    assert probe.engine.session_info().held == HeldInput()


@pytest.mark.qa("QA-INP-053")
def test_unknown_key_error_before_input(probe: Probe) -> None:
    before = probe.status("key_log:")
    mark = probe.mark()

    with pytest.raises(ValueError) as excinfo:
        probe.engine.keyboard_press("ctrl+nokey")

    assert str(excinfo.value) == "Unknown key: ctrl+nokey"
    assert probe.settle(since=mark) == []
    assert probe.status("key_log:") == before
    assert probe.status("raw_events:").startswith("raw_events: bp=0 br=0 kp=0 kr=0 ")
