"""Frame-burst files are never reused within a session (KWin-free).

The default ScreenShot2 backend is faked at its D-Bus and raw-capture seams, so
every capture writes distinct pixels. A later burst must publish new paths
and leave the earlier frames' bytes untouched; a burst that fails part-way
publishes nothing.
"""

from __future__ import annotations

import itertools
import re
import types
from typing import TYPE_CHECKING

import pytest
from PIL import Image

from kwin_mcp import screenshot

if TYPE_CHECKING:
    from pathlib import Path

DBUS_ADDRESS = "unix:path=/nonexistent/kwin-mcp-test-bus"
# ``frame_<YYYYmmdd_HHMMSS>_<index>_<delay>ms.png``, with ``_N`` when the name was taken.
_FRAME_NAME = re.compile(r"frame_\d{8}_\d{6}_000_0ms(_\d+)?\.png")


def _fake_screenshot2(monkeypatch: pytest.MonkeyPatch, *, fail_on_frame: int = 0) -> None:
    """Select the ScreenShot2 path and make each captured frame a distinct colour.

    ``fail_on_frame`` (1-based) makes that capture raise, like a ScreenShot2 error.
    """
    monkeypatch.delenv("KWIN_MCP_X11_SCREENSHOT", raising=False)
    monkeypatch.setattr(
        screenshot.dbus.bus,
        "BusConnection",
        lambda _address: types.SimpleNamespace(get_object=lambda *_args, **_kwargs: object()),
    )
    monkeypatch.setattr(screenshot.dbus, "Interface", lambda *_args: object())
    captures = itertools.count(1)

    def burst_frame(*_args: object) -> tuple[screenshot.FrameMapping, int]:
        frame = next(captures)
        if frame == fail_on_frame:
            msg = "ScreenShot2 failed in this test"
            raise RuntimeError(msg)
        return screenshot._unavailable("screenshot2", "faked in this test"), frame

    monkeypatch.setattr(screenshot, "_burst_frame", burst_frame)
    monkeypatch.setattr(
        screenshot,
        "_image_from_raw_frame",
        lambda frame: Image.new("RGB", (4, 4), (frame * 40 % 256, 0, 0)),
    )


def test_second_burst_never_overwrites_the_first_bursts_frames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_screenshot2(monkeypatch)

    first = screenshot.capture_frame_burst(DBUS_ADDRESS, tmp_path, [0])
    assert [delay for delay, _, _ in first] == [0]
    p1 = first[0][1]
    first_bytes = p1.read_bytes()
    first_mtime = p1.stat().st_mtime_ns

    second = screenshot.capture_frame_burst(DBUS_ADDRESS, tmp_path, [0])
    p2 = second[0][1]

    assert p1 != p2, (p1, p2)
    assert p1.parent == tmp_path and p2.parent == tmp_path, (p1, p2)
    assert _FRAME_NAME.fullmatch(p1.name) and _FRAME_NAME.fullmatch(p2.name), (p1, p2)
    assert p1.read_bytes() == first_bytes
    assert p1.stat().st_mtime_ns == first_mtime
    assert p2.read_bytes() != first_bytes
    # Private capture directories are gone; only the published frames remain.
    assert sorted(tmp_path.iterdir()) == sorted([p1, p2])


def test_failed_burst_publishes_no_frames(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_screenshot2(monkeypatch, fail_on_frame=2)

    def spectacle_unavailable(*_args: object, **_kwargs: object) -> None:
        msg = "Spectacle unavailable in this test"
        raise RuntimeError(msg)

    monkeypatch.setattr(screenshot, "_capture_frame_burst_spectacle", spectacle_unavailable)

    with pytest.raises(RuntimeError, match="Frame burst capture failed"):
        screenshot.capture_frame_burst(DBUS_ADDRESS, tmp_path, [0, 10])

    assert list(tmp_path.iterdir()) == []
