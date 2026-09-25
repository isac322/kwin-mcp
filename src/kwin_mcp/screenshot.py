"""Screenshot capture via KWin ScreenShot2, Spectacle, or opt-in X11 fallback.

Coordinate contract
-------------------
Every PNG this module saves is in KWin's global *logical* coordinate space,
the space EIS pointer/touch input, ``window_geometry`` and accessibility
rectangles use: image pixel ``(px, py)`` shows logical point
``(origin_x + px, origin_y + py)``. ``origin`` is the top-left of KWin's
virtual screen geometry and may be negative. The per-capture
:class:`FrameMapping` reports origin, size, backend and coverage.

Backends deliver different pixel spaces, verified against upstream sources:

- ScreenShot2 ``CaptureWorkspace`` without ``native-resolution`` renders
  ``virtualScreenGeometry`` at scale 1, so it is already logical (KWin 6.3
  through master: ``takeScreenShot(area)`` sets the viewport to ``area``).
- ``spectacle -f`` requests ``native-resolution`` per screen. One screen is
  returned as-is (device pixels, anchored at that screen's logical origin).
  Several screens are composited into one canvas of ``union * d`` where ``d``
  is the common scale, or ``ceil(max scale)`` for mixed scales, and each
  screen is drawn at ``logicalXY * d``, *without* subtracting the union's
  top-left (Spectacle 6.3 and master ``combinedImage``). Pixel ``p`` is
  therefore logical ``p / d``, and screens at negative (or beyond-canvas)
  logical positions are clipped upstream. Clipped regions are reported as
  partial coverage and left transparent; they are never reconstructed.
- ``scrot`` on a nested X11 display captures the X root, where each KWin
  output is an X window of ``logical size * scale`` physical pixels.

Every capture is bracketed by a topology observation immediately before and
immediately after the pixels are taken. Only an identical observation proves
the mapping; a changed one retries a single capture once, then fails (single
shots) or reports ``unavailable`` for the affected burst frame. Unchanged
image dimensions are never treated as proof of unchanged coordinates.
"""

from __future__ import annotations

import json
import math
import os
import re
import select
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import dbus
import dbus.bus

if TYPE_CHECKING:
    from collections.abc import Callable

    from PIL.Image import Image

# Upper bound for a single ScreenShot2 capture. Generous for a real capture
# (tens of milliseconds) yet short enough that the spectacle fallback stays
# responsive when KWin never answers.
_CAPTURE_TIMEOUT_S = 5.0
_READER_POLL_INTERVAL_S = 0.1
_READER_JOIN_TIMEOUT_S = 1.0

_SUBPROCESS_CAPTURE_TIMEOUT_S = 10.0
_TOPOLOGY_TIMEOUT_S = 15.0

# Rounding slack when comparing pixel sizes derived from qreal geometry.
_SIZE_TOLERANCE_PX = 2

# QImage::Format values KWin sends for raw captures, mapped to PIL raw modes.
# The *32 formats are integer formats: little-endian memory order is B,G,R,A.
# The *8888 formats are byte-ordered: memory order is exactly R,G,B,X.
# 4 = RGB32 (padding byte), 5 = ARGB32, 6 = ARGB32_Premultiplied,
# 16 = RGBX8888 (emitted by current upstream master's area/workspace capture).
_RAW_FORMATS: dict[int, tuple[str, str]] = {
    4: ("RGB", "BGRX"),
    5: ("RGBA", "BGRA"),
    6: ("RGBA", "BGRA"),
    16: ("RGB", "RGBX"),
}

_XWININFO_GEOMETRY = re.compile(r"\s(\d+)x(\d+)[+-]-?\d+[+-]-?\d+\s+\+(-?\d+)\+(-?\d+)\s*$")


@dataclass(frozen=True)
class OutputGeometry:
    """One KWin output: logical rectangle and scale."""

    name: str
    x: float
    y: float
    width: float
    height: float
    scale: float


@dataclass(frozen=True)
class WorkspaceTopology:
    """KWin outputs plus the logical rectangle ScreenShot2 captures."""

    outputs: tuple[OutputGeometry, ...]
    virtual: tuple[int, int, int, int]


@dataclass(frozen=True)
class FrameMapping:
    """How a saved screenshot maps to global logical coordinates.

    ``origin``/``size`` are ``None`` only when the mapping could not be proven;
    ``reason`` then says why and no coordinates are claimed.
    """

    backend: str
    origin: tuple[int, int] | None
    size: tuple[int, int] | None
    coverage: str = "full"
    captured: tuple[tuple[int, int, int, int], ...] = ()
    source: str = ""
    reason: str = ""

    def describe(self) -> str:
        """One line stating the image's coordinate space."""
        if self.origin is None or self.size is None:
            return f"Coordinate space: unavailable ({self.reason}); backend {self.backend}"
        text = (
            f"Coordinate space: logical; origin ({self.origin[0]}, {self.origin[1]}); "
            f"size {self.size[0]}x{self.size[1]}; backend {self.backend}"
        )
        if self.source:
            text += f" ({self.source})"
        text += f"; coverage {self.coverage}"
        if self.coverage == "partial":
            regions = ", ".join(f"({x}, {y}, {w}x{h})" for x, y, w, h in self.captured) or "none"
            text += f" (captured {regions}; other pixels are transparent)"
        # Before/after bracketing cannot see a transient topology change that
        # reverted inside the capture, so this is stability observed, not a
        # generation/atomicity guarantee.
        text += "; topology observed stable before/after capture"
        return text


def _unavailable(backend: str, reason: str) -> FrameMapping:
    return FrameMapping(backend=backend, origin=None, size=None, reason=reason)


# ── Topology ─────────────────────────────────────────────────────────────


def query_topology(dbus_address: str) -> WorkspaceTopology:
    """Read KWin's output layout via the geometry helper subprocess.

    The helper runs in its own process for the same reason ``window_geometry``
    does: KWin answers through a D-Bus callback that needs a GLib main-context
    iteration, which must not happen inside the server process.
    """
    env = {**os.environ}
    if dbus_address:
        env["DBUS_SESSION_BUS_ADDRESS"] = dbus_address
    try:
        result = subprocess.run(
            [sys.executable, "-m", "kwin_mcp.geometry"],
            input=json.dumps({"op": "outputs"}),
            env=env,
            capture_output=True,
            text=True,
            timeout=_TOPOLOGY_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        msg = f"KWin output topology query timed out after {_TOPOLOGY_TIMEOUT_S:g}s"
        raise RuntimeError(msg) from None
    try:
        response = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        detail = result.stderr.strip()[-300:]
        msg = f"KWin output topology query failed (exit {result.returncode}): {detail}"
        raise RuntimeError(msg) from None
    if not response.get("ok"):
        msg = f"KWin output topology unavailable: {response.get('error')}"
        raise RuntimeError(msg)
    return _parse_topology(response["result"])


def _parse_topology(payload: dict[str, object]) -> WorkspaceTopology:
    outputs: list[OutputGeometry] = []
    screens = payload.get("screens")
    if not isinstance(screens, list):
        msg = f"KWin output topology is malformed: {payload!r}"
        raise RuntimeError(msg)
    try:
        for screen in screens:
            x, y, width, height = (float(v) for v in screen["geometry"])
            outputs.append(
                OutputGeometry(
                    name=str(screen["name"]),
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                    scale=float(screen["scale"]),
                )
            )
        virtual_raw = payload.get("virtual")
        if isinstance(virtual_raw, list) and len(virtual_raw) == 4:
            vx, vy, vw, vh = (float(v) for v in virtual_raw)
        else:
            vx, vy, vw, vh = _union(outputs)
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"KWin output topology is malformed: {exc}"
        raise RuntimeError(msg) from exc
    if not outputs or vw <= 0 or vh <= 0:
        msg = "KWin reported no outputs"
        raise RuntimeError(msg)
    if any(o.scale <= 0 or o.width <= 0 or o.height <= 0 for o in outputs):
        msg = f"KWin reported an invalid output: {outputs}"
        raise RuntimeError(msg)
    return WorkspaceTopology(
        outputs=tuple(outputs),
        virtual=(round(vx), round(vy), round(vw), round(vh)),
    )


def _union(outputs: list[OutputGeometry] | tuple[OutputGeometry, ...]) -> tuple[float, ...]:
    left = min(o.x for o in outputs)
    top = min(o.y for o in outputs)
    right = max(o.x + o.width for o in outputs)
    bottom = max(o.y + o.height for o in outputs)
    return left, top, right - left, bottom - top


def _close(actual: tuple[int, int], expected: tuple[float, float]) -> bool:
    return all(abs(a - e) <= _SIZE_TOLERANCE_PX for a, e in zip(actual, expected, strict=True))


def _output_rect(output: OutputGeometry) -> tuple[int, int, int, int]:
    return round(output.x), round(output.y), round(output.width), round(output.height)


# ── Normalization ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Placement:
    """A source region to scale into the logical canvas.

    ``box`` is the source crop in device pixels, ``logical`` the logical
    rectangle it shows.
    """

    box: tuple[int, int, int, int]
    logical: tuple[float, float, float, float]
    # Device-pixel rects (in absolute image coordinates, as ``crop`` boxes)
    # inside ``box`` that carry real content. ``None`` means the whole box is
    # real. Spectacle's multi-output canvas is allocated but never filled, so
    # pixels between its drawn output rectangles are uninitialized; they must
    # be cleared before Lanczos resampling, which would otherwise blend their
    # garbage into output edges.
    drawn: tuple[tuple[int, int, int, int], ...] | None = None


def _compose(
    image: Image,
    placements: list[_Placement],
    topology: WorkspaceTopology,
    covered: list[tuple[int, int, int, int]],
) -> Image:
    """Scale each placement into a transparent logical canvas and mask to ``covered``."""
    from PIL import Image as PILImage
    from PIL import ImageDraw

    vx, vy, vw, vh = topology.virtual
    source = image.convert("RGBA")
    canvas = PILImage.new("RGBA", (vw, vh), (0, 0, 0, 0))
    for placement in placements:
        lx, ly, lw, lh = placement.logical
        left = round(lx - vx)
        top = round(ly - vy)
        width = round(lx + lw - vx) - left
        height = round(ly + lh - vy) - top
        if width <= 0 or height <= 0:
            continue
        region = source.crop(placement.box)
        if placement.drawn is not None:
            fill = PILImage.new("L", region.size, 0)
            paint = ImageDraw.Draw(fill)
            for x1, y1, x2, y2 in placement.drawn:
                paint.rectangle(
                    (
                        x1 - placement.box[0],
                        y1 - placement.box[1],
                        x2 - 1 - placement.box[0],
                        y2 - 1 - placement.box[1],
                    ),
                    fill=255,
                )
            cleared = PILImage.new("RGBA", region.size, (0, 0, 0, 0))
            region = PILImage.composite(region, cleared, fill)
        if region.size != (width, height):
            region = region.resize((width, height), PILImage.Resampling.LANCZOS)
        canvas.paste(region, (left, top))

    mask = PILImage.new("L", (vw, vh), 0)
    draw = ImageDraw.Draw(mask)
    for x, y, w, h in covered:
        draw.rectangle((x - vx, y - vy, x - vx + w - 1, y - vy + h - 1), fill=255)
    # Pixels outside the captured regions (upstream-clipped outputs, gaps
    # between outputs, uninitialized compositor canvas) become fully
    # transparent black instead of carrying whatever the backend left there.
    empty = PILImage.new("RGBA", (vw, vh), (0, 0, 0, 0))
    return PILImage.composite(canvas, empty, mask)


def _intersect(
    a: tuple[int, int, int, int], b: tuple[float, float, float, float]
) -> tuple[int, int, int, int] | None:
    left = max(a[0], math.ceil(b[0]))
    top = max(a[1], math.ceil(b[1]))
    right = min(a[0] + a[2], math.floor(b[0] + b[2]))
    bottom = min(a[1] + a[3], math.floor(b[1] + b[3]))
    if right <= left or bottom <= top:
        return None
    return left, top, right - left, bottom - top


def _snap_rect(rect: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    """Snap a logical rect to nearest whole pixels; sub-pixel edges are rounding."""
    x, y, w, h = rect
    left, top = math.floor(x + 0.5), math.floor(y + 0.5)
    return left, top, math.floor(x + w + 0.5) - left, math.floor(y + h + 0.5) - top


def _snapped_extents(
    extents: list[tuple[float, float, float, float]],
) -> list[tuple[int, int, int, int]]:
    return [s for e in extents if (s := _snap_rect(e))[2] > 0 and s[3] > 0]


def _covers(rect: tuple[int, int, int, int], pieces: list[tuple[int, int, int, int]]) -> bool:
    """True iff the union of integer pixel ``pieces`` covers every pixel of ``rect``."""
    left, top, width, height = rect
    clipped = [p for p in (_intersect(rect, p) for p in pieces) if p is not None]
    for row in range(top, top + height):
        # Merge the horizontal intervals covering this pixel row.
        spans: list[tuple[int, int]] = []
        for px, py, pw, ph in clipped:
            if py <= row < py + ph:
                spans.append((px, px + pw))
        spans.sort()
        cursor = left
        for start, end in spans:
            if start > cursor:
                return False
            cursor = max(cursor, end)
            if cursor >= left + width:
                break
        if cursor < left + width:
            return False
    return True


def _mapping_from(
    backend: str,
    topology: WorkspaceTopology,
    extents: list[tuple[float, float, float, float]],
    source: str,
) -> tuple[FrameMapping, list[tuple[int, int, int, int]]]:
    """Coverage of each output by the logical rectangles that hold real pixels.

    Logical extents come from qreal geometry divided by a scale, so their edges
    carry sub-pixel rounding only. Each extent is snapped to the nearest whole
    pixel; a whole missing pixel never disappears in the snap, which keeps real
    clipping (e.g. Spectacle dropping an output column at a negative origin)
    reported as partial coverage instead of promoted to full.
    """
    covered: list[tuple[int, int, int, int]] = []
    snapped = _snapped_extents(extents)
    full = True
    for output in topology.outputs:
        rect = _output_rect(output)
        pieces = [piece for piece in snapped if _intersect(rect, piece) is not None]
        clipped = [p for p in (_intersect(rect, p) for p in pieces) if p is not None]
        if _covers(rect, pieces):
            covered.append(rect)
            continue
        full = False
        covered.extend(clipped)
    vx, vy, vw, vh = topology.virtual
    mapping = FrameMapping(
        backend=backend,
        origin=(vx, vy),
        size=(vw, vh),
        coverage="full" if full else "partial",
        captured=() if full else tuple(covered),
        source=source,
    )
    return mapping, covered


def _spectacle_layout(
    image_size: tuple[int, int], topology: WorkspaceTopology
) -> tuple[list[_Placement], list[tuple[float, float, float, float]], str] | None:
    """Mirror Spectacle's single-screen / ``combinedImage`` placement."""
    width, height = image_size
    outputs = topology.outputs
    if len(outputs) == 1:
        output = outputs[0]
        scale = output.scale
        expected = (output.width * scale, output.height * scale)
        anchor = (output.x, output.y)
    else:
        _, _, union_w, union_h = _union(outputs)
        scales = {o.scale for o in outputs}
        top_scale = max(scales)
        scale = top_scale if len(scales) == 1 else float(math.ceil(top_scale))
        expected = (round(union_w) * scale, round(union_h) * scale)
        # combinedImage draws at logicalXY * scale without subtracting the
        # union's top-left, so canvas pixel 0 is logical 0.
        anchor = (0.0, 0.0)
    if not _close(image_size, expected):
        return None
    canvas = (0, 0, width, height)
    canvas_extent = (anchor[0], anchor[1], width / scale, height / scale)
    extent = canvas_extent
    drawn: tuple[tuple[int, int, int, int], ...] | None = None
    if len(outputs) > 1:
        # Each output is painted at logicalXY * scale with the canvas origin at
        # logical (0, 0); whatever falls outside the canvas is clipped away by
        # Spectacle itself. Logical coverage is exactly what was drawn.
        drawn_boxes: list[tuple[int, int, int, int]] = []
        drawn_extents: list[tuple[float, float, float, float]] = []
        for output in outputs:
            dx1 = round(output.x * scale)
            dy1 = round(output.y * scale)
            dx2 = round((output.x + output.width) * scale)
            dy2 = round((output.y + output.height) * scale)
            box = _intersect(canvas, (dx1, dy1, dx2 - dx1, dy2 - dy1))
            if box is None:
                continue
            bx, by, bw, bh = box
            drawn_boxes.append((bx, by, bx + bw, by + bh))
            drawn_extents.append((bx / scale, by / scale, bw / scale, bh / scale))
        drawn = tuple(drawn_boxes)
        extents = drawn_extents
    else:
        extents = [canvas_extent]
    placement = _Placement(box=(0, 0, width, height), logical=extent, drawn=drawn)
    source = f"normalized from {width}x{height} device pixels at scale {scale:g}"
    return [placement], extents, source


def _x11_layout(
    image_size: tuple[int, int],
    topology: WorkspaceTopology,
    windows: list[tuple[int, int, int, int]],
) -> tuple[list[_Placement], list[tuple[float, float, float, float]], str] | None:
    """Locate each output's X window in the root capture."""
    root = (0, 0, image_size[0], image_size[1])
    candidates = list(dict.fromkeys([*windows, root]))
    placements: list[_Placement] = []
    extents: list[tuple[float, float, float, float]] = []
    used: set[tuple[int, int, int, int]] = set()
    for output in topology.outputs:
        physical = (output.width * output.scale, output.height * output.scale)
        matches = [
            rect
            for rect in candidates
            if rect not in used
            and _close((rect[2], rect[3]), physical)
            and rect[0] >= 0
            and rect[1] >= 0
            and rect[0] + rect[2] <= image_size[0]
            and rect[1] + rect[3] <= image_size[1]
        ]
        # A window equal to the root is the same pixels; prefer the real window.
        distinct = [m for m in matches if m != root] or matches
        if len(distinct) != 1:
            return None
        rect = distinct[0]
        used.add(rect)
        box = (rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3])
        extent = (output.x, output.y, output.width, output.height)
        placements.append(_Placement(box=box, logical=extent))
        extents.append(extent)
    scales = ", ".join(f"{o.name} {o.scale:g}" for o in topology.outputs)
    source = f"normalized from {image_size[0]}x{image_size[1]} X11 pixels; scale {scales}"
    return placements, extents, source


def _x11_windows(display: str) -> list[tuple[int, int, int, int]]:
    """Absolute rectangles of every X window, via ``xwininfo -root -tree``."""
    xwininfo = shutil.which("xwininfo")
    if xwininfo is None:
        msg = "X11/scrot coordinate mapping requires xwininfo (x11-utils)"
        raise RuntimeError(msg)
    try:
        result = subprocess.run(
            [xwininfo, "-root", "-tree"],
            env={**os.environ, "DISPLAY": display},
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_CAPTURE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        msg = "xwininfo timed out while mapping KWin output windows"
        raise RuntimeError(msg) from None
    if result.returncode != 0:
        msg = f"xwininfo failed (exit {result.returncode}): {result.stderr.strip()}"
        raise RuntimeError(msg)
    rects: list[tuple[int, int, int, int]] = []
    for line in result.stdout.splitlines():
        match = _XWININFO_GEOMETRY.search(line)
        if match is None:
            continue
        width, height, abs_x, abs_y = (int(v) for v in match.groups())
        rects.append((abs_x, abs_y, width, height))
    return list(dict.fromkeys(rects))


def _normalize_file(
    path: Path,
    backend: str,
    layout: tuple[list[_Placement], list[tuple[float, float, float, float]], str],
    topology: WorkspaceTopology,
) -> FrameMapping:
    from PIL import Image as PILImage

    placements, extents, source = layout
    mapping, covered = _mapping_from(backend, topology, extents, source)
    with PILImage.open(path) as image:
        image.load()
        normalized = _compose(image, placements, topology, covered)
    normalized.save(path, "PNG")
    return mapping


def _image_size(path: Path) -> tuple[int, int]:
    from PIL import Image as PILImage

    with PILImage.open(path) as image:
        return image.size


def _capture_with_state[T, R](state: Callable[[], T], capture: Callable[[], R]) -> tuple[T, R]:
    """Run ``capture`` bracketed by ``state`` observations.

    KWin scripting exposes no topology generation counter, so a frame is
    associated with its capture-time layout only when the same state is
    observed immediately before the capture and immediately after it returns.
    A changed state means the saved pixels may belong to either layout; the
    capture is retried once, then the mapping is reported unprovable rather
    than guessed.
    """
    for _ in range(2):
        before = state()
        result = capture()
        after = state()
        if before == after:
            return before, result
    msg = "KWin output topology changed during capture; coordinate mapping cannot be proven"
    raise RuntimeError(msg)


def _burst_frame[S, R](
    state: Callable[[], S],
    capture: Callable[[], R],
    map_frame: Callable[[S, R], FrameMapping],
    backend: str,
    changed: str,
) -> tuple[FrameMapping, R]:
    """Capture one burst frame and map it against its bracketed ``state``.

    Observation errors (a state query raising before *or* after the capture)
    are metadata failures: the captured pixels are kept, the requested timing
    stands, and the frame reports ``unavailable`` with the real reason instead
    of being recaptured or replaying the whole burst. Errors from ``capture``
    itself are real backend failures and propagate unchanged.
    """
    try:
        before = state()
    except RuntimeError as exc:
        # The before-observation failed, so no mapping can ever be proven for
        # these pixels; still capture once at the requested time and keep them.
        return _unavailable(backend, str(exc)), capture()
    result = capture()
    try:
        after = state()
    except RuntimeError as exc:
        return _unavailable(backend, str(exc)), result
    if after != before:
        return _unavailable(backend, changed), result
    return map_frame(before, result), result


def _x11_state(
    dbus_address: str, display: str
) -> tuple[WorkspaceTopology, tuple[tuple[int, int, int, int], ...]]:
    """Output topology plus the X window list; equal states imply equal mapping."""
    windows = tuple(sorted(dict.fromkeys(_x11_windows(display))))
    return query_topology(dbus_address), windows


def _normalize_spectacle(path: Path, topology: WorkspaceTopology) -> FrameMapping | None:
    """Normalize a Spectacle capture against its capture-time topology."""
    layout = _spectacle_layout(_image_size(path), topology)
    if layout is None:
        return None
    return _normalize_file(path, "spectacle", layout, topology)


def _normalize_x11(
    path: Path,
    state: tuple[WorkspaceTopology, tuple[tuple[int, int, int, int], ...]],
) -> FrameMapping | None:
    """Normalize a scrot capture against its capture-time topology and windows."""
    size = _image_size(path)
    layout = _x11_layout(size, state[0], list(state[1]))
    if layout is None:
        return None
    return _normalize_file(path, "x11-scrot", layout, state[0])


def _spectacle_mismatch(path: Path, topology: WorkspaceTopology) -> str:
    width, height = _image_size(path)
    outputs = ", ".join(
        f"{o.name} {o.width:g}x{o.height:g}+{o.x:g}+{o.y:g} scale {o.scale:g}"
        for o in topology.outputs
    )
    return f"Spectacle image {width}x{height} does not match KWin outputs [{outputs}]"


def _x11_mismatch(
    path: Path, state: tuple[WorkspaceTopology, tuple[tuple[int, int, int, int], ...]]
) -> str:
    width, height = _image_size(path)
    topology = state[0]
    outputs = ", ".join(
        f"{o.name} {o.width * o.scale:g}x{o.height * o.scale:g} physical" for o in topology.outputs
    )
    return (
        f"cannot locate exactly one X window per KWin output [{outputs}] "
        f"in the {width}x{height} X11 capture (rotated or ambiguous outputs are unsupported)"
    )


def x11_physical_point(dbus_address: str, display: str, x: int, y: int) -> tuple[int, int]:
    """Map a global logical point to the X root pixel showing it.

    Used to mirror the EIS pointer onto the X display scrot captures. The
    topology is read fresh on each call so output changes are never applied
    with stale geometry; an unmappable point raises instead of guessing.
    """
    topology, windows = _x11_state(dbus_address, display)
    root_size = _x11_root_size(display)
    layout = _x11_layout(root_size, topology, list(windows))
    if layout is None:
        msg = (
            "cannot map logical pointer position to X11: KWin output windows "
            "were not found unambiguously (rotated or ambiguous outputs are unsupported)"
        )
        raise RuntimeError(msg)
    for placement in layout[0]:
        ox, oy, ow, oh = placement.logical
        if ox <= x < ox + ow and oy <= y < oy + oh:
            left, top, right, bottom = placement.box
            px = left + math.floor((x - ox) * (right - left) / ow)
            py = top + math.floor((y - oy) * (bottom - top) / oh)
            return min(px, right - 1), min(py, bottom - 1)
    msg = f"logical point ({x}, {y}) is outside every KWin output"
    raise RuntimeError(msg)


def _x11_root_size(display: str) -> tuple[int, int]:
    xdpyinfo = shutil.which("xdpyinfo")
    if xdpyinfo is None:
        msg = "X11 pointer mapping requires xdpyinfo (x11-utils)"
        raise RuntimeError(msg)
    result = subprocess.run(
        [xdpyinfo],
        env={**os.environ, "DISPLAY": display},
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_CAPTURE_TIMEOUT_S,
        check=False,
    )
    match = re.search(r"dimensions:\s+(\d+)x(\d+) pixels", result.stdout)
    if result.returncode != 0 or match is None:
        msg = f"xdpyinfo could not report the X11 root size: {result.stderr.strip()}"
        raise RuntimeError(msg)
    return int(match.group(1)), int(match.group(2))


# ── Public capture entry points ──────────────────────────────────────────


def capture_screenshot_to_file(
    dbus_address: str = "",
    wayland_socket: str = "",
    *,
    include_cursor: bool = False,
    output_dir: Path | None = None,
) -> tuple[Path, FrameMapping]:
    """Capture a logical-space screenshot and save it to a file.

    Args:
        dbus_address: D-Bus session bus address for the isolated session.
        wayland_socket: Wayland socket name for the isolated session.
        include_cursor: Whether to include the mouse cursor.
        output_dir: Directory to save the screenshot. Uses /tmp if not specified.

    Returns:
        The saved PNG path and its logical coordinate mapping.
    """
    if output_dir is None:
        output_dir = Path("/tmp")
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"screenshot_{timestamp}.png"

    x11_enabled = os.environ.get("KWIN_MCP_X11_SCREENSHOT") == "1"
    x11_selected = x11_enabled and bool(os.environ.get("DISPLAY"))
    x11_error: RuntimeError | None = None
    if x11_selected:
        try:
            return output_path, _capture_x11_file(
                dbus_address, output_path, include_cursor=include_cursor
            )
        except RuntimeError as exc:
            x11_error = exc

    # Default path: KWin ScreenShot2 D-Bus, then Spectacle.
    try:
        return capture_screenshot_dbus(
            dbus_address,
            output_path,
            include_cursor=include_cursor,
        )
    except (dbus.DBusException, RuntimeError) as dbus_exc:
        try:
            return output_path, _capture_spectacle_file(
                dbus_address,
                wayland_socket,
                output_path,
                include_cursor=include_cursor,
            )
        except RuntimeError as spectacle_exc:
            if not x11_enabled:
                msg = (
                    "Screenshot capture failed: "
                    f"ScreenShot2 ({dbus_exc}); "
                    f"Spectacle ({spectacle_exc})"
                )
                raise RuntimeError(msg) from spectacle_exc

            if x11_error is None:
                try:
                    return output_path, _capture_x11_file(
                        dbus_address, output_path, include_cursor=include_cursor
                    )
                except RuntimeError as exc:
                    x11_error = exc

            msg = (
                "Screenshot capture failed: "
                f"X11/scrot ({x11_error}); "
                f"ScreenShot2 ({dbus_exc}); "
                f"Spectacle ({spectacle_exc})"
            )
            raise RuntimeError(msg) from x11_error


def _capture_spectacle_file(
    dbus_address: str,
    wayland_socket: str,
    output_path: Path,
    *,
    include_cursor: bool,
) -> FrameMapping:
    topology, _ = _capture_with_state(
        lambda: query_topology(dbus_address),
        lambda: _capture_via_spectacle(
            dbus_address,
            wayland_socket,
            output_path=output_path,
            include_cursor=include_cursor,
        ),
    )
    mapping = _normalize_spectacle(output_path, topology)
    if mapping is None:
        mismatch = _spectacle_mismatch(output_path, topology)
        output_path.unlink(missing_ok=True)
        msg = f"{mismatch}; coordinate mapping cannot be proven"
        raise RuntimeError(msg)
    return mapping


def _capture_x11_file(
    dbus_address: str, output_path: Path, *, include_cursor: bool
) -> FrameMapping:
    display = os.environ.get("DISPLAY", "")
    state, _ = _capture_with_state(
        lambda: _x11_state(dbus_address, display),
        lambda: _capture_via_scrot(output_path, include_cursor=include_cursor),
    )
    mapping = _normalize_x11(output_path, state)
    if mapping is None:
        mismatch = _x11_mismatch(output_path, state)
        output_path.unlink(missing_ok=True)
        msg = f"{mismatch}; coordinate mapping cannot be proven"
        raise RuntimeError(msg)
    return mapping


def capture_screenshot_dbus(
    dbus_address: str,
    output_path: Path,
    *,
    include_cursor: bool = False,
) -> tuple[Path, FrameMapping]:
    """Capture the whole logical workspace via KWin ScreenShot2 ``CaptureWorkspace``.

    Much faster than spectacle CLI (~15-30ms vs ~200-300ms per frame)
    because it avoids process spawn overhead. Suitable for rapid
    frame capture at short intervals (e.g., every 50ms).

    Requires KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1 to be set in the
    KWin process environment (automatically set for isolated sessions).

    Args:
        dbus_address: D-Bus session bus address for the isolated session.
        output_path: Path to save the PNG file.
        include_cursor: Whether to include the mouse cursor.

    Returns:
        The output_path with the saved PNG file and its coordinate mapping.
    """

    bus = dbus.bus.BusConnection(dbus_address)
    screenshot_obj = bus.get_object("org.kde.KWin", "/org/kde/KWin/ScreenShot2")
    iface = dbus.Interface(screenshot_obj, "org.kde.KWin.ScreenShot2")

    options = {"include-cursor": dbus.Boolean(include_cursor)}
    topology, frame = _capture_with_state(
        lambda: query_topology(dbus_address),
        lambda: _capture_raw_frame(iface, options),
    )
    mapping = _dbus_mapping(frame, topology)
    if mapping is None:
        # The topology was stable across the capture, so the size/scale
        # disagreement means KWin did not return the logical workspace.
        msg = f"{_dbus_mismatch(frame, topology)}; coordinate mapping cannot be proven"
        raise RuntimeError(msg)
    img = _image_from_raw_frame(frame)
    img.save(output_path, "PNG")
    return output_path, mapping


@dataclass(frozen=True)
class _RawFrame:
    data: bytes
    width: int
    height: int
    stride: int
    format: int | None
    scale: float


def _dbus_mapping(frame: _RawFrame, topology: WorkspaceTopology) -> FrameMapping | None:
    """CaptureWorkspace is logical when KWin reports scale 1 and the virtual size."""
    vx, vy, vw, vh = topology.virtual
    if abs(frame.scale - 1.0) > 1e-6 or not _close((frame.width, frame.height), (vw, vh)):
        return None
    return FrameMapping(
        backend="screenshot2",
        origin=(vx, vy),
        size=(frame.width, frame.height),
    )


def _dbus_mismatch(frame: _RawFrame, topology: WorkspaceTopology) -> str:
    vx, vy, vw, vh = topology.virtual
    return (
        f"ScreenShot2 returned {frame.width}x{frame.height} at scale {frame.scale:g}, "
        f"not KWin's logical workspace {vw}x{vh}+{vx}+{vy}"
    )


def _capture_raw_frame(
    iface: dbus.Interface,
    options: dict[str, dbus.Boolean],
) -> _RawFrame:
    """Capture the logical workspace over ScreenShot2 as one raw frame.

    KWin streams the pixels into the pipe before it answers the D-Bus call, and
    a frame is far larger than the pipe buffer, so the pipe is drained by a
    reader thread while the call is in flight.

    The call carries an explicit timeout: a compositor that never answers (KWin
    on a headless virtual backend does exactly that) would otherwise burn
    dbus-python's 25s default before callers can fall back to spectacle.
    """
    read_fd, write_fd = os.pipe()
    chunks: list[bytes] = []
    reader_errors: list[OSError] = []
    stop_reader = threading.Event()

    def drain() -> None:
        # The reader owns read_fd: closing it here, rather than in the caller,
        # keeps a still-running thread from reading a recycled fd number.
        try:
            while not stop_reader.is_set():
                try:
                    readable, _, _ = select.select(
                        [read_fd],
                        [],
                        [],
                        _READER_POLL_INTERVAL_S,
                    )
                except InterruptedError:
                    continue
                if not readable:
                    continue

                chunk = os.read(read_fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError as exc:
            reader_errors.append(exc)
        finally:
            os.close(read_fd)

    reader = threading.Thread(
        target=drain,
        name="kwin-mcp-screenshot-reader",
        daemon=True,
    )
    reader.start()
    capture_succeeded = False
    try:
        # CaptureWorkspace without native-resolution renders the virtual screen
        # geometry at scale 1: every pixel is one logical unit, origin included.
        results = iface.CaptureWorkspace(
            options,
            dbus.types.UnixFd(write_fd),
            timeout=_CAPTURE_TIMEOUT_S,
        )
        capture_succeeded = True
    finally:
        os.close(write_fd)
        if not capture_succeeded:
            stop_reader.set()
        reader.join(timeout=_READER_JOIN_TIMEOUT_S)
        if reader.is_alive():
            stop_reader.set()
            reader.join(timeout=_READER_JOIN_TIMEOUT_S)
        if reader.is_alive():
            msg = "KWin ScreenShot2 pipe reader did not terminate"
            raise RuntimeError(msg)

    if reader_errors:
        msg = f"KWin ScreenShot2 pipe read failed: {reader_errors[0]}"
        raise RuntimeError(msg) from reader_errors[0]

    data = b"".join(chunks)
    if not data:
        msg = "KWin ScreenShot2 returned no data"
        raise RuntimeError(msg)
    try:
        return _RawFrame(
            data=data,
            width=int(results["width"]),
            height=int(results["height"]),
            stride=int(results["stride"]),
            format=int(results["format"]) if "format" in results else None,
            scale=float(results.get("scale", 1.0)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"KWin ScreenShot2 returned malformed metadata: {exc}"
        raise RuntimeError(msg) from exc


def _image_from_raw_frame(frame: _RawFrame) -> Image:
    """Convert KWin's raw frame and normalize malformed payloads as backend failures."""
    from PIL import Image

    mode, raw_mode = _RAW_FORMATS.get(frame.format if frame.format is not None else 6, ("", ""))
    if not mode:
        msg = f"KWin ScreenShot2 returned unsupported image format {frame.format}"
        raise RuntimeError(msg)
    try:
        return Image.frombytes(
            mode, (frame.width, frame.height), frame.data, "raw", raw_mode, frame.stride
        )
    except (OSError, ValueError) as exc:
        msg = f"KWin ScreenShot2 returned malformed image data: {exc}"
        raise RuntimeError(msg) from exc


def capture_frame_burst(
    dbus_address: str,
    output_dir: Path,
    delays_ms: list[int],
    *,
    include_cursor: bool = False,
    wayland_socket: str = "",
) -> list[tuple[Path, FrameMapping]]:
    """Capture multiple logical-space screenshots at specified delays after an action.

    Takes screenshots at each delay (in milliseconds). An explicitly enabled
    X11 display uses scrot first; otherwise ScreenShot2 remains the fast path,
    with Spectacle as its fallback. Every frame is bracketed by a topology
    observation before and after its capture; frames are never recaptured,
    and a frame whose surroundings changed reports ``unavailable`` rather
    than a guessed mapping.

    Args:
        dbus_address: D-Bus session bus address for the session.
        output_dir: Directory to save the frame PNG files.
        delays_ms: List of delays in milliseconds (e.g., [0, 50, 100, 200, 500]).
        include_cursor: Whether to include the mouse cursor.
        wayland_socket: Wayland socket name (needed for spectacle fallback).

    Returns:
        ``(path, mapping)`` per captured frame, ordered by delay.
    """
    burst_start = time.monotonic()
    output_dir.mkdir(parents=True, exist_ok=True)
    sorted_delays = sorted(delays_ms)

    x11_enabled = os.environ.get("KWIN_MCP_X11_SCREENSHOT") == "1"
    x11_selected = x11_enabled and bool(os.environ.get("DISPLAY"))
    x11_error: RuntimeError | None = None
    if x11_selected:
        try:
            return _capture_frame_burst_x11(
                dbus_address,
                output_dir,
                sorted_delays,
                include_cursor=include_cursor,
                start_time=burst_start,
            )
        except RuntimeError as exc:
            x11_error = exc

    try:
        return _capture_frame_burst_dbus(
            dbus_address, output_dir, sorted_delays, include_cursor=include_cursor
        )
    except (dbus.DBusException, RuntimeError) as dbus_exc:
        try:
            return _capture_frame_burst_spectacle(
                dbus_address,
                wayland_socket,
                output_dir,
                sorted_delays,
                include_cursor=include_cursor,
            )
        except RuntimeError as spectacle_exc:
            if not x11_enabled:
                msg = (
                    "Frame burst capture failed: "
                    f"ScreenShot2 ({dbus_exc}); "
                    f"Spectacle ({spectacle_exc})"
                )
                raise RuntimeError(msg) from spectacle_exc

            if x11_error is None:
                try:
                    return _capture_frame_burst_x11(
                        dbus_address,
                        output_dir,
                        sorted_delays,
                        include_cursor=include_cursor,
                    )
                except RuntimeError as exc:
                    x11_error = exc

            msg = (
                "Frame burst capture failed: "
                f"X11/scrot ({x11_error}); "
                f"ScreenShot2 ({dbus_exc}); "
                f"Spectacle ({spectacle_exc})"
            )
            raise RuntimeError(msg) from x11_error


def _capture_frame_burst_dbus(
    dbus_address: str,
    output_dir: Path,
    sorted_delays: list[int],
    *,
    include_cursor: bool = False,
) -> list[tuple[Path, FrameMapping]]:
    """Capture frames using fast KWin ScreenShot2 D-Bus interface."""

    # Reuse a single D-Bus connection for all captures. Each frame is
    # bracketed by topology observations so it maps only to the layout proven
    # stable across that frame's capture; frames are never recaptured, which
    # preserves the requested timing.
    bus = dbus.bus.BusConnection(dbus_address)
    screenshot_obj = bus.get_object("org.kde.KWin", "/org/kde/KWin/ScreenShot2")
    iface = dbus.Interface(screenshot_obj, "org.kde.KWin.ScreenShot2")
    options = {"include-cursor": dbus.Boolean(include_cursor)}

    frames: list[tuple[Path, FrameMapping]] = []
    start = time.monotonic()
    for i, delay_ms in enumerate(sorted_delays):
        target_time = start + delay_ms / 1000.0
        now = time.monotonic()
        if now < target_time:
            time.sleep(target_time - now)

        frame_path = output_dir / f"frame_{i:03d}_{delay_ms}ms.png"
        mapping, frame = _burst_frame(
            lambda: query_topology(dbus_address),
            lambda: _capture_raw_frame(iface, options),
            lambda topology, raw: (
                _dbus_mapping(raw, topology)
                or _unavailable("screenshot2", _dbus_mismatch(raw, topology))
            ),
            "screenshot2",
            "output topology changed while this frame was captured",
        )
        _image_from_raw_frame(frame).save(frame_path, "PNG")
        frames.append((frame_path, mapping))
    return frames


def _capture_frame_burst_spectacle(
    dbus_address: str,
    wayland_socket: str,
    output_dir: Path,
    sorted_delays: list[int],
    *,
    include_cursor: bool = False,
) -> list[tuple[Path, FrameMapping]]:
    """Capture frames using spectacle CLI (slower but always authorized)."""
    frames: list[tuple[Path, FrameMapping]] = []
    start = time.monotonic()
    for i, delay_ms in enumerate(sorted_delays):
        target_time = start + delay_ms / 1000.0
        now = time.monotonic()
        if now < target_time:
            time.sleep(target_time - now)

        frame_path = output_dir / f"frame_{i:03d}_{delay_ms}ms.png"
        mapping, _ = _burst_frame(
            lambda: query_topology(dbus_address),
            lambda path=frame_path: _capture_via_spectacle(
                dbus_address,
                wayland_socket,
                output_path=path,
                include_cursor=include_cursor,
            ),
            lambda topology, _, path=frame_path: (
                _normalize_spectacle(path, topology)
                or _unavailable("spectacle", _spectacle_mismatch(path, topology))
            ),
            "spectacle",
            "output topology changed while this frame was captured",
        )
        frames.append((frame_path, mapping))
    return frames


def _capture_frame_burst_x11(
    dbus_address: str,
    output_dir: Path,
    sorted_delays: list[int],
    *,
    include_cursor: bool = False,
    start_time: float | None = None,
) -> list[tuple[Path, FrameMapping]]:
    """Capture frames from the explicitly enabled X11 display."""
    display = os.environ.get("DISPLAY", "")
    frames: list[tuple[Path, FrameMapping]] = []
    start = time.monotonic() if start_time is None else start_time
    for i, delay_ms in enumerate(sorted_delays):
        target_time = start + delay_ms / 1000.0
        now = time.monotonic()
        if now < target_time:
            time.sleep(target_time - now)

        frame_path = output_dir / f"frame_{i:03d}_{delay_ms}ms.png"
        mapping, _ = _burst_frame(
            lambda: _x11_state(dbus_address, display),
            lambda path=frame_path: _capture_via_scrot(path, include_cursor=include_cursor),
            lambda state, _, path=frame_path: (
                _normalize_x11(path, state) or _unavailable("x11-scrot", _x11_mismatch(path, state))
            ),
            "x11-scrot",
            "output topology or windows changed while this frame was captured",
        )
        frames.append((frame_path, mapping))
    return frames


def _capture_via_scrot(output_path: Path, *, include_cursor: bool = False) -> None:
    """Capture the explicitly selected X11 root window with scrot."""
    display = os.environ.get("DISPLAY", "")
    if os.environ.get("KWIN_MCP_X11_SCREENSHOT") != "1":
        msg = "X11/scrot fallback is disabled; set KWIN_MCP_X11_SCREENSHOT=1 to enable it"
        raise RuntimeError(msg)
    if not display:
        msg = "X11/scrot fallback requires DISPLAY"
        raise RuntimeError(msg)

    output_path.unlink(missing_ok=True)
    cmd = ["scrot", "--overwrite", "--display", display]
    if include_cursor:
        cmd.append("--pointer")
    cmd.append(str(output_path))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=_SUBPROCESS_CAPTURE_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        msg = "scrot not found. Install scrot (e.g. 'sudo apt install scrot')."
        raise RuntimeError(msg) from None
    except subprocess.TimeoutExpired as exc:
        stderr = (
            exc.stderr.decode(errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        detail = f": {stderr.strip()}" if stderr.strip() else ""
        msg = f"scrot timed out after {_SUBPROCESS_CAPTURE_TIMEOUT_S:g}s{detail}"
        raise RuntimeError(msg) from None

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        detail = f": {stderr}" if stderr else ""
        msg = f"scrot failed (exit {result.returncode}){detail}"
        raise RuntimeError(msg)

    if not output_path.is_file() or output_path.stat().st_size == 0:
        msg = "scrot produced no output"
        raise RuntimeError(msg)


def _capture_via_spectacle(
    dbus_address: str,
    wayland_socket: str,
    *,
    output_path: Path,
    include_cursor: bool = False,
) -> None:
    """Capture screenshot using spectacle CLI in background mode."""
    cmd = ["spectacle", "-b", "-f", "-n", "-o", str(output_path)]
    if include_cursor:
        cmd.append("-p")

    env = {**os.environ}
    if dbus_address:
        env["DBUS_SESSION_BUS_ADDRESS"] = dbus_address
    if wayland_socket:
        env["WAYLAND_DISPLAY"] = wayland_socket
        env["QT_QPA_PLATFORM"] = "wayland"
    # Remove host display refs
    env.pop("DISPLAY", None)

    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            timeout=_SUBPROCESS_CAPTURE_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        msg = (
            "spectacle not found. Install spectacle "
            "(e.g. 'sudo pacman -S spectacle' or 'sudo apt install kde-spectacle')."
        )
        raise RuntimeError(msg) from None
    except subprocess.TimeoutExpired as exc:
        stderr = (
            exc.stderr.decode(errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        detail = f": {stderr.strip()}" if stderr.strip() else ""
        msg = f"spectacle timed out after {_SUBPROCESS_CAPTURE_TIMEOUT_S:g}s{detail}"
        raise RuntimeError(msg) from None

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")
        msg = f"spectacle failed (exit {result.returncode}): {stderr}"
        raise RuntimeError(msg)

    if not output_path.exists() or output_path.stat().st_size == 0:
        msg = "spectacle produced no output"
        raise RuntimeError(msg)
