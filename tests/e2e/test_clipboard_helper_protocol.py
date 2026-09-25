"""Protocol regression tests for the private clipboard helper (issue #45).

These tests drive ``python -m kwin_mcp.clipboard`` directly against a
test-owned KWin and only observe external state: the helper's stdout event
lines and exit code, its upstream ``WAYLAND_DEBUG`` request trace, and the
selection through ``wl-paste``/``wl-copy``.

The READY-timeout test controls ordering instead of relying on timing. The
prior selection belongs to an independent real data-control client (this file
run with ``--held-source``). It holds back only the helper's final snapshot
request: the first request for the last-offered MIME (a private type no other
client asks for) that arrives after every other offered MIME has been served.
That request has already been routed, so the test can SIGSTOP KWin before
releasing the reply. The snapshot pipe runs directly between the two clients,
so the snapshot finishes, but the helper's set_selection roundtrip cannot. KWin
is resumed only after the trace shows the helper's second set_selection (the
restore request).
"""

from __future__ import annotations

import contextlib
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from session_harness import live_kwin

from kwin_mcp.clipboard import (
    MAX_COPY_BYTES,
    RESTORE_ROUNDTRIP_S,
    SET_ROUNDTRIP_S,
    SNAPSHOT_DEADLINE_S,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from session_harness import LiveKWin

WAIT_TIMEOUT_SECONDS = 5.0
SECRET = "SECRET-45-helper-한글🔑".encode()
LATER_COPY = "LATER-45-user-copy"
SET_SELECTION_REQUEST = re.compile(
    rb"-> \w+_data_control_device_v1#\d+\.set_selection\(\w+_data_control_source_v1#"
)


# ---------------------------------------------------------------------------
# Process plumbing
# ---------------------------------------------------------------------------


class _Proc:
    """Subprocess with line-oriented stdout and a continuously drained stderr."""

    def __init__(self, args: list[str], env: dict[str, str]) -> None:
        self.popen = subprocess.Popen(
            args,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self._stdout = b""
        self.stderr = bytearray()
        self._cond = threading.Condition()
        self._drainer = threading.Thread(target=self._drain_stderr, daemon=True)
        self._drainer.start()

    def _drain_stderr(self) -> None:
        assert self.popen.stderr is not None
        fd = self.popen.stderr.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            with self._cond:
                if not chunk:
                    self._cond.notify_all()
                    return
                self.stderr += chunk
                self._cond.notify_all()

    def wait_stderr(self, pattern: re.Pattern[bytes], count: int, timeout: float) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: len(pattern.findall(self.stderr)) >= count, timeout)

    def send(self, data: bytes) -> None:
        assert self.popen.stdin is not None
        self.popen.stdin.write(data)
        self.popen.stdin.flush()

    def close_stdin(self) -> None:
        if self.popen.stdin is not None:
            with contextlib.suppress(BrokenPipeError, OSError):
                self.popen.stdin.close()

    def read_line(self, timeout: float = WAIT_TIMEOUT_SECONDS) -> str | None:
        assert self.popen.stdout is not None
        fd = self.popen.stdout.fileno()
        deadline = time.monotonic() + timeout
        while b"\n" not in self._stdout:
            remaining = max(deadline - time.monotonic(), 0.0)
            readable, _, _ = select.select([fd], [], [], remaining)
            if not readable:
                return None
            chunk = os.read(fd, 4096)
            if not chunk:
                return None
            self._stdout += chunk
        line, self._stdout = self._stdout.split(b"\n", 1)
        return line.decode(errors="replace")

    def lines_until_eof(self, timeout: float = WAIT_TIMEOUT_SECONDS) -> list[str]:
        lines: list[str] = []
        while (line := self.read_line(timeout)) is not None:
            lines.append(line)
        return lines

    def alive(self) -> bool:
        return self.popen.poll() is None

    def wait(self, timeout: float = WAIT_TIMEOUT_SECONDS) -> int:
        return self.popen.wait(timeout)

    def kill(self) -> None:
        if self.popen.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.popen.pid, signal.SIGKILL)
            self.popen.wait(timeout=WAIT_TIMEOUT_SECONDS)
        self.close_stdin()
        if self.popen.stdout is not None:
            self.popen.stdout.close()
        self._drainer.join(timeout=WAIT_TIMEOUT_SECONDS)
        if self.popen.stderr is not None:
            self.popen.stderr.close()


class _Compositor:
    """SIGSTOP/SIGCONT control of the test-owned kwin_wayland."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._stopped = False

    def stop(self) -> None:
        os.kill(self.pid, signal.SIGSTOP)
        self._stopped = True

    def resume(self) -> None:
        if self._stopped:
            os.kill(self.pid, signal.SIGCONT)
            self._stopped = False


def _kwin_pid(session: LiveKWin) -> int:
    """PID of kwin_wayland inside the harness's own process group."""
    pgid = session.process.pid
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        comm = stat[stat.index("(") + 1 : stat.rindex(")")]
        fields = stat[stat.rindex(")") + 2 :].split()
        if comm == "kwin_wayland" and int(fields[2]) == pgid:
            return int(entry)
    raise AssertionError("test-owned kwin_wayland process not found")


def _client_env(session: LiveKWin) -> dict[str, str]:
    env = {**os.environ, "WAYLAND_DISPLAY": session.wayland_display}
    env.pop("DISPLAY", None)
    return env


def _spawn_helper(env: dict[str, str], *, trace: bool = False) -> _Proc:
    helper_env = dict(env)
    if trace:
        helper_env["WAYLAND_DEBUG"] = "1"
    return _Proc([sys.executable, "-m", "kwin_mcp.clipboard"], helper_env)


def _paste(env: dict[str, str], mime: str | None = None) -> bytes:
    command = ["wl-paste", "--no-newline"] + (["--type", mime] if mime else [])
    result = subprocess.run(command, env=env, capture_output=True, timeout=WAIT_TIMEOUT_SECONDS)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _types(env: dict[str, str]) -> set[str]:
    result = subprocess.run(
        ["wl-paste", "--list-types"], env=env, capture_output=True, timeout=WAIT_TIMEOUT_SECONDS
    )
    if result.returncode != 0:
        return set()
    return set(result.stdout.decode().split())


def _copy(env: dict[str, str], text: str) -> None:
    # wl-copy returns only after its selection roundtrip completed.
    subprocess.run(["wl-copy", "--", text], env=env, timeout=WAIT_TIMEOUT_SECONDS, check=True)


@pytest.fixture
def kwin() -> Iterator[LiveKWin]:
    with live_kwin() as session:
        yield session


@pytest.fixture
def helpers() -> Iterator[list[_Proc]]:
    """Every spawned process is killed on exit, including on assertion failure."""
    spawned: list[_Proc] = []
    try:
        yield spawned
    finally:
        for proc in spawned:
            proc.kill()


# ---------------------------------------------------------------------------
# READY timeout: restored prior selection must stay owned
# ---------------------------------------------------------------------------


def test_ready_timeout_restores_prior_selection_and_keeps_serving(
    kwin: LiveKWin, helpers: list[_Proc]
) -> None:
    env = _client_env(kwin)
    prior = {
        "text/plain;charset=utf-8": "PRIOR-45-timeout-유니코드".encode(),
        "application/x-kwin-mcp-45-held": bytes(range(256)) * 16,
    }
    source_args = [sys.executable, str(Path(__file__)), "--held-source"]
    for mime, data in prior.items():
        source_args += [mime, data.hex()]
    source = _Proc(source_args, env)
    helpers.append(source)
    assert source.read_line() == "OWNED", source.stderr.decode(errors="replace")

    helper = _spawn_helper(env, trace=True)
    helpers.append(helper)
    compositor = _Compositor(_kwin_pid(kwin))
    try:
        source.send(b"COUNT\n")
        helper.send(b"COPY %d\n" % len(SECRET) + SECRET)
        held = source.read_line(timeout=SNAPSHOT_DEADLINE_S)
        assert held == f"HELD {list(prior)[-1]}", held

        compositor.stop()
        stopped_at = time.monotonic()
        source.send(b"RELEASE\n")

        # The first set_selection installs the injected source; the second is
        # the restore request sent only after the unanswered roundtrip expired.
        assert helper.wait_stderr(
            SET_SELECTION_REQUEST, 2, SET_ROUNDTRIP_S + WAIT_TIMEOUT_SECONDS
        ), helper.stderr[-2000:].decode(errors="replace")
        assert time.monotonic() - stopped_at >= SET_ROUNDTRIP_S
        assert helper.read_line(timeout=0) is None  # no stdout line (no READY) yet
    finally:
        compositor.resume()

    first = helper.read_line(timeout=RESTORE_ROUNDTRIP_S + WAIT_TIMEOUT_SECONDS)
    assert first is not None and first.startswith("ERR "), first

    assert _types(env) == set(prior)
    for mime, data in prior.items():
        assert _paste(env, mime) == data
    assert source.lines_until_eof() == ["CANCELLED"]

    # The restored owner survives the parent closing its pipe.
    helper.close_stdin()
    assert _paste(env, "text/plain;charset=utf-8") == prior["text/plain;charset=utf-8"]
    assert helper.alive()

    _copy(env, LATER_COPY)
    assert helper.lines_until_eof() == ["CANCELLED"]
    assert helper.wait() == 2
    assert _paste(env) == LATER_COPY.encode()
    assert SECRET not in helper.stderr


# ---------------------------------------------------------------------------
# Framing errors are terminal
# ---------------------------------------------------------------------------


def _assert_single_protocol_error(helper: _Proc) -> None:
    lines = helper.lines_until_eof()
    assert len(lines) == 1, lines
    assert lines[0].startswith("ERR "), lines
    assert helper.wait() == 2


def test_oversized_copy_frame_is_terminal_without_backpressure(
    kwin: LiveKWin, helpers: list[_Proc]
) -> None:
    env = _client_env(kwin)
    _copy(env, "KEEP-45-oversized")
    helper = _spawn_helper(env)
    helpers.append(helper)

    # Far more than a pipe buffer of command-shaped payload: none of it may be
    # interpreted, and the writer must fail fast instead of blocking forever.
    frame = b"COPY %d\n" % (MAX_COPY_BYTES + 1) + b"ARM\nRESTORE\nQUIT\n" * 65536
    outcome: list[BaseException | None] = []

    def write() -> None:
        try:
            helper.send(frame)
            outcome.append(None)
        except BrokenPipeError as exc:
            outcome.append(exc)

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    _assert_single_protocol_error(helper)
    writer.join(timeout=WAIT_TIMEOUT_SECONDS)
    assert not writer.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], BrokenPipeError), outcome
    assert _paste(env) == b"KEEP-45-oversized"


@pytest.mark.parametrize(
    "frame",
    [b"COPY 12x\nRESTORE\nARM\n", b"COPY -3\nabc", b"COPY\nabc", b"hunter2-45\nARM\n"],
    ids=["non-digit-length", "negative-length", "missing-length", "unknown-command"],
)
def test_malformed_frame_is_terminal(kwin: LiveKWin, helpers: list[_Proc], frame: bytes) -> None:
    env = _client_env(kwin)
    _copy(env, "KEEP-45-malformed")
    helper = _spawn_helper(env)
    helpers.append(helper)
    helper.send(frame)
    lines = helper.lines_until_eof()
    assert len(lines) == 1 and lines[0].startswith("ERR "), lines
    assert b"hunter2" not in lines[0].encode()
    assert helper.wait() == 2
    assert _paste(env) == b"KEEP-45-malformed"


def test_framing_error_while_owned_restores_and_keeps_serving(
    kwin: LiveKWin, helpers: list[_Proc]
) -> None:
    env = _client_env(kwin)
    _copy(env, "PRIOR-45-owned")
    helper = _spawn_helper(env)
    helpers.append(helper)
    helper.send(b"COPY %d\n" % len(SECRET) + SECRET)
    assert helper.read_line() == "READY"
    assert _paste(env, "text/plain;charset=utf-8") == SECRET

    helper.send(b"BOGUS\nARM\n")
    first = helper.read_line()
    assert first is not None and first.startswith("ERR "), first
    assert helper.read_line() == "RESTORED"
    assert _paste(env) == b"PRIOR-45-owned"
    assert helper.alive()

    _copy(env, LATER_COPY)
    assert helper.lines_until_eof() == ["CANCELLED"]
    assert helper.wait() == 2
    assert _paste(env) == LATER_COPY.encode()


# ---------------------------------------------------------------------------
# Independent prior-selection owner (real data-control client)
# ---------------------------------------------------------------------------


def _held_source_main(argv: list[str]) -> int:
    """Own a multi-MIME selection; after COUNT, hold the final snapshot request.

    The held request is the first send for the last-offered MIME that arrives
    after every other offered MIME was served since COUNT, i.e. the last
    receive of a snapshot that walks the offer list; earlier or unrelated
    requests are served at once. stdout: OWNED once the selection is set,
    HELD <mime> when the held request arrives, CANCELLED when replaced.
    stdin: COUNT, RELEASE.
    """
    import ctypes

    from kwin_mcp import clipboard as cb

    offers = {mime: bytes.fromhex(data) for mime, data in zip(argv[0::2], argv[1::2], strict=True)}
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    wl = cb._Wl()
    lib = wl.lib
    lib.wl_display_roundtrip.restype = ctypes.c_int
    lib.wl_display_roundtrip.argtypes = [ctypes.c_void_p]

    display = lib.wl_display_connect(None)
    if not display:
        return 1
    handlers: dict[int, Callable[[str, Any], None]] = {}
    globals_found: list[tuple[int, str]] = []
    pending: list[tuple[str, int]] = []
    state = {"cancelled": False}

    def dispatch(_data: Any, target: int | None, _op: int, msg: Any, args: Any) -> int:
        try:
            handler = handlers.get(int(target or 0))
            if handler is not None:
                handler(msg.contents.name.decode(), args)
        except Exception as exc:  # never unwind through libwayland
            sys.stderr.write(f"held-source handler error: {exc!r}\n")
        return 0

    trampoline = cb._DISPATCHER_T(dispatch)

    def listen(proxy: int, handler: Callable[[str, Any], None]) -> None:
        lib.wl_proxy_add_dispatcher(proxy, trampoline, None, None)
        handlers[int(proxy)] = handler

    def on_registry(name: str, args: Any) -> None:
        if name == "global":
            globals_found.append((args[0].u, args[1].s.decode()))

    def on_source(name: str, args: Any) -> None:
        if name == "send":
            pending.append((args[0].s.decode(), args[1].h))
        elif name == "cancelled":
            state["cancelled"] = True

    none = (cb._WlArgument * 1)()
    registry = lib.wl_proxy_marshal_array_constructor_versioned(
        display, 1, none, ctypes.byref(wl.wl_registry_interface), 1
    )
    listen(registry, on_registry)
    lib.wl_display_roundtrip(display)

    names = {iface: name for name, iface in globals_found}
    prefix = "ext" if "ext_data_control_manager_v1" in names else "zwlr"
    tables = cb._ProtocolTables(prefix, wl.wl_seat_interface)

    def bind(name: int, iface: Any) -> int:
        bind_args = (cb._WlArgument * 4)()
        bind_args[0].u = name
        bind_args[1].s = iface.name
        bind_args[2].u = 1
        return lib.wl_proxy_marshal_array_flags(registry, 0, ctypes.byref(iface), 1, 0, bind_args)

    manager = bind(names[tables.manager_global], tables.manager)
    seat = bind(names["wl_seat"], wl.wl_seat_interface)
    device_args = (cb._WlArgument * 2)()
    device_args[1].o = seat
    device = lib.wl_proxy_marshal_array_constructor_versioned(
        manager, 1, device_args, ctypes.byref(tables.device), 1
    )
    source = lib.wl_proxy_marshal_array_constructor_versioned(
        manager, 0, (cb._WlArgument * 1)(), ctypes.byref(tables.source), 1
    )
    listen(source, on_source)
    for mime in offers:
        offer_args = (cb._WlArgument * 1)()
        offer_args[0].s = mime.encode()
        lib.wl_proxy_marshal_array(source, 0, offer_args)
    select_args = (cb._WlArgument * 1)()
    select_args[0].o = source
    lib.wl_proxy_marshal_array(device, 0, select_args)
    lib.wl_display_roundtrip(display)
    print("OWNED", flush=True)

    def serve(mime: str, fd: int) -> None:
        with contextlib.suppress(OSError):
            os.set_blocking(fd, True)
            view = memoryview(offers.get(mime, b""))
            while view:
                view = view[os.write(fd, view) :]
        os.close(fd)

    counting = False
    released = False
    last_mime = list(offers)[-1]
    served_since_count: set[str] = set()
    held: list[tuple[str, int]] = []
    wl_fd = lib.wl_display_get_fd(display)
    stdin_open = True
    while not state["cancelled"]:
        if lib.wl_display_dispatch_pending(display) < 0:
            return 2
        for mime, fd in pending:
            if (
                counting
                and not released
                and not held
                and mime == last_mime
                and served_since_count >= set(offers) - {last_mime}
            ):
                held.append((mime, fd))
                print(f"HELD {mime}", flush=True)
                continue
            if counting:
                served_since_count.add(mime)
            serve(mime, fd)
        pending.clear()
        if state["cancelled"]:
            break
        lib.wl_display_flush(display)
        poller = select.poll()
        poller.register(wl_fd, select.POLLIN)
        if stdin_open:
            poller.register(0, select.POLLIN)
        for fd, _events in poller.poll():
            if fd == 0:
                line = sys.stdin.buffer.readline()
                if not line:
                    stdin_open = False
                elif line.strip() == b"COUNT":
                    counting = True
                elif line.strip() == b"RELEASE":
                    released = True
                    for mime, held_fd in held:
                        serve(mime, held_fd)
                    held.clear()
            elif lib.wl_display_dispatch(display) < 0:
                return 2
    print("CANCELLED", flush=True)
    lib.wl_display_disconnect(display)
    return 0


if __name__ == "__main__" and sys.argv[1:2] == ["--held-source"]:
    sys.exit(_held_source_main(sys.argv[2:]))
