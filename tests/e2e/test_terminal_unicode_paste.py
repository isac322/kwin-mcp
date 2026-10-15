"""End-to-end contracts for ``keyboard_type(method="unicode")`` in a terminal (Konsole).

KWin has no virtual-keyboard protocol, so the text is pasted through a
transient clipboard owner. Konsole pastes on Ctrl+Shift+V and forwards an
unbound Ctrl+V to the pty as ^V (VLNEXT), which quotes whatever key comes next.
Konsole runs ``cat > file`` here, so the file holds exactly the bytes the
terminal delivered, including a Return turned into a literal CR by a stray ^V.
"""

from __future__ import annotations

import os
import shlex
import signal
import time
from typing import TYPE_CHECKING

import pytest

from kwin_mcp.models import WindowSelector

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import AppLaunchResult, ElementList, SessionInfo

TEXT = "한글42パ"
POLL_INTERVAL_SECONDS = 0.05
WAIT_TIMEOUT_SECONDS = 10.0


def _wait_for_bytes(path: Path, suffix: bytes) -> bytes:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    data = b""
    while time.monotonic() < deadline:
        data = path.read_bytes() if path.exists() else b""
        if data.endswith(suffix):
            return data
        time.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"terminal output never ended with {suffix!r}: {data!r}")


def _start_konsole_cat(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
    tmp_path: Path,
) -> tuple[Path, int]:
    """Start Konsole running ``cat > file`` and prove it receives keystrokes."""
    received = tmp_path / "received.bin"
    script = tmp_path / "cat.sh"
    script.write_text(f"#!/bin/sh\nexec cat > {shlex.quote(str(received))}\n")
    script.chmod(0o755)
    info = start_session()
    assert info.input_backend == "eis", info
    app = launch(f"konsole --separate -e {shlex.quote(str(script))}")
    wait_for_app("konsole")
    focused = engine.window_focus(WindowSelector(app_id="org.kde.konsole"))
    assert focused.window is not None, focused.render()
    assert focused.window.app_id == "org.kde.konsole", focused.render()
    # Healthy control and readiness gate: plain keys reach cat unaltered.
    engine.keyboard_type("ready")
    engine.keyboard_press("Return")
    assert _wait_for_bytes(received, b"ready\n") == b"ready\n"
    return received, app.pid


def _type_next_line(engine: AutomationEngine) -> None:
    """The keystrokes that follow the unicode call: Return, then a fresh line."""
    engine.keyboard_press("Return")
    engine.keyboard_type("ok")
    engine.keyboard_press("Return")


@pytest.mark.qa("QA-INP-061")
def test_konsole_paste_uses_ctrl_shift_v(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
    tmp_path: Path,
) -> None:
    """Konsole gets the text through Ctrl+Shift+V, and the next key arrives unaltered."""
    received, _ = _start_konsole_cat(engine, start_session, launch, wait_for_app, tmp_path)

    result = engine.keyboard_type(TEXT, method="unicode")
    assert result.route == "paste:ctrl+shift+v", result.render()
    assert result.chars == len(TEXT)
    assert result.render() == (
        f"Typed unicode: {TEXT!r}\nRoute: paste:ctrl+shift+v; {len(TEXT)} characters"
    )
    _type_next_line(engine)

    expected = b"ready\n" + TEXT.encode() + b"\nok\n"
    assert _wait_for_bytes(received, b"ok\n") == expected


def test_failed_type_into_stalled_konsole_leaves_next_key_unaltered(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
    tmp_path: Path,
) -> None:
    """A reported failure must not leave anything that changes the next key.

    Stopping Konsole holds the paste chord in its event queue until the call
    has timed out, restored the (empty) prior selection, and reported failure.
    Konsole then handles the chord like any late key, so what it does to the
    following Return is exactly the residue a failed call leaves behind. The
    late chord can add a benign empty line while the window drains its queue;
    what must never appear is the literal CR that a stray ^V quotes it into,
    and the paste must not deliver the text after the call failed.
    """
    received, pid = _start_konsole_cat(engine, start_session, launch, wait_for_app, tmp_path)

    os.kill(pid, signal.SIGSTOP)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            engine.keyboard_type(TEXT, method="unicode")
        assert str(excinfo.value) == f"Failed to type unicode: {TEXT!r}"
    finally:
        os.kill(pid, signal.SIGCONT)
    _type_next_line(engine)

    data = _wait_for_bytes(received, b"ok\n")
    assert data.startswith(b"ready\n")
    assert TEXT.encode() not in data
    assert b"\r" not in data, data
