"""KeyChord grammar: one parser for keyboard_press/keyboard_hold keys and gesture modifiers.

Pure functions of ``kwin_mcp.input``; no EIS, no KWin.
"""

from __future__ import annotations

import pytest

from kwin_mcp.input import (
    chord_keycodes,
    key_name,
    modifier_keycodes,
    paste_chord,
    text_keycodes,
)

# evdev keycodes used below.
SHIFT, CTRL, ALT, SUPER = 42, 29, 56, 125
KEY_A, KEY_C, KEY_T, KEY_1 = 30, 46, 20, 2
KEY_RETURN, KEY_F4, KEY_F11, KEY_LEFT, KEY_PAGE_UP = 28, 62, 87, 105, 104


@pytest.mark.qa("QA-INP-054")
@pytest.mark.parametrize(
    ("chord", "expected"),
    [
        # Modifiers first, in chord order, then the main key.
        ("ctrl+shift+t", [CTRL, SHIFT, KEY_T]),
        ("shift+ctrl+t", [SHIFT, CTRL, KEY_T]),
        ("alt+F4", [ALT, KEY_F4]),
        # Aliases, case-insensitive.
        ("Control+c", [CTRL, KEY_C]),
        ("CTRL+C", [CTRL, KEY_C]),
        ("meta+a", [SUPER, KEY_A]),
        ("Super+a", [SUPER, KEY_A]),
        # Named special keys and their spellings.
        ("Return", [KEY_RETURN]),
        ("enter", [KEY_RETURN]),
        ("f11", [KEY_F11]),
        ("Left", [KEY_LEFT]),
        ("page_up", [KEY_PAGE_UP]),
        ("PageUp", [KEY_PAGE_UP]),
        ("ctrl+Return", [CTRL, KEY_RETURN]),
        # Single characters and whitespace around names.
        ("a", [KEY_A]),
        ("1", [KEY_1]),
        ("ctrl + a", [CTRL, KEY_A]),
        # Modifiers only: the last modifier is the main key.
        ("shift", [SHIFT]),
        ("ctrl+shift", [CTRL, SHIFT]),
        # A modifier after the main key still goes first.
        ("a+shift", [SHIFT, KEY_A]),
    ],
)
def test_keychord_grammar_table(chord: str, expected: list[int]) -> None:
    assert chord_keycodes(chord) == expected


@pytest.mark.qa("QA-INP-054")
@pytest.mark.parametrize("chord", ["foo", "ctrl+foo", "foo+a", "", "ctrl+", "ctrl++", "f13"])
def test_keychord_unknown_name_is_an_error(chord: str) -> None:
    with pytest.raises(ValueError, match=r"^Unknown key: ") as excinfo:
        chord_keycodes(chord)
    assert str(excinfo.value) == f"Unknown key: {chord}"


@pytest.mark.qa("QA-INP-054")
@pytest.mark.parametrize(
    ("chord", "expected"),
    [
        (None, []),
        ("ctrl", [CTRL]),
        ("ctrl+shift", [CTRL, SHIFT]),
        ("Control+META+alt", [CTRL, SUPER, ALT]),
    ],
)
def test_gesture_modifiers_share_the_modifier_names(chord: str | None, expected: list[int]) -> None:
    assert modifier_keycodes(chord) == expected


@pytest.mark.qa("QA-INP-054")
@pytest.mark.parametrize(("chord", "bad"), [("ctrl+a", "a"), ("hyper", "hyper"), ("", "")])
def test_gesture_modifiers_reject_non_modifiers(chord: str, bad: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        modifier_keycodes(chord)
    assert str(excinfo.value) == f"Unknown modifier: {bad!r}"


@pytest.mark.qa("QA-INP-054")
@pytest.mark.parametrize(
    ("keycode", "name"),
    [
        (SHIFT, "shift"),
        (CTRL, "ctrl"),
        (ALT, "alt"),
        (SUPER, "super"),
        (KEY_RETURN, "return"),
        (KEY_PAGE_UP, "page_up"),
        (KEY_F4, "f4"),
        (KEY_LEFT, "left"),
        (57, "space"),
        (KEY_A, "a"),
        (KEY_1, "1"),
    ],
)
def test_canonical_key_names(keycode: int, name: str) -> None:
    assert key_name(keycode) == name


@pytest.mark.qa("QA-INP-054")
def test_canonical_names_round_trip_through_the_grammar() -> None:
    for chord in ("ctrl+shift+t", "control+meta+Return", "alt+F4", "shift+A"):
        names = [key_name(code) for code in chord_keycodes(chord)]
        assert chord_keycodes("+".join(names)) == chord_keycodes(chord)


@pytest.mark.qa("QA-INP-054")
def test_text_keycodes_include_shift_only_for_shifted_characters() -> None:
    assert text_keycodes("ab") == {KEY_A, 48}
    assert text_keycodes("A") == {KEY_A, SHIFT}
    assert text_keycodes("!") == {KEY_1, SHIFT}
    assert text_keycodes("\n") == {KEY_RETURN}
    # Characters outside the key map are left to check_typeable.
    assert text_keycodes("é") == set()


@pytest.mark.qa("QA-INP-054")
def test_paste_chords_parse_as_key_chords() -> None:
    for resource_class in ("org.kde.kate", "org.kde.konsole"):
        chord = paste_chord(resource_class)
        assert chord is not None
        assert [key_name(code) for code in chord_keycodes(chord)] == chord.split("+")
