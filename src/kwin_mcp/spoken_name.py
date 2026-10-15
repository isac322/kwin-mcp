"""Spoken-name keys for ``invoke_ui_action(spoken_name=True)``.

A mechanical, deterministic accessible-name normalisation for labels that were
spoken rather than typed: case, whitespace and a trailing run of sentence
punctuation are ignored, and everything else stays significant. It is not
fuzzy matching and knows nothing about language.

This module has no GI dependency so that the engine can validate a query before
it starts a session, and the AT-SPI worker can match element names with exactly
the same rules.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# Terminal punctuation that speech recognisers add or drop. Only a trailing run
# is ignored (spaces inside the run too); the same characters anywhere else in a
# name stay significant.
TRAILING_PUNCTUATION = ".,!?;:…"


@dataclass(frozen=True)
class SpokenNameKey:
    """A name's normalised words; ``()`` means nothing is left to match."""

    words: tuple[str, ...]

    @property
    def compact(self) -> str:
        """The normalised words joined without spaces."""
        return "".join(self.words)


def normalize_spoken_name(text: str) -> SpokenNameKey:
    """Normalise ``text`` for spoken-name matching.

    NFC, case-folding and NFC again (canonical caseless matching; no NFKC, so
    compatibility characters stay distinct), then whitespace runs collapsed
    (``str.split`` whitespace; format characters such as zero-width joiners stay
    significant), then a trailing run of ``TRAILING_PUNCTUATION`` removed.
    """
    folded = unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())
    trimmed = " ".join(folded.split()).rstrip(TRAILING_PUNCTUATION + " ")
    return SpokenNameKey(tuple(trimmed.split()))


def spoken_name_matches(query: SpokenNameKey, candidate: SpokenNameKey) -> bool:
    """Whether a spoken query names this candidate.

    The same words always match. A multi-word query also matches a single-word
    candidate with the same characters ("save as" names "SaveAs"), since the
    query supplied the word boundaries and the label joined them. A single-word
    query never matches a multi-word candidate: how "saveas" splits into words
    is not guessed. An empty query matches nothing.
    """
    if not query.words:
        return False
    if query.words == candidate.words:
        return True
    return len(query.words) > 1 and len(candidate.words) == 1 and query.compact == candidate.compact
