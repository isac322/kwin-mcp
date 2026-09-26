"""Clipboard lifecycle end-to-end tests for ``keyboard_type_unicode`` (issue #45).

``keyboard_type_unicode`` pastes through a transient clipboard selection. These
tests drive the public ``AutomationEngine`` against real KWin compositors and
observe only external state: the KWrite document over AT-SPI2, the selection
through ``wl-paste``/``wl-copy`` on the session's Wayland socket, and the
processes the engine call spawned, read from ``/proc``.

Spawned processes are identified exactly. Right before typing (after the app
runs) the test exports a unique marker variable, so every process the engine
call starts inherits it, while the test's own wl-clipboard tools run without
it. Such a process counts while it is not a zombie and its environment targets
the selected ``WAYLAND_DISPLAY``.

Ordering is controlled instead of timed: tests that must observe the transient
selection SIGSTOP KWrite, so the requested paste cannot complete, and the prior
selection cannot come back, before the observation is done. The engine's paste
deadline bounds how long that hold may last.
"""

from __future__ import annotations

import ast
import contextlib
import io
import os
import re
import shlex
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from _asserts import element_count
from PIL import Image
from session_harness import live_kwin

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from concurrent.futures import Future
    from pathlib import Path

    from kwin_mcp.core import AutomationEngine

_RECT = r"\((-?\d+), (-?\d+), (\d+)x(\d+)\)"
POLL_INTERVAL_SECONDS = 0.05
WAIT_TIMEOUT_SECONDS = 5.0
CALL_TIMEOUT_SECONDS = 30.0
CALL_MARKER = "KWIN_MCP_E2E_UNICODE_CALL"
HINT_MIME = "x-kde-passwordManagerHint"
TRANSIENT_MIMES = {"text/plain;charset=utf-8", "text/plain", HINT_MIME}

SECRET = "SECRET-45-한글🔑"
PRIOR_TEXT = "PRIOR-45-유니코드-clip"
USER_COPY = "USER-45-concurrent-copy"
REPLACEMENT = "REPLACED-45-after-restore"


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (3, 2), (12, 34, 56, 78)).save(buffer, format="PNG")
    return buffer.getvalue()


def _wait_until(
    predicate: Callable[[], bool], description: str, call: Future[str] | None = None
) -> None:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while not predicate():
        if call is not None and call.done():
            pytest.fail(f"{description}: engine call already returned {call.result()!r}")
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out waiting for {description}")
        time.sleep(POLL_INTERVAL_SECONDS)


def _start_kwrite(
    start_session: Callable[..., str], wait_for_app: Callable[[str], str], document: Path
) -> tuple[str, int]:
    """Start a clipboard-enabled virtual session with KWrite; return socket and PID."""
    output = start_session(f"kwrite {shlex.quote(str(document))}", enable_clipboard=True)
    assert "Input backend: KWin EIS" in output, output
    socket = re.search(r"^Session started\. Wayland socket: (\S+)$", output, re.MULTILINE)
    pid = re.search(r"\(PID=(\d+)\)", output)
    assert socket is not None and pid is not None, output
    wait_for_app("kwrite")
    return socket.group(1), int(pid.group(1))


def _mark_engine_calls(monkeypatch: pytest.MonkeyPatch) -> str:
    token = uuid4().hex
    monkeypatch.setenv(CALL_MARKER, token)
    return token


def _tool_env(display: str) -> dict[str, str]:
    """Environment for the test's own wl-clipboard tools, never marked."""
    env = {key: value for key, value in os.environ.items() if key != CALL_MARKER}
    env["WAYLAND_DISPLAY"] = display
    return env


def _spawned(display: str, token: str) -> dict[int, bytes]:
    """Live processes started by marked engine calls on ``display``: PID -> argv + environ."""
    marker = f"{CALL_MARKER}={token}".encode()
    target = f"WAYLAND_DISPLAY={display}".encode()
    found: dict[int, bytes] = {}
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                with open(f"{entry.path}/stat", "rb") as stat:
                    state = stat.read().rsplit(b")", 1)[1].split()[0]
                with open(f"{entry.path}/environ", "rb") as environ_file:
                    environ = environ_file.read()
                with open(f"{entry.path}/cmdline", "rb") as cmdline:
                    argv = cmdline.read()
            except OSError:
                continue
            variables = environ.split(b"\0")
            if state != b"Z" and marker in variables and target in variables:
                found[int(entry.name)] = argv + b"\0" + environ
    return found


def _wait_for_sole_owner(display: str, token: str, retired: frozenset[int] = frozenset()) -> int:
    """Exactly one spawned process may own a selection, and none of ``retired``."""

    def settled() -> bool:
        live = _spawned(display, token)
        return len(live) == 1 and not retired & live.keys()

    _wait_until(settled, "exactly one new selection owner")
    (pid,) = _spawned(display, token)
    return pid


def _assert_secret_not_exposed(display: str, token: str) -> None:
    for pid, blob in _spawned(display, token).items():
        assert SECRET.encode() not in blob, f"PID {pid} carries typed text in argv/environ"


def _wl_paste(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["wl-paste", *args], env=env, capture_output=True, timeout=WAIT_TIMEOUT_SECONDS
    )


def _offered_types(env: dict[str, str]) -> set[str]:
    """MIME types the compositor announces; never requests the selection's data."""
    result = _wl_paste(env, "--list-types")
    if result.returncode != 0:
        return set()
    return {line for line in result.stdout.decode().splitlines() if line}


def _selection(env: dict[str, str]) -> dict[str, bytes]:
    """Every offered MIME type of the current selection with its exact bytes."""
    contents: dict[str, bytes] = {}
    for mime in sorted(_offered_types(env)):
        result = _wl_paste(env, "--no-newline", "--type", mime)
        assert result.returncode == 0, (mime, result.stderr[:200])
        contents[mime] = result.stdout
    return contents


def _seed(env: dict[str, str], data: bytes, mime: str | None = None) -> None:
    """Take the selection with a wl-copy daemon that keeps serving it."""
    args = ["wl-copy"] if mime is None else ["wl-copy", "--type", mime]
    # The forked daemon inherits stdio, so nothing may be captured.
    subprocess.run(
        args,
        input=data,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=WAIT_TIMEOUT_SECONDS,
        check=True,
    )
    # Checking the types first keeps a still-current transient text unread.
    _wait_until(
        lambda: (
            HINT_MIME not in _offered_types(env)
            and _wl_paste(env, "--no-newline", "--type", mime or "text/plain").stdout == data
        ),
        "seeded selection",
    )


def _assert_owners_released_on_replace(env: dict[str, str], display: str, token: str) -> None:
    """Once another client copies, nothing the engine spawned may keep running."""
    _seed(env, REPLACEMENT.encode())
    _wait_until(lambda: not _spawned(display, token), "spawned owners to exit after a new copy")


@contextlib.contextmanager
def _typing(engine: AutomationEngine, text: str) -> Iterator[Future[str]]:
    with ThreadPoolExecutor(max_workers=1) as pool:
        yield pool.submit(engine.keyboard_type_unicode, text)


@contextlib.contextmanager
def _paste_target_held(pid: int) -> Iterator[None]:
    """Freeze KWrite so its requested paste, and thus the restore, waits for the test."""
    os.kill(pid, signal.SIGSTOP)
    try:
        yield
    finally:
        os.kill(pid, signal.SIGCONT)


def _focused_text(engine: AutomationEngine) -> str:
    output = engine.find_ui_elements(query="text", app_name="kwrite", states=["focused"])
    assert element_count(output) > 0, output[:500]
    matches = re.findall(
        rf'^- \[text] "[^"]*" @ (?:screen {_RECT}|unavailable \([^)]+\))'
        rf"(?: text=(.*?))?(?: \[actions:.*])?$",
        output,
        re.MULTILINE,
    )
    assert len(matches) == 1, output[:500]
    text_repr = matches[0][-1]
    return ast.literal_eval(text_repr) if text_repr else ""


def _wait_for_text(engine: AutomationEngine, expected: str) -> None:
    _wait_until(lambda: _focused_text(engine) == expected, f"document text {expected!r}")


@pytest.mark.parametrize(
    ("mime", "data"),
    [
        pytest.param(None, PRIOR_TEXT.encode(), id="utf8-text"),
        pytest.param("image/png", _png(), id="png"),
    ],
)
def test_type_unicode_restores_every_prior_mime_byte_for_byte(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mime: str | None,
    data: bytes,
) -> None:
    display, _ = _start_kwrite(start_session, wait_for_app, tmp_path / "restore.txt")
    env = _tool_env(display)
    _seed(env, data, mime)
    prior = _selection(env)
    assert data in prior.values(), prior
    token = _mark_engine_calls(monkeypatch)

    assert engine.keyboard_type_unicode(SECRET) == f"Typed unicode: {SECRET!r}"

    _wait_for_text(engine, SECRET)
    assert _selection(env) == prior
    _wait_for_sole_owner(display, token)
    _assert_secret_not_exposed(display, token)
    _assert_owners_released_on_replace(env, display, token)


def test_type_unicode_leaves_an_empty_selection_empty(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    display, _ = _start_kwrite(start_session, wait_for_app, tmp_path / "empty.txt")
    env = _tool_env(display)
    subprocess.run(
        ["wl-copy", "--clear"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=WAIT_TIMEOUT_SECONDS,
        check=True,
    )
    _wait_until(lambda: not _offered_types(env), "empty selection")
    token = _mark_engine_calls(monkeypatch)

    assert engine.keyboard_type_unicode(SECRET) == f"Typed unicode: {SECRET!r}"

    _wait_for_text(engine, SECRET)
    assert _offered_types(env) == set()
    _wait_until(lambda: not _spawned(display, token), "spawned processes to exit")


def test_transient_offer_carries_the_secret_hint_until_the_paste(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    display, kwrite_pid = _start_kwrite(start_session, wait_for_app, tmp_path / "hint.txt")
    env = _tool_env(display)
    _seed(env, PRIOR_TEXT.encode())
    prior = _selection(env)
    _mark_engine_calls(monkeypatch)

    with _typing(engine, SECRET) as call:
        with _paste_target_held(kwrite_pid):
            _wait_until(lambda: HINT_MIME in _offered_types(env), "transient selection", call)
            # Only the type list and the hint are read; the text stays for KWrite.
            assert _offered_types(env) == TRANSIENT_MIMES
            hint = _wl_paste(env, "--no-newline", "--type", HINT_MIME)
            assert (hint.returncode, hint.stdout) == (0, b"secret")
            assert not call.done()
        assert call.result(timeout=CALL_TIMEOUT_SECONDS) == f"Typed unicode: {SECRET!r}"

    _wait_for_text(engine, SECRET)
    assert _selection(env) == prior


def test_user_copy_during_transient_ownership_is_never_overwritten(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    display, kwrite_pid = _start_kwrite(start_session, wait_for_app, tmp_path / "race.txt")
    env = _tool_env(display)
    _seed(env, PRIOR_TEXT.encode())
    token = _mark_engine_calls(monkeypatch)

    with _typing(engine, SECRET) as call:
        with _paste_target_held(kwrite_pid):
            _wait_until(lambda: HINT_MIME in _offered_types(env), "transient selection", call)
            owner = _wait_for_sole_owner(display, token)
            _seed(env, USER_COPY.encode())
            # The transient owner ends once it observes its source cancelled;
            # after that nothing spawned by the call remains to restore.
            _wait_until(lambda: owner not in _spawned(display, token), "observed cancellation")
        output = call.result(timeout=CALL_TIMEOUT_SECONDS)

    assert output == f"Failed to type unicode: {SECRET!r}"
    assert not _spawned(display, token)
    assert _wl_paste(env, "--no-newline").stdout == USER_COPY.encode()
    assert SECRET not in _focused_text(engine)


def test_stalled_prior_owner_fails_before_paste_without_leaving_an_owner(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    display, _ = _start_kwrite(start_session, wait_for_app, tmp_path / "stalled.txt")
    env = _tool_env(display)
    owner = subprocess.Popen(
        ["wl-copy", "--foreground", "--", PRIOR_TEXT],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_until(
            lambda: _wl_paste(env, "--no-newline").stdout == PRIOR_TEXT.encode(), "prior owner"
        )
        prior_types = _offered_types(env)
        token = _mark_engine_calls(monkeypatch)
        owner.send_signal(signal.SIGSTOP)
        try:
            output = engine.keyboard_type_unicode(SECRET)
            # The compositor announces types without asking the frozen owner.
            assert _offered_types(env) == prior_types
        finally:
            owner.send_signal(signal.SIGCONT)

        assert output == f"Failed to type unicode: {SECRET!r}"
        assert _focused_text(engine) == ""
        _wait_until(lambda: not _spawned(display, token), "failed helper to exit")
    finally:
        owner.terminate()
        owner.wait(timeout=WAIT_TIMEOUT_SECONDS)


def test_repeated_calls_hand_the_prior_selection_to_exactly_one_owner(
    engine: AutomationEngine,
    start_session: Callable[..., str],
    wait_for_app: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    display, _ = _start_kwrite(start_session, wait_for_app, tmp_path / "repeat.txt")
    env = _tool_env(display)
    _seed(env, PRIOR_TEXT.encode())
    prior = _selection(env)
    token = _mark_engine_calls(monkeypatch)

    typed = ""
    retired: frozenset[int] = frozenset()
    for index in range(3):
        text = f"{SECRET}{index}"
        assert engine.keyboard_type_unicode(text) == f"Typed unicode: {text!r}"
        typed += text
        _wait_for_text(engine, typed)
        assert _selection(env) == prior
        retired |= {_wait_for_sole_owner(display, token, retired)}

    _assert_secret_not_exposed(display, token)
    _assert_owners_released_on_replace(env, display, token)


def test_live_session_keeps_the_restored_selection_after_disconnect(
    engine: AutomationEngine,
    wait_for_app: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with live_kwin() as live:
        env = _tool_env(live.wayland_display)
        output = engine.session_connect(
            dbus_address=live.dbus_address, wayland_display=live.wayland_display
        )
        assert output.startswith("Connected to live KWin session."), output
        assert "Input backend: KWin EIS" in output, output
        document = tmp_path / "live.txt"
        launch = engine.launch_app(f"kwrite {shlex.quote(str(document))}")
        assert launch.startswith("App launched:"), launch
        wait_for_app("kwrite")
        engine.focus_window("kwrite")
        _seed(env, PRIOR_TEXT.encode())
        prior = _selection(env)
        token = _mark_engine_calls(monkeypatch)

        assert engine.keyboard_type_unicode(SECRET) == f"Typed unicode: {SECRET!r}"
        _wait_for_text(engine, SECRET)
        assert _selection(env) == prior

        assert engine.session_stop() == "Disconnected from live session."
        assert live.process.poll() is None
        # The restored selection belongs to the desktop, not to the MCP connection.
        assert _selection(env) == prior
        _wait_for_sole_owner(live.wayland_display, token)
        _assert_secret_not_exposed(live.wayland_display, token)
        _assert_owners_released_on_replace(env, live.wayland_display, token)
