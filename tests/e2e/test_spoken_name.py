"""Spoken-name normalisation and matching for ``invoke_ui_action(spoken_name=True)``.

Pure functions, so no GI and no session: ``kwin_mcp.spoken_name`` is the single
source of these rules for both the engine's pre-session check and the AT-SPI
worker. Non-ASCII characters are written as escapes so each one is explicit.
"""

from __future__ import annotations

import pytest

from kwin_mcp.spoken_name import (
    TRAILING_PUNCTUATION,
    SpokenNameKey,
    normalize_spoken_name,
    spoken_name_matches,
)

ELLIPSIS = "\u2026"
NBSP = "\u00a0"
SAVE_AS = SpokenNameKey(("save", "as"))


def matches(query: str, candidate: str) -> bool:
    return spoken_name_matches(normalize_spoken_name(query), normalize_spoken_name(candidate))


# ── Unicode ──────────────────────────────────────────────────────────────


def test_composed_and_decomposed_forms_match() -> None:
    assert matches("R\u00e9sum\u00e9", "Re\u0301sume\u0301")


def test_case_and_canonical_form_differences_match_together() -> None:
    """Case folding can emit decomposed text, so NFC runs again after it."""
    assert matches("\u0390", "\u03aa\u0301")


@pytest.mark.parametrize(
    ("query", "candidate"),
    [("save", "\uff33\uff41\uff56\uff45"), ("1", "\u2460"), ("x2", "x\u00b2")],
    ids=["fullwidth", "circled-digit", "superscript"],
)
def test_compatibility_characters_stay_distinct(query: str, candidate: str) -> None:
    """No NFKC: compatibility characters are not folded into other text."""
    assert not matches(query, candidate)


def test_case_folding_is_full_unicode_case_folding() -> None:
    assert matches("STRASSE", "stra\u00dfe")


# ── whitespace ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["save  as", "save\tas", "save\nas", f"save{NBSP}as", "  Save \t\n As  "],
    ids=["spaces", "tab", "newline", "nbsp", "mixed"],
)
def test_unicode_whitespace_separates_words(text: str) -> None:
    assert normalize_spoken_name(text) == SAVE_AS


@pytest.mark.parametrize(
    "char", ["\u200b", "\u200c", "\u200d", "\u200e"], ids=["zwsp", "zwnj", "zwj", "lrm"]
)
def test_format_characters_stay_significant(char: str) -> None:
    assert normalize_spoken_name(f"save{char}as").words == (f"save{char}as",)
    assert not matches("saveas", f"save{char}as")
    assert not matches("save as", f"save{char}as")


# ── trailing punctuation ─────────────────────────────────────────────────


def test_the_whitelist_is_exactly_seven_terminal_marks() -> None:
    assert f".,!?;:{ELLIPSIS}" == TRAILING_PUNCTUATION


@pytest.mark.parametrize("mark", list(TRAILING_PUNCTUATION))
def test_each_whitelisted_trailing_mark_is_ignored(mark: str) -> None:
    assert normalize_spoken_name(f"Save As{mark}") == SAVE_AS
    assert matches("save as", f"Save As{mark}")
    assert matches(f"save as{mark}", "Save As")


@pytest.mark.parametrize(
    "text",
    [
        "Save As?!...",
        "Save As,;:",
        "Save As!!!",
        "Save As . . .",
        "Save As , ?",
        "Save As \u2026 .",
    ],
)
def test_mixed_repeated_and_spaced_trailing_runs_are_ignored(text: str) -> None:
    assert normalize_spoken_name(text) == SAVE_AS


@pytest.mark.parametrize("mark", [")", "'", '"', "-", "+", "%", "\u3002", "\uff01"])
def test_other_trailing_characters_stay_significant(mark: str) -> None:
    assert normalize_spoken_name(f"Save{mark}").words == (f"save{mark}",)
    assert not matches("save", f"Save{mark}")


# ── internal and leading punctuation ─────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "candidate"),
    [
        ("v12", "v1.2"),
        ("C", "C#"),
        ("C", "C++"),
        ("AB", "A/B"),
        ("RD", "R&D"),
        ("saveas", "Save-As"),
        ("save as", "Save-As"),
        ("50", "50%"),
        ("Save", "...Save"),
        ("qu\u00e9", "\u00bfQu\u00e9?"),
    ],
)
def test_internal_and_leading_punctuation_stays_significant(query: str, candidate: str) -> None:
    assert not matches(query, candidate)


@pytest.mark.parametrize(
    ("query", "candidate"),
    [("c#", "C#"), ("a/b", "A/B"), ("save-as", "Save-As."), ("\u00bfqu\u00e9", "\u00bfQu\u00e9?")],
)
def test_internal_and_leading_punctuation_matches_itself(query: str, candidate: str) -> None:
    assert matches(query, candidate)


# ── word boundaries ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "candidate", "expected"),
    [
        ("save as", "Save As...", True),
        ("save as", "SaveAs", True),
        ("save as", "Saves A", False),
        ("save as dialog", "SaveAs Dialog", False),
        ("saveas", "SaveAs", True),
        ("saveas", "Save As", False),
        ("saveas", "Saves A", False),
        ("save - as", "Save - As", True),
        ("save - as", "Save-As", True),
        ("save-as", "Save - As", False),
    ],
)
def test_word_boundaries(query: str, candidate: str, expected: bool) -> None:
    """A multi-word query may name a joined label; a one-word query is never split."""
    assert matches(query, candidate) is expected


# ── empty names ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["", "   ", "...", f"?!{ELLIPSIS}", ". . .", "; :", ",", NBSP])
def test_punctuation_only_text_has_no_words(text: str) -> None:
    key = normalize_spoken_name(text)
    assert key == SpokenNameKey(())
    assert key.compact == ""


@pytest.mark.parametrize("candidate", ["", "...", "?"])
def test_an_empty_query_matches_nothing(candidate: str) -> None:
    assert not spoken_name_matches(SpokenNameKey(()), normalize_spoken_name(candidate))
