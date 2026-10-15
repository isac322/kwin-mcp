"""Unit tests for action frame bursts: the ``capture`` options equal ``screen_capture``'s.

``capture_frame_burst`` is driven with the ScreenShot2 backend replaced by a
stub that writes synthetic PNGs whose pixels encode their logical position,
so a wrong crop offset shows up as a wrong colour, not only as a wrong size.
No KWin, D-Bus or Spectacle is involved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from PIL import Image

from kwin_mcp import screenshot
from kwin_mcp.models import ImageMapping, Point
from kwin_mcp.screenshot import FrameMapping, capture_frame_burst, to_frame

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

WORKSPACE = (400, 200)
PROVEN = FrameMapping(backend="screenshot2", origin=(0, 0), size=WORKSPACE)
UNPROVEN = FrameMapping(
    backend="screenshot2",
    origin=None,
    size=None,
    reason="output topology changed while this frame was captured",
)


def _encoded_workspace(path: Path) -> None:
    """Save a workspace PNG whose pixel at logical (x, y) is (x % 256, y % 256, 0)."""
    width, height = WORKSPACE
    image = Image.new("RGB", WORKSPACE)
    image.putdata([(x % 256, y % 256, 0) for y in range(height) for x in range(width)])
    image.save(path, "PNG")


class _Backends:
    """Records the backend calls a burst makes; ScreenShot2 succeeds, Spectacle is tracked."""

    def __init__(self, mapping: FrameMapping) -> None:
        self.mapping = mapping
        self.dbus_calls: list[dict[str, object]] = []
        self.spectacle_calls = 0

    def dbus(
        self,
        dbus_address: str,
        private_dir: Path,
        sorted_delays: list[int],
        *,
        include_cursor: bool = False,
        on_frame: Callable[[int, int], None] | None = None,
    ) -> list[tuple[Path, FrameMapping]]:
        self.dbus_calls.append(
            {
                "dbus_address": dbus_address,
                "delays": list(sorted_delays),
                "include_cursor": include_cursor,
            }
        )
        frames: list[tuple[Path, FrameMapping]] = []
        for i, delay_ms in enumerate(sorted_delays):
            path = private_dir / f"frame_{i:03d}.png"
            _encoded_workspace(path)
            frames.append((path, self.mapping))
            if on_frame is not None:
                on_frame(i, delay_ms)
        return frames

    def spectacle(self, *_args: object, **_kwargs: object) -> list[tuple[Path, FrameMapping]]:
        self.spectacle_calls += 1
        msg = "Spectacle must not run when ScreenShot2 succeeded"
        raise AssertionError(msg)


def _install(monkeypatch: pytest.MonkeyPatch, mapping: FrameMapping) -> _Backends:
    monkeypatch.delenv("KWIN_MCP_X11_SCREENSHOT", raising=False)
    backends = _Backends(mapping)
    monkeypatch.setattr(screenshot, "_capture_frame_burst_dbus", backends.dbus)
    monkeypatch.setattr(screenshot, "_capture_frame_burst_spectacle", backends.spectacle)
    return backends


def _published(output_dir: Path) -> list[str]:
    return sorted(p.name for p in output_dir.iterdir())


@pytest.mark.qa("QA-CAP-012")
def test_burst_frames_honour_region_max_edge_and_include_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backends = _install(monkeypatch, PROVEN)

    frames = capture_frame_burst(
        "unix:path=/nonexistent",
        tmp_path,
        [0, 30],
        include_cursor=True,
        region=(100, 50, 200, 100),
        max_edge=100,
    )

    assert backends.dbus_calls == [
        {"dbus_address": "unix:path=/nonexistent", "delays": [0, 30], "include_cursor": True}
    ]
    assert [delay for delay, _, _ in frames] == [0, 30]
    for _, path, mapping in frames:
        assert path.parent == tmp_path
        assert (mapping.origin, mapping.size, mapping.image) == ((100, 50), (200, 100), (100, 50))
        with Image.open(path) as image:
            assert image.size == (100, 50)
    assert _published(tmp_path) == sorted(path.name for _, path, _ in frames)


@pytest.mark.qa("QA-CAP-012")
def test_burst_region_crop_keeps_the_logical_pixels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, PROVEN)

    [(_, path, mapping)] = capture_frame_burst(
        "unix:path=/nonexistent", tmp_path, [0], region=(100, 50, 200, 100)
    )

    assert (mapping.origin, mapping.size, mapping.image) == ((100, 50), (200, 100), None)
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        assert rgb.size == (200, 100)
        assert rgb.getpixel((0, 0)) == (100, 50, 0)
        assert rgb.getpixel((199, 99)) == (299 % 256, 149, 0)


@pytest.mark.qa("QA-CAP-012")
def test_burst_defaults_are_the_full_workspace_without_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backends = _install(monkeypatch, PROVEN)

    [(_, path, mapping)] = capture_frame_burst("unix:path=/nonexistent", tmp_path, [0])

    assert backends.dbus_calls[0]["include_cursor"] is False
    assert mapping == PROVEN
    with Image.open(path) as image:
        assert image.size == WORKSPACE


@pytest.mark.qa("QA-CAP-010")
@pytest.mark.qa("QA-CAP-017")
def test_burst_delays_sorted_duplicates_kept_negative_clamped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backends = _install(monkeypatch, PROVEN)

    frames = capture_frame_burst("unix:path=/nonexistent", tmp_path, [20, -5, 0, 0])

    assert backends.dbus_calls[0]["delays"] == [0, 0, 0, 20]
    assert [delay for delay, _, _ in frames] == [0, 0, 0, 20]
    assert len({path for _, path, _ in frames}) == 4


@pytest.mark.qa("QA-CAP-013")
def test_empty_delays_capture_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backends = _install(monkeypatch, PROVEN)
    output_dir = tmp_path / "out"

    assert capture_frame_burst("unix:path=/nonexistent", output_dir, []) == []
    assert backends.dbus_calls == []
    assert not output_dir.exists()


@pytest.mark.qa("QA-CAP-012")
def test_region_outside_the_workspace_fails_without_files_or_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backends = _install(monkeypatch, PROVEN)

    with pytest.raises(ValueError, match=r"outside the captured workspace \(0, 0, 400x200\)"):
        capture_frame_burst("unix:path=/nonexistent", tmp_path, [0, 10], region=(500, 0, 10, 10))

    assert backends.spectacle_calls == 0
    assert _published(tmp_path) == []


@pytest.mark.qa("QA-CAP-008")
def test_region_on_unproven_burst_frame_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backends = _install(monkeypatch, UNPROVEN)

    with pytest.raises(RuntimeError, match="Cannot crop to a region: Coordinate space: unavail"):
        capture_frame_burst("unix:path=/nonexistent", tmp_path, [0], region=(0, 0, 10, 10))

    # A reframe failure is not a backend failure: nothing is recaptured.
    assert backends.spectacle_calls == 0
    assert _published(tmp_path) == []


@pytest.mark.qa("QA-CAP-001")
def test_to_frame_reports_the_saved_png(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install(monkeypatch, PROVEN)
    [(delay, path, mapping)] = capture_frame_burst(
        "unix:path=/nonexistent", tmp_path, [40], region=(100, 50, 200, 100), max_edge=100
    )

    frame = to_frame(path, mapping, delay_ms=delay)

    assert frame.delay_ms == 40
    assert frame.path == str(path)
    assert frame.uri == f"kwin://screenshots/{path.name}"
    assert (frame.width, frame.height) == (100, 50)
    assert frame.size_kb == round(path.stat().st_size / 1024, 1)
    assert frame.mapping == ImageMapping(scale=0.5, origin=Point(x=100, y=50))
    assert frame.coverage == "full"
    assert frame.coordinate_space == mapping.describe()


@pytest.mark.qa("QA-CAP-001")
def test_to_frame_of_an_unproven_capture_claims_no_mapping(tmp_path: Path) -> None:
    path = tmp_path / "frame.png"
    _encoded_workspace(path)

    frame = to_frame(path, UNPROVEN, delay_ms=None)

    assert frame.delay_ms is None
    assert frame.mapping is None
    assert (frame.width, frame.height) == WORKSPACE
    assert frame.coordinate_space.startswith("Coordinate space: unavailable (")


@pytest.mark.qa("QA-CAP-001")
def test_to_frame_reports_partial_coverage(tmp_path: Path) -> None:
    path = tmp_path / "frame.png"
    _encoded_workspace(path)
    partial = FrameMapping(
        backend="spectacle",
        origin=(0, 0),
        size=WORKSPACE,
        coverage="partial",
        captured=((0, 0, 200, 200),),
    )

    frame = to_frame(path, partial, delay_ms=0)

    assert frame.coverage == "partial"
    assert frame.mapping == ImageMapping(scale=1.0, origin=Point(x=0, y=0))
