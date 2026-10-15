"""KWin-free coverage for the ``app_log`` ``last_n_lines`` contract.

``last_n_lines`` must be 0 (the whole log) or a positive integer. Anything else
is rejected with ``ValueError`` before the engine touches the session, so a
recording ``_require_session`` stub stands in for a real KWin session.
"""

from __future__ import annotations

import pytest

from kwin_mcp.core import AutomationEngine
from kwin_mcp.models import AppLogResult

PID = 1234


class _RecordingSession:
    """Session stand-in that records every ``read_app_log`` call."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def read_app_log(self, pid: int, last_n_lines: int = 50) -> AppLogResult:
        self.calls.append((pid, last_n_lines))
        return AppLogResult(pid=pid, log_path="/tmp/app.log", lines=["log text"], truncated=False)


class _RecordingEngine:
    """An engine whose ``_require_session`` is replaced by a recording stub."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.engine = AutomationEngine()
        self.session = _RecordingSession()
        self.session_requests = 0
        monkeypatch.setattr(self.engine, "_require_session", self._require_session)

    def _require_session(self) -> _RecordingSession:
        self.session_requests += 1
        return self.session


@pytest.fixture
def recording(monkeypatch: pytest.MonkeyPatch) -> _RecordingEngine:
    return _RecordingEngine(monkeypatch)


@pytest.mark.parametrize("last_n_lines", [-1, True], ids=["negative", "bool"])
def test_invalid_last_n_lines_is_rejected_before_session(
    recording: _RecordingEngine, last_n_lines: int
) -> None:
    with pytest.raises(ValueError, match=r"last_n_lines must be 0 \(all\) or a positive integer"):
        recording.engine.app_log(pid=PID, last_n_lines=last_n_lines)

    assert recording.session_requests == 0
    assert recording.session.calls == []


@pytest.mark.parametrize("last_n_lines", [0, 1, 50], ids=["all", "one", "default"])
def test_valid_last_n_lines_reaches_session(recording: _RecordingEngine, last_n_lines: int) -> None:
    result = recording.engine.app_log(pid=PID, last_n_lines=last_n_lines)

    assert result == AppLogResult(
        pid=PID, log_path="/tmp/app.log", lines=["log text"], truncated=False
    )
    assert recording.session_requests == 1
    assert recording.session.calls == [(PID, last_n_lines)]
