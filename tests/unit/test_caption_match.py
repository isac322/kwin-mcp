"""Regression tests for matching AT-SPI frame names to KWin window captions.

The pairs are the strings observed in the E2E images: KWrite 25.04 on Debian
(KWin 6.3.6) and KWrite 26.08 on Fedora 44, openSUSE Tumbleweed and Arch Linux
(KWin 6.7.5). No session is needed; the check is a pure function.
"""

from __future__ import annotations

import pytest

from kwin_mcp.accessibility import _caption_consistent


@pytest.mark.parametrize(
    ("caption", "name"),
    [
        # KWrite 26.08: the AT-SPI frame name has two spaces before the em dash,
        # KWin's caption has one.
        ("kwin-mcp-scroll.txt — KWrite", "kwin-mcp-scroll.txt  — KWrite"),
        ("kwin-mcp-mcp-touch.txt — KWrite", "kwin-mcp-mcp-touch.txt  — KWrite"),
        # KWrite 25.04: the AT-SPI frame name is the document with a trailing space.
        ("kwin-mcp-scroll.txt — KWrite", "kwin-mcp-scroll.txt "),
        # Dialogs of KDE apps: KWin appends the app name.
        ("Open File — KWrite", "Open File"),
        ("Calculator", "Calculator"),
    ],
)
def test_observed_kde_captions_match_their_frames(caption: str, name: str) -> None:
    assert _caption_consistent(caption, name)


@pytest.mark.parametrize(
    ("caption", "name"),
    [
        # Different document in the same app.
        ("other.txt — KWrite", "kwin-mcp-scroll.txt  — KWrite"),
        ("other.txt — KWrite", "kwin-mcp-scroll.txt "),
        # Same document shown by a different app.
        ("kwin-mcp-scroll.txt — Kate", "kwin-mcp-scroll.txt  — KWrite"),
        # A bare prefix is not an app-name suffix.
        ("Document 2 — KWrite", "Document"),
        ("kwin-mcp-scroll.txt.bak — KWrite", "kwin-mcp-scroll.txt"),
    ],
)
def test_unrelated_captions_do_not_match(caption: str, name: str) -> None:
    assert not _caption_consistent(caption, name)


def test_empty_side_skips_the_check() -> None:
    assert _caption_consistent("", "kwin-mcp-scroll.txt  — KWrite")
    assert _caption_consistent("kwin-mcp-scroll.txt — KWrite", "   ")
