"""Cropping, downscaling and the backend chain of saved captures, without a KWin session.

``reframe_screenshot`` rewrites a capture in place and returns its mapping.
These tests build synthetic PNGs whose pixels encode their logical position,
so a wrong crop offset or scale factor shows up as a wrong colour at a
mapped pixel rather than only as a wrong size. The backend tests replace
ScreenShot2, Spectacle and scrot at their process/D-Bus seams and drive
``screen_capture`` through the in-process server.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image, ImageDraw

from kwin_mcp import screenshot, server
from kwin_mcp.core import AutomationEngine
from kwin_mcp.screenshot import (
    FrameMapping,
    OutputGeometry,
    WorkspaceTopology,
    reframe_screenshot,
)
from kwin_mcp.server import KwinServer, ServerFlags

if TYPE_CHECKING:
    from pathlib import Path

    from ._fakes import FakeEngine

MARKER = (255, 0, 0)
BACKGROUND = (0, 0, 64)
_IMAGE_TERM = re.compile(
    r"image (\d+)x(\d+) downscaled: pixel \(px, py\) shows "
    r"\((-?\d+) \+ \(px \+ 0\.5\) \* (\d+) / (\d+) - 0\.5, "
    r"(-?\d+) \+ \(py \+ 0\.5\) \* (\d+) / (\d+) - 0\.5\)"
)


def _mapping(
    origin: tuple[int, int],
    size: tuple[int, int],
    *,
    captured: tuple[tuple[int, int, int, int], ...] = (),
) -> FrameMapping:
    return FrameMapping(
        backend="kwin-screenshot2",
        origin=origin,
        size=size,
        coverage="partial" if captured else "full",
        captured=captured,
    )


def _capture(
    tmp_path: Path,
    size: tuple[int, int],
    marker: tuple[int, int, int, int] | None = None,
) -> Path:
    """Save a capture with a solid marker rectangle at image pixels ``marker``."""
    image = Image.new("RGB", size, BACKGROUND)
    if marker is not None:
        x, y, width, height = marker
        image.paste(MARKER, (x, y, x + width, y + height))
    path = tmp_path / "screenshot.png"
    image.save(path)
    return path


def _logical_point(line: str, pixel: tuple[float, float]) -> tuple[float, float]:
    """Apply the formula a downscaled coordinate line states to an image pixel."""
    match = _IMAGE_TERM.search(line)
    assert match is not None, line
    _, _, ox, width, iw, oy, height, ih = (int(group) for group in match.groups())
    return (
        ox + (pixel[0] + 0.5) * width / iw - 0.5,
        oy + (pixel[1] + 0.5) * height / ih - 0.5,
    )


def test_downscaled_mapping_lands_on_a_narrow_stripe(tmp_path: Path) -> None:
    """The stated point is the scaled pixel's source center, not its left edge.

    At 12 logical pixels per image pixel, mapping an image pixel to its left
    edge lands 5.5 px left of what it shows: outside this 4 px stripe.
    """
    stripe = (3004, 3008)
    image = Image.new("RGB", (7680, 1440), (0, 0, 0))
    ImageDraw.Draw(image).rectangle((stripe[0], 0, stripe[1] - 1, 1439), fill=(255, 255, 255))
    path = tmp_path / "screenshot.png"
    image.save(path)

    mapping = reframe_screenshot(path, _mapping((0, 0), (7680, 1440)), max_edge=640)

    assert mapping.image == (640, 120)
    with Image.open(path) as scaled:
        # One byte per pixel in mode "L": the brightness of each scaled column.
        levels = scaled.convert("L").crop((0, 60, 640, 61)).tobytes()
    brightest = max(range(640), key=levels.__getitem__)
    logical_x, _ = _logical_point(mapping.describe(), (brightest, 60))
    assert stripe[0] <= logical_x < stripe[1], (brightest, logical_x)


def _alpha_outside(path: Path, keep: tuple[int, int, int, int]) -> list[tuple[int, int]]:
    """Pixels outside the ``keep`` box (left, top, right, bottom) that are not transparent."""
    with Image.open(path) as image:
        alpha = image.convert("RGBA").getchannel("A")
    left, top, right, bottom = keep
    return [
        (px, py)
        for py in range(alpha.height)
        for px in range(alpha.width)
        if not (left <= px < right and top <= py < bottom) and alpha.getpixel((px, py))
    ]


def test_partial_downscale_keeps_uncaptured_pixels_transparent(tmp_path: Path) -> None:
    """Lanczos must not leak captured alpha past the reported captured region."""
    image = Image.new("RGBA", (2000, 1000), (0, 0, 0, 0))
    image.paste((255, 0, 0, 255), (0, 0, 1000, 1000))
    path = tmp_path / "screenshot.png"
    image.save(path)
    captured = ((0, 0, 1000, 1000),)

    mapping = reframe_screenshot(
        path, _mapping((0, 0), (2000, 1000), captured=captured), max_edge=200
    )

    assert (mapping.image, mapping.captured) == ((200, 100), captured)
    # Columns whose source center (px + 0.5) * 10 - 0.5 lies in [0, 1000).
    assert _alpha_outside(path, (0, 0, 100, 100)) == []
    with Image.open(path) as scaled:
        assert scaled.convert("RGBA").getpixel((50, 50)) == (255, 0, 0, 255)


def test_crop_then_downscale_masks_at_a_fractional_boundary(tmp_path: Path) -> None:
    # Workspace at x -500; captured logical x -500..500 is the opaque left part.
    image = Image.new("RGBA", (2000, 1000), (0, 0, 0, 0))
    image.paste((255, 0, 0, 255), (0, 0, 1000, 1000))
    path = tmp_path / "screenshot.png"
    image.save(path)
    captured = ((-500, 0, 1000, 1000),)

    mapping = reframe_screenshot(
        path,
        _mapping((-500, 0), (2000, 1000), captured=captured),
        region=(0, 0, 1400, 1000),
        max_edge=300,
    )

    assert (mapping.origin, mapping.size, mapping.image) == ((0, 0), (1400, 1000), (300, 214))
    assert mapping.captured == ((0, 0, 500, 1000),)
    # fx = 1400 / 300: the captured span ends at ceil(500.5 / fx - 0.5) = 107,
    # between scaled pixels rather than on a whole-pixel multiple.
    assert _alpha_outside(path, (0, 0, 107, 214)) == []
    with Image.open(path) as scaled:
        assert scaled.convert("RGBA").getpixel((50, 100)) == (255, 0, 0, 255)


def test_ultrawide_downscale_states_a_mapping_that_lands_on_the_marker(tmp_path: Path) -> None:
    """A 7680x1440 desktop bounded to 2576 px maps image pixels back to logical points."""
    path = _capture(tmp_path, (7680, 1440), marker=(5000, 600, 240, 120))

    mapping = reframe_screenshot(path, _mapping((0, 0), (7680, 1440)), max_edge=2576)

    assert (mapping.origin, mapping.size, mapping.image) == ((0, 0), (7680, 1440), (2576, 483))
    with Image.open(path) as image:
        assert image.size == (2576, 483)
        scaled = image.convert("RGB")
    # Marker centre (5120, 660) in logical pixels, scaled by 2576/7680 and 483/1440.
    center = (round(5120 * 2576 / 7680), round(660 * 483 / 1440))
    assert scaled.getpixel(center) == MARKER
    line = mapping.describe()
    logical = _logical_point(line, center)
    assert 5000 <= logical[0] < 5240, (line, logical)
    assert 600 <= logical[1] < 720, (line, logical)
    # The logical part of the line is unchanged: callers that only parse it
    # still read the logical origin and size.
    assert line.startswith("Coordinate space: logical; origin (0, 0); size 7680x1440; "), line


def test_image_within_max_edge_is_left_byte_identical(tmp_path: Path) -> None:
    # Uncompressed, so re-encoding with Pillow's defaults would change the bytes.
    path = tmp_path / "screenshot.png"
    Image.new("RGB", (1280, 800), BACKGROUND).save(path, compress_level=0)
    before = path.read_bytes()
    original = _mapping((0, 0), (1280, 800))

    assert reframe_screenshot(path, original, max_edge=1280) is original
    assert reframe_screenshot(path, original, max_edge=0) is original
    assert path.read_bytes() == before
    assert "image" not in original.describe()


def test_region_at_negative_origin_is_clipped_to_the_workspace(tmp_path: Path) -> None:
    """Pixel (0, 0) of a crop shows the crop's reported logical origin."""
    # Workspace spans logical x -1280..2560; logical (-1280, 100) is image (0, 100).
    path = _capture(tmp_path, (3840, 1080), marker=(0, 100, 10, 10))

    mapping = reframe_screenshot(
        path, _mapping((-1280, 0), (3840, 1080)), region=(-1300, 100, 400, 300)
    )

    assert (mapping.origin, mapping.size, mapping.image) == ((-1280, 100), (380, 300), None)
    with Image.open(path) as image:
        assert image.size == (380, 300)
        cropped = image.convert("RGB")
    assert cropped.getpixel((0, 0)) == MARKER
    assert cropped.getpixel((10, 10)) == BACKGROUND


def test_region_then_downscale_maps_through_the_crop_origin(tmp_path: Path) -> None:
    path = _capture(tmp_path, (3840, 1080), marker=(2000, 500, 200, 100))

    mapping = reframe_screenshot(
        path, _mapping((-1280, 0), (3840, 1080)), region=(0, 0, 2560, 1080), max_edge=1280
    )

    assert (mapping.origin, mapping.size, mapping.image) == ((0, 0), (2560, 1080), (1280, 540))
    # The marker spans logical x 720..920, y 500..600: image (360..460, 250..300).
    logical = _logical_point(mapping.describe(), (410, 275))
    assert 720 <= logical[0] < 920
    assert 500 <= logical[1] < 600
    with Image.open(path) as image:
        assert image.convert("RGB").getpixel((410, 275)) == MARKER


def test_crop_keeps_only_the_captured_regions_inside_it(tmp_path: Path) -> None:
    path = _capture(tmp_path, (3840, 1080))
    captured = ((0, 0, 1920, 1080), (1920, 0, 1920, 540))

    mapping = reframe_screenshot(
        path, _mapping((0, 0), (3840, 1080), captured=captured), region=(1800, 500, 300, 100)
    )

    assert mapping.coverage == "partial"
    assert mapping.captured == ((1800, 500, 120, 100), (1920, 500, 180, 40))
    assert "(captured (1800, 500, 120x100), (1920, 500, 180x40);" in mapping.describe()


def test_region_outside_the_workspace_is_refused_and_the_file_kept(tmp_path: Path) -> None:
    path = _capture(tmp_path, (1280, 800))
    before = path.read_bytes()

    with pytest.raises(ValueError, match=r"outside the captured workspace \(0, 0, 1280x800\)"):
        reframe_screenshot(path, _mapping((0, 0), (1280, 800)), region=(1280, 0, 100, 100))

    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["screenshot.png"]


def test_unproven_mapping_refuses_a_region_but_still_downscales(tmp_path: Path) -> None:
    unavailable = FrameMapping(
        backend="spectacle", origin=None, size=None, reason="topology changed"
    )
    path = _capture(tmp_path, (4000, 1000))

    with pytest.raises(
        RuntimeError, match="Cannot crop to a region: Coordinate space: unavailable"
    ):
        reframe_screenshot(path, unavailable, region=(0, 0, 10, 10))

    mapping = reframe_screenshot(path, unavailable, max_edge=1000)
    assert mapping.image == (1000, 250)
    assert mapping.describe() == (
        "Coordinate space: unavailable (topology changed); backend spectacle; "
        "image downscaled to 1000x250"
    )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], 0),
        (["--screenshot-max-edge", "1568"], 1568),
        (["--screenshot-images", "--screenshot-max-edge=2576"], 2576),
        (["--screenshot-max-edge=0"], 0),
    ],
)
def test_max_edge_flag_is_parsed(argv: list[str], expected: int) -> None:
    assert server._parse_argv(argv).screenshot_max_edge == expected


def _assert_usage_error(argv: list[str], message: str, capsys: pytest.CaptureFixture[str]) -> None:
    """``_parse_argv`` exits 2 with argparse's usage line and ``message`` on stderr."""
    with pytest.raises(SystemExit) as exc_info:
        server._parse_argv(argv)
    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert stderr.startswith("usage: kwin-mcp "), stderr
    assert "kwin-mcp: error: " in stderr, stderr
    assert message in stderr, stderr
    assert "Traceback" not in stderr, stderr


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--screenshot-max-edge"], "expected one argument"),
        (["--screenshot-max-edge", "--screenshot-images"], "expected one argument"),
        (["--screenshot-max-edge", "1568", "--screenshot-max-edge"], "expected one argument"),
        (["--screenshot-max-edge=-1"], "needs a pixel count"),
        (["--screenshot-max-edge=wide"], "needs a pixel count"),
        (["--screenshot-max-edge", "1568", "--screenshot-max-edge=wide"], "needs a pixel count"),
        # Non-ASCII digits, superscript two and Arabic-Indic three: str.isdigit() accepts
        # both, and int() reads the second as 3.
        (["--screenshot-max-edge", "\u00b2"], "needs a pixel count (0 = no limit), got '\u00b2'"),
        (["--screenshot-max-edge", "\u0663"], "needs a pixel count (0 = no limit), got '\u0663'"),
    ],
)
def test_invalid_max_edge_flag_stops_the_server(
    argv: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _assert_usage_error(argv, message, capsys)


def _sessionless_error(arguments: dict[str, Any]) -> str:
    """``screen_capture`` through the in-process server of an engine with no session."""
    tool_server = KwinServer(AutomationEngine(), ServerFlags())
    with pytest.raises(ToolError) as exc_info:
        asyncio.run(tool_server.call_tool("screen_capture", arguments))
    return str(exc_info.value)


@pytest.mark.parametrize(
    ("region", "max_edge", "message"),
    [
        # A region is a Rect: width/height must be positive and all four fields present.
        pytest.param(
            [0, 0, 0, 10],
            None,
            r"region\.width\n  Input should be greater than 0",
            id="region0-None-region must be",
        ),
        pytest.param(
            [0, 0, 10],
            None,
            r"region\.height\n  Field required",
            id="region1-None-region must be",
        ),
        pytest.param(
            None, -1, "max_edge must be 0 or positive", id="None--1-max_edge must be 0 or positive"
        ),
    ],
)
def test_engine_rejects_bad_sizes_before_needing_a_session(
    region: list[int] | None, max_edge: int | None, message: str
) -> None:
    if region is None:
        # The engine checks max_edge before it looks for a session.
        with pytest.raises(ValueError, match=message):
            AutomationEngine().screen_capture(max_edge=max_edge)
        return
    keys = ("x", "y", "width", "height")
    text = _sessionless_error({"region": dict(zip(keys, region, strict=False))})
    assert re.search(message, text), text
    assert "session" not in text.lower(), text


def _fake_backend(monkeypatch: pytest.MonkeyPatch, *, fail_after_save: bool) -> list[Path]:
    """Make ScreenShot2 save a PNG to the requested path, then succeed or fail."""
    saved: list[Path] = []

    def capture(_address: str, path: Path, *, include_cursor: bool) -> tuple[Path, FrameMapping]:
        Image.new("RGB", (64, 32), BACKGROUND).save(path)
        saved.append(path)
        if fail_after_save:
            # As when the topology observation after a capture fails.
            raise RuntimeError("KWin query failed after capture")
        return path, _mapping((0, 0), (64, 32))

    def spectacle(*_args: object, **_kwargs: object) -> FrameMapping:
        raise RuntimeError("Spectacle unavailable")

    monkeypatch.delenv("KWIN_MCP_X11_SCREENSHOT", raising=False)
    monkeypatch.setattr(screenshot, "capture_screenshot_dbus", capture)
    monkeypatch.setattr(screenshot, "_capture_spectacle_file", spectacle)
    monkeypatch.setattr(screenshot.time, "strftime", lambda _fmt: "20261009_023307")
    return saved


def test_captures_in_the_same_second_keep_separate_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_backend(monkeypatch, fail_after_save=False)

    first, _ = screenshot.capture_screenshot_to_file(output_dir=tmp_path)
    second, _ = screenshot.capture_screenshot_to_file(output_dir=tmp_path)

    assert (first.name, second.name) == (
        "screenshot_20261009_023307.png",
        "screenshot_20261009_023307_1.png",
    )
    assert first.is_file() and second.is_file()


def test_a_capture_that_fails_after_saving_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_backend(monkeypatch, fail_after_save=False)
    earlier, _ = screenshot.capture_screenshot_to_file(output_dir=tmp_path)
    failing = _fake_backend(monkeypatch, fail_after_save=True)

    with pytest.raises(RuntimeError, match="KWin query failed after capture"):
        screenshot.capture_screenshot_to_file(output_dir=tmp_path)

    # The failed capture wrote only into its private directory, which is gone;
    # the earlier one is intact.
    assert len(failing) == 1 and failing[0].parent.parent == tmp_path
    assert not failing[0].parent.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == [earlier.name]


@pytest.mark.parametrize("fail_after_save", [True, False], ids=["fails", "succeeds"])
def test_a_name_taken_during_the_capture_is_never_overwritten_or_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_after_save: bool
) -> None:
    """Another caller publishes this second's name while our capture runs.

    Choosing the name before capturing and writing the backend's image straight
    to it would overwrite that caller's file, and deleting it on failure would
    remove it; only publishing a finished capture to a free name avoids both.
    """
    _fake_backend(monkeypatch, fail_after_save=False)
    other = tmp_path / "screenshot_20261009_023307.png"
    other_bytes = b"another caller's PNG"

    def capture(_address: str, path: Path, *, include_cursor: bool) -> tuple[Path, FrameMapping]:
        other.write_bytes(other_bytes)
        Image.new("RGB", (64, 32), BACKGROUND).save(path)
        if fail_after_save:
            raise RuntimeError("KWin query failed after capture")
        return path, _mapping((0, 0), (64, 32))

    monkeypatch.setattr(screenshot, "capture_screenshot_dbus", capture)

    if fail_after_save:
        with pytest.raises(RuntimeError, match="KWin query failed after capture"):
            screenshot.capture_screenshot_to_file(output_dir=tmp_path)
        assert other.read_bytes() == other_bytes
        assert sorted(path.name for path in tmp_path.iterdir()) == [other.name]
        return

    published, _ = screenshot.capture_screenshot_to_file(output_dir=tmp_path)

    assert published.name == "screenshot_20261009_023307_1.png"
    assert other.read_bytes() == other_bytes
    with Image.open(published) as image:
        assert image.size == (64, 32)
    assert sorted(path.name for path in tmp_path.iterdir()) == [other.name, published.name]


@pytest.mark.parametrize(
    "argv",
    [
        ["--screenshot-max-edge", "1568", "--screenshot-max-edge", "2576"],
        ["--screenshot-max-edge=1568", "--screenshot-max-edge=2576"],
        ["--screenshot-max-edge", "1568", "--screenshot-max-edge=2576"],
    ],
)
def test_repeated_max_edge_flag_stops_the_server(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    _assert_usage_error(argv, "--screenshot-max-edge may only be specified once", capsys)


# ── Backend chain (QA-CAP-006/007/008) ──────────────────────────────────


SCROT_COLOUR = (12, 200, 34)
WORKSPACE_1280 = WorkspaceTopology(
    outputs=(OutputGeometry("Virtual-1", 0, 0, 1280, 800, 1.0),), virtual=(0, 0, 1280, 800)
)


class _Chain:
    """Records which capture backends run, in order; each one succeeds or fails on demand."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, scrot_fails: bool) -> None:
        self.order: list[str] = []
        self.scrot_fails = scrot_fails
        monkeypatch.setattr(screenshot, "_x11_state", lambda *_args: (WORKSPACE_1280, ()))
        monkeypatch.setattr(screenshot, "_capture_via_scrot", self._scrot)
        monkeypatch.setattr(screenshot, "_normalize_x11", self._normalize_x11)
        monkeypatch.setattr(screenshot, "capture_screenshot_dbus", self._screenshot2)
        monkeypatch.setattr(screenshot, "_capture_spectacle_file", self._spectacle)
        monkeypatch.setattr(screenshot, "_capture_frame_burst_dbus", self._screenshot2)
        monkeypatch.setattr(screenshot, "_capture_frame_burst_spectacle", self._spectacle)

    def _scrot(self, output_path: Path, *, include_cursor: bool = False) -> None:
        self.order.append("scrot")
        if self.scrot_fails:
            msg = "scrot failed in this test"
            raise RuntimeError(msg)
        Image.new("RGB", (1280, 800), SCROT_COLOUR).save(output_path)

    @staticmethod
    def _normalize_x11(path: Path, _state: object) -> FrameMapping:
        return FrameMapping(backend="x11-scrot", origin=(0, 0), size=(1280, 800))

    def _screenshot2(self, *_args: object, **_kwargs: object) -> Any:
        self.order.append("screenshot2")
        msg = "ScreenShot2 failed in this test"
        raise RuntimeError(msg)

    def _spectacle(self, *_args: object, **_kwargs: object) -> Any:
        self.order.append("spectacle")
        msg = "Spectacle failed in this test"
        raise RuntimeError(msg)


@pytest.mark.qa("QA-CAP-006")
def test_x11_scrot_backend_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """KWIN_MCP_X11_SCREENSHOT=1 with a DISPLAY tries scrot first, for captures and bursts."""
    monkeypatch.setenv("KWIN_MCP_X11_SCREENSHOT", "1")
    monkeypatch.setenv("DISPLAY", ":99")
    chain = _Chain(monkeypatch, scrot_fails=False)

    path, mapping = screenshot.capture_screenshot_to_file(output_dir=tmp_path)

    assert chain.order == ["scrot"]
    assert mapping.backend == "x11-scrot"
    with Image.open(path) as image:
        assert image.size == (1280, 800)
        assert image.convert("RGB").getpixel((640, 400)) == SCROT_COLOUR

    chain.order.clear()
    frames = screenshot.capture_frame_burst("", tmp_path / "burst", [0, 0])
    assert chain.order == ["scrot", "scrot"]
    assert [(delay, m.backend) for delay, _, m in frames] == [(0, "x11-scrot"), (0, "x11-scrot")]
    for _, frame_path, _ in frames:
        with Image.open(frame_path) as image:
            assert image.convert("RGB").getpixel((0, 0)) == SCROT_COLOUR

    # A failing scrot falls through to ScreenShot2, then Spectacle, and the error
    # names every attempt in that order; scrot is not retried.
    failing = _Chain(monkeypatch, scrot_fails=True)
    with pytest.raises(RuntimeError) as exc_info:
        screenshot.capture_screenshot_to_file(output_dir=tmp_path / "failed")
    assert failing.order == ["scrot", "screenshot2", "spectacle"]
    assert str(exc_info.value) == (
        "Screenshot capture failed: X11/scrot (scrot failed in this test); "
        "ScreenShot2 (ScreenShot2 failed in this test); Spectacle (Spectacle failed in this test)"
    )
    assert list((tmp_path / "failed").iterdir()) == []

    # Without the opt-in, scrot is never tried, even with a DISPLAY.
    monkeypatch.delenv("KWIN_MCP_X11_SCREENSHOT")
    default = _Chain(monkeypatch, scrot_fails=False)
    with pytest.raises(RuntimeError, match=r"^Screenshot capture failed: ScreenShot2 \("):
        screenshot.capture_screenshot_to_file(output_dir=tmp_path / "default")
    assert default.order == ["screenshot2", "spectacle"]


class _Spectacle:
    """ScreenShot2 is unavailable; Spectacle is installed and writes ``image_size`` PNGs."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        topology: WorkspaceTopology,
        *,
        version: tuple[int, int, int] = (6, 3, 5),
        image_size: tuple[int, int] = (1280, 800),
    ) -> None:
        self.started = 0
        self.version_probes = 0
        self.image_size = image_size
        self._version = version

        def screenshot2(*_args: object, **_kwargs: object) -> Any:
            msg = "ScreenShot2 unavailable in this test"
            raise RuntimeError(msg)

        monkeypatch.setattr(screenshot, "capture_screenshot_dbus", screenshot2)
        monkeypatch.setattr(screenshot, "query_topology", lambda _address: topology)
        monkeypatch.setattr(
            screenshot.shutil,
            "which",
            lambda name: "/usr/bin/spectacle" if name == "spectacle" else None,
        )
        monkeypatch.setattr(screenshot, "_spectacle_version", self._probe)
        monkeypatch.setattr(screenshot, "_capture_via_spectacle", self._capture)

    def _probe(self) -> tuple[int, int, int]:
        self.version_probes += 1
        return self._version

    def _capture(self, *_args: object, output_path: Path, **_kwargs: object) -> None:
        self.started += 1
        Image.new("RGB", self.image_size, BACKGROUND).save(output_path)


def _screen_capture_error(fake: FakeEngine, arguments: dict[str, Any]) -> str:
    """``screen_capture`` through the in-process server over the fake live engine."""
    tool_server = KwinServer(fake.engine, ServerFlags())
    with pytest.raises(ToolError) as exc_info:
        asyncio.run(tool_server.call_tool("screen_capture", arguments))
    return str(exc_info.value)


def _published(directory: Path) -> list[str]:
    """Every entry in the screenshot directory, private capture directories included."""
    return sorted(p.name for p in directory.iterdir())


@pytest.mark.qa("QA-CAP-007")
def test_spectacle_mixed_scale_refused(
    fake_engine: FakeEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two outputs of different scales, one left of logical 0: Spectacle is never started."""
    topology = WorkspaceTopology(
        outputs=(
            OutputGeometry("Left", -1280, 0, 1280, 800, 1.0),
            OutputGeometry("Right", 0, 0, 1280, 800, 2.0),
        ),
        virtual=(-1280, 0, 2560, 800),
    )
    spectacle = _Spectacle(monkeypatch, topology)

    text = _screen_capture_error(fake_engine, {})

    assert (
        "Screenshot capture failed: ScreenShot2 (ScreenShot2 unavailable in this test); "
        "Spectacle (Spectacle 6.3.5 cannot capture this layout: mixed output scales with "
        "Left (-1280, 0, 1280x800) outside its canvas anchored at logical (0, 0); "
        "Spectacle's OpenCV compositor aborts on or distorts such outputs; "
        "Spectacle was not started)"
    ) in text, text
    assert spectacle.started == 0
    assert spectacle.version_probes >= 1
    assert _published(tmp_path) == []


@pytest.mark.qa("QA-CAP-008")
def test_region_with_unproven_mapping_is_error(
    fake_engine: FakeEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A region is never cut from an image whose logical mapping is not proven.

    Mixed scales 1 and 2 compose into a 5120x1600 Spectacle canvas. An image
    of any other size (here the 2560x800 a scale-1 assumption would expect)
    proves no mapping: the call fails and no Frame or file is left behind.
    """
    topology = WorkspaceTopology(
        outputs=(
            OutputGeometry("Left", 0, 0, 1280, 800, 1.0),
            OutputGeometry("Right", 1280, 0, 1280, 800, 2.0),
        ),
        virtual=(0, 0, 2560, 800),
    )
    spectacle = _Spectacle(monkeypatch, topology, image_size=(2560, 800))

    text = _screen_capture_error(
        fake_engine, {"region": {"x": 100, "y": 100, "width": 200, "height": 150}}
    )

    assert spectacle.started >= 1
    assert (
        "Spectacle (Spectacle image 2560x800 does not match KWin outputs "
        "[Left 1280x800+0+0 scale 1, Right 1280x800+1280+0 scale 2]; "
        "coordinate mapping cannot be proven)"
    ) in text, text
    assert _published(tmp_path) == []

    # A burst frame whose topology changed mid-capture is kept as "unavailable",
    # but a region cannot be cut from it: the burst fails and publishes nothing.
    monkeypatch.setattr(
        screenshot,
        "_capture_burst_frames",
        lambda _bus, _socket, delays, attempt_dir, **_kwargs: [
            (
                _capture(attempt_dir(), (2560, 800)),
                screenshot._unavailable(
                    "spectacle", "output topology changed while this frame was captured"
                ),
            )
            for _ in delays
        ],
    )
    burst_dir = tmp_path / "burst"
    with pytest.raises(
        RuntimeError, match=r"^Cannot crop to a region: Coordinate space: unavailable \(output"
    ):
        screenshot.capture_frame_burst("", burst_dir, [0], region=(100, 100, 200, 150))
    assert _published(burst_dir) == []
