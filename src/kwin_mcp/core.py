"""Core automation engine for KDE Wayland GUI automation.

Contains all tool logic independent of the MCP transport layer: one public
method per tool, taking and returning the ``kwin_mcp.models`` types. The CLI
calls these methods directly; the MCP server wraps them.
"""

import contextlib
import json
import os
import re
import selectors
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from kwin_mcp import progress
from kwin_mcp.geometry import select_one, select_windows, window_from_kwin
from kwin_mcp.input import (
    EIS_DISCONNECTED_WARNING,
    InputBackend,
    MouseButton,
    check_typeable,
    chord_keycodes,
    modifier_keycodes,
    text_keycodes,
)
from kwin_mcp.models import (
    ANY_ELEMENT,
    NO_RETENTION,
    AccessibilityState,
    AppLaunchResult,
    AppLogResult,
    Capture,
    ClipboardText,
    ClipboardWriteResult,
    CloseConsent,
    DbusReply,
    Desktop,
    DesktopList,
    ElementActResult,
    ElementAddress,
    ElementList,
    ElementPoint,
    ElementWaitResult,
    Frame,
    HeldInput,
    HomeMode,
    KeyboardHoldPhase,
    KeyboardHoldResult,
    KeyboardPressResult,
    KeyboardTypeResult,
    KeyChord,
    Locator,
    MouseButtonName,
    PartialRect,
    PlaceApplied,
    PlaceBefore,
    Point,
    PointerClickResult,
    PointerDragResult,
    PointerHoldPhase,
    PointerHoldResult,
    PointerMoveResult,
    PointerScrollResult,
    Rect,
    RefAddr,
    Retention,
    ScreenSize,
    ScrollAxis,
    ScrollUnit,
    SessionInfo,
    SessionInfoResult,
    SessionKind,
    SessionStopResult,
    Target,
    TouchPinchResult,
    TouchSwipeResult,
    TouchTapResult,
    TypedArg,
    TypeMethod,
    UIElement,
    Wait,
    WaylandGlobal,
    WaylandGlobals,
    Waypoint,
    Window,
    WindowCloseResult,
    WindowFocusResult,
    WindowList,
    WindowPlaceResult,
    WindowSelector,
    text_echo,
)
from kwin_mcp.screenshot import (
    capture_frame_burst,
    capture_screenshot_to_file,
    reframe_screenshot,
    to_frame,
)
from kwin_mcp.session import (
    AccessibilityError,
    LiveSession,
    Session,
    SessionConfig,
    accessibility_enabled,
    process_registry,
)

# ── Gate texts (the server's resolvers import these, so both layers agree) ──

SESSION_RUNNING_TEXT = "Session already running.\nCall session_stop first."
NO_SESSION_TEXT = (
    "No session running.\nCall session_start (virtual) or session_connect (live desktop)."
)
NO_INPUT_TEXT = (
    "No input backend (KWin EIS unavailable); install kwin-common or reconnect.\n"
    "Call session_stop, then session_start or session_connect again."
)
NO_CLIPBOARD_TEXT = (
    "Clipboard not enabled.\n"
    "Pass enable_clipboard=true to session_start, or use session_connect "
    "(clipboard is always enabled for live sessions)."
)

# Longest single wait inside an AT-SPI exchange, so a heartbeat can run while
# the worker is busy (e.g. a long element_query wait).
_ATSPI_HEARTBEAT_S = 0.25

# Per-attempt bound for one AT-SPI2 request, and how long a worker may take to
# exit on stdin EOF before it is killed.
_ATSPI_TIMEOUT_S = 30.0
_ATSPI_EXIT_GRACE_S = 2.0

# Upper bound for one KWin scripting query subprocess.
_KWIN_QUERY_TIMEOUT_S = 30

# Refs minted by the AT-SPI worker: e<generation>.<n>.
_REF_PATTERN = re.compile(r"e(\d+)\.(\d+)")

# One global in wayland-info's output.
_WAYLAND_GLOBAL_PATTERN = re.compile(r"interface: '([^']+)',\s*version:\s*(\d+),\s*name:\s*(\d+)")

# KWin geometry is a QRectF; bounding every frame field to the signed 32-bit
# range keeps the value exact on its way through JSON and the script's JS numbers.
_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1


class _AtspiRequestNotSentError(RuntimeError):
    """An AT-SPI2 request failed before any attempt reached the worker.

    The worker could not be started (e.g. the process registry refused the
    spawn) or exited before the request line was written, so the request
    cannot have run.
    """


class _RequestNotSentError(EOFError):
    """The worker's stdin was gone before the request line was written."""


@dataclass(frozen=True)
class CloseTarget:
    """The one window a window_close selector names, as the consent prompt shows it."""

    window_id: str
    app_id: str
    caption: str
    closeable: bool


# Install hints for external binaries
_INSTALL_HINTS: dict[str, str] = {
    "wl-paste": (
        "wl-paste not found. Install wl-clipboard "
        "(e.g. 'sudo pacman -S wl-clipboard' or 'sudo apt install wl-clipboard')."
    ),
    "wl-copy": (
        "wl-copy not found. Install wl-clipboard "
        "(e.g. 'sudo pacman -S wl-clipboard' or 'sudo apt install wl-clipboard')."
    ),
    "wayland-info": (
        "wayland-info not found. Install wayland-utils "
        "(e.g. 'sudo pacman -S wayland-utils' or 'sudo apt install wayland-utils')."
    ),
}

# wl-copy forks its selection owner and exits 0 once the compositor confirmed
# the selection; a parent still running after this is treated as a failure.
_CLIPBOARD_SET_TIMEOUT = 5.0

# Upper bound for each D-Bus round trip made by dbus_call (introspection and the
# method call), the same bound the former dbus-send subprocess had.
_DBUS_CALL_TIMEOUT = 10.0


def _introspected_in_signatures(xml_data: str, interface: str, method: str) -> list[str] | None:
    """Return every input signature for ``interface.method`` in introspection XML.

    Qt exports overloaded slots and slots with default arguments as several
    ``<method>`` entries under one name (KWin's ``loadScript`` has ``s`` and
    ``ss``), so a name can map to more than one candidate. Returns None when
    the XML does not describe that method at all.
    """
    try:
        root = ElementTree.fromstring(xml_data)
    except ElementTree.ParseError:
        return None
    signatures: list[str] = []
    for iface in root.findall("interface"):
        if iface.get("name") != interface:
            continue
        for candidate in iface.findall("method"):
            if candidate.get("name") == method:
                signatures.append(
                    "".join(
                        arg.get("type", "")
                        for arg in candidate.findall("arg")
                        if arg.get("direction", "in") == "in"
                    )
                )
    return signatures or None


def _arg_signature(arg: object) -> str:
    """Return the complete D-Bus signature of one D-Bus value.

    ``parse_arg`` returns values with ``variant_level=1`` for an explicit
    ``variant`` argument, and dbus-python unwraps variants in replies the same
    way; those marshal as ``v``. Everything else has a fixed signature.
    """
    import dbus
    import dbus.types

    if getattr(arg, "variant_level", 0) > 0:
        return "v"
    if isinstance(arg, dbus.Boolean):
        return "b"
    if isinstance(arg, dbus.Byte):
        return "y"
    if isinstance(arg, dbus.Int16):
        return "n"
    if isinstance(arg, dbus.UInt16):
        return "q"
    if isinstance(arg, dbus.Int32):
        return "i"
    if isinstance(arg, dbus.UInt32):
        return "u"
    if isinstance(arg, dbus.Int64):
        return "x"
    if isinstance(arg, dbus.UInt64):
        return "t"
    if isinstance(arg, dbus.Double):
        return "d"
    if isinstance(arg, dbus.ObjectPath):
        return "o"
    if isinstance(arg, dbus.Signature):
        return "g"
    if isinstance(arg, dbus.String):
        return "s"
    if isinstance(arg, dbus.types.UnixFd):
        return "h"
    if isinstance(arg, dbus.Array):
        return "a" + str(arg.signature)
    if isinstance(arg, dbus.Dictionary):
        return "a{" + str(arg.signature) + "}"
    if isinstance(arg, dbus.Struct):
        return "(" + "".join(_arg_signature(item) for item in arg) + ")"
    raise TypeError(f"unsupported argument type {type(arg).__name__}")


def _select_signature(parsed_args: list[object], candidates: list[str]) -> str | None:
    """Pick the candidate input signature that exactly fits ``parsed_args``.

    An argument fits a position when its own signature equals the candidate's
    complete type there, or the candidate's type is ``v`` (a variant accepts
    any value). Returns the first exact match, or None when no candidate fits.
    """
    import dbus

    own = [_arg_signature(arg) for arg in parsed_args]
    for signature in candidates:
        candidate = [str(t) for t in dbus.Signature(signature)]
        if len(candidate) != len(own):
            continue
        if all(c == "v" or c == o for c, o in zip(candidate, own, strict=True)):
            return signature
    return None


def _dbus_to_json(value: object) -> Any:
    import dbus

    if value is None:
        return None
    if isinstance(value, dbus.Boolean):
        return bool(value)
    if isinstance(value, dbus.ObjectPath | dbus.Signature | dbus.String):
        return str(value)
    if isinstance(
        value,
        dbus.Byte | dbus.Int16 | dbus.UInt16 | dbus.Int32 | dbus.UInt32 | dbus.Int64 | dbus.UInt64,
    ):
        return int(value)
    if isinstance(value, dbus.Double):
        return float(value)
    if isinstance(value, dbus.Array):
        return [_dbus_to_json(x) for x in value]
    if isinstance(value, dbus.Dictionary | dict):
        return {str(_dbus_to_json(k)): _dbus_to_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_dbus_to_json(x) for x in value]
    if isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _reply_signature(reply: object) -> str:
    """The D-Bus signature of a method reply: ``""`` for void.

    dbus-python returns a plain tuple for several out arguments and the bare
    value for one (a ``dbus.Struct`` is one value, not several).
    """
    if reply is None:
        return ""
    values = reply if type(reply) is tuple else (reply,)
    return "".join(_arg_signature(value) for value in values)


def _split_command(text: str, field: str) -> list[str]:
    """Split a shell-style command line, rejecting empty and malformed ones.

    ``field`` names the argument in the ValueError, so the caller sees
    which input to fix.
    """
    try:
        argv = shlex.split(text)
    except ValueError as exc:
        msg = f"Invalid {field}: {exc}"
        raise ValueError(msg) from exc
    if not argv:
        msg = f"{field} must contain a command to launch"
        raise ValueError(msg)
    return argv


def _check_selector(window: WindowSelector) -> None:
    """Refuse an empty or ``kind=[]`` selector before any session lookup.

    Argument errors are decided before the session is consulted, so a caller
    without a session still learns how to fix the selector. ``select_one`` on
    no windows raises exactly the selector's teaching error for those cases.
    """
    if window.is_empty() or (window.kind is not None and not window.kind):
        select_one([], window)


def _rect_tuple(rect: Rect | None) -> tuple[int, int, int, int] | None:
    return None if rect is None else (rect.x, rect.y, rect.width, rect.height)


def _rect_from(values: list[float]) -> Rect:
    """Rect from a raw KWin [x, y, w, h] list: KWin may report fractional
    geometry and zero sizes, so round and clamp like ``geometry._rect``."""
    return Rect(
        x=round(float(values[0])),
        y=round(float(values[1])),
        width=max(1, round(float(values[2]))),
        height=max(1, round(float(values[3]))),
    )


class AutomationEngine:
    """One method per kwin-mcp tool; every other attribute is private.

    Manages session lifecycle, input injection, screenshot capture,
    accessibility queries, and clipboard operations.

    ``screenshot_max_edge`` bounds the long edge of every saved screenshot and
    frame in pixels; ``0`` keeps full resolution. A ``screen_capture`` call or
    a ``capture`` option can override it.
    """

    def __init__(self, screenshot_max_edge: int = 0) -> None:
        if screenshot_max_edge < 0:
            msg = f"screenshot_max_edge must be 0 or positive, got {screenshot_max_edge}"
            raise ValueError(msg)
        self._screenshot_max_edge = screenshot_max_edge
        self._session: Session | LiveSession | None = None
        self._input: InputBackend | None = None
        self._clipboard_enabled: bool = False
        # What session_start/session_connect decided, reported by session_info.
        self._retention: Retention = NO_RETENTION
        self._screen: ScreenSize | None = None
        self._accessibility: AccessibilityState = "on"
        self._session_warnings: list[str] = []
        self._eis_lost: bool = False
        self._atspi_proc: subprocess.Popen[bytes] | None = None
        self._atspi_bus: str = ""
        self._atspi_buffer: bytes = b""
        self._atspi_a11y: str = ""
        # Generation of the current AT-SPI worker; refs embed it (e<gen>.<n>).
        # Incremented on every spawn, so 0 means no worker was started yet.
        self._atspi_generation: int = 0
        self._atspi_lock = threading.Lock()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self._teardown_atspi_worker()

    # ── Gates ─────────────────────────────────────────────────────────────

    def _require_session(self) -> Session | LiveSession:
        if self._session is None or not self._session.is_running:
            raise RuntimeError(NO_SESSION_TEXT)
        return self._session

    def _require_no_session(self) -> None:
        if self._session is not None and self._session.is_running:
            raise RuntimeError(SESSION_RUNNING_TEXT)

    def _require_input(self) -> InputBackend:
        self._require_session()
        if self._input is None:
            raise RuntimeError(NO_INPUT_TEXT)
        return self._input

    def _require_clipboard(self) -> None:
        self._require_session()
        if not self._clipboard_enabled:
            raise RuntimeError(NO_CLIPBOARD_TEXT)

    def _require_info(self) -> Any:
        """The running session's ``session.SessionInfo``."""
        info = self._require_session().info
        if info is None:
            raise RuntimeError(NO_SESSION_TEXT)
        return info

    # ── Private helpers ───────────────────────────────────────────────────

    def _session_env(self) -> dict[str, str]:
        """Build environment dict for tools that need the session."""
        info = self._require_info()
        env = {**os.environ}
        if info.dbus_address:
            env["DBUS_SESSION_BUS_ADDRESS"] = info.dbus_address
        env["WAYLAND_DISPLAY"] = info.wayland_socket
        if info.home_dir:
            home = str(info.home_dir)
            env["HOME"] = home
            env["XDG_CONFIG_HOME"] = str(info.home_dir / ".config")
            env["XDG_DATA_HOME"] = str(info.home_dir / ".local" / "share")
            env["XDG_CACHE_HOME"] = str(info.home_dir / ".cache")
            env["XDG_STATE_HOME"] = str(info.home_dir / ".local" / "state")
        env["QT_QPA_PLATFORM"] = "wayland"
        env.pop("DISPLAY", None)
        return env

    def _a11y_bus_address(self, env: dict[str, str]) -> str:
        """Identity of the session's AT-SPI bus; ``""`` when it cannot be read.

        Apps and the worker reach the accessibility bus through
        ``AT_SPI_BUS_ADDRESS`` when it is set, else through
        ``org.a11y.Bus.GetAddress`` on the session bus. The reply embeds the
        bus's guid, so it changes when the a11y bus restarts even though its
        socket path stays the same. A worker that bound the old identity keeps
        answering with no applications from its dead connection forever, so
        this is probed on every call.
        """
        explicit = env.get("AT_SPI_BUS_ADDRESS", "")
        if explicit:
            return explicit
        session_bus = env.get("DBUS_SESSION_BUS_ADDRESS", "")
        if not session_bus:
            return ""
        try:
            import dbus.bus

            conn = dbus.bus.BusConnection(session_bus)
            try:
                return str(
                    conn.get_object("org.a11y.Bus", "/org/a11y/bus").GetAddress(
                        dbus_interface="org.a11y.Bus"
                    )
                )
            finally:
                conn.close()
        except Exception:
            return ""

    def _ensure_atspi_worker(self) -> subprocess.Popen[bytes]:
        """Return the AT-SPI worker for the current session bus, (re)spawning it if needed.

        ``gi.repository.Atspi`` binds its D-Bus connections once per process, so
        the worker is keyed to the bus identities it was started with. A
        different session bus, a restarted a11y bus, or a dead worker means the
        old process would answer from the wrong or a dead bus: replace it. Each
        spawn starts a new ref generation, so refs of the old worker go stale.

        The worker is an owned process group in ``process_registry``: it runs in
        its own session, so a terminal Ctrl-C aimed at kwin-mcp-cli does not reach
        it, and the exit paths that terminate every owned group (a tool still busy
        at server exit, the CLI's final sweep) terminate it too.
        """
        env = self._session_env()
        bus = env.get("DBUS_SESSION_BUS_ADDRESS", "")
        a11y = self._a11y_bus_address(env)
        proc = self._atspi_proc
        if proc is not None and (
            process_registry.poll(proc) is not None
            or bus != self._atspi_bus
            or a11y != self._atspi_a11y
        ):
            self._teardown_atspi_worker()
            proc = None
        if proc is None:
            generation = self._atspi_generation + 1
            proc = process_registry.spawn(
                [
                    sys.executable,
                    "-m",
                    "kwin_mcp.accessibility",
                    "--serve",
                    "--generation",
                    str(generation),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
            self._atspi_generation = generation
            self._atspi_proc = proc
            self._atspi_bus = bus
            self._atspi_a11y = a11y
            self._atspi_buffer = b""
        return proc

    def _atspi_exchange(
        self,
        proc: subprocess.Popen[bytes],
        line: bytes,
        heartbeat: Callable[[], None] | None = None,
        timeout_s: float = _ATSPI_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Send one request line to the worker and read one response line.

        Raises ``TimeoutError`` after ``timeout_s`` and ``EOFError`` if the
        worker exits or closes its pipes. ``heartbeat`` runs at least every
        ``_ATSPI_HEARTBEAT_S`` while waiting for the response.
        """
        stdin = proc.stdin
        stdout = proc.stdout
        if stdin is None or stdout is None:
            raise _RequestNotSentError("AT-SPI2 worker has no pipes")
        try:
            stdin.write(line)
            stdin.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise _RequestNotSentError("AT-SPI2 worker exited before the request was sent") from exc

        deadline = time.monotonic() + timeout_s
        fd = stdout.fileno()
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while b"\n" not in self._atspi_buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                if not selector.select(min(remaining, _ATSPI_HEARTBEAT_S)):
                    if heartbeat is not None:
                        heartbeat()
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise EOFError(f"AT-SPI2 worker exited (status {proc.poll()})")
                self._atspi_buffer += chunk
        response, _, self._atspi_buffer = self._atspi_buffer.partition(b"\n")
        return json.loads(response)

    def _run_atspi(
        self,
        op: str,
        *,
        heartbeat: Callable[[], None] | None = None,
        retry: bool = True,
        timeout_s: float = _ATSPI_TIMEOUT_S,
        **kwargs: object,
    ) -> dict[str, Any]:
        """Run an AT-SPI2 request in the long-lived worker bound to the session bus.

        The worker (``python -m kwin_mcp.accessibility --serve``) is started once
        per session bus and reused, so each call skips interpreter startup and the
        PyGObject/Atspi import. A timeout, worker exit or worker-side exception
        discards the worker and retries once on a fresh one. ``heartbeat`` is
        called periodically while a response is awaited. ``timeout_s`` bounds
        each attempt; requests that wait inside the worker pass a longer one.

        Raises :class:`_AtspiRequestNotSentError` when no attempt wrote the
        request to a worker (so it cannot have run), ``RuntimeError`` otherwise.
        """
        line = json.dumps({"op": op, **kwargs}).encode() + b"\n"
        last_error = ""
        sent = False
        with self._atspi_lock:
            attempts = 2 if retry else 1
            for attempt in range(attempts):
                if attempt > 0:
                    time.sleep(0.5)
                try:
                    proc = self._ensure_atspi_worker()
                except RuntimeError as exc:
                    if sent:
                        raise
                    raise _AtspiRequestNotSentError(str(exc)) from exc
                try:
                    resp = self._atspi_exchange(proc, line, heartbeat, timeout_s)
                except _RequestNotSentError as exc:
                    last_error = f"AT-SPI2 query failed: {exc}"
                except TimeoutError:
                    sent = True
                    last_error = f"AT-SPI2 query timed out after {timeout_s:g}s (op={op})"
                except EOFError as exc:
                    sent = True
                    last_error = f"AT-SPI2 query failed: {exc}"
                except json.JSONDecodeError as exc:
                    sent = True
                    last_error = f"AT-SPI2 query returned invalid JSON: {exc}"
                else:
                    sent = True
                    if "worker_error" not in resp:
                        return resp
                    last_error = f"AT-SPI2 query failed: {resp['worker_error']}"
                self._teardown_atspi_worker()

        if retry:
            msg = f"{last_error}. Retried once but still failed — the AT-SPI2 bus may be unstable."
        else:
            msg = last_error
        raise (RuntimeError if sent else _AtspiRequestNotSentError)(msg)

    @staticmethod
    def _atspi_result(resp: dict[str, Any]) -> Any:
        """The ``result`` of a worker reply; a refusal (``ok: false``) raises its text."""
        if resp.get("ok") is False:
            raise RuntimeError(str(resp.get("error", "AT-SPI2 request failed")))
        return resp["result"]

    def _teardown_atspi_worker(self) -> None:
        """Stop the AT-SPI worker with bounded latency.

        Closing stdin lets an idle worker exit on EOF; a busy, hung or stopped
        worker is killed after a short grace period. Every poll, wait, kill and
        reap goes through ``process_registry``, which owns the worker's group.
        """
        proc = self._atspi_proc
        self._atspi_proc = None
        self._atspi_bus = ""
        self._atspi_a11y = ""
        self._atspi_buffer = b""
        if proc is None:
            return
        with contextlib.suppress(OSError):
            if proc.stdin is not None:
                proc.stdin.close()
        try:
            process_registry.wait(proc, timeout=_ATSPI_EXIT_GRACE_S)
        except subprocess.TimeoutExpired:
            process_registry.kill(proc)
            process_registry.reap(proc)
        if proc.stdout is not None:
            proc.stdout.close()

    def _check_ref(self, ref: str) -> None:
        """Reject a malformed ref, or one minted by another worker generation."""
        match = _REF_PATTERN.fullmatch(ref)
        if match is None:
            msg = (
                f"Malformed ref {ref!r}: refs look like e<generation>.<n>, as element_query "
                "returns them.\nRe-run element_query and copy a ref."
            )
            raise ValueError(msg)
        if int(match.group(1)) != self._atspi_generation:
            msg = f"Stale ref {ref} (AT-SPI worker restarted); re-run element_query."
            raise RuntimeError(msg)

    def _resolve_targets(self, *targets: Target) -> list[tuple[Point, UIElement | None]]:
        """Turn targets into global points; every ElementPoint in one worker request.

        A ``Point`` passes through. An ``ElementPoint`` re-reads its element's
        live rect and aims at its centre plus ``(dx, dy)``; an element without a
        screen rect is an error that quotes why.
        """
        refs = [target.ref for target in targets if isinstance(target, ElementPoint)]
        elements: dict[str, UIElement] = {}
        if refs:
            for ref in refs:
                self._check_ref(ref)
            resp = self._run_atspi("resolve", refs=list(dict.fromkeys(refs)))
            for entry in self._atspi_result(resp):
                if not entry.get("ok"):
                    raise RuntimeError(str(entry["error"]))
                elements[str(entry["ref"])] = UIElement.from_worker(entry["element"])
        resolved: list[tuple[Point, UIElement | None]] = []
        for target in targets:
            if isinstance(target, Point):
                resolved.append((target, None))
                continue
            element = elements.get(target.ref)
            if element is None:
                msg = f"Stale ref {target.ref} (element no longer exists); re-run element_query."
                raise RuntimeError(msg)
            if element.rect is None:
                msg = (
                    f"Element {target.ref} has no screen position (unavailable: "
                    f"{element.unavailable}).\nUse element_act(element={{ref}}, action) "
                    "or pick another element."
                )
                raise RuntimeError(msg)
            rect = element.rect
            point = Point(
                x=rect.x + rect.width // 2 + target.dx,
                y=rect.y + rect.height // 2 + target.dy,
            )
            resolved.append((point, element))
        return resolved

    def _capture(self, capture: Capture | None) -> tuple[list[Frame], list[str]]:
        """Frames ``capture`` asks for, taken after the action committed.

        A capture failure cannot undo the action, so it becomes a warning
        instead of an error that would invite a duplicate retry.
        """
        if capture is None or not capture.delays_ms:
            return [], []
        try:
            info = self._require_info()
            max_edge = self._screenshot_max_edge if capture.max_edge is None else capture.max_edge
            burst = capture_frame_burst(
                info.dbus_address,
                info.screenshot_dir,
                list(capture.delays_ms),
                include_cursor=capture.include_cursor,
                region=_rect_tuple(capture.region),
                max_edge=max_edge,
                wayland_socket=info.wayland_socket,
            )
            frames: list[Frame] = []
            for delay_ms, path, mapping in burst:
                progress.record_image(path)
                frames.append(to_frame(path, mapping, delay_ms=delay_ms))
        except Exception as exc:
            return [], [f"frame capture failed after the action: {exc}"]
        return frames, []

    def _revalidate(self, inp: InputBackend) -> list[str]:
        """Dispatch pending EIS events; warnings for held input KWin dropped."""
        warnings = inp.revalidate()
        if EIS_DISCONNECTED_WARNING in warnings:
            self._eis_lost = True
        return warnings

    def _input_prologue(self) -> tuple[InputBackend, list[str]]:
        """Gate an input tool and revalidate held state before it sends anything."""
        inp = self._require_input()
        return inp, self._revalidate(inp)

    def _input_epilogue(
        self, inp: InputBackend, warnings: list[str], capture: Capture | None
    ) -> tuple[list[Frame], HeldInput]:
        """Capture frames, then report held input as it is after the action."""
        frames, capture_warnings = self._capture(capture)
        warnings.extend(capture_warnings)
        warnings.extend(self._revalidate(inp))
        return frames, inp.held()

    def _run_kwin_query(self, request: dict[str, object]) -> dict[str, Any]:
        """Run a KWin scripting query in a subprocess bound to the session bus."""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "kwin_mcp.geometry"],
                input=json.dumps(request),
                env=self._session_env(),
                capture_output=True,
                text=True,
                timeout=_KWIN_QUERY_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"KWin query timed out after {_KWIN_QUERY_TIMEOUT_S}s"}
        if result.returncode != 0:
            return {"ok": False, "error": f"exit {result.returncode}: {result.stderr[:200]}"}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"invalid JSON: {result.stdout[:200]}"}

    def _windows(self) -> list[Window]:
        """Every KWin window, in KWin's order."""
        resp = self._run_kwin_query({"op": "windows"})
        if not resp["ok"]:
            msg = f"Window list unavailable: {resp['error']}"
            raise RuntimeError(msg)
        return [window_from_kwin(raw) for raw in resp["result"]]

    def _observe_window(self, window_id: str) -> tuple[Window | None, str | None]:
        """Read one window back after a request: (window or None if gone, error)."""
        resp = self._run_kwin_query({"op": "windows"})
        if not resp["ok"]:
            return None, str(resp["error"])
        for raw in resp["result"]:
            if raw.get("id") == window_id:
                return window_from_kwin(raw), None
        return None, None

    def _session_info_model(self) -> SessionInfo:
        """The current session as session_start/session_connect/session_info report it."""
        info = self._require_info()
        return SessionInfo(
            kind="live" if isinstance(self._session, LiveSession) else "virtual",
            wayland_socket=info.wayland_socket,
            dbus_address=info.dbus_address,
            screen=self._screen,
            home_dir=None if info.home_dir is None else str(info.home_dir),
            retention=self._retention,
            artifacts_dir=str(info.screenshot_dir),
            input_backend="eis" if self._input is not None and not self._eis_lost else None,
            clipboard=self._clipboard_enabled,
            accessibility=self._accessibility,
            warnings=list(self._session_warnings),
        )

    def _reset_session_state(self) -> None:
        self._session = None
        self._input = None
        self._clipboard_enabled = False
        self._retention = NO_RETENTION
        self._screen = None
        self._accessibility = "on"
        self._session_warnings = []
        self._eis_lost = False

    # ── Session ───────────────────────────────────────────────────────────

    def session_start(
        self,
        screen_width: int = 1920,
        screen_height: int = 1080,
        enable_clipboard: bool = False,
        home: HomeMode = "host",
        retention: Retention = NO_RETENTION,
    ) -> SessionInfo:
        """Start an isolated virtual KWin Wayland session (one at a time).

        Startup is atomic: when any step after argument validation fails, the
        partly started session is stopped (with the usual retention rules) and
        the original exception propagates, so no session is left behind.
        """
        for name, value in (("screen_width", screen_width), ("screen_height", screen_height)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                msg = f"{name} must be a positive integer, got {value!r}"
                raise ValueError(msg)
        if retention.home and home == "host":
            msg = (
                "retention.home=true needs home='isolated'.\n"
                "Pass home='isolated', or drop retention.home."
            )
            raise ValueError(msg)
        self._require_no_session()

        # Any worker left over from an earlier session (crashed or never stopped)
        # is bound to that session's bus.
        self._teardown_atspi_worker()

        total_steps = 4
        warnings: list[str] = []
        accessibility: AccessibilityState = "on"
        try:
            session = Session()
            self._session = session
            self._clipboard_enabled = enable_clipboard
            config = SessionConfig(
                screen_width=screen_width,
                screen_height=screen_height,
                enable_clipboard=enable_clipboard,
                home=home,
                retention=retention,
            )
            info = session.start(config, progress_total=total_steps)
            warnings.extend(info.startup_warnings)

            # start() returned only after KWin took org.kde.KWin, so the EIS
            # object is reachable.
            try:
                self._input = InputBackend(info.dbus_address)
            except RuntimeError as exc:
                self._input = None
                warnings.append(f"No input backend available ({exc})")

            # The startup wrapper switches IsEnabled on; report what the bus
            # actually says, so a failed switch is not claimed as "on" (the
            # wrapper's own warning is already in the startup warnings).
            if any(
                "AT-SPI bus activation failed, accessibility tools may be unavailable" in w
                for w in info.startup_warnings
            ):
                accessibility = "unavailable"
            else:
                try:
                    if not accessibility_enabled(info.dbus_address):
                        accessibility = "off"
                except AccessibilityError as exc:
                    accessibility = "unavailable"
                    warnings.append(f"org.a11y.Status unavailable ({exc})")
        except Exception as exc:
            # Roll back everything started so far. Exception, not BaseException:
            # CLI interrupts are cleaned up by its own exit path.
            try:
                self.session_stop()
            except Exception as teardown_exc:
                exc.add_note(f"Rolling back the failed session_start also failed: {teardown_exc}")
            raise

        self._retention = retention
        self._screen = ScreenSize(width=screen_width, height=screen_height)
        self._accessibility = accessibility
        self._session_warnings = warnings
        self._eis_lost = False
        progress.report(total_steps, total_steps, "Input backend ready")
        return self._session_info_model()

    def session_connect(
        self,
        dbus_address: str = "",
        wayland_display: str = "",
        enable_accessibility: bool = False,
        retention: Retention = NO_RETENTION,
    ) -> SessionInfo:
        """Attach to an already running KWin session (the real desktop or a container).

        ``enable_accessibility`` switches ``org.a11y.Status.IsEnabled`` on for the
        connection when it is off; ``session_stop`` switches it back off.
        """
        if retention.home:
            msg = (
                "retention.home is not available on session_connect: live sessions use "
                "the host HOME.\nDrop retention.home."
            )
            raise ValueError(msg)
        self._require_no_session()

        # Any worker left over from an earlier session (crashed or never stopped)
        # is bound to that session's bus.
        self._teardown_atspi_worker()

        dbus_addr = dbus_address or os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
        wayland_disp = wayland_display or os.environ.get("WAYLAND_DISPLAY", "")

        if not dbus_addr:
            msg = (
                "No D-Bus address available. Provide dbus_address parameter "
                "or ensure $DBUS_SESSION_BUS_ADDRESS is set."
            )
            raise RuntimeError(msg)
        if not wayland_disp:
            msg = (
                "No Wayland display available. Provide wayland_display parameter "
                "or ensure $WAYLAND_DISPLAY is set."
            )
            raise RuntimeError(msg)

        # Validate KWin is reachable on the given D-Bus
        import dbus as dbus_module
        import dbus.bus

        try:
            bus = dbus.bus.BusConnection(dbus_addr)
            bus.get_object("org.kde.KWin", "/org/kde/KWin")
        except dbus_module.DBusException as exc:
            msg = f"Cannot reach KWin on D-Bus ({dbus_addr}): {exc}"
            raise RuntimeError(msg) from exc
        progress.report(1, 3, "KWin reachable on D-Bus")

        display_path = Path(wayland_disp)
        if not display_path.is_absolute():
            runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
            if not runtime_dir:
                msg = f"Cannot reach Wayland display ({wayland_disp}): XDG_RUNTIME_DIR is not set"
                raise RuntimeError(msg)
            display_path = Path(runtime_dir) / display_path

        try:
            display_mode = display_path.stat().st_mode
        except OSError as exc:
            msg = f"Cannot reach Wayland display ({wayland_disp}): {exc}"
            raise RuntimeError(msg) from exc
        if not stat.S_ISSOCK(display_mode):
            msg = (
                f"Cannot reach Wayland display ({wayland_disp}): "
                f"{display_path} is not a Unix socket"
            )
            raise RuntimeError(msg)

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                probe.connect(str(display_path))
        except OSError as exc:
            msg = f"Cannot reach Wayland display ({wayland_disp}): {exc}"
            raise RuntimeError(msg) from exc
        progress.report(2, 3, "Wayland display reachable")

        screenshot_dir = Path(tempfile.mkdtemp(prefix="kwin-mcp-screenshots-"))
        # Record the removable screenshot dir so the exit path (when it cannot
        # run session_stop) mirrors session_stop's retention semantics: a
        # retained dir is the caller's to keep, so it is not recorded.
        if not retention.artifacts:
            process_registry.register_temp_dir(screenshot_dir)

        session = LiveSession(dbus_addr, wayland_disp, screenshot_dir, retention=retention)
        self._session = session
        self._retention = retention
        self._eis_lost = False
        # Clipboard is always available on live sessions
        self._clipboard_enabled = True

        warnings: list[str] = []
        # The check above already found org.kde.KWin owned, so no settle delay
        # is needed before the EIS connection.
        try:
            self._input = InputBackend(dbus_addr)
        except RuntimeError as exc:
            self._input = None
            warnings.append(f"No input backend available ({exc})")

        self._accessibility = self._live_accessibility(
            session, dbus_addr, enable=enable_accessibility, warnings=warnings
        )

        outputs = self._run_kwin_query({"op": "outputs"})
        if not outputs["ok"]:
            self._screen = None
            warnings.append(f"workspace size unavailable ({outputs['error']})")
        else:
            workspace = outputs["result"].get("virtual")
            self._screen = (
                None
                if workspace is None
                else ScreenSize(width=round(workspace[2]), height=round(workspace[3]))
            )
        self._session_warnings = warnings

        progress.report(3, 3, "Input backend ready")
        return self._session_info_model()

    @staticmethod
    def _live_accessibility(
        session: LiveSession, dbus_addr: str, *, enable: bool, warnings: list[str]
    ) -> AccessibilityState:
        """Apply ``enable_accessibility`` and read the desktop's accessibility switch."""
        try:
            if enable:
                if not session.enable_accessibility():
                    return "on"
                warnings.append(
                    "switched on for this connection; an app that reads it only at startup "
                    "must be restarted"
                )
                return "switched_on"
            if accessibility_enabled(dbus_addr):
                return "on"
        except AccessibilityError as exc:
            warnings.append(f"org.a11y.Status unavailable ({exc})")
            return "unavailable"
        warnings.append("Reconnect with enable_accessibility=true")
        return "off"

    def session_stop(self) -> SessionStopResult:
        """End the current session, releasing held input. Idempotent."""
        session = self._session
        if session is None:
            return SessionStopResult(stopped=False)
        kind: SessionKind = "live" if isinstance(session, LiveSession) else "virtual"

        self._clipboard_enabled = False
        warnings: list[str] = []
        released = HeldInput()
        if self._input is not None:
            warnings.extend(self._revalidate(self._input))
            if self._eis_lost and EIS_DISCONNECTED_WARNING not in warnings:
                # revalidate() reports the loss once; the stop still states it.
                warnings.append(EIS_DISCONNECTED_WARNING)
            released = self._input.release_all()
            self._input.close()

        # The AT-SPI worker is bound to this session's bus; stop it before the bus goes away.
        self._teardown_atspi_worker()

        kept = session.stop()
        self._reset_session_state()
        return SessionStopResult(
            stopped=True,
            kind=kind,
            kept=kept,
            released=released,
            warnings=warnings,
        )

    def session_info(self) -> SessionInfoResult:
        """Report the current session (or none), its backends, launched apps and held input."""
        session = self._session
        if session is None or not session.is_running or session.info is None:
            return SessionInfoResult(session=None)
        warnings: list[str] = []
        held = HeldInput()
        if self._input is not None:
            warnings.extend(self._revalidate(self._input))
            held = self._input.held()
        return SessionInfoResult(
            session=self._session_info_model(),
            apps=session.apps(),
            held=held,
            warnings=warnings,
        )

    # ── Apps ──────────────────────────────────────────────────────────────

    def app_launch(self, command: str, env: dict[str, str] | None = None) -> AppLaunchResult:
        """Launch a program in the session (shlex-split, no shell); output goes to a log."""
        argv = _split_command(command, "command")
        session = self._require_session()
        try:
            app = session.launch_app(argv, extra_env=env)
        except (OSError, RuntimeError) as exc:
            msg = (
                f"Failed to launch {command!r}: {exc}\n"
                "The session is still running; call session_stop to roll back."
            )
            raise RuntimeError(msg) from exc
        return AppLaunchResult(
            pid=app.pid,
            command=command,
            log_path=str(app.log_path),
            log_uri=f"kwin://apps/{app.pid}/log",
        )

    def app_log(self, pid: int, last_n_lines: int = 50) -> AppLogResult:
        """Return the tail of the captured stdout/stderr of a launched app (0 = whole log)."""
        if isinstance(last_n_lines, bool) or not isinstance(last_n_lines, int) or last_n_lines < 0:
            msg = f"last_n_lines must be 0 (all) or a positive integer, got {last_n_lines!r}"
            raise ValueError(msg)
        session = self._require_session()
        return session.read_app_log(pid, last_n_lines=last_n_lines)

    # ── Screen and elements ───────────────────────────────────────────────

    def screen_capture(
        self,
        region: Rect | None = None,
        max_edge: int | None = None,
        include_cursor: bool = False,
    ) -> Frame:
        """Capture the workspace (or a rectangle of it) as a PNG with its coordinate mapping.

        ``max_edge`` bounds the image's long edge in pixels (``0`` keeps full
        resolution); ``None`` uses the engine's ``screenshot_max_edge``.
        """
        if max_edge is None:
            max_edge = self._screenshot_max_edge
        elif max_edge < 0:
            msg = f"max_edge must be 0 or positive, got {max_edge}"
            raise ValueError(msg)
        info = self._require_info()

        path, captured = capture_screenshot_to_file(
            dbus_address=info.dbus_address,
            wayland_socket=info.wayland_socket,
            include_cursor=include_cursor,
            output_dir=info.screenshot_dir,
        )
        try:
            mapping = reframe_screenshot(
                path, captured, region=_rect_tuple(region), max_edge=max_edge
            )
        except Exception:
            # The caller gets no path for this capture, so do not leave it behind.
            path.unlink(missing_ok=True)
            raise
        progress.record_image(path)
        return to_frame(path, mapping, delay_ms=None)

    def element_query(
        self,
        locator: Locator = ANY_ELEMENT,
        max_depth: int = 15,
        wait: Wait | None = None,
    ) -> ElementList:
        """Return AT-SPI2 elements matching locator, in pre-order; with wait, poll first."""
        self._require_session()
        if wait is None:
            resp = self._run_atspi("query", locator=locator.model_dump(), max_depth=max_depth)
            elements = [UIElement.from_worker(el) for el in self._atspi_result(resp)]
            return ElementList(locator=locator, elements=elements)

        timeout_ms = wait.timeout_ms
        description = f"Waiting for {locator.describe()}"
        wait_start = time.monotonic()
        last_reported = 0.0

        def heartbeat() -> None:
            # The worker answers only after its deadline, so heartbeats can run
            # past timeout_ms. Clamp to the total and skip non-advancing values:
            # progress.report() would treat an equal value as a new phase.
            nonlocal last_reported
            if timeout_ms <= 0:
                return
            elapsed_ms = min((time.monotonic() - wait_start) * 1000, timeout_ms)
            if elapsed_ms <= last_reported:
                return
            last_reported = elapsed_ms
            progress.report(elapsed_ms, timeout_ms, description)

        resp = self._run_atspi(
            "wait",
            heartbeat=heartbeat,
            timeout_s=max(timeout_ms, 0) / 1000 + _ATSPI_TIMEOUT_S,
            locator=locator.model_dump(),
            max_depth=max_depth,
            until=wait.until,
            timeout_ms=timeout_ms,
            poll_interval_ms=max(wait.poll_interval_ms, 0),
        )
        result = self._atspi_result(resp)
        return ElementList(
            locator=locator,
            elements=[UIElement.from_worker(el) for el in result["elements"]],
            wait=ElementWaitResult(
                until=wait.until,
                timeout_ms=timeout_ms,
                satisfied=bool(result["satisfied"]),
                elapsed_ms=int(result["elapsed_ms"]),
            ),
        )

    def element_act(
        self, element: ElementAddress, action: str | Sequence[str], capture: Capture | None = None
    ) -> ElementActResult:
        """Invoke a named AT-SPI action on exactly one element ({ref} or {strict: Locator}).

        Refusals (stale ref, missing action, no or several candidates,
        incomplete discovery) raise. A matched action whose dispatch the
        application did not confirm is reported as sent, since it may have run.
        """
        if isinstance(action, str):
            if not action.strip():
                raise ValueError('element_act needs an action name, e.g. "Press".')
            requested: Sequence[object] = action.split("|") if "|" in action else [action]
        elif isinstance(action, Sequence):
            if not action:
                raise ValueError("element_act action list cannot be empty.")
            requested = action
        else:
            raise ValueError('element_act needs an action name, e.g. "Press".')
        actions: list[str] = []
        seen: set[str] = set()
        for name in requested:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("element_act action names must be non-blank strings.")
            name = name.strip()
            if name.lower() not in seen:
                seen.add(name.lower())
                actions.append(name)
        self._require_session()
        address: dict[str, object]
        if isinstance(element, RefAddr):
            self._check_ref(element.ref)
            address = {"ref": element.ref}
        else:
            address = {"strict": element.strict.model_dump()}

        # Once the request reached the worker, the worker may finish the walk and
        # invoke the action even after this request timed out or the worker was
        # torn down. A request that never reached a worker cannot have run.
        try:
            resp = self._run_atspi("action", retry=False, address=address, action_name=actions)
        except _AtspiRequestNotSentError:
            raise
        except RuntimeError as exc:
            msg = f"{exc}. The action may still have run; check the UI before retrying."
            raise RuntimeError(msg) from exc
        result = self._atspi_result(resp)
        chosen_action = str(result["action"])

        dispatch_error = result.get("error")
        if not dispatch_error and not result.get("invoked"):
            msg = (
                f"AT-SPI action {chosen_action!r} matched one element but "
                "the application reported failure."
            )
            raise RuntimeError(msg)

        warnings: list[str] = []
        frames, capture_warnings = self._capture(capture)
        warnings.extend(capture_warnings)
        held = HeldInput()
        if self._input is not None:
            warnings.extend(self._revalidate(self._input))
            held = self._input.held()
        return ElementActResult(
            element=UIElement.from_worker(result["element"]),
            action=chosen_action,
            dispatch="sent_unconfirmed" if dispatch_error else "confirmed",
            dispatch_error=str(dispatch_error) if dispatch_error else None,
            frames=frames,
            held=held,
            warnings=warnings,
        )

    # ── Windows and desktops ──────────────────────────────────────────────

    def window_list(self, filter: WindowSelector | None = None) -> WindowList:  # noqa: A002
        """List KWin windows (every kind) with id, kind, rects, capabilities and desktops."""
        self._require_session()
        return WindowList(filter=filter, windows=select_windows(self._windows(), filter))

    def desktop_list(self) -> DesktopList:
        """List KWin virtual desktops in pager order with the current one marked."""
        self._require_session()
        resp = self._run_kwin_query({"op": "desktops"})
        if not resp["ok"]:
            msg = f"Virtual desktops unavailable: {resp['error']}"
            raise RuntimeError(msg)
        result = resp["result"]
        return DesktopList(
            desktops=[Desktop.model_validate(desktop) for desktop in result["desktops"]],
            current_desktop_id=result.get("current"),
        )

    def window_focus(
        self, window: WindowSelector, capture: Capture | None = None
    ) -> WindowFocusResult:
        """Raise and activate exactly one window, then read it back."""
        _check_selector(window)
        self._require_session()
        target = select_one(self._windows(), window)
        resp = self._run_kwin_query({"op": "activate", "window_id": target.id})
        if not resp["ok"]:
            msg = f"Failed to focus {target.id!r}: {resp['error']}"
            raise RuntimeError(msg)
        observed, observation_error = self._observe_window(target.id)
        frames, warnings = self._capture(capture)
        return WindowFocusResult(
            window_id=target.id,
            window=observed,
            observation_error=observation_error,
            frames=frames,
            warnings=warnings,
        )

    def window_place(
        self,
        window: WindowSelector,
        frame: PartialRect | None = None,
        desktop: str | None = None,
        capture: Capture | None = None,
    ) -> WindowPlaceResult:
        """Set the frame and/or virtual desktop of exactly one window, then read it back.

        The desktop part runs first, then the frame part (the order KWin
        applies them). A desktop move KWin would carry over to related windows
        is refused before anything is assigned. The readback is post-action
        compositor state, not proof that every requested field was applied.
        """
        if frame is None and desktop is None:
            msg = (
                "window_place needs at least one of frame/desktop.\n"
                "Pass frame={x, y, width, height} and/or desktop=<id from desktop_list>."
            )
            raise ValueError(msg)
        requested: list[tuple[str, int]] = []
        if frame is not None:
            for field in ("x", "y", "width", "height"):
                value = getattr(frame, field)
                if value is None:
                    continue
                if not _INT32_MIN <= value <= _INT32_MAX:
                    msg = (
                        f"window_place frame.{field} must be within the signed 32-bit "
                        f"range, got {value}."
                    )
                    raise ValueError(msg)
                requested.append((field, value))

        _check_selector(window)
        self._require_session()
        target = select_one(self._windows(), window)
        window_id = target.id

        before_desktops = list(target.desktops)
        before_on_all = target.on_all_desktops
        before_frame = target.frame
        app_id = target.app_id
        caption = target.caption
        moveable = target.moveable
        resizeable = target.resizeable
        current_before: str | None = None

        if desktop is not None:
            # Every desktop refusal must leave the window untouched, so the checks
            # run before the frame changes; the assignment itself comes last.
            self._set_desktop(window_id, desktop, check_only=True)
        if frame is not None:
            # KWin does not apply frameGeometry to a window on a non-current
            # (hidden) desktop, so the frame is set before the desktop move.
            reply = self._set_geometry(window_id, frame)
            before_frame = _rect_from(reply["before"])
            app_id = str(reply.get("app", app_id))
            caption = str(reply.get("caption", caption))
            moveable = bool(reply.get("moveable", moveable))
            resizeable = bool(reply.get("resizeable", resizeable))
        if desktop is not None:
            assigned = self._set_desktop(window_id, desktop)
            before_desktops = list(assigned["before"])
            before_on_all = bool(assigned["beforeOnAllDesktops"])
            current_before = assigned.get("current")

        before = PlaceBefore(
            app_id=app_id,
            caption=caption,
            frame=before_frame,
            desktops=before_desktops,
            on_all_desktops=before_on_all,
            moveable=moveable,
            resizeable=resizeable,
        )

        # frameGeometry changes are committed asynchronously by KWin, so a
        # separate query reads the post-action state.
        observed, observation_error = self._observe_window(window_id)
        current_after: str | None = None
        if desktop is not None:
            readback = self._run_kwin_query({"op": "desktops", "window_id": window_id})
            if readback["ok"]:
                current_after = readback["result"].get("current")

        pending: list[str] = []
        applied = PlaceApplied()
        if frame is not None:
            if observed is None:
                if observation_error is not None:
                    pending = [field for field, _ in requested]
                applied_frame = False
            else:
                pending = [
                    field for field, value in requested if getattr(observed.frame, field) != value
                ]
                applied_frame = not pending
            applied = applied.model_copy(update={"frame": applied_frame})
        if desktop is not None:
            applied_desktop = (
                observed is not None
                and observed.desktops == [desktop]
                and not observed.on_all_desktops
            )
            applied = applied.model_copy(update={"desktop": applied_desktop})

        frames, warnings = self._capture(capture)
        return WindowPlaceResult(
            window_id=window_id,
            requested_frame=frame,
            requested_desktop=desktop,
            window=observed,
            observation_error=observation_error,
            before=before,
            applied=applied,
            pending=pending,
            current_desktop_before=current_before,
            current_desktop_after=current_after,
            frames=frames,
            warnings=warnings,
        )

    def _set_desktop(
        self, window_id: str, desktop_id: str, *, check_only: bool = False
    ) -> dict[str, Any]:
        """Put one window on exactly one desktop; refusals and failures raise.

        ``check_only`` runs the same checks (and raises the same refusals) without
        assigning anything.
        """
        resp = self._run_kwin_query(
            {
                "op": "set_desktop",
                "window_id": window_id,
                "desktop_id": desktop_id,
                "check_only": check_only,
            }
        )
        if not resp["ok"]:
            if check_only:
                msg = (
                    f"Failed to check the desktop move of {window_id!r}: {resp['error']}; "
                    "nothing was changed."
                )
                raise RuntimeError(msg)
            # The script may have assigned the window before its reply was lost.
            msg = (
                f"Failed to confirm the desktop of {window_id!r}: {resp['error']}. "
                "The window may already have moved."
            )
            raise RuntimeError(msg)
        assigned: dict[str, Any] = resp["result"]
        # The script assigns nothing unless both the window and the desktop exist.
        if not assigned["windowFound"]:
            msg = f"No window with id {window_id!r}."
            raise RuntimeError(msg)
        if not assigned["desktopFound"]:
            known = self._run_kwin_query({"op": "desktops"})
            if known["ok"]:
                ids = ", ".join(repr(str(d["id"])) for d in known["result"]["desktops"])
                remedy = f"Use an id from desktop_list: {ids}."
            else:
                remedy = "Use an id from desktop_list."
            msg = (
                f"No virtual desktop with id {desktop_id!r} (unknown desktop id); "
                f"nothing was changed.\n{remedy}"
            )
            raise RuntimeError(msg)
        if assigned.get("refused") == "unsupported":
            msg = (
                "Refused: a window with an unresolvable transient relationship "
                f"(X11 group transient) exists, so KWin could move windows besides {window_id!r}; "
                "nothing was changed."
            )
            raise RuntimeError(msg)
        if assigned.get("refused") == "related":
            related = assigned["related"]
            listed = "; ".join(f'{w["id"]} {w["app"]} "{w["caption"]}"' for w in related)
            msg = (
                f"Refused: KWin would also move {len(related)} related window(s) of "
                f"{window_id!r} (transient or modal relationships): {listed}; "
                "nothing was changed."
            )
            raise RuntimeError(msg)
        return assigned

    def _set_geometry(self, window_id: str, frame: PartialRect) -> dict[str, Any]:
        """Request a new frame for one window; failures and oversize sizes raise."""
        resp = self._run_kwin_query(
            {
                "op": "set_geometry",
                "window_id": window_id,
                "x": frame.x,
                "y": frame.y,
                "width": frame.width,
                "height": frame.height,
            }
        )
        if not resp["ok"]:
            msg = f"Failed to set geometry for {window_id!r}: {resp['error']}"
            raise RuntimeError(msg)
        result: dict[str, Any] = resp["result"]
        if not result["found"]:
            msg = f"No window with id {window_id!r}."
            raise RuntimeError(msg)
        # The script refuses sizes beyond the live workspace without assigning
        # anything, so there is no post-action state to observe.
        rejected = result.get("rejected")
        if rejected is not None:
            oversized = frame.width if rejected == "width" else frame.height
            msg = (
                f"window_place frame.{rejected} must be at most {result['limit']} "
                f"(the KWin workspace {rejected}; start the session with a larger screen "
                f"for bigger windows), got {oversized}."
            )
            raise ValueError(msg)
        return result

    def window_close(
        self, window: WindowSelector, capture: Capture | None = None
    ) -> WindowCloseResult:
        """Ask exactly one window to close (the app may refuse); refused on a live desktop.

        Without a consent step (the CLI), a live session refuses: closing can
        discard unsaved work on the real desktop. The MCP server asks the user
        first and passes the outcome to the same implementation.
        """
        return self._close(window, capture, None)

    def _close_target(self, window: WindowSelector) -> CloseTarget | None:
        """The one window ``window`` names, or None when an id-only selector matches nothing.

        Other selectors that match no window or several raise, as every
        window mutator does.
        """
        _check_selector(window)
        self._require_session()
        windows = self._windows()
        if window.id is not None and window.model_copy(update={"id": None}).is_empty():
            matches = [w for w in windows if w.id == window.id]
            if not matches:
                return None
            target = matches[0]
        else:
            target = select_one(windows, window)
        return CloseTarget(
            window_id=target.id,
            app_id=target.app_id,
            caption=target.caption,
            closeable=target.closeable,
        )

    def _close(
        self,
        window: WindowSelector,
        capture: Capture | None,
        consent: CloseConsent | None,
    ) -> WindowCloseResult:
        """Close the window ``window`` names, honouring the consent outcome.

        ``consent`` is what the caller's consent step concluded, or None when
        there was none. The session kind is re-checked here on the tool thread:
        a live session needs ``accepted``, so a session that turned live after
        the caller decided no consent was needed refuses. The window is
        re-resolved, so one that vanished meanwhile is reported, not closed.
        """
        _check_selector(window)
        session = self._require_session()
        live = isinstance(session, LiveSession)
        if live:
            effective: CloseConsent = (
                "unsupported" if consent is None or consent == "not_required" else consent
            )
        else:
            effective = "not_required" if consent is None else consent

        target = self._close_target(window)
        if target is None:
            # Nothing was asked or attempted: consent reports null.
            return WindowCloseResult(
                window_id=window.id or "",
                requested=False,
                consent=None,
                refusal="no_window",
            )
        if not target.closeable:
            return WindowCloseResult(
                window_id=target.window_id,
                app_id=target.app_id,
                caption=target.caption,
                requested=False,
                consent=effective,
                refusal="not_closeable",
            )
        if effective not in ("not_required", "accepted"):
            return WindowCloseResult(
                window_id=target.window_id,
                app_id=target.app_id,
                caption=target.caption,
                requested=False,
                consent=effective,
                refusal="no_consent",
            )

        resp = self._run_kwin_query({"op": "close", "window_id": target.window_id})
        if not resp["ok"]:
            msg = f"Failed to close window {target.window_id!r}: {resp['error']}"
            raise RuntimeError(msg)
        result = resp["result"]
        if not result["found"]:
            return WindowCloseResult(
                window_id=target.window_id,
                requested=False,
                consent=None,
                refusal="no_window",
            )
        app_id = str(result.get("app", target.app_id))
        caption = str(result.get("caption", target.caption))
        if not result["closeable"]:
            return WindowCloseResult(
                window_id=target.window_id,
                app_id=app_id,
                caption=caption,
                requested=False,
                consent=effective,
                refusal="not_closeable",
            )
        frames, warnings = self._capture(capture)
        return WindowCloseResult(
            window_id=target.window_id,
            app_id=app_id,
            caption=caption,
            requested=True,
            consent=effective,
            frames=frames,
            warnings=warnings,
        )

    # ── Pointer ───────────────────────────────────────────────────────────

    def pointer_click(
        self,
        target: Target,
        button: MouseButtonName = "left",
        count: int = 1,
        modifiers: KeyChord | None = None,
        hold_ms: int = 0,
        capture: Capture | None = None,
    ) -> PointerClickResult:
        """Move the pointer to a target and click 1-3 times, optionally with modifiers held."""
        if isinstance(count, bool) or count not in (1, 2, 3):
            msg = f"count must be 1, 2 or 3, got {count}."
            raise ValueError(msg)
        modifier_codes = modifier_keycodes(modifiers)
        btn = MouseButton(button)
        inp, warnings = self._input_prologue()
        ((point, element),) = self._resolve_targets(target)
        inp.check_not_held("pointer_click", buttons=[btn], keycodes=modifier_codes)
        moved = inp.moved_while_held(btn)
        if moved is not None:
            warnings.append(moved)
        inp.mouse_click(
            point.x,
            point.y,
            btn,
            click_count=count,
            modifiers=modifiers.split("+") if modifiers else None,
            hold_ms=hold_ms,
        )
        frames, held = self._input_epilogue(inp, warnings, capture)
        return PointerClickResult(
            at=point,
            element=element,
            button=button,
            count=count,
            modifiers=modifiers,
            hold_ms=hold_ms,
            frames=frames,
            held=held,
            warnings=warnings,
        )

    def pointer_move(self, target: Target, capture: Capture | None = None) -> PointerMoveResult:
        """Move the pointer onto a target and leave it there (hover, tooltips)."""
        inp, warnings = self._input_prologue()
        ((point, element),) = self._resolve_targets(target)
        inp.check_move_allowed()
        inp.mouse_move(point.x, point.y)
        frames, held = self._input_epilogue(inp, warnings, capture)
        return PointerMoveResult(
            at=point, element=element, frames=frames, held=held, warnings=warnings
        )

    def pointer_scroll(
        self,
        target: Target,
        delta: int,
        axis: ScrollAxis = "vertical",
        unit: ScrollUnit = "smooth_step",
        steps: int = 1,
        capture: Capture | None = None,
    ) -> PointerScrollResult:
        """Move to a target and scroll the wheel (positive delta = down/right)."""
        steps = max(steps, 1)
        inp, warnings = self._input_prologue()
        ((point, element),) = self._resolve_targets(target)
        moved = inp.moved_while_held(None)
        if moved is not None:
            warnings.append(moved)
        if delta != 0:
            inp.mouse_scroll(
                point.x,
                point.y,
                delta,
                horizontal=axis == "horizontal",
                discrete=unit == "detent",
                steps=steps,
            )
        frames, held = self._input_epilogue(inp, warnings, capture)
        return PointerScrollResult(
            at=point,
            element=element,
            delta=delta,
            axis=axis,
            unit=unit,
            steps=steps,
            frames=frames,
            held=held,
            warnings=warnings,
        )

    def pointer_drag(
        self,
        from_: Target,
        to: Target,
        button: MouseButtonName = "left",
        modifiers: KeyChord | None = None,
        waypoints: list[Waypoint] | None = None,
        dwell_ms: int = 0,
        capture: Capture | None = None,
    ) -> PointerDragResult:
        """Press a button at one target, move through waypoints, release at another."""
        modifier_codes = modifier_keycodes(modifiers)
        btn = MouseButton(button)
        inp, warnings = self._input_prologue()
        (start, _), (end, element) = self._resolve_targets(from_, to)
        inp.check_not_held("pointer_drag", buttons=[btn], keycodes=modifier_codes)
        moved = inp.moved_while_held(btn)
        if moved is not None:
            warnings.append(moved)
        inp.mouse_drag(
            start.x,
            start.y,
            end.x,
            end.y,
            button=btn,
            modifiers=modifiers.split("+") if modifiers else None,
            waypoints=[(w.x, w.y, w.dwell_ms) for w in waypoints] if waypoints else None,
            dwell_ms=dwell_ms,
        )
        frames, held = self._input_epilogue(inp, warnings, capture)
        return PointerDragResult(
            at=end,
            element=element,
            start=start,
            button=button,
            modifiers=modifiers,
            waypoints=len(waypoints) if waypoints else 0,
            dwell_ms=dwell_ms,
            frames=frames,
            held=held,
            warnings=warnings,
        )

    def pointer_hold(
        self,
        phase: PointerHoldPhase,
        target: Target,
        button: MouseButtonName = "left",
        capture: Capture | None = None,
    ) -> PointerHoldResult:
        """Press, move or release one mouse button across calls; each phase moves to target."""
        btn = MouseButton(button)
        inp, warnings = self._input_prologue()
        ((point, element),) = self._resolve_targets(target)
        if phase == "press":
            changed = inp.mouse_button_down(point.x, point.y, btn)
        elif phase == "move":
            inp.mouse_hold_move(point.x, point.y, btn)
            changed = False
        else:
            changed = inp.mouse_button_up(point.x, point.y, btn)
        frames, held = self._input_epilogue(inp, warnings, capture)
        return PointerHoldResult(
            at=point,
            element=element,
            phase=phase,
            button=button,
            changed=changed,
            frames=frames,
            held=held,
            warnings=warnings,
        )

    # ── Keyboard ──────────────────────────────────────────────────────────

    def keyboard_press(self, keys: KeyChord, capture: Capture | None = None) -> KeyboardPressResult:
        """Press and release one key or chord, e.g. "Return" or "ctrl+shift+t"."""
        keycodes = chord_keycodes(keys)
        inp, warnings = self._input_prologue()
        inp.check_not_held("keyboard_press", keycodes=keycodes)
        inp.keyboard_key(keys)
        frames, held = self._input_epilogue(inp, warnings, capture)
        return KeyboardPressResult(keys=keys, frames=frames, held=held, warnings=warnings)

    def keyboard_type(
        self, text: str, method: TypeMethod = "keys", capture: Capture | None = None
    ) -> KeyboardTypeResult:
        """Insert text at the keyboard focus: per-key US-QWERTY events, or unicode text."""
        if method == "keys":
            check_typeable(text)
        elif "\0" in text:
            # NUL can neither travel in wtype's argv nor be typed meaningfully.
            msg = "text must not contain NUL characters"
            raise ValueError(msg)
        inp, warnings = self._input_prologue()

        if method == "keys":
            inp.check_not_held("keyboard_type", keycodes=text_keycodes(text))
            inp.keyboard_type(text)
            route = "keys"
        elif not text:
            # Nothing to insert: the paste route would still send the chord.
            route = "wtype"
        else:
            # The paste chord depends on the focused app: an unbound Ctrl+V in a
            # terminal reaches the pty as ^V and alters the next keystroke.
            # Passing the query as a callable keeps it off the wtype path.
            def focused_class() -> str | None:
                focus = self._run_kwin_query({"op": "active_class"})
                return str(focus["result"]) if focus["ok"] else None

            def before_paste(chord: str) -> None:
                inp.check_not_held("keyboard_type", keycodes=chord_keycodes(chord))

            # Both routes are Wayland clients: without the session's
            # WAYLAND_DISPLAY they would act on whatever compositor the
            # server inherited.
            typed = inp.keyboard_type_unicode(
                text,
                env=self._session_env(),
                focused_class=focused_class,
                before_paste=before_paste,
            )
            if typed is None:
                msg = f"Failed to type unicode: {text_echo(text)}"
                raise RuntimeError(msg)
            route = typed.render()

        frames, held = self._input_epilogue(inp, warnings, capture)
        return KeyboardTypeResult(
            method=method,
            route=route,
            chars=len(text),
            echo=text_echo(text),
            frames=frames,
            held=held,
            warnings=warnings,
        )

    def keyboard_hold(
        self, phase: KeyboardHoldPhase, keys: KeyChord, capture: Capture | None = None
    ) -> KeyboardHoldResult:
        """Press or release one key chord across calls (modifiers first on press)."""
        chord_keycodes(keys)
        inp, warnings = self._input_prologue()
        changed = inp.keyboard_key_down(keys) if phase == "press" else inp.keyboard_key_up(keys)
        frames, held = self._input_epilogue(inp, warnings, capture)
        return KeyboardHoldResult(
            phase=phase, keys=keys, changed=changed, frames=frames, held=held, warnings=warnings
        )

    # ── Touch ─────────────────────────────────────────────────────────────

    def touch_tap(
        self, target: Target, hold_ms: int = 0, capture: Capture | None = None
    ) -> TouchTapResult:
        """Tap a target with one touch contact (does not move the pointer)."""
        inp, warnings = self._input_prologue()
        ((point, element),) = self._resolve_targets(target)
        inp.touch_tap(point.x, point.y, hold_ms=hold_ms)
        frames, held = self._input_epilogue(inp, warnings, capture)
        return TouchTapResult(
            at=point,
            element=element,
            hold_ms=hold_ms,
            frames=frames,
            held=held,
            warnings=warnings,
        )

    def touch_swipe(
        self,
        from_: Target,
        to: Target,
        fingers: int = 1,
        duration_ms: int = 300,
        capture: Capture | None = None,
    ) -> TouchSwipeResult:
        """Swipe with N parallel contacts from one target to another."""
        if fingers < 1:
            msg = f"touch_swipe fingers must be at least 1, got {fingers}"
            raise ValueError(msg)
        inp, warnings = self._input_prologue()
        (start, _), (end, element) = self._resolve_targets(from_, to)
        if fingers == 1:
            inp.touch_swipe(start.x, start.y, end.x, end.y, duration_ms=duration_ms)
        else:
            inp.touch_multi_swipe(
                start.x, start.y, end.x, end.y, fingers=fingers, duration_ms=duration_ms
            )
        frames, held = self._input_epilogue(inp, warnings, capture)
        return TouchSwipeResult(
            at=end,
            element=element,
            start=start,
            fingers=fingers,
            duration_ms=duration_ms,
            frames=frames,
            held=held,
            warnings=warnings,
        )

    def touch_pinch(
        self,
        center: Target,
        start_distance: int,
        end_distance: int,
        duration_ms: int = 500,
        capture: Capture | None = None,
    ) -> TouchPinchResult:
        """Move two contacts symmetrically toward or away from a centre."""
        inp, warnings = self._input_prologue()
        ((point, element),) = self._resolve_targets(center)
        inp.touch_pinch(point.x, point.y, start_distance, end_distance, duration_ms=duration_ms)
        frames, held = self._input_epilogue(inp, warnings, capture)
        return TouchPinchResult(
            at=point,
            element=element,
            start_distance=start_distance,
            end_distance=end_distance,
            duration_ms=duration_ms,
            frames=frames,
            held=held,
            warnings=warnings,
        )

    # ── Clipboard ─────────────────────────────────────────────────────────

    def clipboard_read(self) -> ClipboardText:
        """Return the session clipboard text (wl-paste)."""
        self._require_clipboard()
        env = self._session_env()
        try:
            result = subprocess.run(
                ["wl-paste", "--no-newline"],
                env=env,
                capture_output=True,
                timeout=5,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(_INSTALL_HINTS["wl-paste"]) from exc
        if result.returncode != 0:
            msg = f"Failed to read clipboard: {result.stderr.decode(errors='replace')}"
            raise RuntimeError(msg)
        return ClipboardText(text=result.stdout.decode(errors="replace"))

    def clipboard_write(self, text: str) -> ClipboardWriteResult:
        """Own the session clipboard with this text (wl-copy, waits up to 5 s)."""
        self._require_clipboard()
        env = self._session_env()
        # The text goes through stdin: argv is size-limited (E2BIG) and cannot
        # carry NUL. Output stays on DEVNULL because the forked owner inherits it.
        try:
            proc = subprocess.Popen(
                ["wl-copy", "--type", "text/plain;charset=utf-8"],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(_INSTALL_HINTS["wl-copy"]) from exc
        # The exit is the readiness signal; the forked owner intentionally
        # outlives this call and serves the text until something replaces it.
        try:
            proc.communicate(text.encode(), timeout=_CLIPBOARD_SET_TIMEOUT)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            msg = (
                "Failed to set clipboard: wl-copy did not confirm the selection "
                f"within {_CLIPBOARD_SET_TIMEOUT:g}s"
            )
            raise RuntimeError(msg) from None
        if proc.returncode != 0:
            msg = f"Failed to set clipboard: wl-copy exited with status {proc.returncode}"
            raise RuntimeError(msg)
        return ClipboardWriteResult(length=len(text), echo=text_echo(text))

    # ── Escape hatches ────────────────────────────────────────────────────

    def dbus_call(
        self,
        service: str,
        path: str,
        interface: str,
        method: str,
        args: list[str | TypedArg] | None = None,
    ) -> DbusReply:
        """Call any D-Bus method on the session bus; the declared signature is introspected.

        ``args`` accepts dbus-send strings (``"type:value"``) and/or typed
        arguments; both shapes may mix in one call. When the object is
        introspectable, the arguments must match one of the method's declared
        input signatures: argument types may differ only where the signature
        expects a variant, so e.g. ``int32`` does not silently become
        ``int64``. Qt overloads and slots with default arguments declare
        several signatures; the first one that fits is used. Argument errors
        raise ``ValueError("D-Bus call failed: ...")`` and nothing is sent.
        When the object does not introspect, the call is sent with each
        argument's own signature and the remote side validates it; a remote
        error reply raises ``RuntimeError("D-Bus error: <name>: <message>")``.
        """
        import dbus
        import dbus.bus

        from kwin_mcp.dbus_args import parse_arg

        info = self._require_info()
        if not info.dbus_address:
            msg = "D-Bus call failed: session has no D-Bus address"
            raise RuntimeError(msg)

        try:
            parsed_args = [
                parse_arg(a if isinstance(a, str) else a.model_dump(exclude_none=True))
                for a in (args or [])
            ]
        except ValueError as exc:
            msg = f"D-Bus call failed: {exc}"
            raise ValueError(msg) from exc

        signature: str | None = None
        # Raised after the try so the marshalling handler below does not wrap it.
        mismatch = ""
        result: object = None
        try:
            bus = dbus.bus.BusConnection(info.dbus_address)
            # Introspect here rather than inside the proxy so the selection and
            # the marshalling below use the same signature. dbus-python's own
            # marshalling appends nothing for an empty signature, which would
            # drop surplus arguments without an error.
            obj = bus.get_object(service, path, introspect=False)
            try:
                xml_data = obj.Introspect(
                    dbus_interface="org.freedesktop.DBus.Introspectable",
                    timeout=_DBUS_CALL_TIMEOUT,
                )
            except dbus.DBusException:
                # Not introspectable (or unreachable): the call below reports
                # the real error, or the remote side checks the arguments.
                xml_data = ""
            if xml_data:
                candidates = _introspected_in_signatures(str(xml_data), interface, method)
                if candidates is not None:
                    signature = _select_signature(parsed_args, candidates)
                    if signature is None:
                        shown = "/".join(repr(c) for c in candidates)
                        sent = "".join(_arg_signature(a) for a in parsed_args)
                        mismatch = (
                            f"D-Bus call failed: {interface}.{method} takes "
                            f"{shown}, got signature '{sent}'"
                        )
            if not mismatch:
                result = obj.get_dbus_method(method, interface)(
                    *parsed_args, signature=signature, timeout=_DBUS_CALL_TIMEOUT
                )
        except dbus.DBusException as exc:
            name = exc.get_dbus_name() or type(exc).__name__
            detail = exc.get_dbus_message() or str(exc)
            msg = f"D-Bus error: {name}: {detail}"
            raise RuntimeError(msg) from exc
        except (TypeError, ValueError, OverflowError) as exc:
            # dbus-python raises these while validating names or marshalling
            # arguments that do not fit the signature; nothing was sent.
            expected_sig = f" (signature '{signature}')" if signature is not None else ""
            msg = f"D-Bus call failed: {interface}.{method}{expected_sig}: {exc}"
            raise ValueError(msg) from exc
        if mismatch:
            raise ValueError(mismatch)
        return DbusReply(reply=_dbus_to_json(result), signature=_reply_signature(result))

    def wayland_list(self, filter: str = "") -> WaylandGlobals:  # noqa: A002
        """List the Wayland protocol globals the compositor advertises (wayland-info)."""
        env = self._session_env()
        try:
            result = subprocess.run(
                ["wayland-info"],
                env=env,
                capture_output=True,
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(_INSTALL_HINTS["wayland-info"]) from exc
        if result.returncode != 0:
            msg = f"wayland-info failed: {result.stderr.decode(errors='replace')}"
            raise RuntimeError(msg)

        lines = result.stdout.decode(errors="replace").splitlines()
        if filter:
            lines = [line for line in lines if filter in line]
        globals_: list[WaylandGlobal] = []
        for line in lines:
            match = _WAYLAND_GLOBAL_PATTERN.search(line)
            if match is not None:
                globals_.append(
                    WaylandGlobal(
                        interface=match.group(1),
                        version=int(match.group(2)),
                        name=int(match.group(3)),
                    )
                )
        return WaylandGlobals(filter=filter, lines=lines, globals=globals_)
