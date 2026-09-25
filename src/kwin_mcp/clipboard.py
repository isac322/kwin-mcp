"""Private native clipboard helper for issue #45 (internal, not public API).

One-shot data-control client for the official ext-data-control-v1 Wayland
protocol. It falls back to zwlr-data-control-unstable-v1, which has the same
wire layout under different interface names; KWin 6.3 advertises only zwlr.
The bindings are stdlib ctypes over libwayland-client. There is no pywayland,
GTK, generated code or hand-rolled wire formatting. Requests go through
wl_proxy_marshal_array / wl_proxy_marshal_array_flags /
wl_proxy_marshal_array_constructor_versioned, using wl_interface/wl_message
tables that match generated protocol code. Events arrive through
wl_proxy_add_dispatcher.

Invocation: ``sys.executable -m kwin_mcp.clipboard`` with no arguments. There
is no console entry point. The helper inherits WAYLAND_DISPLAY and
XDG_RUNTIME_DIR from the parent.

stdin command protocol (binary-safe framing):

    COPY <len>\\n<len raw bytes>
        One COPY per process; len <= MAX_COPY_BYTES is checked before any
        payload is buffered. The helper first snapshots EVERY MIME type of
        the current selection, with per-MIME, total and count bounds and a
        deadline. If a read is partial, errors or exceeds a bound, or if the
        selection changes mid-snapshot, the snapshot fails before anything
        is mutated. An empty selection is recorded as "empty", not as a
        failure. On success the helper creates a source offering
        ``text/plain;charset=utf-8``, ``text/plain`` and the KDE secret
        marker ``x-kde-passwordManagerHint``, calls set_selection, completes
        a compositor roundtrip and only then prints ``READY``. If stdin
        closes before set_selection, the helper aborts without mutating
        anything. Any failure before READY prints ``ERR`` and exits 2. The
        one exception is a set_selection roundtrip timeout: the helper then
        tries a bounded restore. If that installs a non-empty prior
        clipboard, it still prints ``ERR`` (no READY, so the parent must not
        paste) but keeps serving the restored clipboard like any restored
        owner, until a later copy or compositor disconnect, and exits 2.
    ARM\\n
        Arms paste-completion accounting; prints ``ARMED``. Only a
        text/plain* send that completes (fully written and closed) AFTER
        arming counts as the requested paste and emits ``TRANSFERRED``;
        sends completed before ARMED, sends for the secret-hint MIME, and
        sends ending in EPIPE/error never count. A manager peeking at the
        text MIME before ARMED cannot fake completion.
    RESTORE\\n
        If our source still owns the selection (no ``cancelled`` observed),
        it is replaced by a restore source re-offering every snapshotted MIME
        type and payload (or the selection is cleared when the prior
        selection was empty). A roundtrip completes and ``RESTORED`` prints.
        If the prior
        selection was empty, the helper then exits 0. Otherwise it stays
        alive serving the restored data until a later selection cancels it
        (exit 0) or the compositor connection ends; like wl-copy there is no
        lifetime cap. Once ``cancelled`` has been
        observed the helper has already exited, so it never restores over
        the new copy. A failed restore prints ``ERR`` and exits 2.
    QUIT\\n
        While idle: clean exit. While owning the injected selection: bounded
        restore, then continue like stdin EOF. While restore-owned: treated
        like stdin EOF; the helper keeps serving, because exiting would drop
        the restored selection.

Framing errors are terminal: a malformed or oversized COPY header, an
unknown command, a command line over MAX_HEADER_BYTES, or buffered input over
MAX_INBUF_BYTES prints a single ``ERR protocol <why>``. Every remaining byte
is discarded and never parsed as a command, and stdin is swapped for
/dev/null so a parent that is still writing gets EPIPE instead of blocking.
If nothing is owned, the helper exits 2. If the injected secret is owned, it
runs the bounded restore and then keeps serving the restored clipboard. The
exit code is 2 in all of these cases. Semantic errors on well-formed frames
(``busy``, ``cannot arm now``, ``nothing to restore``) are single ERR lines
and leave the helper running.

stdout event lines: READY, ARMED, TRANSFERRED, CANCELLED, RESTORED,
ERR <why>. stderr carries diagnostics only; payload bytes never reach argv,
the environment, stdout, or any file.

Honest boundaries (Wayland provides no stronger guarantees):

- ``send`` carries no requester identity: ``TRANSFERRED`` means *some* client
  fully read a text MIME while armed, not provably the intended app. The
  secret marker suppresses cooperative clipboard managers (Klipper stores
  hinted selections transiently, never in history); a non-cooperative
  manager reading ``text/plain`` anyway can still observe or falsely
  complete the transfer. There is no compare-and-swap on the selection: a
  third-party copy landing between our last dispatched event and RESTORE is
  undetectable.
- Bytes already written to a ``send`` fd remain readable by that requester
  after our selection is replaced; late/second ``receive`` requests are
  served only while we own the selection and are not guaranteed otherwise.
- Observed ``cancelled`` on our source is authoritative for "another client
  now owns the selection" and always ends the helper without restoring over
  the new copy.
- After ``RESTORED`` the process is intentionally persistent (like
  wl-copy): the restored selection lives only as long as its owner process.
  The parent must start the helper in its own session and must not signal
  it after ``RESTORED``. stdin EOF while restore-owned is ignored and the
  helper keeps serving. stdin EOF, a phase deadline, or a display error
  while the injected secret is still owned triggers a best-effort bounded
  restore. It is event-driven, with no fixed sleep. If a non-empty prior
  selection was restored, the helper keeps serving it after the parent is
  gone. SIGTERM/SIGINT perform the same bounded restore and then exit,
  which drops the restored source.
- The injected secret and the snapshotted bytes are held in bytearrays that
  are wiped on every exit path. The secret is never persisted in files and
  is wiped once the restore swap completes. Python object lifetimes make an
  absolute zero-copy guarantee impossible; only the canonical buffers are
  wiped. The payload served for the marker MIME is the inert string
  "secret".

Exit codes: 0 for clean completion (including the CANCELLED path and
SIGTERM), 1 for startup or usage failure (no libwayland, no compositor, no
data-control), 2 for failure before READY, a failed restore, or a runtime
protocol/compositor error.
"""

import contextlib
import ctypes
import ctypes.util
import fcntl
import os
import select
import signal
import sys
import time
from collections.abc import Callable
from typing import Any

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

MIME_TEXT_UTF8 = "text/plain;charset=utf-8"
MIME_TEXT = "text/plain"
MIME_KDE_HINT = "x-kde-passwordManagerHint"
HINT_PAYLOAD = b"secret"

MAX_MIMES = 32
MAX_MIME_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_COPY_BYTES = 1024 * 1024
MAX_INBUF_BYTES = MAX_COPY_BYTES + 4096
MAX_HEADER_BYTES = 64

ROUNDTRIP_DEADLINE_S = 1.5
SNAPSHOT_DEADLINE_S = 3.0
SET_ROUNDTRIP_S = 2.0
SEND_DEADLINE_S = 30.0
UNARMED_DEADLINE_S = 60.0
ARMED_DEADLINE_S = 120.0
RESTORE_ROUNDTRIP_S = 3.0
# Worst-case helper-internal time from COPY to READY: two connect roundtrips,
# snapshot and the set_selection roundtrip. The parent's READY timeout must
# exceed this plus interpreter startup. RESTORE -> RESTORED is bounded by
# RESTORE_ROUNDTRIP_S. The module is safe to import for these constants:
# libwayland is loaded only in main().
READY_BUDGET_S = 2 * ROUNDTRIP_DEADLINE_S + SNAPSHOT_DEADLINE_S + SET_ROUNDTRIP_S
RESTORED_OWNER_NO_DEADLINE = float("inf")
IDLE_DEADLINE_S = 3600.0

_OP_DISPLAY_SYNC = 0
_OP_DISPLAY_GET_REGISTRY = 1
_OP_REGISTRY_BIND = 0

# Opcodes shared by ext- and zwlr-data-control interfaces (same wire layout).
_OP_MGR_CREATE_SOURCE = 0
_OP_MGR_GET_DATA_DEVICE = 1
_OP_MGR_DESTROY = 2
_OP_DEV_SET_SELECTION = 0
_OP_DEV_DESTROY = 1
_OP_SRC_OFFER = 0
_OP_SRC_DESTROY = 1
_OP_OFFER_RECEIVE = 0
_OP_OFFER_DESTROY = 1


# ---------------------------------------------------------------------------
# libwayland-client ctypes surface
# ---------------------------------------------------------------------------


class _WlInterface(ctypes.Structure):
    pass


class _WlMessage(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("signature", ctypes.c_char_p),
        ("types", ctypes.POINTER(ctypes.POINTER(_WlInterface))),
    ]


_WlInterface._fields_ = [
    ("name", ctypes.c_char_p),
    ("version", ctypes.c_int),
    ("method_count", ctypes.c_int),
    ("methods", ctypes.POINTER(_WlMessage)),
    ("event_count", ctypes.c_int),
    ("events", ctypes.POINTER(_WlMessage)),
]


class _WlArgument(ctypes.Union):
    _fields_ = [
        ("i", ctypes.c_int32),
        ("u", ctypes.c_uint32),
        ("f", ctypes.c_int32),
        ("s", ctypes.c_char_p),
        ("o", ctypes.c_void_p),
        ("n", ctypes.c_uint32),
        ("a", ctypes.c_void_p),
        ("h", ctypes.c_int32),
    ]


_DISPATCHER_T = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,  # dispatcher_data
    ctypes.c_void_p,  # target wl_object* (== wl_proxy* on the client)
    ctypes.c_uint32,  # opcode
    ctypes.POINTER(_WlMessage),
    ctypes.POINTER(_WlArgument),
)


def _load_lib() -> ctypes.CDLL:
    candidates = []
    found = ctypes.util.find_library("wayland-client")
    if found:
        candidates.append(found)
    candidates += ["libwayland-client.so.0", "libwayland-client.so"]
    for name in candidates:
        try:
            return ctypes.CDLL(name, use_errno=True)
        except OSError:
            continue
    raise RuntimeError("libwayland-client not found")


class _Wl:
    """libwayland-client function signatures (pointer-width safe)."""

    def __init__(self) -> None:
        lib = _load_lib()
        self.lib = lib
        vp = ctypes.c_void_p
        ui = ctypes.c_uint32
        ip = ctypes.POINTER(_WlInterface)
        ap = ctypes.POINTER(_WlArgument)

        lib.wl_display_connect.restype = vp
        lib.wl_display_connect.argtypes = [ctypes.c_char_p]
        lib.wl_display_disconnect.restype = None
        lib.wl_display_disconnect.argtypes = [vp]
        lib.wl_display_get_fd.restype = ctypes.c_int
        lib.wl_display_get_fd.argtypes = [vp]
        lib.wl_display_dispatch.restype = ctypes.c_int
        lib.wl_display_dispatch.argtypes = [vp]
        lib.wl_display_dispatch_pending.restype = ctypes.c_int
        lib.wl_display_dispatch_pending.argtypes = [vp]
        lib.wl_display_flush.restype = ctypes.c_int
        lib.wl_display_flush.argtypes = [vp]
        lib.wl_display_get_error.restype = ctypes.c_int
        lib.wl_display_get_error.argtypes = [vp]

        lib.wl_proxy_add_dispatcher.restype = ctypes.c_int
        lib.wl_proxy_add_dispatcher.argtypes = [vp, _DISPATCHER_T, vp, vp]
        lib.wl_proxy_destroy.restype = None
        lib.wl_proxy_destroy.argtypes = [vp]
        lib.wl_proxy_marshal_array.restype = None
        lib.wl_proxy_marshal_array.argtypes = [vp, ui, ap]
        lib.wl_proxy_marshal_array_flags.restype = vp
        lib.wl_proxy_marshal_array_flags.argtypes = [vp, ui, ip, ui, ui, ap]
        lib.wl_proxy_marshal_array_constructor_versioned.restype = vp
        lib.wl_proxy_marshal_array_constructor_versioned.argtypes = [
            vp,
            ui,
            ap,
            ip,
            ui,
        ]

        # Core interfaces generated into the library.
        self.wl_callback_interface = _WlInterface.in_dll(lib, "wl_callback_interface")
        self.wl_registry_interface = _WlInterface.in_dll(lib, "wl_registry_interface")
        self.wl_seat_interface = _WlInterface.in_dll(lib, "wl_seat_interface")


# ---------------------------------------------------------------------------
# Data-control interface tables (identical to generated protocol code)
# ---------------------------------------------------------------------------


class _ProtocolTables:
    """wl_interface/wl_message definitions; kept alive for process lifetime."""

    def __init__(self, prefix: str, seat_iface: _WlInterface) -> None:
        self._keep: list[object] = []
        self.manager_global = f"{prefix}_data_control_manager_v1"

        self.source = _WlInterface()
        self.device = _WlInterface()
        self.offer = _WlInterface()
        self.manager = _WlInterface()

        def types(*ifaces: _WlInterface | None) -> object:
            arr = (ctypes.POINTER(_WlInterface) * len(ifaces))()
            for i, f in enumerate(ifaces):
                if f is not None:
                    arr[i] = ctypes.pointer(f)
            self._keep.append(arr)
            return arr

        def msgs(spec: list[tuple[str, bytes, object]]) -> object:
            arr = (_WlMessage * len(spec))()
            for i, (name, sig, ty) in enumerate(spec):
                arr[i].name = name.encode()
                arr[i].signature = sig
                arr[i].types = ty
            self._keep.append(arr)
            return arr

        self.source.name = f"{prefix}_data_control_source_v1".encode()
        self.source.version = 1
        self.source.methods = msgs(
            [
                ("offer", b"s", types(None)),
                ("destroy", b"", types()),
            ]
        )
        self.source.method_count = 2
        self.source.events = msgs(
            [
                ("send", b"sh", types(None, None)),
                ("cancelled", b"", types()),
            ]
        )
        self.source.event_count = 2

        self.device.name = f"{prefix}_data_control_device_v1".encode()
        self.device.version = 2
        self.device.methods = msgs(
            [
                ("set_selection", b"?o", types(self.source)),
                ("destroy", b"", types()),
                ("set_primary_selection", b"2?o", types(self.source)),
            ]
        )
        self.device.method_count = 3
        self.device.events = msgs(
            [
                ("data_offer", b"n", types(self.offer)),
                ("selection", b"?o", types(self.offer)),
                ("finished", b"", types()),
                ("primary_selection", b"2?o", types(self.offer)),
            ]
        )
        self.device.event_count = 4

        self.offer.name = f"{prefix}_data_control_offer_v1".encode()
        self.offer.version = 1
        self.offer.methods = msgs(
            [
                ("receive", b"sh", types(None, None)),
                ("destroy", b"", types()),
            ]
        )
        self.offer.method_count = 2
        self.offer.events = msgs([("offer", b"s", types(None))])
        self.offer.event_count = 1

        self.manager.name = self.manager_global.encode()
        self.manager.version = 2
        self.manager.methods = msgs(
            [
                ("create_data_source", b"n", types(self.source)),
                ("get_data_device", b"no", types(self.device, seat_iface)),
                ("destroy", b"", types()),
            ]
        )
        self.manager.method_count = 3
        self.manager.event_count = 0
        self.manager.events = msgs([])


# ---------------------------------------------------------------------------
# Helper runtime
# ---------------------------------------------------------------------------


class _AbortError(Exception):
    """Abort the current bounded operation (timeout, selection change, IO)."""


class _TerminateError(Exception):
    """SIGTERM/SIGINT."""


def _sig_args(sig: bytes) -> list[int]:
    """Wire-type chars of a signature, skipping since/'?'/'!' markers."""
    return [b for b in sig if chr(b) not in "?!" and not chr(b).isdigit()]


class _SendCtx:
    """One in-flight non-blocking write to a requester's fd."""

    __slots__ = ("data", "deadline", "fd", "mime", "offset", "source_kind")

    def __init__(self, fd: int, mime: str, data: bytes | bytearray, source_kind: str) -> None:
        self.fd = fd
        self.mime = mime
        self.data = data
        self.offset = 0
        self.deadline = time.monotonic() + SEND_DEADLINE_S
        self.source_kind = source_kind


class _SourceCtx:
    __slots__ = ("alive", "kind", "payloads", "proxy")

    def __init__(self, proxy: int, kind: str, payloads: dict[str, bytes | bytearray]) -> None:
        self.proxy = proxy
        self.kind = kind  # "secret" | "restore"
        self.payloads = payloads
        self.alive = True


class _ClipboardHelper:
    """Single-shot data-control client driven by the stdin protocol."""

    def __init__(self, wl: _Wl) -> None:
        self._wl = wl
        self.display = 0
        self.wl_fd = -1
        self._handlers: dict[int, Callable[[str, list], None]] = {}
        self._trampoline = _DISPATCHER_T(self._dispatch_event)
        self._out = sys.stdout.buffer
        self._err = sys.stderr
        self._inbuf = bytearray()
        self._stdin_open = True
        self._pending_copy_len: int | None = None
        self._abort_pending: str | None = None
        self._sig_pending = False
        self._sig_r = -1
        self._protocol_failed = False

        self.tables: _ProtocolTables | None = None
        self.registry = 0
        self.manager = 0
        self.seat = 0
        self.device = 0
        self.manager_version = 1

        self._global_names: list[tuple[int, str, int]] = []  # (name, iface, ver)
        self._seat_globals: list[tuple[int, int]] = []

        self._sel_offer = 0
        self._offer_mimes: dict[int, list[str]] = {}
        self._stale_offers: list[int] = []
        self._sel_gen = 0
        self._snapshot_offer = 0

        # idle|setting|own-unarmed|own-armed|restoring|restore-owned
        self.phase = "idle"
        self.phase_deadline = time.monotonic() + IDLE_DEADLINE_S
        self._secret: bytearray | None = None
        self._prior: list[tuple[str, bytearray]] | None = None
        self._copied = False
        self._set_cancelled = False
        self._secret_src: _SourceCtx | None = None
        self._restore_src: _SourceCtx | None = None
        self._sends: list[_SendCtx] = []
        self._writable: set[int] = set()
        self._transferred = False
        self._exit_code = 0
        self._done = False

    # -- output -------------------------------------------------------------

    def _say(self, line: str) -> None:
        try:
            self._out.write(line.encode() + b"\n")
            self._out.flush()
        except OSError:
            pass  # parent gone; state continues regardless

    def _diag(self, msg: str) -> None:
        try:
            self._err.write(f"kwin-mcp-clipboard: {msg}\n")
            self._err.flush()
        except OSError:
            pass

    def _fail(self, why: str) -> None:
        self._say(f"ERR {why}")

    # -- proxy plumbing -----------------------------------------------------

    def _register(self, proxy: int, handler: Callable[[str, list], None]) -> None:
        self._handlers[int(proxy)] = handler

    def _add_dispatcher(self, proxy: int) -> None:
        rc = self._wl.lib.wl_proxy_add_dispatcher(proxy, self._trampoline, None, None)
        if rc != 0:
            raise _AbortError("wl_proxy_add_dispatcher failed")

    def _fill_args(self, args: list) -> object:
        arr = (_WlArgument * max(len(args), 1))()
        for i, (kind, val) in enumerate(args):
            if kind == "u":
                arr[i].u = val
            elif kind == "i" or kind == "h":
                arr[i].i = val
            elif kind == "s":
                arr[i].s = val.encode()
            elif kind == "o":
                arr[i].o = val or None
            elif kind == "n":
                arr[i].n = 0  # overwritten by create_outgoing_proxy
        return arr

    def _marshal(self, proxy: int, opcode: int, args: list) -> None:
        self._wl.lib.wl_proxy_marshal_array(proxy, opcode, self._fill_args(args))

    def _marshal_new(
        self, proxy: int, opcode: int, iface: _WlInterface, version: int, args: list
    ) -> int:
        new_proxy = self._wl.lib.wl_proxy_marshal_array_constructor_versioned(
            proxy, opcode, self._fill_args(args), ctypes.byref(iface), version
        )
        if not new_proxy:
            raise _AbortError("proxy construction failed")
        return new_proxy

    def _destroy_proxy(self, proxy: int, opcode: int | None = None) -> None:
        if not proxy:
            return
        try:
            if opcode is not None:
                self._marshal(proxy, opcode, [])
            self._wl.lib.wl_proxy_destroy(proxy)
        except Exception:
            pass  # teardown must never raise
        self._handlers.pop(int(proxy), None)

    def _destroy_offer(self, offer: int) -> None:
        """Destroy an offer exactly once; _offer_mimes tracks live offers."""
        if offer and self._offer_mimes.pop(offer, None) is not None:
            self._destroy_proxy(offer, _OP_OFFER_DESTROY)

    # -- event dispatch -----------------------------------------------------

    def _dispatch_event(
        self,
        _user_data: int,
        target: int,
        opcode: int,
        msg: Any,
        args: Any,
    ) -> int:
        try:
            name = msg.contents.name.decode()
            decoded: list[object] = []
            for i, k in enumerate(_sig_args(msg.contents.signature)):
                a = args[i]
                if k == ord("i") or k == ord("f") or k == ord("h"):
                    decoded.append(a.i)
                elif k == ord("u"):
                    decoded.append(a.u)
                elif k == ord("s"):
                    decoded.append(a.s.decode(errors="replace") if a.s else "")
                elif k == ord("o") or k == ord("n"):
                    decoded.append(a.o)  # events carry wl_object* for new_id
                else:
                    decoded.append(None)
            handler = self._handlers.get(int(target))
            if handler is not None:
                handler(name, decoded)
        except Exception as exc:  # never unwind through libwayland
            self._abort_pending = f"event handler error: {exc}"
        return 0

    # -- display pump -------------------------------------------------------

    def _read_stdin(self) -> None:
        try:
            chunk = os.read(0, 65536)
        except OSError:
            chunk = b""
        if not chunk:
            self._stdin_open = False
            return
        self._inbuf += chunk
        if len(self._inbuf) > MAX_INBUF_BYTES:
            self._protocol_error("stdin input exceeds bound")

    def _protocol_error(self, why: str) -> None:
        """Unrecoverable stdin framing error: stop interpreting input at once.

        Remaining bytes may be payload, so nothing after the bad frame is ever
        parsed as a command. stdin is replaced with /dev/null so a parent
        still writing gets EPIPE instead of blocking. Exactly one ERR line
        is printed. Nothing owned -> exit 2. Injected secret owned -> bounded
        restore, then keep serving the restored clipboard (never dropped).
        Restore-owned -> keep serving. The final exit code is 2.
        """
        self._protocol_failed = True
        self._inbuf[:] = bytes(len(self._inbuf))
        self._inbuf.clear()
        self._pending_copy_len = None
        if self._stdin_open:
            self._stdin_open = False
            try:
                devnull = os.open(os.devnull, os.O_RDONLY)
                os.dup2(devnull, 0)
                os.close(devnull)
            except OSError:
                pass
        self._fail(f"protocol {why}")
        self._exit_code = 2
        if self.phase == "idle":
            self._done = True

    def _pump(self, deadline: float, read_fds: tuple[int, ...] = ()) -> set[int]:
        """Dispatch queued events, flush, then poll once up to ``deadline``.

        Returns the set of caller-supplied fds that are ready. stdin bytes are
        buffered (commands run only in the main loop); Wayland events are
        dispatched inline. Sets self._writable for send fds.
        """
        wl = self._wl.lib
        self._writable = set()
        while True:
            if wl.wl_display_dispatch_pending(self.display) < 0:
                raise _AbortError(self._display_error())
            if self._abort_pending:
                raise _AbortError(self._abort_pending)
            if self._sig_pending:
                self._sig_pending = False
                raise _TerminateError()
            if wl.wl_display_flush(self.display) < 0:
                self._diag("flush failed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return set()

            p = select.poll()
            p.register(self.wl_fd, select.POLLIN)
            if self._sig_r >= 0:
                p.register(self._sig_r, select.POLLIN)
            if self._stdin_open:
                p.register(0, select.POLLIN)
            for fd in read_fds:
                p.register(fd, select.POLLIN)
            for c in self._sends:
                p.register(c.fd, select.POLLOUT)
            try:
                events = p.poll(int(remaining * 1000))
            except InterruptedError:
                continue

            ready: set[int] = set()
            wl_ready = False
            for fd, ev in events:
                if fd == self._sig_r:
                    with contextlib.suppress(OSError):
                        os.read(self._sig_r, 512)
                elif fd == self.wl_fd:
                    if ev & (select.POLLIN | select.POLLHUP | select.POLLERR):
                        wl_ready = True
                elif fd == 0:
                    if ev & (select.POLLIN | select.POLLHUP | select.POLLERR):
                        self._read_stdin()
                elif fd in read_fds:
                    if ev & (select.POLLIN | select.POLLHUP | select.POLLERR):
                        ready.add(fd)
                else:
                    for c in self._sends:
                        if c.fd == fd and ev & (select.POLLOUT | select.POLLERR | select.POLLHUP):
                            self._writable.add(fd)
            if wl_ready and wl.wl_display_dispatch(self.display) < 0:
                raise _AbortError(self._display_error())
            if self._abort_pending:
                raise _AbortError(self._abort_pending)
            return ready

    def _display_error(self) -> str:
        err = self._wl.lib.wl_display_get_error(self.display)
        return f"display dispatch failed (errno {err})"

    def _roundtrip(self, timeout_s: float) -> bool:
        """Bounded wl_display.sync roundtrip; False on timeout."""
        state = {"done": False}

        def on_cb(name: str, _a: list) -> None:
            if name == "done":
                state["done"] = True

        cb = self._marshal_new(
            self.display, _OP_DISPLAY_SYNC, self._wl.wl_callback_interface, 1, []
        )
        self._add_dispatcher(cb)
        self._register(cb, on_cb)
        deadline = time.monotonic() + timeout_s
        try:
            while not state["done"]:
                if time.monotonic() >= deadline:
                    return False
                self._pump(deadline)
            return True
        finally:
            self._destroy_proxy(cb)

    # -- connect / registry ---------------------------------------------------

    def _on_registry(self, name: str, args: list) -> None:
        if name == "global":
            gname, iface, ver = int(args[0]), str(args[1]), int(args[2])
            self._global_names.append((gname, iface, ver))
            if iface == "wl_seat":
                self._seat_globals.append((gname, ver))

    def _bind(self, name: int, iface: _WlInterface, version: int) -> int:
        arr = (_WlArgument * 4)()
        arr[0].u = name
        arr[1].s = iface.name
        arr[2].u = version
        arr[3].n = 0
        proxy = self._wl.lib.wl_proxy_marshal_array_flags(
            self.registry, _OP_REGISTRY_BIND, ctypes.byref(iface), version, 0, arr
        )
        if not proxy:
            raise _AbortError(f"bind failed for {iface.name!r}")
        return proxy

    def _connect(self) -> None:
        wl = self._wl.lib
        self.display = wl.wl_display_connect(None)
        if not self.display:
            raise _AbortError("wl_display_connect failed (WAYLAND_DISPLAY unset?)")
        self.wl_fd = wl.wl_display_get_fd(self.display)
        # wl_display_get_registry is a header-inline wrapper, not an export.
        self.registry = self._marshal_new(
            self.display,
            _OP_DISPLAY_GET_REGISTRY,
            self._wl.wl_registry_interface,
            1,
            [("n", 0)],
        )
        self._add_dispatcher(self.registry)
        self._register(self.registry, self._on_registry)
        if not self._roundtrip(ROUNDTRIP_DEADLINE_S):
            raise _AbortError("registry roundtrip timed out")

        for prefix in ("ext", "zwlr"):
            gname = f"{prefix}_data_control_manager_v1"
            hit = [g for g in self._global_names if g[1] == gname]
            if hit:
                self.tables = _ProtocolTables(prefix, self._wl.wl_seat_interface)
                self.manager_version = min(hit[0][2], 1)
                self.manager = self._bind(hit[0][0], self.tables.manager, self.manager_version)
                break
        if self.tables is None or not self.manager:
            raise _AbortError("compositor lacks ext/zwlr data-control protocol")
        if not self._seat_globals:
            raise _AbortError("no wl_seat global")

        sname, sver = self._seat_globals[0]
        self.seat = self._bind(sname, self._wl.wl_seat_interface, min(sver, 1))
        self.device = self._marshal_new(
            self.manager,
            _OP_MGR_GET_DATA_DEVICE,
            self.tables.device,
            self.manager_version,
            [("n", 0), ("o", self.seat)],
        )
        self._add_dispatcher(self.device)
        self._register(self.device, self._on_device)
        if not self._roundtrip(ROUNDTRIP_DEADLINE_S):
            raise _AbortError("device roundtrip timed out")

    # -- device / offer / source events --------------------------------------

    def _on_device(self, name: str, args: list) -> None:
        if name == "data_offer":
            offer = int(args[0] or 0)
            self._offer_mimes[offer] = []
            self._add_dispatcher(offer)

            def on_offer(n: str, a: list, p: int = offer) -> None:
                if n == "offer" and p in self._offer_mimes:
                    self._offer_mimes[p].append(a[0])

            self._register(offer, on_offer)
        elif name == "selection":
            offer = int(args[0] or 0)
            old = self._sel_offer
            self._sel_gen += 1
            self._sel_offer = offer
            if old and old != offer:
                # Defer destroy: a snapshot may still marshal receive on it.
                self._stale_offers.append(old)
        elif name == "finished":
            self._abort_pending = "data-control device finished"
        # primary_selection intentionally ignored (regular clipboard only).

    def _reap_stale_offers(self) -> None:
        keep = []
        for offer in self._stale_offers:
            if offer == self._snapshot_offer:
                keep.append(offer)
            else:
                self._destroy_offer(offer)
        self._stale_offers = keep

    def _on_source(self, ctx: _SourceCtx, name: str, args: list) -> None:
        if name == "send":
            mime, fd = str(args[0]), int(args[1])
            if not ctx.alive:
                os.close(fd)  # source retired; never serve wiped bytes
                return
            payload = ctx.payloads.get(mime, b"")
            with contextlib.suppress(OSError):
                fcntl.fcntl(fd, fcntl.F_SETFL, os.O_NONBLOCK)
            self._sends.append(_SendCtx(fd, mime, payload, ctx.kind))
        elif name == "cancelled":
            self._handle_cancelled(ctx)

    def _retire(self, ctx: _SourceCtx) -> None:
        """Destroy a source once; drop its pending sends before any wipe."""
        if not ctx.alive:
            return
        ctx.alive = False
        for c in list(self._sends):
            if c.source_kind == ctx.kind:
                self._drop_send(c)
        self._destroy_proxy(ctx.proxy, _OP_SRC_DESTROY)

    def _handle_cancelled(self, ctx: _SourceCtx) -> None:
        if ctx.kind == "secret":
            self._retire(ctx)
            if ctx is self._secret_src:
                self._secret_src = None
            if self.phase == "restoring" or self.phase == "setting":
                # Expected self-cancel while swapping (restoring) or a lost
                # race while setting (recorded, reported after roundtrip).
                if self.phase == "setting":
                    self._set_cancelled = True
                return
            # External replacement while we owned the injected selection:
            # never restore over the new copy.
            self._say("CANCELLED")
            self._wipe_secret()
            self._done = True
        else:  # restore source replaced: job complete
            self._retire(ctx)
            if ctx is self._restore_src:
                self._restore_src = None
            self._say("CANCELLED")
            self._done = True

    # -- snapshot ------------------------------------------------------------

    def _receive_mime(self, offer: int, mime: str, deadline: float) -> bytearray:
        rfd, wfd = os.pipe()
        try:
            self._marshal(offer, _OP_OFFER_RECEIVE, [("s", mime), ("h", wfd)])
        finally:
            os.close(wfd)
        gen = self._sel_gen
        buf = bytearray()
        try:
            os.set_blocking(rfd, False)
            while True:
                if len(buf) > MAX_MIME_BYTES:
                    raise _AbortError("mime exceeds size cap")
                ready = self._pump(deadline, (rfd,))
                if self._sel_gen != gen:
                    raise _AbortError("selection changed")
                if rfd not in ready:
                    if time.monotonic() >= deadline:
                        raise _AbortError("read timed out")
                    continue
                try:
                    chunk = os.read(rfd, 65536)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    raise _AbortError(f"read error: {exc}") from exc
                if not chunk:
                    return buf
                buf += chunk
        finally:
            os.close(rfd)

    def _snapshot(self, deadline: float) -> bool:
        """Capture all MIME payloads of the current selection.

        True + fills self._prior on success; False + ERR on failure.
        Never mutates compositor state on failure.
        """
        offer = self._sel_offer
        if not offer:
            self._prior = []
            return True
        mimes = list(self._offer_mimes.get(offer, []))
        if len(mimes) > MAX_MIMES:
            self._fail("snapshot too many mimes")
            return False
        gen = self._sel_gen
        self._snapshot_offer = offer
        prior: list[tuple[str, bytearray]] = []
        total = 0
        try:
            for mime in mimes:
                data = self._receive_mime(offer, mime, deadline)
                total += len(data)
                if total > MAX_TOTAL_BYTES:
                    raise _AbortError("total size cap exceeded")
                prior.append((mime, data))
                if self._sel_gen != gen:
                    raise _AbortError("selection changed")
        except _AbortError as exc:
            for _m, d in prior:
                d[:] = bytes(len(d))
            self._fail(f"snapshot {exc}")
            return False
        finally:
            self._snapshot_offer = 0
            self._destroy_offer(offer)
            if self._sel_offer == offer:
                self._sel_offer = 0
        self._prior = prior
        return True

    # -- sources --------------------------------------------------------------

    def _new_source(
        self, kind: str, payloads: dict[str, bytes | bytearray], mimes: list[str]
    ) -> _SourceCtx:
        assert self.tables is not None
        src = self._marshal_new(
            self.manager,
            _OP_MGR_CREATE_SOURCE,
            self.tables.source,
            self.manager_version,
            [("n", 0)],
        )
        ctx = _SourceCtx(src, kind, payloads)
        self._add_dispatcher(src)
        self._register(src, lambda n, a, c=ctx: self._on_source(c, n, a))
        for mime in mimes:
            self._marshal(src, _OP_SRC_OFFER, [("s", mime)])
        return ctx

    # -- commands -------------------------------------------------------------

    def _fatal_before_ready(self, why: str) -> None:
        """Any failure before READY is terminal (exit 2), never lingering."""
        self._wipe_secret()
        if not self._protocol_failed:
            self._fail(why)  # a protocol error already printed its one ERR
        self._exit_code = 2
        self._done = True

    def _cmd_copy(self, payload: bytearray) -> None:
        if self.phase != "idle" or self._copied:
            payload[:] = bytes(len(payload))
            self._fail("busy")
            return
        self._copied = True
        self._secret = payload
        if not self._snapshot(time.monotonic() + SNAPSHOT_DEADLINE_S):
            self._fatal_before_ready("snapshot failed")
            return
        if not self._stdin_open:
            # Parent gave up before READY: abort without mutation.
            self._fatal_before_ready("stdin closed before set_selection")
            return
        assert self._prior is not None
        payloads: dict[str, bytes | bytearray] = {
            MIME_TEXT_UTF8: payload,
            MIME_TEXT: payload,
            MIME_KDE_HINT: HINT_PAYLOAD,
        }
        try:
            ctx = self._new_source("secret", payloads, [MIME_TEXT_UTF8, MIME_TEXT, MIME_KDE_HINT])
        except _AbortError as exc:
            self._fatal_before_ready(f"copy {exc}")
            return
        self._secret_src = ctx
        self._set_cancelled = False
        self.phase = "setting"
        try:
            self._marshal(self.device, _OP_DEV_SET_SELECTION, [("o", ctx.proxy)])
            confirmed = self._roundtrip(SET_ROUNDTRIP_S)
        except _AbortError as exc:
            self._retire(ctx)
            self._secret_src = None
            self.phase = "idle"
            self._fatal_before_ready(f"copy {exc}")
            return
        if self._set_cancelled:
            # Replaced before READY: never restore over the new copy.
            self._secret_src = None
            self.phase = "idle"
            self._fatal_before_ready("selection replaced during set")
            return
        if not confirmed:
            # set_selection may have landed, so restore now. READY is never
            # printed, so the parent must not paste and cannot report success.
            restored = self._do_restore(RESTORE_ROUNDTRIP_S)
            if self._done:
                return  # restore source already replaced (CANCELLED printed)
            if restored and self._restore_src is not None:
                # The prior clipboard is back and owned by us. Report the
                # failed operation, then keep the normal restored-owner loop
                # (serve until a later copy or compositor disconnect), exit 2.
                self._wipe_secret()
                self._fail("set_selection roundtrip timed out; prior clipboard restored")
                self._exit_code = 2
                return
            self._fatal_before_ready("set_selection roundtrip timed out")
            return
        self.phase = "own-unarmed"
        self.phase_deadline = time.monotonic() + UNARMED_DEADLINE_S
        self._say("READY")

    def _cmd_arm(self) -> None:
        if self.phase == "own-unarmed":
            self.phase = "own-armed"
            self.phase_deadline = time.monotonic() + ARMED_DEADLINE_S
            self._say("ARMED")
        elif self.phase == "own-armed":
            self._say("ARMED")
        else:
            self._fail("cannot arm now")

    def _cmd_restore(self) -> None:
        if self.phase == "restore-owned":
            self._say("RESTORED")
            return
        if self.phase not in ("own-unarmed", "own-armed"):
            self._fail("nothing to restore")
            return
        ok = self._do_restore(RESTORE_ROUNDTRIP_S)
        if self._done:
            return  # restore source already replaced (CANCELLED printed)
        if not ok:
            self._fail("restore failed")
            self._exit_code = 2
            self._done = True
            return
        self._say("RESTORED")
        if self._restore_src is None:
            self._done = True  # prior selection was empty: nothing to serve

    def _do_restore(self, timeout_s: float) -> bool:
        """Swap our selection for the snapshotted prior content.

        Returns True once a compositor roundtrip confirms the restore source
        (or the cleared selection) is installed; False on failure. Secret
        source and injected bytes are released on every path.
        """
        assert self.tables is not None
        self.phase = "restoring"
        if self._secret_src is not None:
            # Drop pending secret sends first; the proxy stays alive until the
            # restore source is confirmed so the swap is a single replacement.
            for c in list(self._sends):
                if c.source_kind == "secret":
                    self._drop_send(c)
        ok = False
        try:
            if self._prior:
                payloads: dict[str, bytes | bytearray] = dict(self._prior)
                ctx = self._new_source("restore", payloads, [m for m, _ in self._prior])
                self._marshal(self.device, _OP_DEV_SET_SELECTION, [("o", ctx.proxy)])
                self._restore_src = ctx
                if not self._roundtrip(timeout_s):
                    self._retire(ctx)
                    self._restore_src = None
                    return False
            else:
                self._marshal(self.device, _OP_DEV_SET_SELECTION, [("o", None)])
                if not self._roundtrip(timeout_s):
                    return False
            ok = True
            return True
        except _AbortError:
            return False
        finally:
            if self._secret_src is not None:
                self._retire(self._secret_src)
                self._secret_src = None
            self._wipe_secret()
            if self._restore_src is not None and not self._restore_src.alive:
                self._restore_src = None  # replaced mid-restore by a new copy
            if ok:
                self.phase = "restore-owned"
                # No expiry: the restored clipboard lives until replaced or
                # until the compositor disconnects (wl-copy semantics).
                self.phase_deadline = RESTORED_OWNER_NO_DEADLINE
            else:
                self.phase = "idle"

    def _cmd_quit(self) -> None:
        if self.phase == "restore-owned":
            # Persisting the restored selection matters more than exiting.
            self._stdin_open = False
            return
        if self.phase in ("own-unarmed", "own-armed") and self._do_restore(RESTORE_ROUNDTRIP_S):
            self._say("RESTORED")
            # Keep serving like the EOF path when a restore source is
            # owned; only exit when nothing is owned.
            if self._restore_src is not None:
                self._stdin_open = False
                return
        self._done = True
        self._exit_code = 0

    def _parse_commands(self) -> None:
        while True:
            if self._pending_copy_len is not None:
                need = self._pending_copy_len
                if len(self._inbuf) < need:
                    return
                payload = bytearray(self._inbuf[:need])
                self._inbuf[:need] = bytes(need)
                del self._inbuf[:need]
                self._pending_copy_len = None
                self._cmd_copy(payload)
                continue
            if self._protocol_failed:
                return  # after a protocol error nothing more is interpreted
            idx = self._inbuf.find(b"\n", 0, MAX_HEADER_BYTES + 1)
            if idx < 0:
                if len(self._inbuf) > MAX_HEADER_BYTES:
                    self._protocol_error("command line too long")
                return
            line = bytes(self._inbuf[:idx]).strip()
            del self._inbuf[: idx + 1]
            if not line:
                continue
            parts = line.split(None, 1)
            cmd = parts[0].upper()
            if cmd == b"COPY":
                n = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else -1
                if n < 0:
                    self._protocol_error("bad copy length")
                    return
                if n > MAX_COPY_BYTES:
                    self._protocol_error("copy length out of bounds")
                    return
                self._pending_copy_len = n
            elif cmd == b"ARM":
                self._cmd_arm()
            elif cmd == b"RESTORE":
                self._cmd_restore()
            elif cmd == b"QUIT":
                self._cmd_quit()
                if self._done:
                    return
            else:
                self._protocol_error("unknown command")
                return

    # -- sends ----------------------------------------------------------------

    def _service_sends(self) -> None:
        for c in list(self._sends):
            if c.fd not in self._writable:
                if time.monotonic() > c.deadline:
                    self._drop_send(c)
                continue
            try:
                n = os.write(c.fd, bytes(c.data[c.offset : c.offset + 65536]))
            except BlockingIOError:
                continue
            except OSError:
                self._drop_send(c)  # EPIPE etc: never counts
                continue
            c.offset += n
            if c.offset >= len(c.data):
                self._drop_send(c, completed=True)

    def _drop_send(self, c: _SendCtx, completed: bool = False) -> None:
        with contextlib.suppress(OSError):
            os.close(c.fd)
        if c in self._sends:
            self._sends.remove(c)
        if (
            completed
            and c.source_kind == "secret"
            and self.phase == "own-armed"
            and c.mime in (MIME_TEXT_UTF8, MIME_TEXT)
            and not self._transferred
        ):
            self._transferred = True
            self._say("TRANSFERRED")

    # -- secret hygiene --------------------------------------------------------

    def _wipe_secret(self) -> None:
        if self._secret is not None:
            self._secret[:] = bytes(len(self._secret))
            self._secret = None

    def _wipe_all(self) -> None:
        self._wipe_secret()
        if self._prior:
            for _m, d in self._prior:
                d[:] = bytes(len(d))
            self._prior = None
        self._inbuf[:] = bytes(len(self._inbuf))
        self._inbuf.clear()

    # -- abort paths ------------------------------------------------------------

    def _abort_owned(self, persist: bool) -> None:
        """Parent vanished / deadline / signal while we own the selection."""
        self._diag("abort while selection owned; best-effort restore")
        if self._do_restore(RESTORE_ROUNDTRIP_S):
            self._say("RESTORED")
            if persist and self._restore_src is not None:
                return  # keep serving restored data without the parent
        else:
            self._diag("best-effort restore failed")
            self._exit_code = 2
        self._done = True

    # -- main loop --------------------------------------------------------------

    def run(self) -> int:
        try:
            self._connect()
        except _AbortError as exc:
            self._fail(str(exc))
            self._cleanup()
            return 1

        try:
            while not self._done:
                self._reap_stale_offers()
                self._service_sends()
                self._parse_commands()
                if self._done:
                    break

                if self.phase == "idle" and (
                    not self._stdin_open or time.monotonic() > self.phase_deadline
                ):
                    break  # nothing owned: parent gone or never sent COPY
                if not self._stdin_open and self.phase in (
                    "own-unarmed",
                    "own-armed",
                    "setting",
                    "restoring",
                ):
                    self._abort_owned(persist=True)
                    continue
                if self.phase in ("own-unarmed", "own-armed") and (
                    time.monotonic() > self.phase_deadline
                ):
                    self._diag("phase deadline reached")
                    self._abort_owned(persist=True)
                    continue

                # Block until an event, a send, stdin, or a deadline tick.
                tick = min(time.monotonic() + 0.25, self.phase_deadline)
                try:
                    self._pump(tick)
                except _AbortError as exc:
                    if self.phase in (
                        "own-unarmed",
                        "own-armed",
                        "setting",
                        "restoring",
                    ):
                        self._diag(f"display error: {exc}")
                        self._abort_owned(persist=True)
                        continue
                    raise
        except _TerminateError:
            if self.phase in (
                "own-unarmed",
                "own-armed",
                "setting",
                "restoring",
            ):
                self._abort_owned(persist=False)
            self._exit_code = 0
        except _AbortError as exc:
            self._fail(str(exc))
            self._exit_code = 2
        finally:
            self._wipe_all()
            self._cleanup()
        return self._exit_code

    def _cleanup(self) -> None:
        for c in list(self._sends):
            with contextlib.suppress(OSError):
                os.close(c.fd)
        self._sends.clear()
        for offer in list(self._offer_mimes):
            self._destroy_offer(offer)
        self._stale_offers.clear()
        for ctx in (self._secret_src, self._restore_src):
            if ctx is not None:
                self._retire(ctx)
        self._secret_src = None
        self._restore_src = None
        if self.device:
            self._destroy_proxy(self.device, _OP_DEV_DESTROY)
            self.device = 0
        if self.manager:
            self._destroy_proxy(self.manager, _OP_MGR_DESTROY)
            self.manager = 0
        if self.registry:
            self._wl.lib.wl_proxy_destroy(self.registry)
            self.registry = 0
        if self.display:
            with contextlib.suppress(Exception):
                self._wl.lib.wl_display_flush(self.display)
            self._wl.lib.wl_display_disconnect(self.display)
            self.display = 0


def main() -> int:
    if len(sys.argv) > 1:
        sys.stderr.write("usage: python3 -m kwin_mcp.clipboard (no arguments)\n")
        return 1
    try:
        wl = _Wl()
    except (RuntimeError, AttributeError, ValueError) as exc:
        sys.stderr.write(f"kwin-mcp-clipboard: {exc}\n")
        return 1

    helper = _ClipboardHelper(wl)

    # Signals only set a flag and wake poll through a self-pipe; raising from
    # a Python signal handler could otherwise unwind through a libwayland
    # dispatcher callback.
    sig_r, sig_w = os.pipe()
    os.set_blocking(sig_r, False)
    os.set_blocking(sig_w, False)
    signal.set_wakeup_fd(sig_w, warn_on_full_buffer=False)
    helper._sig_r = sig_r

    def _sig(_signum: int, _frame: object) -> None:
        helper._sig_pending = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    return helper.run()


if __name__ == "__main__":
    sys.exit(main())
