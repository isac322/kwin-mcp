"""Clipboard lifecycle end-to-end tests for ``keyboard_type(method="unicode")`` (issue #45).

The unicode method pastes through a transient clipboard selection. These
tests drive the public ``AutomationEngine`` against real KWin compositors and
observe only external state: the KWrite document through the independent
AT-SPI oracle, the selection
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

import contextlib
import io
import os
import shlex
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from atspi_oracle import AtspiOracle
from PIL import Image
from session_harness import live_kwin

from kwin_mcp.models import WindowSelector

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from concurrent.futures import Future
    from pathlib import Path

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import AppLaunchResult, ElementList, KeyboardTypeResult, SessionInfo

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
    predicate: Callable[[], bool], description: str, call: Future[KeyboardTypeResult] | None = None
) -> None:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while not predicate():
        if call is not None and call.done():
            pytest.fail(f"{description}: engine call already returned {call.result()!r}")
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out waiting for {description}")
        time.sleep(POLL_INTERVAL_SECONDS)


def _start_kwrite(
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
    document: Path,
) -> tuple[str, int, AtspiOracle]:
    """Start a clipboard-enabled virtual session with KWrite; return socket, PID, oracle."""
    info = start_session(enable_clipboard=True)
    assert info.input_backend == "eis", info
    assert info.clipboard is True, info
    app = launch(f"kwrite {shlex.quote(str(document))}")
    wait_for_app("kwrite")
    return info.wayland_socket, app.pid, AtspiOracle(info.dbus_address)


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
def _typing(engine: AutomationEngine, text: str) -> Generator[Future[KeyboardTypeResult]]:
    with ThreadPoolExecutor(max_workers=1) as pool:
        yield pool.submit(engine.keyboard_type, text, "unicode")


def _type_unicode(engine: AutomationEngine, text: str) -> None:
    """Type through the clipboard route and check the reported result."""
    result = engine.keyboard_type(text, method="unicode")
    _assert_pasted(result, text)


def _assert_pasted(result: KeyboardTypeResult, text: str) -> None:
    # KWin has no virtual-keyboard protocol, so wtype fails and KWrite pastes on Ctrl+V.
    assert result.route == "paste:ctrl+v", result.render()
    assert result.render().splitlines()[0] == f"Typed unicode: {text!r}"


@contextlib.contextmanager
def _paste_target_held(pid: int) -> Generator[None]:
    """Freeze KWrite so its requested paste, and thus the restore, waits for the test."""
    os.kill(pid, signal.SIGSTOP)
    try:
        yield
    finally:
        os.kill(pid, signal.SIGCONT)


def _focused_text(atspi: AtspiOracle) -> str:
    """Text of KWrite's focused document, read by the test's own AT-SPI walk."""
    nodes = [
        n
        for n in atspi.find(app="kwrite", role="text")
        if "focused" in n.states and n.error is None
    ]
    assert len(nodes) == 1, [(n.role, n.name, sorted(n.states)) for n in nodes]
    return nodes[0].text or ""


def _wait_for_text(atspi: AtspiOracle, expected: str) -> None:
    _wait_until(lambda: _focused_text(atspi) == expected, f"document text {expected!r}")


@pytest.mark.parametrize(
    ("mime", "data"),
    [
        pytest.param(None, PRIOR_TEXT.encode(), id="utf8-text"),
        pytest.param("image/png", _png(), id="png"),
    ],
)
def test_keyboard_type_method_unicode_restores_every_prior_mime_byte_for_byte(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mime: str | None,
    data: bytes,
) -> None:
    display, _, atspi = _start_kwrite(start_session, launch, wait_for_app, tmp_path / "restore.txt")
    env = _tool_env(display)
    _seed(env, data, mime)
    prior = _selection(env)
    assert data in prior.values(), prior
    token = _mark_engine_calls(monkeypatch)

    _type_unicode(engine, SECRET)

    _wait_for_text(atspi, SECRET)
    assert _selection(env) == prior
    _wait_for_sole_owner(display, token)
    _assert_secret_not_exposed(display, token)
    _assert_owners_released_on_replace(env, display, token)


def test_keyboard_type_method_unicode_leaves_an_empty_selection_empty(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    display, _, atspi = _start_kwrite(start_session, launch, wait_for_app, tmp_path / "empty.txt")
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

    _type_unicode(engine, SECRET)

    _wait_for_text(atspi, SECRET)
    assert _offered_types(env) == set()
    _wait_until(lambda: not _spawned(display, token), "spawned processes to exit")


def test_transient_offer_carries_the_secret_hint_until_the_paste(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    display, kwrite_pid, atspi = _start_kwrite(
        start_session, launch, wait_for_app, tmp_path / "hint.txt"
    )
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
        _assert_pasted(call.result(timeout=CALL_TIMEOUT_SECONDS), SECRET)

    _wait_for_text(atspi, SECRET)
    assert _selection(env) == prior


def test_user_copy_during_transient_ownership_is_never_overwritten(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    display, kwrite_pid, atspi = _start_kwrite(
        start_session, launch, wait_for_app, tmp_path / "race.txt"
    )
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
        with pytest.raises(RuntimeError) as excinfo:
            call.result(timeout=CALL_TIMEOUT_SECONDS)
        assert str(excinfo.value) == f"Failed to type unicode: {SECRET!r}"

    assert not _spawned(display, token)
    assert _wl_paste(env, "--no-newline").stdout == USER_COPY.encode()
    assert SECRET not in _focused_text(atspi)


def test_stalled_prior_owner_fails_before_paste_without_leaving_an_owner(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    display, _, atspi = _start_kwrite(start_session, launch, wait_for_app, tmp_path / "stalled.txt")
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
            with pytest.raises(RuntimeError) as excinfo:
                engine.keyboard_type(SECRET, method="unicode")
            assert str(excinfo.value) == f"Failed to type unicode: {SECRET!r}"
            # The compositor announces types without asking the frozen owner.
            assert _offered_types(env) == prior_types
        finally:
            owner.send_signal(signal.SIGCONT)

        assert _focused_text(atspi) == ""
        _wait_until(lambda: not _spawned(display, token), "failed helper to exit")
    finally:
        owner.terminate()
        owner.wait(timeout=WAIT_TIMEOUT_SECONDS)


def test_repeated_calls_hand_the_prior_selection_to_exactly_one_owner(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    display, _, atspi = _start_kwrite(start_session, launch, wait_for_app, tmp_path / "repeat.txt")
    env = _tool_env(display)
    _seed(env, PRIOR_TEXT.encode())
    prior = _selection(env)
    token = _mark_engine_calls(monkeypatch)

    typed = ""
    retired: frozenset[int] = frozenset()
    for index in range(3):
        text = f"{SECRET}{index}"
        _type_unicode(engine, text)
        typed += text
        _wait_for_text(atspi, typed)
        assert _selection(env) == prior
        retired |= {_wait_for_sole_owner(display, token, retired)}

    _assert_secret_not_exposed(display, token)
    _assert_owners_released_on_replace(env, display, token)


def test_live_session_keeps_the_restored_selection_after_disconnect(
    engine: AutomationEngine,
    wait_for_app: Callable[[str], ElementList],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with live_kwin() as live:
        env = _tool_env(live.wayland_display)
        info = engine.session_connect(
            dbus_address=live.dbus_address, wayland_display=live.wayland_display
        )
        assert info.kind == "live", info
        assert info.input_backend == "eis", info
        atspi = AtspiOracle(live.dbus_address)
        document = tmp_path / "live.txt"
        engine.app_launch(f"kwrite {shlex.quote(str(document))}")
        wait_for_app("kwrite")
        engine.window_focus(WindowSelector(app_id="org.kde.kwrite"))
        _seed(env, PRIOR_TEXT.encode())
        prior = _selection(env)
        token = _mark_engine_calls(monkeypatch)

        _type_unicode(engine, SECRET)
        _wait_for_text(atspi, SECRET)
        assert _selection(env) == prior

        stopped = engine.session_stop()
        assert stopped.render() == "Disconnected from live session.", stopped.render()
        assert live.process.poll() is None
        # The restored selection belongs to the desktop, not to the MCP connection.
        assert _selection(env) == prior
        _wait_for_sole_owner(live.wayland_display, token)
        _assert_secret_not_exposed(live.wayland_display, token)
        _assert_owners_released_on_replace(env, live.wayland_display, token)
