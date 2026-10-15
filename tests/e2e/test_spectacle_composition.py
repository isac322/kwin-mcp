"""Spectacle fallback decisions on layouts Spectacle cannot compose (issue #67).

Spectacle before 6.7.90 composes screens of different scales through OpenCV
ROIs on a canvas anchored at logical (0, 0). A screen outside that canvas
aborts Spectacle (native QA: Spectacle 6.3.5 / OpenCV 4.10 exits with SIGABRT
from ``cv::resize`` for Virtual-1 at (-1920, 0) next to a 1.45-scaled
Virtual-0) or is distorted. These tests drive ``screen_capture`` and an action
tool's ``capture`` option in a virtual session with ScreenShot2 unavailable and
a stand-in ``spectacle`` on ``PATH`` that records every invocation and
produces what the real binary produced for each layout.

What is asserted is kwin-mcp's own behavior: whether it starts a Spectacle
capture at all, and what the caller receives.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from _asserts import frame_png
from PIL import Image

from kwin_mcp import screenshot
from kwin_mcp.models import Capture, Point
from kwin_mcp.screenshot import OutputGeometry, WorkspaceTopology

if TYPE_CHECKING:
    from collections.abc import Callable

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import Frame, SessionInfo

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

# Where the burst's carrier action (pointer_move) puts the pointer.
_BURST_TARGET = Point(x=10, y=10)
_BURST_WARNING_PREFIX = "frame capture failed after the action: "

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


def _disable_screenshot2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every ScreenShot2 request fail so the chain falls back to Spectacle."""
    monkeypatch.delenv("KWIN_MCP_X11_SCREENSHOT", raising=False)

    def screenshot2_unavailable(*_args: object, **_kwargs: object) -> None:
        msg = "ScreenShot2 unavailable in this test"
        raise RuntimeError(msg)

    monkeypatch.setattr(screenshot, "capture_screenshot_dbus", screenshot2_unavailable)
    monkeypatch.setattr(screenshot, "_capture_frame_burst_dbus", screenshot2_unavailable)


@pytest.fixture
def spectacle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Spectacle:
    """ScreenShot2 unavailable, X11 disabled, and a recording Spectacle stand-in."""
    _disable_screenshot2(monkeypatch)
    return _Spectacle(tmp_path, monkeypatch)


@pytest.fixture
def session(spectacle: _Spectacle, start_session: Callable[..., SessionInfo]) -> SessionInfo:
    """A virtual session started after the stand-in is first on ``PATH``."""
    del spectacle
    return start_session()


def _use_topology(monkeypatch: pytest.MonkeyPatch, topology: WorkspaceTopology) -> None:
    monkeypatch.setattr(screenshot, "query_topology", lambda _dbus_address: topology)


def _pngs(session: SessionInfo) -> list[Path]:
    """Every PNG in the session's screenshot directory, private attempts included."""
    return sorted(Path(session.artifacts_dir).rglob("*.png"))


def _screen_capture(engine: AutomationEngine) -> Frame:
    return engine.screen_capture(max_edge=0)


def _burst(engine: AutomationEngine) -> Frame:
    result = engine.pointer_move(_BURST_TARGET, capture=Capture(delays_ms=[0], max_edge=0))
    assert result.warnings == [], result.warnings
    assert len(result.frames) == 1, result.frames
    assert result.frames[0].delay_ms == 0, result.frames
    return result.frames[0]


def _screen_capture_failure(engine: AutomationEngine) -> str:
    with pytest.raises(RuntimeError) as failure:
        engine.screen_capture(max_edge=0)
    return str(failure.value)


def _burst_failure(engine: AutomationEngine) -> str:
    # The action committed, so a capture failure is a warning, not an error.
    result = engine.pointer_move(_BURST_TARGET, capture=Capture(delays_ms=[0], max_edge=0))
    assert result.at == _BURST_TARGET, result
    assert result.frames == [], result.frames
    assert len(result.warnings) == 1, result.warnings
    assert result.warnings[0].startswith(_BURST_WARNING_PREFIX), result.warnings
    return result.warnings[0]


CAPTURES: dict[str, Callable[[AutomationEngine], Frame]] = {
    "screenshot": _screen_capture,
    "burst": _burst,
}
FAILURES: dict[str, Callable[[AutomationEngine], str]] = {
    "screenshot": _screen_capture_failure,
    "burst": _burst_failure,
}


def _assert_spectacle_frame(
    frame: Frame,
    *,
    origin: tuple[int, int],
    size: tuple[int, int],
    coverage: str,
    captured: tuple[tuple[int, int, int, int], ...],
) -> None:
    """The frame is Spectacle's, mapped 1:1 onto ``origin``/``size`` with ``coverage``."""
    space = frame.coordinate_space
    expected_space = (
        f"Coordinate space: logical; origin ({origin[0]}, {origin[1]}); "
        f"size {size[0]}x{size[1]}; backend spectacle"
    )
    assert space.startswith(expected_space), space
    assert f"; coverage {coverage}" in space, space
    assert frame.coverage == coverage, frame
    assert frame.mapping is not None, frame
    assert (frame.mapping.origin.x, frame.mapping.origin.y) == origin, frame
    assert frame.mapping.scale == 1.0, frame
    assert (frame.width, frame.height) == size, frame
    if coverage == "partial":
        regions = ", ".join(f"({x}, {y}, {w}x{h})" for x, y, w, h in captured)
        assert f"(captured {regions}; other pixels are transparent)" in space, space
    with Image.open(frame_png(frame)) as image:
        assert image.size == size, image.size


@pytest.mark.parametrize("capture", sorted(FAILURES))
# 24.x are Gear-numbered releases (Spectacle moved to Plasma numbering with 6.3);
# they all predate the QPainter compositor even though 24 > 6.
@pytest.mark.parametrize(
    "version", ["6.3.5", "6.4.3", "6.7.5", "24.08.3", "24.12.3", "not-a-version"]
)
def test_negative_mixed_scale_layout_is_refused_before_spectacle_starts(
    spectacle: _Spectacle,
    session: SessionInfo,
    engine: AutomationEngine,
    monkeypatch: pytest.MonkeyPatch,
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

    message = FAILURES[capture](engine)

    assert spectacle.captures() == [], spectacle.captures()
    assert "ScreenShot2" in message, message
    assert "Spectacle" in message, message
    assert "Virtual-1" in message, message
    assert _pngs(session) == [], _pngs(session)


@pytest.mark.parametrize("capture", sorted(CAPTURES))
def test_qpainter_spectacle_still_captures_negative_mixed_layout(
    spectacle: _Spectacle,
    session: SessionInfo,
    engine: AutomationEngine,
    monkeypatch: pytest.MonkeyPatch,
    capture: str,
) -> None:
    """From 6.7.90 Spectacle clips such screens cleanly; the capture proceeds as partial."""
    del session
    _use_topology(monkeypatch, NEGATIVE_MIXED)
    # canvas = round(3244) * ceil(1.45); Virtual-0 drawn at its logical origin * 2.
    spectacle.configure("6.7.90", canvas=(6488, 2160), drawn=[(0, 0, 2648, 1490)])

    frame = CAPTURES[capture](engine)

    assert len(spectacle.captures()) == 1, spectacle.captures()
    _assert_spectacle_frame(
        frame,
        origin=(-1920, 0),
        size=(3244, 1080),
        coverage="partial",
        captured=((0, 0, 1324, 745),),
    )


def test_burst_probes_the_spectacle_version_once(
    spectacle: _Spectacle,
    session: SessionInfo,
    engine: AutomationEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A frame burst on a guarded layout reads the version once, not per frame."""
    del session
    _use_topology(monkeypatch, NEGATIVE_MIXED)
    spectacle.configure("6.7.90", canvas=(6488, 2160), drawn=[(0, 0, 2648, 1490)])

    result = engine.pointer_move(_BURST_TARGET, capture=Capture(delays_ms=[0, 10, 20], max_edge=0))

    assert result.warnings == [], result.warnings
    assert [frame.delay_ms for frame in result.frames] == [0, 10, 20], result.frames
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
    session: SessionInfo,
    engine: AutomationEngine,
    monkeypatch: pytest.MonkeyPatch,
    capture: str,
    topology: WorkspaceTopology,
    canvas: tuple[int, int],
    drawn: list[tuple[int, int, int, int]],
    coverage: str,
    captured: tuple[tuple[int, int, int, int], ...],
) -> None:
    """Layouts Spectacle 6.3.5 composes (native QA controls) keep using it unchanged."""
    del session
    _use_topology(monkeypatch, topology)
    spectacle.configure("6.3.5", canvas=canvas, drawn=drawn)

    frame = CAPTURES[capture](engine)

    assert len(spectacle.captures()) == 1, spectacle.captures()
    x, y, width, height = topology.virtual
    _assert_spectacle_frame(
        frame, origin=(x, y), size=(width, height), coverage=coverage, captured=captured
    )


@pytest.mark.qa("QA-CAP-005")
def test_spectacle_fallback_when_screenshot2_fails(
    spectacle: _Spectacle,
    session: SessionInfo,
    engine: AutomationEngine,
    screen_size: tuple[int, int],
) -> None:
    """With ScreenShot2 failing, screen_capture spawns Spectacle and returns its frame.

    The session's real output topology is used: the only stand-ins are the
    failing ScreenShot2 request and the recording ``spectacle`` binary, whose
    invocation log is the process-table oracle.
    """
    width, height = screen_size
    spectacle.configure("6.3.5", canvas=(width, height), drawn=[(0, 0, width, height)])

    frame = engine.screen_capture(max_edge=0)

    captures = spectacle.captures()
    assert len(captures) == 1, captures
    argv = captures[0]
    assert argv[:4] == ["-b", "-f", "-n", "-o"], argv
    # Spectacle writes into a private attempt directory inside the session's
    # screenshot directory; only the finished capture is published.
    assert Path(argv[4]).parent.parent == Path(session.artifacts_dir), argv
    _assert_spectacle_frame(
        frame, origin=(0, 0), size=(width, height), coverage="full", captured=()
    )
    text = frame.render()
    assert text.splitlines()[0] == f"Screenshot saved: {frame.path} ({frame.size_kb:.1f} KB)", text
    assert "; backend spectacle" in text, text
