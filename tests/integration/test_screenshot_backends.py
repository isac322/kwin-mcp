"""End-to-end tests for the screenshot backend probe + dispatch + SessionInfo wiring.

Covers Tasks 16-18 of the kwin-mcp-backend-overhaul plan:

* Task 16 -- ``_probe_screenshot_capability`` returns one of the three valid
  decision strings.
* Task 17 -- ``capture_screenshot_to_file`` and ``capture_frame_burst``
  dispatch on ``SessionInfo.screenshot_backend`` and raise a clear
  ``RuntimeError`` when the value is ``"unavailable"``.
* Task 18 -- ``Session.start`` populates ``SessionInfo.screenshot_backend``
  so both single-shot and burst paths share one decision.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from kwin_mcp.core import AutomationEngine

pytestmark = pytest.mark.kwin

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_VALID_BACKENDS = {"screenshot2_dbus", "spectacle_cli", "unavailable"}


def _extract_screenshot_path(result: str) -> Path:
    """Extract the saved PNG path from ``engine.screenshot``'s return string.

    ``AutomationEngine.screenshot`` returns ``"Screenshot saved: <path> (<size> KB)"``.
    We split on the literal prefix and trailing size annotation rather than
    using a regex to keep the parser obvious.
    """
    prefix = "Screenshot saved: "
    assert result.startswith(prefix), f"unexpected screenshot result: {result!r}"
    remainder = result[len(prefix) :]
    # Trim the trailing " (<size> KB)" suffix; the path itself never contains " (".
    path_str, _, _ = remainder.rpartition(" (")
    assert path_str, f"could not parse path from screenshot result: {result!r}"
    return Path(path_str)


def _extract_burst_paths(result: str) -> list[Path]:
    """Pull every burst frame path out of an action result string.

    ``_with_frame_capture`` formats burst lines as
    ``"  <delay>ms: <path> (<size> KB)"``. We split each line on ``"ms: "``
    and ``" ("`` so a path containing spaces still survives intact.
    """
    paths: list[Path] = []
    for raw_line in result.splitlines():
        line = raw_line.strip()
        if "ms: " not in line:
            continue
        _, _, after = line.partition("ms: ")
        path_str, sep, _ = after.rpartition(" (")
        if not sep:
            continue
        paths.append(Path(path_str))
    return paths


def _assert_is_png(path: Path) -> None:
    assert path.exists(), f"expected PNG at {path} but it does not exist"
    size = path.stat().st_size
    assert size > 0, f"PNG at {path} is empty (0 bytes)"
    with path.open("rb") as fh:
        header = fh.read(8)
    assert header == _PNG_SIGNATURE, f"file at {path} is not a PNG (size={size}, header={header!r})"


def test_probe_returns_valid_decision(engine: AutomationEngine) -> None:
    """``Session.start`` must populate ``screenshot_backend`` with a known value."""
    session = engine._session
    assert session is not None, "engine fixture should yield a running session"
    info = session.info
    assert info is not None, "running session must expose SessionInfo"
    assert info.screenshot_backend in _VALID_BACKENDS, (
        f"screenshot_backend={info.screenshot_backend!r} not in {_VALID_BACKENDS}"
    )


def test_screenshot_to_file_in_virtual_session_produces_png(engine: AutomationEngine) -> None:
    """The single-shot ``screenshot`` path must produce a real PNG via the chosen backend."""
    session = engine._session
    assert session is not None
    info = session.info
    assert info is not None
    if info.screenshot_backend == "unavailable":
        pytest.skip("no screenshot backend available in this environment")

    result = engine.screenshot()
    path = _extract_screenshot_path(result)
    _assert_is_png(path)


def test_capture_frame_burst_uses_same_backend(engine: AutomationEngine) -> None:
    """A burst-capable action must reuse the same backend as the single-shot path."""
    session = engine._session
    assert session is not None
    info = session.info
    assert info is not None
    if info.screenshot_backend == "unavailable":
        pytest.skip("no screenshot backend available in this environment")

    backend_before = info.screenshot_backend

    # ``mouse_move`` is the cheapest action that plumbs ``screenshot_after_ms``
    # through ``_with_frame_capture`` -> ``capture_frame_burst``.
    result = engine.mouse_move(x=100, y=100, screenshot_after_ms=[0, 50])

    backend_after = info.screenshot_backend
    assert backend_after == backend_before, (
        f"backend changed mid-call: {backend_before!r} -> {backend_after!r}"
    )

    paths = _extract_burst_paths(result)
    assert len(paths) == 2, f"expected 2 burst frames, got {len(paths)} in:\n{result}"
    for path in paths:
        _assert_is_png(path)


def test_unavailable_raises(engine: AutomationEngine) -> None:
    """Forcing ``screenshot_backend='unavailable'`` must surface a clear ``RuntimeError``."""
    session = engine._session
    assert session is not None
    info = session.info
    assert info is not None

    original = info.screenshot_backend
    info.screenshot_backend = "unavailable"
    try:
        with pytest.raises(RuntimeError, match="No screenshot backend available"):
            engine.screenshot()
    finally:
        info.screenshot_backend = original
