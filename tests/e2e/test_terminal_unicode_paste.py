"""End-to-end contracts for ``keyboard_type_unicode`` in a terminal (Konsole).

KWin has no virtual-keyboard protocol, so the text is pasted through a
transient clipboard owner. Konsole pastes on Ctrl+Shift+V and forwards an
unbound Ctrl+V to the pty as ^V (VLNEXT), which quotes whatever key comes next.
Konsole runs ``cat > file`` here, so the file holds exactly the bytes the
terminal delivered, including a Return turned into a literal CR by a stray ^V.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from kwin_mcp.core import AutomationEngine

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
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
    tmp_path: Path,
) -> tuple[Path, int]:
    """Start Konsole running ``cat > file`` and prove it receives keystrokes."""
    received = tmp_path / "received.bin"
    script = tmp_path / "cat.sh"
    script.write_text(f"#!/bin/sh\nexec cat > {shlex.quote(str(received))}\n")
    script.chmod(0o755)
    output = start_session(f"konsole --separate -e {shlex.quote(str(script))}")
    assert "Session started" in output, output
    pid = re.search(r"\(PID=(\d+)\)", output)
    assert pid is not None, output
    wait_for_app("konsole")
    focused = engine.focus_window("konsole")
    assert focused.startswith("Focused: org.kde.konsole"), focused
    # Healthy control and readiness gate: plain keys reach cat unaltered.
    engine.keyboard_type("ready")
    engine.keyboard_key("Return")
    assert _wait_for_bytes(received, b"ready\n") == b"ready\n"
    return received, int(pid.group(1))


def _type_next_line(engine: AutomationEngine) -> None:
    """The keystrokes that follow the unicode call: Return, then a fresh line."""
    engine.keyboard_key("Return")
    engine.keyboard_type("ok")
    engine.keyboard_key("Return")


def test_type_unicode_pastes_into_konsole_and_leaves_next_key_unaltered(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
    tmp_path: Path,
) -> None:
    received, _ = _start_konsole_cat(engine, start_session, wait_for_app, tmp_path)

    assert engine.keyboard_type_unicode(TEXT) == f"Typed unicode: {TEXT!r}"
    _type_next_line(engine)

    expected = b"ready\n" + TEXT.encode() + b"\nok\n"
    assert _wait_for_bytes(received, b"ok\n") == expected


def test_failed_type_into_stalled_konsole_leaves_next_key_unaltered(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
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
    received, pid = _start_konsole_cat(engine, start_session, wait_for_app, tmp_path)

    os.kill(pid, signal.SIGSTOP)
    try:
        output = engine.keyboard_type_unicode(TEXT)
    finally:
        os.kill(pid, signal.SIGCONT)
    assert output == f"Failed to type unicode: {TEXT!r}"
    _type_next_line(engine)

    data = _wait_for_bytes(received, b"ok\n")
    assert data.startswith(b"ready\n")
    assert TEXT.encode() not in data
    assert b"\r" not in data, data
