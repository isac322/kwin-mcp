"""End-to-end contracts for clipboard_read and clipboard_write on the engine.

The selection is checked with the test's own ``wl-copy``/``wl-paste`` on the
session socket, KWrite's document through the independent AT-SPI oracle, and
helper processes through ``/proc``; never only through the tool's own report.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from typing import TYPE_CHECKING

import pytest
from atspi_oracle import AtspiOracle
from session_harness import live_kwin

from kwin_mcp.core import NO_CLIPBOARD_TEXT
from kwin_mcp.models import ClipboardText, ClipboardWriteResult, WindowSelector

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import AppLaunchResult, ElementList, SessionInfo

POLL_INTERVAL_SECONDS = 0.1
WAIT_TIMEOUT_SECONDS = 5.0
WL_TIMEOUT_SECONDS = 30.0
CLIPBOARD_SET_TIMEOUT_SECONDS = 5.0


def _env(socket: str) -> dict[str, str]:
    env = {**os.environ, "WAYLAND_DISPLAY": socket}
    env.pop("DISPLAY", None)
    return env


def _wl_paste(socket: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["wl-paste", "--no-newline"],
        env=_env(socket),
        capture_output=True,
        timeout=WL_TIMEOUT_SECONDS,
        check=False,
    )


def _wl_copy(socket: str, data: bytes) -> None:
    # The forked owner inherits stdio, so nothing may be captured.
    subprocess.run(
        ["wl-copy"],
        input=data,
        env=_env(socket),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=WAIT_TIMEOUT_SECONDS,
        check=True,
    )


def _wl_processes(socket: str) -> set[int]:
    """Live wl-copy/wl-paste processes whose environment targets ``socket``."""
    target = f"WAYLAND_DISPLAY={socket}".encode()
    found: set[int] = set()
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                with open(f"{entry.path}/stat", "rb") as stat:
                    state = stat.read().rsplit(b")", 1)[1].split()[0]
                with open(f"{entry.path}/cmdline", "rb") as cmdline:
                    argv0 = cmdline.read().split(b"\0", 1)[0]
                with open(f"{entry.path}/environ", "rb") as environ:
                    variables = environ.read().split(b"\0")
            except OSError:
                continue
            name = os.path.basename(argv0)
            if state != b"Z" and name in {b"wl-copy", b"wl-paste"} and target in variables:
                found.add(int(entry.name))
    return found


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as stat:
            return stat.read().rsplit(b")", 1)[1].split()[0] != b"Z"
    except OSError:
        return False


def _wait_until(predicate: Callable[[], bool], description: str) -> None:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out waiting for {description}")
        time.sleep(POLL_INTERVAL_SECONDS)


def _focused_text(atspi: AtspiOracle) -> str:
    """Text of KWrite's focused document, read by the test's own AT-SPI walk."""
    nodes = [
        n
        for n in atspi.find(app="kwrite", role="text")
        if "focused" in n.states and n.error is None
    ]
    assert len(nodes) == 1, [(n.role, n.name, sorted(n.states)) for n in nodes]
    return nodes[0].text or ""


def _clipboard_session(start_session: Callable[..., SessionInfo]) -> SessionInfo:
    info = start_session(enable_clipboard=True)
    assert info.clipboard is True, info
    return info


def test_clipboard_roundtrip_and_paste_reach_kwrite(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
    tmp_path: Path,
) -> None:
    info = _clipboard_session(start_session)
    assert info.input_backend == "eis", info
    launch(f"kwrite {shlex.quote(str(tmp_path / 'kwin-mcp-clipboard.txt'))}")
    wait_for_app("kwrite")
    engine.window_focus(WindowSelector(app_id="org.kde.kwrite"))
    atspi = AtspiOracle(info.dbus_address)
    payload = "Clipboard QA\n한글\t42!"

    written = engine.clipboard_write(payload)
    assert written.render() == f"Clipboard set: {payload!r}"
    assert _wl_paste(info.wayland_socket).stdout == payload.encode()
    assert engine.clipboard_read() == ClipboardText(text=payload)

    engine.keyboard_press("ctrl+v")
    _wait_until(lambda: _focused_text(atspi) == payload, f"document text {payload!r}")


@pytest.mark.qa("QA-GATE-005")
def test_disabled_virtual_gate_has_remedy(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    info = start_session()
    assert info.clipboard is False, info
    assert "Clipboard: disabled" in info.render().splitlines()
    before = _wl_processes(info.wayland_socket)

    for call in (engine.clipboard_read, lambda: engine.clipboard_write(text="x")):
        with pytest.raises(RuntimeError) as excinfo:
            call()
        message = str(excinfo.value)
        assert message == NO_CLIPBOARD_TEXT
        assert message.splitlines()[0] == "Clipboard not enabled."
        assert "enable_clipboard" in message and "session_connect" in message

    # The gate refuses before anything is spawned, so no wl-* child can hang.
    assert _wl_processes(info.wayland_socket) == before == set()


@pytest.mark.qa("QA-CLIP-001")
def test_read_returns_structured_text(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    info = _clipboard_session(start_session)
    _wl_copy(info.wayland_socket, "héllo\n".encode())
    # The test's own wl-paste --no-newline is the 0.11 reference: it appends no
    # newline of its own and returns the selection bytes as they are.
    reference = _wl_paste(info.wayland_socket)
    assert reference.returncode == 0, reference.stderr
    assert reference.stdout == "héllo\n".encode()

    read = engine.clipboard_read()

    assert read == ClipboardText(text="héllo\n")
    assert read.model_dump(mode="json") == {"text": "héllo\n"}
    assert read.render() == "héllo\n"


@pytest.mark.qa("QA-CLIP-002")
def test_empty_selection_is_failed_to_read(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    info = _clipboard_session(start_session)
    reference = _wl_paste(info.wayland_socket)
    assert reference.returncode != 0, reference.stdout

    # SPEC-GAP-12: an empty selection is the 0.11 nonzero-exit error, not "".
    with pytest.raises(RuntimeError) as excinfo:
        engine.clipboard_read()
    assert str(excinfo.value).startswith("Failed to read clipboard: "), excinfo.value
    assert str(excinfo.value) == (
        f"Failed to read clipboard: {reference.stderr.decode(errors='replace')}"
    )

    empty_path = tmp_path / "no-wl-clipboard"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))
    with pytest.raises(RuntimeError) as excinfo:
        engine.clipboard_read()
    assert str(excinfo.value) == (
        "wl-paste not found. Install wl-clipboard "
        "(e.g. 'sudo pacman -S wl-clipboard' or 'sudo apt install wl-clipboard')."
    )


@pytest.mark.qa("QA-CLIP-003")
def test_write_echo_and_length(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    info = _clipboard_session(start_session)
    socket = info.wayland_socket

    mixed = "a\x00b\r\n😀"
    written = engine.clipboard_write(mixed)
    assert written == ClipboardWriteResult(length=6, echo=repr(mixed))
    assert written.render() == f"Clipboard set: {mixed!r}"
    assert _wl_paste(socket).stdout == mixed.encode()
    assert engine.clipboard_read() == ClipboardText(text=mixed)

    large = "x" * 3_000_000
    written = engine.clipboard_write(large)
    assert written == ClipboardWriteResult(length=3_000_000, echo="<3000000 characters>")
    assert written.render() == "Clipboard set: <3000000 characters>"
    assert _wl_paste(socket).stdout == large.encode()

    # 8 MiB of mixed-width text: length counts characters, not bytes.
    unit = "é😀z"
    huge = unit * ((8 * 1024 * 1024) // len(unit.encode()))
    written = engine.clipboard_write(huge)
    assert written.length == len(huge)
    assert written.echo == f"<{len(huge)} characters>"
    assert _wl_paste(socket).stdout == huge.encode()
    assert engine.clipboard_read().text == huge


@pytest.mark.qa("QA-CLIP-004")
def test_ownership_timeout_kills_group(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    info = _clipboard_session(start_session)
    shim_dir = tmp_path / "wl-copy-shim"
    shim_dir.mkdir()
    pid_file = tmp_path / "shim-child.pid"
    # Never confirms: the shim waits on a child of its own, in its process group.
    (shim_dir / "wl-copy").write_text(
        f"#!/bin/sh\ncat > /dev/null\nsleep 120 &\necho $! > {shlex.quote(str(pid_file))}\nwait\n",
        encoding="utf-8",
    )
    (shim_dir / "wl-copy").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")

    started = time.monotonic()
    with pytest.raises(RuntimeError) as excinfo:
        engine.clipboard_write("x")
    elapsed = time.monotonic() - started

    assert str(excinfo.value) == (
        "Failed to set clipboard: wl-copy did not confirm the selection within 5s"
    )
    assert CLIPBOARD_SET_TIMEOUT_SECONDS * 0.9 <= elapsed <= CLIPBOARD_SET_TIMEOUT_SECONDS * 3
    child = int(pid_file.read_text(encoding="utf-8"))
    try:
        _wait_until(lambda: not _alive(child), "the shim's process group to be killed")
    finally:
        if _alive(child):
            os.kill(child, signal.SIGKILL)
    assert _wl_paste(info.wayland_socket).stdout != b"x"


@pytest.mark.qa("QA-CLIP-005")
def test_clipboard_persists_until_session_end(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
) -> None:
    info = _clipboard_session(start_session)
    socket = info.wayland_socket
    engine.clipboard_write("a")
    owners = _wl_processes(socket)
    assert owners, "clipboard_write left no wl-copy owner serving the text"

    kcalc = launch("kcalc")
    wait_for_app("kcalc")
    os.kill(kcalc.pid, signal.SIGTERM)
    _wait_until(lambda: not _alive(kcalc.pid), "kcalc to quit")
    assert _wl_paste(socket).stdout == b"a"
    assert engine.clipboard_read() == ClipboardText(text="a")

    engine.session_stop()
    _wait_until(lambda: not any(_alive(pid) for pid in owners), "the old owner to end")

    restarted = _clipboard_session(start_session)
    with pytest.raises(RuntimeError) as excinfo:
        engine.clipboard_read()
    assert str(excinfo.value).startswith("Failed to read clipboard: "), excinfo.value
    assert _wl_paste(restarted.wayland_socket).stdout != b"a"


@pytest.mark.qa("QA-CLIP-006")
def test_live_always_enabled(engine: AutomationEngine) -> None:
    with live_kwin() as live:
        info = engine.session_connect(
            dbus_address=live.dbus_address, wayland_display=live.wayland_display
        )
        assert info.kind == "live", info
        assert info.clipboard is True, info
        assert "Clipboard: enabled" in info.render().splitlines()

        written = engine.clipboard_write("live-45-클립")
        assert written == ClipboardWriteResult(length=len("live-45-클립"), echo="'live-45-클립'")
        assert _wl_paste(live.wayland_display).stdout == "live-45-클립".encode()
        _wl_copy(live.wayland_display, b"from-the-desktop")
        assert engine.clipboard_read() == ClipboardText(text="from-the-desktop")
        engine.session_stop()
