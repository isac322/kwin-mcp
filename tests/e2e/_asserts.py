"""Assertion helpers shared by the end-to-end tests.

Kept out of conftest.py so test modules can import it directly: pytest puts this
directory on sys.path, while `tests.e2e.conftest` is not importable.

Model helpers (engine-level and structured MCP results):
    ``element_count(found: ElementList) -> int``: ``len(found.elements)``; fails when
    the list is empty, so an empty result never passes vacuously.
    ``frame_png(frame: Frame) -> Path``: the frame's PNG file, after checking it exists,
    is a PNG and has the frame's ``width`` x ``height``.
    ``assert_render_roundtrip(model_cls, structured, text) -> model``: validates
    ``structured`` (a ``structuredContent`` dict) as ``model_cls``, asserts it dumps
    back unchanged and that ``model.render() == text``; returns the model.
    ``has_kcalc_binary(found: ElementList, expected: str) -> bool``: whether a label
    element shows ``expected`` as KCalc's binary display.

Text helpers (MCP text checks):
    ``screenshot_path(output: str) -> Path``: the PNG named by a ``screen_capture`` text.
    ``coordinate_spaces(output: str) -> list[CoordinateSpace]``: every logical
    coordinate-space line in a ``screen_capture`` text or an action's frame block.
    ``kcalc_binary_label(expected: str) -> re.Pattern[str]``: matches the element
    line (any indentation) of KCalc's binary display showing ``expected``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from PIL import Image

if TYPE_CHECKING:
    from pydantic import BaseModel

    from kwin_mcp.models import ElementList, Frame

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_SCREENSHOT_LINE = re.compile(r"^Screenshot saved: (.+) \((\d+(?:\.\d+)?) KB\)$", re.MULTILINE)
_COORDINATE_LINE = re.compile(
    r"Coordinate space: logical; origin \((-?\d+), (-?\d+)\); size (\d+)x(\d+); "
    r"backend (\S+)(?: \([^)]*\))?; coverage (full|partial)"
)


@runtime_checkable
class _Renderable(Protocol):
    def render(self) -> str: ...


@dataclass(frozen=True)
class CoordinateSpace:
    """The logical mapping a screenshot or frame line reports."""

    origin: tuple[int, int]
    size: tuple[int, int]
    backend: str
    coverage: str


def screenshot_path(output: str) -> Path:
    """Path of the PNG named by a ``screen_capture`` result text."""
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


def element_count(found: ElementList) -> int:
    """Number of elements in an ``element_query`` result; at least one is required.

    Requiring a non-empty list means a query that silently matched nothing fails
    loudly rather than passing vacuously.
    """
    count = len(found.elements)
    assert count > 0, f"AT-SPI2 query matched nothing: {found.render()[:500]}"
    return count


def frame_png(frame: Frame) -> Path:
    """The PNG a ``Frame`` names, checked to exist with the reported image size."""
    path = Path(frame.path)
    assert path.is_file(), f"frame PNG missing: {path}"
    with path.open("rb") as handle:
        assert handle.read(len(_PNG_MAGIC)) == _PNG_MAGIC, f"not a PNG: {path}"
    with Image.open(path) as image:
        assert image.size == (frame.width, frame.height), (image.size, frame)
    return path


def assert_render_roundtrip[M: BaseModel](
    model_cls: type[M], structured: dict[str, Any], text: str
) -> M:
    """Validate ``structured`` as ``model_cls``; its dump and ``render()`` must match."""
    model = model_cls.model_validate(structured)
    assert model.model_dump(mode="json") == structured, (model, structured)
    assert isinstance(model, _Renderable), f"{model_cls.__name__} has no render()"
    rendered = model.render()
    assert rendered == text, f"text differs from render():\n{text}\n---\n{rendered}"
    return model


def _binary_digits(expected: str) -> str:
    # KCalc 26.08 groups binary digits in fours from the right (``100 0110``), older
    # releases show them ungrouped (``1000110``); either rendering of the same value
    # matches.
    return " ?".join(re.escape(digit) for digit in expected)


def kcalc_binary_label(expected: str) -> re.Pattern[str]:
    """Match an ``element_query`` text line for KCalc's binary display showing ``expected``.

    Search with an empty or app-only locator: a name query for the ungrouped
    digits misses the grouped label.
    """
    return re.compile(rf'^\s*- \[label\] "{_binary_digits(expected)}"(?: |$)', re.MULTILINE)


def has_kcalc_binary(found: ElementList, expected: str) -> bool:
    """Whether a label in ``found`` is KCalc's binary display showing ``expected``."""
    pattern = re.compile(_binary_digits(expected))
    return any(
        element.role == "label" and pattern.fullmatch(element.name) is not None
        for element in found.elements
    )
