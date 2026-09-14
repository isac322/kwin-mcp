"""Assertion helpers shared by the end-to-end tests.

Kept out of conftest.py so test modules can import it directly: pytest puts this
directory on sys.path, while `tests.e2e.conftest` is not importable.
"""

from __future__ import annotations

import re

_FOUND = re.compile(r"Found (\d+) elements")


def element_count(output: str) -> int:
    """Number of elements reported by find_ui_elements / wait_for_element.

    Parsing the count instead of testing for the absence of "Found 0 elements"
    means an error string or a changed output format fails loudly rather than
    passing vacuously.
    """
    match = _FOUND.match(output)
    assert match is not None, f"unexpected AT-SPI2 query output: {output[:200]}"
    return int(match.group(1))
