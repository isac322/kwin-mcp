"""Assertion helpers shared by the end-to-end tests.

Kept out of conftest.py so test modules can import it directly: pytest puts this
directory on sys.path, while `tests.e2e.conftest` is not importable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_FOUND = re.compile(r"Found (\d+) elements")
_SCREENSHOT_LINE = re.compile(r"^Screenshot saved: (.+) \((\d+(?:\.\d+)?) KB\)$", re.MULTILINE)
_COORDINATE_LINE = re.compile(
    r"Coordinate space: logical; origin \((-?\d+), (-?\d+)\); size (\d+)x(\d+); "
    r"backend (\S+)(?: \([^)]*\))?; coverage (full|partial)"
)


@dataclass(frozen=True)
class CoordinateSpace:
    """The logical mapping a screenshot or frame line reports."""

    origin: tuple[int, int]
    size: tuple[int, int]
    backend: str
    coverage: str


def screenshot_path(output: str) -> Path:
    """Path of the PNG named by a ``screenshot`` result."""
    match = _SCREENSHOT_LINE.search(output)
    assert match is not None, f"unexpected screenshot output: {output[:500]}"
    return Path(match.group(1))


def coordinate_spaces(output: str) -> list[CoordinateSpace]:
    """Every logical coordinate-space line in a screenshot or frame-burst result."""
    return [
        CoordinateSpace(
            origin=(int(ox), int(oy)),
            size=(int(width), int(height)),
            backend=backend,
            coverage=coverage,
        )
        for ox, oy, width, height, backend, coverage in _COORDINATE_LINE.findall(output)
    ]


def element_count(output: str) -> int:
    """Number of elements reported by find_ui_elements / wait_for_element.

    Parsing the count instead of testing for the absence of "Found 0 elements"
    means an error string or a changed output format fails loudly rather than
    passing vacuously.
    """
    match = _FOUND.match(output)
    assert match is not None, f"unexpected AT-SPI2 query output: {output[:200]}"
    return int(match.group(1))
