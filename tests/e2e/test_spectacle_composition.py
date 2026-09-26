"""Spectacle fallback decisions on layouts Spectacle cannot compose (issue #67).

Spectacle before 6.7.90 composes screens of different scales through OpenCV
ROIs on a canvas anchored at logical (0, 0). A screen outside that canvas
aborts Spectacle (native QA: Spectacle 6.3.5 / OpenCV 4.10 exits with SIGABRT
from ``cv::resize`` for Virtual-1 at (-1920, 0) next to a 1.45-scaled
Virtual-0) or is distorted. These tests drive the public capture chain with
ScreenShot2 unavailable and a stand-in ``spectacle`` on ``PATH`` that records
every invocation and produces what the real binary produced for each layout.

What is asserted is kwin-mcp's own behavior: whether it starts a Spectacle
capture at all, and what the caller receives.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from typing import TYPE_CHECKING

import pytest
from PIL import Image

from kwin_mcp import screenshot
from kwin_mcp.screenshot import OutputGeometry, WorkspaceTopology

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# Topologies as KWin 6.3.6 reported them in the native QEMU QA.
NEGATIVE_MIXED = WorkspaceTopology(
    outputs=(
        OutputGeometry("Virtual-0", 0.0, 0.0, 1324.0, 745.0, 1.45),
        OutputGeometry("Virtual-1", -1920.0, 0.0, 1920.0, 1080.0, 1.0),
    ),
    virtual=(-1920, 0, 3244, 1080),
)
NEGATIVE_UNIFORM = WorkspaceTopology(
    outputs=(
        OutputGeometry("Virtual-0", 0.0, 0.0, 1920.0, 1080.0, 1.0),
        OutputGeometry("Virtual-1", -1920.0, 0.0, 1920.0, 1080.0, 1.0),
    ),
    virtual=(-1920, 0, 3840, 1080),
)
MIXED_NONNEGATIVE = WorkspaceTopology(
    outputs=(
        OutputGeometry("Virtual-0", 0.0, 0.0, 1324.0, 745.0, 1.45),
        OutputGeometry("Virtual-1", 1325.0, 0.0, 1920.0, 1080.0, 1.0),
    ),
    virtual=(0, 0, 3245, 1080),
)
SINGLE = WorkspaceTopology(
    outputs=(OutputGeometry("Virtual-0", 0.0, 0.0, 1920.0, 1080.0, 1.0),),
    virtual=(0, 0, 1920, 1080),
)

# Stand-in for the spectacle binary. ``--version`` prints the configured
# version; a capture either aborts like Spectacle 6.3.5 did on the negative
# mixed layout, or writes a canvas of the configured device size whose listed
# device rectangles are painted (the rest stays transparent, as Spectacle
# leaves clipped screens).
_FAKE_SPECTACLE = """\
import json, os, sys
with open(os.environ["FAKE_SPECTACLE_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["--version"]:
    print("spectacle " + os.environ["FAKE_SPECTACLE_VERSION"])
    sys.exit(0)
canvas = json.loads(os.environ["FAKE_SPECTACLE_CANVAS"])
if canvas is None:
    sys.stderr.write(
        "terminate called after throwing an instance of 'cv::Exception'\\n"
        "  what():  OpenCV(4.10.0) ./modules/imgproc/src/resize.cpp:4155: error: "
        "(-215:Assertion failed) inv_scale_x > 0 in function 'resize'\\n"
    )
    sys.stderr.flush()
    os.abort()
from PIL import Image, ImageDraw
image = Image.new("RGBA", tuple(canvas["size"]), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
for x, y, w, h in canvas["drawn"]:
    draw.rectangle((x, y, x + w - 1, y + h - 1), fill=(31, 95, 159, 255))
image.save(sys.argv[sys.argv.index("-o") + 1], "PNG")
"""


class _Spectacle:
    """Recording stand-in ``spectacle`` installed first on ``PATH``."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        executable = bin_dir / "spectacle"
        executable.write_text(f"#!{sys.executable}\n{_FAKE_SPECTACLE}")
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        self._log = tmp_path / "spectacle-invocations.jsonl"
        self._log.touch()
        self._monkeypatch = monkeypatch
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        monkeypatch.setenv("FAKE_SPECTACLE_LOG", str(self._log))

    def configure(
        self,
        version: str,
        canvas: tuple[int, int] | None,
        drawn: list[tuple[int, int, int, int]] | None = None,
    ) -> None:
        self._monkeypatch.setenv("FAKE_SPECTACLE_VERSION", version)
        payload = None if canvas is None else {"size": canvas, "drawn": drawn or []}
        self._monkeypatch.setenv("FAKE_SPECTACLE_CANVAS", json.dumps(payload))

    def _calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self._log.read_text().splitlines()]

    def captures(self) -> list[list[str]]:
        """Argument lists of every started capture (``--version`` probes excluded)."""
        return [argv for argv in self._calls() if argv != ["--version"]]

    def version_probes(self) -> int:
        """Number of ``spectacle --version`` runs."""
        return sum(argv == ["--version"] for argv in self._calls())


@pytest.fixture
def spectacle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Spectacle:
    """ScreenShot2 unavailable, X11 disabled, and a recording Spectacle stand-in."""
    monkeypatch.delenv("KWIN_MCP_X11_SCREENSHOT", raising=False)

    def screenshot2_unavailable(*_args: object, **_kwargs: object) -> None:
        msg = "ScreenShot2 unavailable in this test"
        raise RuntimeError(msg)

    monkeypatch.setattr(screenshot, "capture_screenshot_dbus", screenshot2_unavailable)
    monkeypatch.setattr(screenshot, "_capture_frame_burst_dbus", screenshot2_unavailable)
    return _Spectacle(tmp_path, monkeypatch)


def _use_topology(monkeypatch: pytest.MonkeyPatch, topology: WorkspaceTopology) -> None:
    monkeypatch.setattr(screenshot, "query_topology", lambda _dbus_address: topology)


def _screenshot(output_dir: Path) -> screenshot.FrameMapping:
    _, mapping = screenshot.capture_screenshot_to_file("", "", output_dir=output_dir)
    return mapping


def _burst(output_dir: Path) -> screenshot.FrameMapping:
    frames = screenshot.capture_frame_burst("", output_dir, [0], wayland_socket="")
    assert len(frames) == 1, frames
    return frames[0][1]


CAPTURES: dict[str, Callable[[Path], screenshot.FrameMapping]] = {
    "screenshot": _screenshot,
    "burst": _burst,
}


@pytest.mark.parametrize("capture", sorted(CAPTURES))
# 24.x are Gear-numbered releases (Spectacle moved to Plasma numbering with 6.3);
# they all predate the QPainter compositor even though 24 > 6.
@pytest.mark.parametrize(
    "version", ["6.3.5", "6.4.3", "6.7.5", "24.08.3", "24.12.3", "not-a-version"]
)
def test_negative_mixed_scale_layout_is_refused_before_spectacle_starts(
    spectacle: _Spectacle,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capture: str,
    version: str,
) -> None:
    """A layout that aborts OpenCV-composing Spectacle never starts a capture.

    Every Spectacle before 6.7.90, and one whose version cannot be read, is
    treated as the OpenCV compositor. The caller gets a capture error naming
    both backends and the output Spectacle cannot place, and no PNG is left.
    """
    _use_topology(monkeypatch, NEGATIVE_MIXED)
    spectacle.configure(version, canvas=None)
    output_dir = tmp_path / "out"

    with pytest.raises(RuntimeError) as failure:
        CAPTURES[capture](output_dir)

    assert spectacle.captures() == [], spectacle.captures()
    message = str(failure.value)
    assert "ScreenShot2" in message, message
    assert "Spectacle" in message, message
    assert "Virtual-1" in message, message
    assert not list(output_dir.glob("*.png"))


@pytest.mark.parametrize("capture", sorted(CAPTURES))
def test_qpainter_spectacle_still_captures_negative_mixed_layout(
    spectacle: _Spectacle,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capture: str,
) -> None:
    """From 6.7.90 Spectacle clips such screens cleanly; the capture proceeds as partial."""
    _use_topology(monkeypatch, NEGATIVE_MIXED)
    # canvas = round(3244) * ceil(1.45); Virtual-0 drawn at its logical origin * 2.
    spectacle.configure("6.7.90", canvas=(6488, 2160), drawn=[(0, 0, 2648, 1490)])

    mapping = CAPTURES[capture](tmp_path / "out")

    assert len(spectacle.captures()) == 1, spectacle.captures()
    assert mapping.backend == "spectacle", mapping
    assert (mapping.origin, mapping.size) == ((-1920, 0), (3244, 1080)), mapping
    assert mapping.coverage == "partial", mapping
    assert mapping.captured == ((0, 0, 1324, 745),), mapping


def test_burst_probes_the_spectacle_version_once(
    spectacle: _Spectacle,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A frame burst on a guarded layout reads the version once, not per frame."""
    _use_topology(monkeypatch, NEGATIVE_MIXED)
    spectacle.configure("6.7.90", canvas=(6488, 2160), drawn=[(0, 0, 2648, 1490)])

    frames = screenshot.capture_frame_burst("", tmp_path / "out", [0, 10, 20], wayland_socket="")

    assert len(frames) == 3, frames
    assert len(spectacle.captures()) == 3, spectacle.captures()
    assert spectacle.version_probes() == 1, spectacle.version_probes()


@pytest.mark.parametrize("capture", sorted(CAPTURES))
@pytest.mark.parametrize(
    ("topology", "canvas", "drawn", "coverage", "captured"),
    [
        pytest.param(
            MIXED_NONNEGATIVE,
            (6490, 2160),
            [(0, 0, 2648, 1490), (2650, 0, 3840, 2160)],
            "full",
            (),
            id="mixed-nonnegative",
        ),
        pytest.param(
            NEGATIVE_UNIFORM,
            (3840, 1080),
            [(0, 0, 1920, 1080)],
            "partial",
            ((0, 0, 1920, 1080),),
            id="negative-uniform",
        ),
        pytest.param(
            SINGLE,
            (1920, 1080),
            [(0, 0, 1920, 1080)],
            "full",
            (),
            id="single-output",
        ),
    ],
)
def test_layouts_spectacle_composes_are_still_captured(
    spectacle: _Spectacle,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capture: str,
    topology: WorkspaceTopology,
    canvas: tuple[int, int],
    drawn: list[tuple[int, int, int, int]],
    coverage: str,
    captured: tuple[tuple[int, int, int, int], ...],
) -> None:
    """Layouts Spectacle 6.3.5 composes (native QA controls) keep using it unchanged."""
    _use_topology(monkeypatch, topology)
    spectacle.configure("6.3.5", canvas=canvas, drawn=drawn)

    mapping = CAPTURES[capture](tmp_path / "out")

    assert len(spectacle.captures()) == 1, spectacle.captures()
    assert mapping.backend == "spectacle", mapping
    assert mapping.origin == topology.virtual[:2], mapping
    assert mapping.size == topology.virtual[2:], mapping
    assert mapping.coverage == coverage, mapping
    if coverage == "partial":
        assert mapping.captured == captured, mapping
    with Image.open(next((tmp_path / "out").glob("*.png"))) as image:
        assert image.size == topology.virtual[2:], image.size
