"""Held-input state machine of ``InputBackend`` and the EIS client behind it.

The backend runs over ``FakeEISClient`` (libei-level recorder) for the rule
table, and over the real ``EISClient`` with a scripted fake libei, fake D-Bus
and a fake clock for the handshake, revalidation and touch-fallback cases.
Nothing here starts KWin or touches a real bus.
"""

from __future__ import annotations

import subprocess
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import dbus
import dbus.bus
import dbus.exceptions
import pytest

import kwin_mcp.input
import kwin_mcp.screenshot
from kwin_mcp.input import (
    _EI_CAP_BUTTON,
    _EI_CAP_KEYBOARD,
    _EI_CAP_POINTER_ABSOLUTE,
    _EI_CAP_SCROLL,
    _EI_EVENT_DEVICE_ADDED,
    _EI_EVENT_DEVICE_REMOVED,
    _EI_EVENT_DEVICE_RESUMED,
    _EI_EVENT_DISCONNECT,
    _EI_EVENT_SEAT_ADDED,
    EISClient,
    HeldControlError,
    InputBackend,
    MouseButton,
    chord_keycodes,
    modifier_keycodes,
    paste_chord,
    text_keycodes,
)
from kwin_mcp.models import HeldInput, TypeRoute

from ._fakes import FAKE_DBUS_ADDRESS, PRESSED, RELEASED, FakeEISClient

if TYPE_CHECKING:
    from collections.abc import Callable

LEFT, RIGHT, MIDDLE = MouseButton.LEFT, MouseButton.RIGHT, MouseButton.MIDDLE
BTN_LEFT, BTN_RIGHT = 0x110, 0x111
SHIFT, CTRL, ALT = 42, 29, 56
KEY_A = 30
# Captured at import, before any fixture replaces it with a fake libei.
_REAL_GET_LIBEI = kwin_mcp.input._get_libei


@pytest.fixture
def backend(fake_eis: FakeEISClient) -> InputBackend:
    return InputBackend(FAKE_DBUS_ADDRESS)


def _key_events(eis: FakeEISClient) -> list[tuple[int, int]]:
    return [(int(event.args[0]), int(event.args[1])) for event in eis.of_kind("key")]


def _button_events(eis: FakeEISClient) -> list[tuple[int, int]]:
    return [(int(event.args[0]), int(event.args[1])) for event in eis.of_kind("button")]


def _motions(eis: FakeEISClient) -> list[tuple[float, float]]:
    return [(event.args[0], event.args[1]) for event in eis.of_kind("motion")]


# ── Rule table (QA-INP-050) ──────────────────────────────────────────────


def _press_on_held_button(backend: InputBackend, eis: FakeEISClient) -> None:
    assert backend.mouse_button_down(10, 10, LEFT) is True
    eis.events.clear()
    assert backend.mouse_button_down(20, 30, LEFT) is False
    assert _button_events(eis) == []
    # The pointer still moves to the target, straight (a drag segment, no nudge).
    assert _motions(eis) == [(20.0, 30.0)]
    assert backend.held() == HeldInput(buttons=["left"])


def _press_on_held_key(backend: InputBackend, eis: FakeEISClient) -> None:
    assert backend.keyboard_key_down("shift") is True
    eis.events.clear()
    assert backend.keyboard_key_down("Shift") is False
    assert eis.events == []
    assert backend.held() == HeldInput(keys=["shift"])


def _press_chord_presses_only_unheld_keys(backend: InputBackend, eis: FakeEISClient) -> None:
    backend.keyboard_key_down("shift")
    eis.events.clear()
    assert backend.keyboard_key_down("ctrl+shift+a") is True
    assert _key_events(eis) == [(CTRL, PRESSED), (KEY_A, PRESSED)]
    assert backend.held() == HeldInput(keys=["shift", "ctrl", "a"])


def _release_on_unheld_button(backend: InputBackend, eis: FakeEISClient) -> None:
    assert backend.mouse_button_up(5, 6, LEFT) is False
    assert _button_events(eis) == [(BTN_LEFT, RELEASED)]
    assert backend.held().is_empty()


def _release_on_unheld_key(backend: InputBackend, eis: FakeEISClient) -> None:
    assert backend.keyboard_key_up("shift") is False
    assert _key_events(eis) == [(SHIFT, RELEASED)]
    assert backend.held().is_empty()


def _release_held_chord_in_reverse(backend: InputBackend, eis: FakeEISClient) -> None:
    backend.keyboard_key_down("ctrl+shift")
    eis.events.clear()
    assert backend.keyboard_key_up("ctrl+shift") is True
    assert _key_events(eis) == [(SHIFT, RELEASED), (CTRL, RELEASED)]
    assert backend.held().is_empty()


def _release_held_button(backend: InputBackend, eis: FakeEISClient) -> None:
    backend.mouse_button_down(10, 10, LEFT)
    eis.events.clear()
    assert backend.mouse_button_up(40, 50, LEFT) is True
    # Still held while moving to the release point: a straight drag segment.
    assert _motions(eis) == [(40.0, 50.0)]
    assert _button_events(eis) == [(BTN_LEFT, RELEASED)]
    assert backend.held().is_empty()


def _move_on_unheld_button(backend: InputBackend, eis: FakeEISClient) -> None:
    with pytest.raises(HeldControlError) as excinfo:
        backend.mouse_hold_move(30, 40, LEFT)
    assert str(excinfo.value) == (
        "pointer_hold(phase='move') refused: left is not held.\n"
        "Press it first with pointer_hold(phase='press', button='left', target={x, y})."
    )
    assert eis.events == []


def _move_on_held_button(backend: InputBackend, eis: FakeEISClient) -> None:
    backend.mouse_button_down(10, 10, LEFT)
    eis.events.clear()
    backend.mouse_hold_move(30, 40, LEFT)
    assert _motions(eis) == [(30.0, 40.0)]
    assert _button_events(eis) == []
    assert backend.held() == HeldInput(buttons=["left"])


def _gesture_touching_held_button(backend: InputBackend, eis: FakeEISClient) -> None:
    backend.mouse_button_down(10, 10, LEFT)
    eis.events.clear()
    with pytest.raises(HeldControlError) as excinfo:
        backend.check_not_held("pointer_click", buttons=[LEFT])
    assert str(excinfo.value) == (
        "pointer_click refused: left is held by pointer_hold; release it first.\n"
        "Release with pointer_hold(phase='release', button='left', target={x, y}), then retry."
    )
    assert eis.events == []


def _gesture_modifiers_touching_held_key(backend: InputBackend, eis: FakeEISClient) -> None:
    backend.keyboard_key_down("ctrl")
    with pytest.raises(HeldControlError) as excinfo:
        backend.check_not_held(
            "pointer_drag", buttons=[RIGHT], keycodes=modifier_keycodes("shift+ctrl")
        )
    assert str(excinfo.value) == (
        "pointer_drag refused: ctrl is held by keyboard_hold; release it first.\n"
        "Release with keyboard_hold(phase='release', keys='ctrl'), then retry."
    )


def _gesture_touching_held_button_and_keys(backend: InputBackend, eis: FakeEISClient) -> None:
    backend.mouse_button_down(10, 10, LEFT)
    backend.keyboard_key_down("ctrl+shift")
    with pytest.raises(HeldControlError) as excinfo:
        backend.check_not_held(
            "pointer_click", buttons=[LEFT], keycodes=modifier_keycodes("shift+ctrl")
        )
    assert str(excinfo.value) == (
        "pointer_click refused: left is held by pointer_hold and ctrl, shift are held by "
        "keyboard_hold; release it first.\n"
        "Release with pointer_hold(phase='release', button='left', target={x, y}); "
        "keyboard_hold(phase='release', keys='ctrl+shift'), then retry."
    )


def _typing_shift_while_shift_held(backend: InputBackend, eis: FakeEISClient) -> None:
    backend.keyboard_key_down("shift")
    for text in ("aA!", "A"):
        with pytest.raises(HeldControlError, match="shift is held by keyboard_hold"):
            backend.check_not_held("keyboard_type", keycodes=text_keycodes(text))
    backend.check_not_held("keyboard_type", keycodes=text_keycodes("b"))


def _pointer_move_while_held(backend: InputBackend, eis: FakeEISClient) -> None:
    backend.check_move_allowed()
    backend.mouse_button_down(10, 10, LEFT)
    with pytest.raises(HeldControlError) as excinfo:
        backend.check_move_allowed()
    assert str(excinfo.value) == (
        "pointer_move refused: left is held by pointer_hold.\n"
        "To continue: use pointer_hold(phase='move', button, target) to move a held button."
    )


def _other_control_gesture_warns(backend: InputBackend, eis: FakeEISClient) -> None:
    assert backend.moved_while_held(RIGHT) is None
    backend.mouse_button_down(10, 10, LEFT)
    backend.keyboard_key_down("ctrl")
    backend.check_not_held("pointer_click", buttons=[RIGHT])
    assert backend.moved_while_held(RIGHT) == "moved while left was held"
    assert backend.moved_while_held(None) == "moved while left was held"
    assert backend.moved_while_held(LEFT) is None
    backend.mouse_button_down(10, 10, MIDDLE)
    assert backend.moved_while_held(RIGHT) == "moved while left, middle were held"
    # The gesture itself runs with left held: its moves skip the approach nudge.
    eis.events.clear()
    backend.mouse_click(50, 60, RIGHT)
    assert _motions(eis) == [(50.0, 60.0)]
    assert _button_events(eis) == [(BTN_RIGHT, PRESSED), (BTN_RIGHT, RELEASED)]
    assert backend.held() == HeldInput(buttons=["left", "middle"], keys=["ctrl"])


def _disconnect_clears_everything(backend: InputBackend, eis: FakeEISClient) -> None:
    backend.mouse_button_down(10, 10, LEFT)
    backend.keyboard_key_down("shift")
    assert backend.revalidate() == []
    eis.disconnected = True
    assert backend.revalidate() == [
        "EIS disconnected: held input is no longer tracked; KWin may keep it pressed"
    ]
    assert backend.held().is_empty()
    # Reported once.
    assert backend.revalidate() == []
    with pytest.raises(RuntimeError, match="KWin closed the EIS connection"):
        backend.mouse_button_up(10, 10, LEFT)
    assert backend.held().is_empty()


def _keyboard_replacement_keeps_keys(backend: InputBackend, eis: FakeEISClient) -> None:
    backend.mouse_button_down(10, 10, LEFT)
    backend.keyboard_key_down("ctrl+shift")
    eis.lose_role("keyboard")
    eis.events.clear()
    # Reporting is read-only: a warning, no event, and the keys stay held.
    assert backend.revalidate() == [
        "KWin replaced the EIS keyboard device: held ctrl, shift stay pressed until released, "
        "but new keys no longer see them as modifiers"
    ]
    assert eis.events == []
    assert backend.held() == HeldInput(buttons=["left"], keys=["ctrl", "shift"])
    assert backend.revalidate() == []
    # The explicit release goes through the replacement device: a press KWin
    # absorbs (the key is still down there), then the release that counts.
    assert backend.keyboard_key_up("ctrl+shift") is True
    assert _key_events(eis) == [
        (SHIFT, PRESSED),
        (SHIFT, RELEASED),
        (CTRL, PRESSED),
        (CTRL, RELEASED),
    ]
    assert backend.held() == HeldInput(buttons=["left"])
    # A key pressed after the replacement releases plainly.
    eis.events.clear()
    backend.keyboard_key_down("alt")
    backend.keyboard_key_up("alt")
    assert _key_events(eis) == [(ALT, PRESSED), (ALT, RELEASED)]


def _pointer_replacement_keeps_buttons(backend: InputBackend, eis: FakeEISClient) -> None:
    backend.mouse_button_down(10, 10, LEFT)
    backend.keyboard_key_down("shift")
    eis.lose_role("pointer")
    eis.lose_role("touch")
    eis.events.clear()
    assert backend.revalidate() == ["KWin replaced the EIS pointer device: held left stays pressed"]
    assert eis.events == []
    assert backend.held() == HeldInput(buttons=["left"], keys=["shift"])
    assert backend.revalidate() == []
    # The drag goes on, and its release goes through the replacement device.
    backend.mouse_hold_move(30, 30, LEFT)
    eis.events.clear()
    assert backend.mouse_button_up(40, 40, LEFT) is True
    assert _button_events(eis) == [(BTN_LEFT, PRESSED), (BTN_LEFT, RELEASED)]
    assert backend.held() == HeldInput(keys=["shift"])
    with pytest.raises(HeldControlError, match="left is not held"):
        backend.mouse_hold_move(30, 30, LEFT)


def _release_all_after_replacement(backend: InputBackend, eis: FakeEISClient) -> None:
    backend.mouse_button_down(10, 10, LEFT)
    backend.keyboard_key_down("shift")
    eis.lose_role("pointer")
    eis.lose_role("keyboard")
    backend.revalidate()
    eis.events.clear()
    # session_stop and server exit release replaced controls the same way.
    assert backend.release_all() == HeldInput(buttons=["left"], keys=["shift"])
    assert _key_events(eis) == [(SHIFT, PRESSED), (SHIFT, RELEASED)]
    assert _button_events(eis) == [(BTN_LEFT, PRESSED), (BTN_LEFT, RELEASED)]
    assert backend.held().is_empty()


def _failed_release_stays_held(backend: InputBackend, eis: FakeEISClient) -> None:
    backend.mouse_button_down(10, 10, LEFT)
    backend.keyboard_key_down("shift")
    eis.lose_role("pointer")
    eis.lose_role("keyboard")
    backend.revalidate()
    # A release that does not go out (a refused send, or a shutdown signal inside the
    # press-then-release) leaves the control held, so cleanup still releases it.
    eis.fail_on("key", RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        backend.keyboard_key_up("shift")
    eis.fail_on("button", RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        backend.mouse_button_up(40, 40, LEFT)
    assert backend.held() == HeldInput(buttons=["left"], keys=["shift"])
    eis.clear_failures()
    eis.events.clear()
    assert backend.release_all() == HeldInput(buttons=["left"], keys=["shift"])
    assert _key_events(eis) == [(SHIFT, PRESSED), (SHIFT, RELEASED)]
    assert _button_events(eis) == [(BTN_LEFT, PRESSED), (BTN_LEFT, RELEASED)]
    assert backend.held().is_empty()


def _replacement_without_held_controls_is_silent(backend: InputBackend, eis: FakeEISClient) -> None:
    eis.lose_role("pointer")
    eis.lose_role("keyboard")
    assert backend.revalidate() == []


def _release_all_reports_and_releases(backend: InputBackend, eis: FakeEISClient) -> None:
    backend.mouse_button_down(10, 10, LEFT)
    backend.keyboard_key_down("ctrl+shift")
    eis.events.clear()
    assert backend.release_all() == HeldInput(buttons=["left"], keys=["ctrl", "shift"])
    assert _key_events(eis) == [(SHIFT, RELEASED), (CTRL, RELEASED)]
    assert _button_events(eis) == [(BTN_LEFT, RELEASED)]
    assert _motions(eis) == []
    assert backend.held().is_empty()
    eis.disconnected = True
    assert backend.release_all() == HeldInput()


def _release_all_never_raises(backend: InputBackend, eis: FakeEISClient) -> None:
    backend.mouse_button_down(10, 10, LEFT)
    backend.keyboard_key_down("shift")
    eis.disconnected = True
    assert backend.release_all() == HeldInput(buttons=["left"], keys=["shift"])
    assert backend.held().is_empty()


HELD_RULES: dict[str, Callable[[InputBackend, FakeEISClient], None]] = {
    "press_on_held_button": _press_on_held_button,
    "press_on_held_key": _press_on_held_key,
    "press_chord_presses_only_unheld_keys": _press_chord_presses_only_unheld_keys,
    "release_on_unheld_button": _release_on_unheld_button,
    "release_on_unheld_key": _release_on_unheld_key,
    "release_held_chord_in_reverse": _release_held_chord_in_reverse,
    "release_held_button": _release_held_button,
    "move_on_unheld_button": _move_on_unheld_button,
    "move_on_held_button": _move_on_held_button,
    "gesture_touching_held_button": _gesture_touching_held_button,
    "gesture_modifiers_touching_held_key": _gesture_modifiers_touching_held_key,
    "gesture_touching_held_button_and_keys": _gesture_touching_held_button_and_keys,
    "typing_shift_while_shift_held": _typing_shift_while_shift_held,
    "pointer_move_while_held": _pointer_move_while_held,
    "other_control_gesture_warns": _other_control_gesture_warns,
    "disconnect_clears_everything": _disconnect_clears_everything,
    "keyboard_replacement_keeps_keys": _keyboard_replacement_keeps_keys,
    "pointer_replacement_keeps_buttons": _pointer_replacement_keeps_buttons,
    "release_all_after_replacement": _release_all_after_replacement,
    "failed_release_stays_held": _failed_release_stays_held,
    "replacement_without_held_controls_is_silent": _replacement_without_held_controls_is_silent,
    "release_all_reports_and_releases": _release_all_reports_and_releases,
    "release_all_never_raises": _release_all_never_raises,
}


@pytest.mark.qa("QA-INP-050")
@pytest.mark.parametrize("rule", list(HELD_RULES))
def test_held_state_machine_table(
    rule: str, backend: InputBackend, fake_eis: FakeEISClient
) -> None:
    HELD_RULES[rule](backend, fake_eis)


# The Q10 (a) refusal predicate: a gesture is refused exactly when one of the
# controls it presses is held; any other held control only produces a warning.
GESTURE_CONTROLS: dict[str, tuple[list[MouseButton], list[int]]] = {
    "pointer_click(left)": ([LEFT], []),
    "pointer_click(right, modifiers='ctrl')": ([RIGHT], modifier_keycodes("ctrl")),
    "pointer_drag(left, modifiers='shift')": ([LEFT], modifier_keycodes("shift")),
    "pointer_scroll": ([], []),
    "keyboard_press('ctrl+shift+t')": ([], chord_keycodes("ctrl+shift+t")),
    "keyboard_press('a')": ([], chord_keycodes("a")),
    "keyboard_type('Ab')": ([], sorted(text_keycodes("Ab"))),
    "keyboard_type('b')": ([], sorted(text_keycodes("b"))),
    "keyboard_type(unicode, paste chord ctrl+v)": ([], chord_keycodes("ctrl+v")),
}
HELD_CONTROLS: dict[str, tuple[MouseButton | None, str | None]] = {
    "left": (LEFT, None),
    "right": (RIGHT, None),
    "ctrl": (None, "ctrl"),
    "shift": (None, "shift"),
}


@pytest.mark.qa("QA-INP-050")
@pytest.mark.parametrize("gesture", list(GESTURE_CONTROLS))
@pytest.mark.parametrize("held", list(HELD_CONTROLS))
def test_refusal_predicate_every_gesture_and_held_control(
    gesture: str, held: str, backend: InputBackend, fake_eis: FakeEISClient
) -> None:
    buttons, keycodes = GESTURE_CONTROLS[gesture]
    held_button, held_key = HELD_CONTROLS[held]
    if held_button is not None:
        backend.mouse_button_down(1, 1, held_button)
    if held_key is not None:
        backend.keyboard_key_down(held_key)
    fake_eis.events.clear()
    touched = (held_button in buttons) or (
        held_key is not None and chord_keycodes(held_key)[0] in keycodes
    )
    if touched:
        holder = "pointer_hold" if held_button is not None else "keyboard_hold"
        refusal = rf"^gesture refused: {held} is held by {holder}"
        with pytest.raises(HeldControlError, match=refusal):
            backend.check_not_held("gesture", buttons=buttons, keycodes=keycodes)
    else:
        backend.check_not_held("gesture", buttons=buttons, keycodes=keycodes)
    assert fake_eis.events == []


@pytest.mark.qa("QA-INP-029")
def test_release_always_sent_untracked(backend: InputBackend, fake_eis: FakeEISClient) -> None:
    assert backend.mouse_button_up(100, 200, LEFT) is False
    assert backend.keyboard_key_up("shift") is False
    assert _button_events(fake_eis) == [(BTN_LEFT, RELEASED)]
    assert _key_events(fake_eis) == [(SHIFT, RELEASED)]
    assert backend.held().is_empty()


# ── Gestures release what they pressed (QA-INP-024) ──────────────────────


class _FaultAfter:
    """Wraps one FakeEISClient method so its call number ``fail_at`` raises."""

    def __init__(self, method: Callable[..., object], fail_at: int) -> None:
        self._method = method
        self._fail_at = fail_at
        self.calls = 0

    def __call__(self, *args: float) -> object:
        self.calls += 1
        if self.calls == self._fail_at:
            msg = "EIS fault injected by the test"
            raise RuntimeError(msg)
        return self._method(*args)


def _assert_nothing_left_down(backend: InputBackend, eis: FakeEISClient) -> None:
    assert eis.buttons_down() == set()
    assert eis.keys_down() == set()
    assert eis.active_touches() == set()
    assert backend.held().is_empty()


@pytest.mark.qa("QA-INP-024")
def test_gesture_finally_releases(
    monkeypatch: pytest.MonkeyPatch, backend: InputBackend, fake_eis: FakeEISClient
) -> None:
    # pointer_drag: the fault hits the first motion of segment 3 (2 approach
    # motions, then 10 motions per short segment).
    fault = _FaultAfter(fake_eis.pointer_move_absolute, fail_at=2 + 10 + 10 + 1)
    monkeypatch.setattr(fake_eis, "pointer_move_absolute", fault)
    with pytest.raises(RuntimeError, match="EIS fault"):
        backend.mouse_drag(10, 10, 60, 10, LEFT, ["ctrl"], [(20, 10, 0), (40, 10, 0)])
    order = [
        (event.kind, int(event.args[0]), int(event.args[1]))
        for event in fake_eis.of_kind("key", "button")
    ]
    assert order == [
        ("key", CTRL, PRESSED),
        ("button", BTN_LEFT, PRESSED),
        ("button", BTN_LEFT, RELEASED),
        ("key", CTRL, RELEASED),
    ]
    _assert_nothing_left_down(backend, fake_eis)

    # pointer_click: the fault hits the long-press hold.
    def _broken_hold(_seconds: float, _message: str) -> None:
        msg = "EIS fault injected by the test"
        raise RuntimeError(msg)

    monkeypatch.setattr(kwin_mcp.input, "_hold", _broken_hold)
    fake_eis.events.clear()
    with pytest.raises(RuntimeError, match="EIS fault"):
        backend.mouse_click(10, 10, LEFT, modifiers=["ctrl", "shift"], hold_ms=500)
    assert _key_events(fake_eis)[-2:] == [(SHIFT, RELEASED), (CTRL, RELEASED)]
    assert _button_events(fake_eis) == [(BTN_LEFT, PRESSED), (BTN_LEFT, RELEASED)]
    _assert_nothing_left_down(backend, fake_eis)

    # keyboard_press: the fault hits the main key press; ctrl is released.
    original_key = fake_eis.keyboard_key
    fake_eis.events.clear()
    monkeypatch.setattr(fake_eis, "keyboard_key", _FaultAfter(original_key, fail_at=2))
    with pytest.raises(RuntimeError, match="EIS fault"):
        backend.keyboard_key("ctrl+a")
    assert _key_events(fake_eis) == [(CTRL, PRESSED), (CTRL, RELEASED)]
    _assert_nothing_left_down(backend, fake_eis)

    # keyboard_type: the fault hits 'A' after shift went down; shift is released.
    fake_eis.events.clear()
    monkeypatch.setattr(fake_eis, "keyboard_key", _FaultAfter(original_key, fail_at=2))
    with pytest.raises(RuntimeError, match="EIS fault"):
        backend.keyboard_type("A")
    assert _key_events(fake_eis) == [(SHIFT, PRESSED), (SHIFT, RELEASED)]
    _assert_nothing_left_down(backend, fake_eis)

    # touch_swipe and touch_pinch: a failing motion still lifts every contact.
    fake_eis.fail_on("touch_move", RuntimeError("EIS fault injected by the test"))
    with pytest.raises(RuntimeError, match="EIS fault"):
        backend.touch_swipe(10, 10, 100, 10, duration_ms=100)
    with pytest.raises(RuntimeError, match="EIS fault"):
        backend.touch_pinch(50, 50, 20, 80, duration_ms=100)
    with pytest.raises(RuntimeError, match="EIS fault"):
        backend.touch_multi_swipe(10, 10, 100, 10, fingers=3, duration_ms=100)
    assert len(fake_eis.of_kind("touch_up")) == 1 + 2 + 3
    _assert_nothing_left_down(backend, fake_eis)


# ── Unicode paste chord refusals (QA-INP-061) ────────────────────────────


@pytest.mark.qa("QA-INP-061")
@pytest.mark.parametrize(
    "resource_class", ["xterm", "XTerm", "urxvt", "rxvt-unicode", "my-terminal-app", None]
)
def test_terminal_refuse_list(
    monkeypatch: pytest.MonkeyPatch,
    backend: InputBackend,
    fake_eis: FakeEISClient,
    resource_class: str | None,
) -> None:
    monkeypatch.setattr(kwin_mcp.input.shutil, "which", lambda _name: None)

    def _no_helper(_env: dict[str, str]) -> None:
        pytest.fail("the clipboard helper must not start for a target without a paste chord")

    monkeypatch.setattr(kwin_mcp.input, "_ClipboardHelper", _no_helper)
    chords: list[str] = []
    assert paste_chord(resource_class) is None
    route = backend.keyboard_type_unicode(
        "한글", env={}, focused_class=resource_class, before_paste=chords.append
    )
    assert route is None
    assert chords == []
    assert fake_eis.events == []


@pytest.mark.qa("QA-INP-061")
def test_terminal_paste_chord_is_checked_before_the_clipboard(
    monkeypatch: pytest.MonkeyPatch, backend: InputBackend, fake_eis: FakeEISClient
) -> None:
    monkeypatch.setattr(kwin_mcp.input.shutil, "which", lambda _name: None)

    def _no_helper(_env: dict[str, str]) -> None:
        pytest.fail("a refused paste chord must not touch the clipboard")

    monkeypatch.setattr(kwin_mcp.input, "_ClipboardHelper", _no_helper)
    backend.keyboard_key_down("shift")
    fake_eis.events.clear()

    def _held_check(chord: str) -> None:
        backend.check_not_held("keyboard_type", keycodes=chord_keycodes(chord))

    with pytest.raises(HeldControlError, match="shift is held by keyboard_hold"):
        backend.keyboard_type_unicode(
            "한글", env={}, focused_class="org.kde.konsole", before_paste=_held_check
        )
    assert fake_eis.events == []
    assert backend.held() == HeldInput(keys=["shift"])


@pytest.mark.qa("QA-INP-061")
def test_unicode_routes_are_reported(
    monkeypatch: pytest.MonkeyPatch, backend: InputBackend, fake_eis: FakeEISClient
) -> None:
    monkeypatch.setattr(kwin_mcp.input.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _wtype_ok(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(kwin_mcp.input.subprocess, "run", _wtype_ok)
    chords: list[str] = []
    assert backend.keyboard_type_unicode("한글", env={}, before_paste=chords.append) == TypeRoute(
        kind="wtype"
    )
    assert chords == []

    def _wtype_fails(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 1, b"", b"no virtual keyboard")

    monkeypatch.setattr(kwin_mcp.input.subprocess, "run", _wtype_fails)
    monkeypatch.setattr(
        InputBackend, "_paste_via_clipboard", lambda _self, _text, _env, _chord: True
    )
    route = backend.keyboard_type_unicode(
        "한글", env={}, focused_class=lambda: "org.kde.konsole", before_paste=chords.append
    )
    assert route == TypeRoute(kind="paste", chord="ctrl+shift+v")
    assert route.render() == "paste:ctrl+shift+v"
    assert chords == ["ctrl+shift+v"]


# ── X11 screenshot cursor mirror (QA-INP-014) ────────────────────────────


@pytest.mark.qa("QA-INP-014")
def test_x11_xdotool_mirror_invoked(
    monkeypatch: pytest.MonkeyPatch, backend: InputBackend, fake_eis: FakeEISClient
) -> None:
    monkeypatch.setenv("KWIN_MCP_X11_SCREENSHOT", "1")
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(
        kwin_mcp.screenshot, "x11_physical_point", lambda _bus, _display, x, y: (x * 2, y * 2)
    )
    monkeypatch.setattr(kwin_mcp.input.shutil, "which", lambda name: f"/usr/bin/{name}")
    runs: list[tuple[list[str], dict[str, str]]] = []

    def _run(args: list[str], *, env: dict[str, str], **_kwargs: object) -> object:
        runs.append((args, env))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(kwin_mcp.input.subprocess, "run", _run)
    backend.mouse_move(10, 20)
    assert _motions(fake_eis) == [(9.0, 20.0), (10.0, 20.0)]
    assert len(runs) == 1
    args, env = runs[0]
    assert args == ["/usr/bin/xdotool", "mousemove", "20", "40"]
    assert env["DISPLAY"] == ":99"


# ── Real EISClient over a scripted libei ─────────────────────────────────

POINTER_DEV, KEYBOARD_DEV = 11, 12
EI_FD = 42


@dataclass
class FakeClock:
    """Stands in for the ``time`` module inside ``kwin_mcp.input``."""

    now: float = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class _VariadicCall:
    """ctypes-like function object: ``restype`` is assignable, calls are recorded."""

    def __init__(self, calls: list[tuple[str, tuple[object, ...]]], name: str) -> None:
        self.restype: object = None
        self._calls = calls
        self._name = name

    def __call__(self, *args: object) -> None:
        self._calls.append((self._name, args))


@dataclass
class FakeLibei:
    """The libei calls EISClient makes, answered from a scripted event queue."""

    queue: deque[tuple[int, int]] = field(default_factory=deque)
    capabilities: dict[int, set[int]] = field(default_factory=dict)
    calls: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)
    _events: dict[int, tuple[int, int]] = field(default_factory=dict)
    _next_handle: int = 1000
    ei_seat_bind_capabilities: _VariadicCall = field(init=False)

    def __post_init__(self) -> None:
        self.ei_seat_bind_capabilities = _VariadicCall(self.calls, "ei_seat_bind_capabilities")

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))

    def called(self, name: str) -> list[tuple[object, ...]]:
        return [args for call, args in self.calls if call == name]

    def ei_new_sender(self, _context: object) -> int:
        return 1

    def ei_configure_name(self, _ei: int, _name: bytes) -> None:
        pass

    def ei_setup_backend_fd(self, _ei: int, _fd: int) -> int:
        return 0

    def ei_get_fd(self, _ei: int) -> int:
        return EI_FD

    def ei_dispatch(self, _ei: int) -> None:
        self._record("ei_dispatch")

    def ei_get_event(self, _ei: int) -> int | None:
        if not self.queue:
            return None
        handle = self._next_handle
        self._next_handle += 1
        self._events[handle] = self.queue.popleft()
        return handle

    def ei_event_get_type(self, event: int) -> int:
        return self._events[event][0]

    def ei_event_get_device(self, event: int) -> int:
        return self._events[event][1]

    def ei_event_get_seat(self, _event: int) -> int:
        return 7

    def ei_event_unref(self, event: int) -> None:
        del self._events[event]

    def ei_seat_has_capability(self, _seat: int, _cap: int) -> int:
        return 1

    def ei_device_has_capability(self, device: int, cap: int) -> int:
        return int(cap in self.capabilities.get(device, set()))

    def ei_device_ref(self, device: int) -> int:
        return device

    def ei_device_unref(self, device: int) -> None:
        self._record("ei_device_unref", device)

    def ei_device_start_emulating(self, device: int, _sequence: int) -> None:
        self._record("ei_device_start_emulating", device)

    def ei_device_stop_emulating(self, device: int) -> None:
        self._record("ei_device_stop_emulating", device)

    def ei_unref(self, ei: int) -> None:
        self._record("ei_unref", ei)

    def ei_device_pointer_motion_absolute(self, device: int, x: float, y: float) -> None:
        self._record("motion", device, x, y)

    def ei_device_button_button(self, device: int, button: int, state: int) -> None:
        self._record("button", device, button, state)

    def ei_device_keyboard_key(self, device: int, keycode: int, state: int) -> None:
        self._record("key", device, keycode, state)

    def ei_device_frame(self, _device: int, _time_us: int) -> None:
        pass

    def ei_device_touch_new(self, device: int) -> int:
        self._record("ei_device_touch_new", device)
        return 500 + len(self.called("ei_device_touch_new"))

    def ei_touch_down(self, touch: int, x: float, y: float) -> None:
        self._record("touch_down", touch, x, y)

    def ei_touch_motion(self, touch: int, x: float, y: float) -> None:
        self._record("touch_motion", touch, x, y)

    def ei_touch_up(self, touch: int) -> None:
        self._record("touch_up", touch)

    def ei_touch_unref(self, touch: int) -> None:
        self._record("ei_touch_unref", touch)


@dataclass
class FakeSelect:
    """Stands in for the ``select`` module: readable only while libei has queued events."""

    lib: FakeLibei
    clock: FakeClock
    timeouts: list[float] = field(default_factory=list)

    def select(
        self, rlist: list[int], _wlist: list[int], _xlist: list[int], timeout: float
    ) -> tuple[list[int], list[int], list[int]]:
        self.timeouts.append(timeout)
        if self.lib.queue:
            return list(rlist), [], []
        self.clock.sleep(timeout)
        return [], [], []


class _FakeUnixFd:
    def take(self) -> int:
        return -1


@dataclass
class FakeRemoteDesktop:
    """KWin's org.kde.KWin.EIS.RemoteDesktop object."""

    missing: bool = False
    disconnected_cookies: list[int] = field(default_factory=list)

    def connectToEIS(self, _capabilities: int) -> tuple[_FakeUnixFd, int]:  # noqa: N802
        return _FakeUnixFd(), 77

    def disconnect(self, cookie: int) -> None:
        self.disconnected_cookies.append(int(cookie))


class _FakeBus:
    def __init__(self, _address: str) -> None:
        pass

    def get_object(self, _service: str, _path: str) -> object:
        return object()


@dataclass
class EisWorld:
    lib: FakeLibei
    clock: FakeClock
    select: FakeSelect
    kwin: FakeRemoteDesktop

    def script_handshake(self) -> None:
        """KWin announces a seat, an absolute pointer and a keyboard (no touch device),
        then resumes them."""
        self.lib.capabilities[POINTER_DEV] = {
            _EI_CAP_POINTER_ABSOLUTE,
            _EI_CAP_BUTTON,
            _EI_CAP_SCROLL,
        }
        self.lib.capabilities[KEYBOARD_DEV] = {_EI_CAP_KEYBOARD}
        self.lib.queue.extend(
            [
                (_EI_EVENT_SEAT_ADDED, 0),
                (_EI_EVENT_DEVICE_ADDED, POINTER_DEV),
                (_EI_EVENT_DEVICE_ADDED, KEYBOARD_DEV),
                (_EI_EVENT_DEVICE_RESUMED, POINTER_DEV),
                (_EI_EVENT_DEVICE_RESUMED, KEYBOARD_DEV),
            ]
        )


@pytest.fixture
def eis_world(monkeypatch: pytest.MonkeyPatch) -> EisWorld:
    lib = FakeLibei()
    clock = FakeClock()
    world = EisWorld(lib=lib, clock=clock, select=FakeSelect(lib, clock), kwin=FakeRemoteDesktop())

    def _interface(_obj: object, _name: str) -> FakeRemoteDesktop:
        if world.kwin.missing:
            raise dbus.exceptions.DBusException(
                "No such interface 'org.kde.KWin.EIS.RemoteDesktop'"
            )
        return world.kwin

    def _no_main_loop(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(kwin_mcp.input, "DBusGMainLoop", _no_main_loop)
    monkeypatch.setattr(dbus.bus, "BusConnection", _FakeBus)
    monkeypatch.setattr(dbus, "Interface", _interface)
    monkeypatch.setattr(kwin_mcp.input, "_get_libei", lambda: lib)
    monkeypatch.setattr(kwin_mcp.input, "time", clock)
    monkeypatch.setattr(kwin_mcp.input, "select", world.select)
    # The X11 cursor mirror is a separate path (test_x11_xdotool_mirror_invoked).
    monkeypatch.delenv("KWIN_MCP_X11_SCREENSHOT", raising=False)
    return world


def _backend_over(monkeypatch: pytest.MonkeyPatch, client: EISClient) -> InputBackend:
    monkeypatch.setattr(kwin_mcp.input, "EISClient", lambda _address: client)
    return InputBackend(FAKE_DBUS_ADDRESS)


def _sent_keys(lib: FakeLibei) -> list[tuple[object, ...]]:
    return lib.called("key")


@pytest.mark.qa("QA-INP-048")
def test_revalidation_never_blocks(monkeypatch: pytest.MonkeyPatch, eis_world: EisWorld) -> None:
    eis_world.script_handshake()
    client = EISClient(FAKE_DBUS_ADDRESS)
    backend = _backend_over(monkeypatch, client)
    eis_world.select.timeouts.clear()
    for _ in range(200):
        assert backend.revalidate() == []
        backend.keyboard_key_down("shift")
        backend.held()
    # Every select after the handshake polled without waiting; the fd was never readable.
    assert eis_world.select.timeouts
    assert set(eis_world.select.timeouts) == {0.0}
    assert _sent_keys(eis_world.lib) == [(KEYBOARD_DEV, SHIFT, PRESSED)]
    assert backend.held() == HeldInput(keys=["shift"])


@pytest.mark.qa("QA-INP-050")
def test_real_client_replacement_and_disconnect_reach_revalidate(
    monkeypatch: pytest.MonkeyPatch, eis_world: EisWorld
) -> None:
    eis_world.script_handshake()
    client = EISClient(FAKE_DBUS_ADDRESS)
    backend = _backend_over(monkeypatch, client)
    backend.mouse_button_down(10, 10, LEFT)
    backend.keyboard_key_down("shift")

    # KWin replaces the keyboard (layout reconfigure): reported, the key stays held.
    eis_world.lib.queue.extend(
        [
            (_EI_EVENT_DEVICE_REMOVED, KEYBOARD_DEV),
            (_EI_EVENT_DEVICE_ADDED, 13),
            (_EI_EVENT_DEVICE_RESUMED, 13),
        ]
    )
    eis_world.lib.capabilities[13] = {_EI_CAP_KEYBOARD}
    assert backend.revalidate() == [
        "KWin replaced the EIS keyboard device: held shift stays pressed until released, "
        "but new keys no longer see it as a modifier"
    ]
    assert backend.held() == HeldInput(buttons=["left"], keys=["shift"])
    assert backend.revalidate() == []

    # The EIS connection closes: everything is dropped, reported once.
    eis_world.lib.queue.append((_EI_EVENT_DISCONNECT, 0))
    assert backend.revalidate() == [
        "EIS disconnected: held input is no longer tracked; KWin may keep it pressed"
    ]
    assert backend.held().is_empty()
    assert backend.revalidate() == []


@pytest.mark.qa("QA-INP-072")
def test_touch_eis_setup_errors_and_fallback(
    monkeypatch: pytest.MonkeyPatch, eis_world: EisWorld
) -> None:
    # libei missing: fails before KWin hands out a connection.
    fake_get_libei = kwin_mcp.input._get_libei
    monkeypatch.setattr(kwin_mcp.input, "_get_libei", _REAL_GET_LIBEI)
    monkeypatch.setattr(kwin_mcp.input, "_libei", None)

    def _no_libei() -> object:
        raise OSError("libei.so.1: cannot open shared object file")

    monkeypatch.setattr(kwin_mcp.input, "_load_libei", _no_libei)
    with pytest.raises(RuntimeError) as excinfo:
        EISClient(FAKE_DBUS_ADDRESS)
    assert str(excinfo.value) == (
        "libei could not be loaded (libei.so.1: cannot open shared object file); install "
        "libei (e.g. 'sudo apt install libei1' or 'sudo pacman -S libei')"
    )
    monkeypatch.setattr(kwin_mcp.input, "_get_libei", fake_get_libei)

    # KWin without the RemoteDesktop EIS interface.
    eis_world.kwin.missing = True
    with pytest.raises(RuntimeError, match=r"^KWin EIS interface unavailable: ") as excinfo:
        EISClient(FAKE_DBUS_ADDRESS)
    assert "org.kde.KWin.EIS.RemoteDesktop" in str(excinfo.value)
    eis_world.kwin.missing = False

    # No device ever arrives: the handshake gives up after 5 s and disconnects.
    started = eis_world.clock.now
    with pytest.raises(RuntimeError, match=r"^No pointer device available from EIS$"):
        EISClient(FAKE_DBUS_ADDRESS)
    assert eis_world.clock.now - started >= 5.0
    assert eis_world.kwin.disconnected_cookies == [77]
    assert eis_world.lib.called("ei_unref")

    # KWin drops the connection during the handshake.
    eis_world.lib.queue.append((_EI_EVENT_DISCONNECT, 0))
    with pytest.raises(RuntimeError, match=r"^EIS server disconnected during handshake$"):
        EISClient(FAKE_DBUS_ADDRESS)

    # No touch device: touches go through the absolute pointer's slot.
    eis_world.script_handshake()
    client = EISClient(FAKE_DBUS_ADDRESS)
    touch_id = client.touch_down(5.0, 6.0)
    client.touch_move(touch_id, 7.0, 8.0)
    client.touch_up(touch_id)
    assert eis_world.lib.called("ei_device_touch_new") == [(POINTER_DEV,)]
    assert [name for name, _args in eis_world.lib.calls if name.startswith("touch_")] == [
        "touch_down",
        "touch_motion",
        "touch_up",
    ]

    # After KWin closes the connection every call fails with the 0.11 text.
    eis_world.lib.queue.append((_EI_EVENT_DISCONNECT, 0))
    client.poll()
    assert client.disconnected
    closed = "^KWin closed the EIS connection; input is unavailable$"
    with pytest.raises(RuntimeError, match=closed):
        client.touch_down(1.0, 1.0)
    with pytest.raises(RuntimeError, match=closed):
        client.keyboard_key(SHIFT, PRESSED)
