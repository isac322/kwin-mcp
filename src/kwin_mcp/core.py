"""Core automation engine for KDE Wayland GUI automation.

Contains all tool logic independent of the MCP transport layer.
Can be used directly from the CLI or wrapped by the MCP server.
"""

from __future__ import annotations

import contextlib
import json
import os
import selectors
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from xml.etree import ElementTree

from kwin_mcp.input import InputBackend, MouseButton
from kwin_mcp.screenshot import capture_frame_burst, capture_screenshot_to_file
from kwin_mcp.session import LiveSession, Session, SessionConfig

# Per-attempt bound for one AT-SPI2 request, and how long a worker may take to
# exit on stdin EOF before it is killed.
_ATSPI_TIMEOUT_S = 30.0
_ATSPI_EXIT_GRACE_S = 2.0

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
    "wtype": (
        "wtype not found. Install wtype "
        "(e.g. 'sudo pacman -S wtype' or build from https://github.com/atx/wtype)."
    ),
    "spectacle": (
        "spectacle not found. Install spectacle "
        "(e.g. 'sudo pacman -S spectacle' or 'sudo apt install kde-spectacle')."
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


def _element_position(el: dict) -> str:
    """Format an element's position for find_ui_elements / wait_for_element.

    Coordinates are global screen coordinates (the space mouse_click and
    touch_tap take). Elements whose window could not be identified with
    certainty report "unavailable" with a reason and no numbers, so callers
    never click a plausible-looking wrong point.
    """
    if el.get("mapped"):
        return f"@ screen ({el['x']}, {el['y']}, {el['width']}x{el['height']})"
    return f"@ unavailable ({el.get('unavailable') or 'unmapped'})"


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
    """Return the complete D-Bus signature of one parsed argument.

    ``parse_arg`` returns values with ``variant_level=1`` for an explicit
    ``variant`` argument; those marshal as ``v``. Everything else has a
    fixed signature. Parsed args are always dbus types, so the final raise
    is unreachable but keeps the checker honest if that changes.
    """
    import dbus

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
    if isinstance(arg, dbus.Array):
        return "a" + str(arg.signature)
    if isinstance(arg, dbus.Dictionary):
        return "a{" + str(arg.signature) + "}"
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


def _dbus_to_json(value: object) -> object:
    import dbus

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
        return {_dbus_to_json(k): _dbus_to_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_dbus_to_json(x) for x in value]
    if isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _format_dbus_result(result: object) -> str:
    """Render a D-Bus reply for MCP clients.

    Returns the empty string for void replies, the bare value for single
    primitives (so ``GetId`` returns just the UUID), and JSON for
    containers and multi-value tuples.
    """
    import dbus

    if result is None:
        return ""
    if isinstance(result, dbus.Boolean):
        return "true" if bool(result) else "false"
    if isinstance(result, dbus.ObjectPath | dbus.Signature | dbus.String | str):
        return str(result)
    if isinstance(
        result,
        dbus.Byte | dbus.Int16 | dbus.UInt16 | dbus.Int32 | dbus.UInt32 | dbus.Int64 | dbus.UInt64,
    ):
        return str(int(result))
    if isinstance(result, dbus.Double | float):
        return str(float(result))
    return json.dumps(_dbus_to_json(result))


class AutomationEngine:
    """Core automation engine encapsulating all tool logic.

    Manages session lifecycle, input injection, screenshot capture,
    accessibility queries, and clipboard operations.
    """

    def __init__(self) -> None:
        self._session: Session | LiveSession | None = None
        self._input: InputBackend | None = None
        self._clipboard_enabled: bool = False
        self._keep_screenshots: bool = False
        self._atspi_proc: subprocess.Popen[bytes] | None = None
        self._atspi_bus: str = ""
        self._atspi_buffer: bytes = b""
        self._atspi_a11y: str = ""
        self._atspi_lock = threading.Lock()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self._teardown_atspi_worker()

    # ── Private helpers ───────────────────────────────────────────────────

    def _get_session(self) -> Session | LiveSession:
        if self._session is None or not self._session.is_running:
            msg = "No active session. Call session_start or session_connect first."
            raise RuntimeError(msg)
        return self._session

    def _get_input(self) -> InputBackend:
        if self._input is None:
            msg = "No input backend. Call session_start or session_connect first."
            raise RuntimeError(msg)
        return self._input

    def _session_env(self) -> dict[str, str]:
        """Build environment dict for tools that need the isolated session."""
        session = self._get_session()
        env = {**os.environ}
        info = session.info
        if info:
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
        answering ``(no accessible applications found)`` from its dead
        connection forever, so this is probed on every call.
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
        old process would answer from the wrong or a dead bus: replace it.
        """
        env = self._session_env()
        bus = env.get("DBUS_SESSION_BUS_ADDRESS", "")
        a11y = self._a11y_bus_address(env)
        proc = self._atspi_proc
        if proc is not None and (
            proc.poll() is not None or bus != self._atspi_bus or a11y != self._atspi_a11y
        ):
            self._teardown_atspi_worker()
            proc = None
        if proc is None:
            proc = subprocess.Popen(
                [sys.executable, "-m", "kwin_mcp.accessibility", "--serve"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                env=env,
            )
            self._atspi_proc = proc
            self._atspi_bus = bus
            self._atspi_a11y = a11y
            self._atspi_buffer = b""
        return proc

    def _atspi_exchange(self, proc: subprocess.Popen[bytes], line: bytes) -> dict:
        """Send one request line to the worker and read one response line.

        Raises ``TimeoutError`` after ``_ATSPI_TIMEOUT_S`` and ``EOFError`` if the
        worker exits or closes its pipes.
        """
        assert proc.stdin is not None
        assert proc.stdout is not None
        try:
            proc.stdin.write(line)
            proc.stdin.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise EOFError("AT-SPI2 worker exited before the request was sent") from exc

        deadline = time.monotonic() + _ATSPI_TIMEOUT_S
        fd = proc.stdout.fileno()
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while b"\n" not in self._atspi_buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise EOFError(f"AT-SPI2 worker exited (status {proc.poll()})")
                self._atspi_buffer += chunk
        response, _, self._atspi_buffer = self._atspi_buffer.partition(b"\n")
        return json.loads(response)

    def _run_atspi(self, op: str, **kwargs: object) -> dict:
        """Run an AT-SPI2 query in the long-lived worker bound to the session bus.

        The worker (``python -m kwin_mcp.accessibility --serve``) is started once
        per session bus and reused, so each call skips interpreter startup and the
        PyGObject/Atspi import. A timeout, worker exit or worker-side exception
        discards the worker and retries once on a fresh one.
        """
        line = json.dumps({"op": op, **kwargs}).encode() + b"\n"
        last_error = ""
        with self._atspi_lock:
            for attempt in range(2):
                if attempt > 0:
                    time.sleep(0.5)
                proc = self._ensure_atspi_worker()
                try:
                    resp = self._atspi_exchange(proc, line)
                except TimeoutError:
                    last_error = f"AT-SPI2 query timed out after {_ATSPI_TIMEOUT_S:g}s (op={op})"
                except EOFError as exc:
                    last_error = f"AT-SPI2 query failed: {exc}"
                except json.JSONDecodeError as exc:
                    last_error = f"AT-SPI2 query returned invalid JSON: {exc}"
                else:
                    if "worker_error" not in resp:
                        return resp
                    last_error = f"AT-SPI2 query failed: {resp['worker_error']}"
                self._teardown_atspi_worker()

        msg = f"{last_error}. Retried once but still failed — the AT-SPI2 bus may be unstable."
        raise RuntimeError(msg)

    def _teardown_atspi_worker(self) -> None:
        """Stop the AT-SPI worker with bounded latency.

        Closing stdin lets an idle worker exit on EOF; a busy, hung or stopped
        worker is killed after a short grace period.
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
            proc.wait(timeout=_ATSPI_EXIT_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()

    def _with_frame_capture(
        self,
        action_result: str,
        screenshot_after_ms: list[int] | None,
    ) -> str:
        """Append frame captures to an action result if requested."""
        if not screenshot_after_ms:
            return action_result

        session = self._get_session()
        info = session.info
        if info is None:
            return action_result

        frames = capture_frame_burst(
            dbus_address=info.dbus_address,
            output_dir=info.screenshot_dir,
            delays_ms=screenshot_after_ms,
            wayland_socket=info.wayland_socket,
        )

        lines = [action_result, f"Captured {len(frames)} frames:"]
        for delay_ms, (path, mapping) in zip(sorted(screenshot_after_ms), frames, strict=True):
            size_kb = path.stat().st_size / 1024
            lines.append(f"  {delay_ms}ms: {path} ({size_kb:.1f} KB)")
            lines.append(f"    {mapping.describe()}")
        return "\n".join(lines)

    # ── Session management ────────────────────────────────────────────────

    def session_start(
        self,
        app_command: str = "",
        screen_width: int = 1920,
        screen_height: int = 1080,
        enable_clipboard: bool = False,
        keep_screenshots: bool = False,
        isolate_home: bool = False,
        keep_home: bool = False,
        env: dict[str, str] | None = None,
    ) -> str:
        """Start an isolated KWin Wayland session, optionally launching an app."""
        if self._session is not None and self._session.is_running:
            return "Session already running. Call session_stop first."

        # Any worker left over from an earlier session (crashed or never stopped)
        # is bound to that session's bus.
        self._teardown_atspi_worker()

        self._clipboard_enabled = enable_clipboard

        self._session = Session()
        config = SessionConfig(
            screen_width=screen_width,
            screen_height=screen_height,
            enable_clipboard=enable_clipboard,
            keep_screenshots=keep_screenshots,
            isolate_home=isolate_home,
            keep_home=keep_home,
        )
        info = self._session.start(config)

        result = f"Session started. Wayland socket: {info.wayland_socket}"
        if info.home_dir:
            result += f"\nIsolated home: {info.home_dir}"
        for warning in info.startup_warnings:
            result += f"\nWarning: {warning}"

        if app_command:
            cmd = shlex.split(app_command)
            app_info = self._session.launch_app(cmd, extra_env=env)
            result += f"\nApp launched: {app_command} (PID={app_info.pid})"
            result += f"\nApp log: {app_info.log_path}"

        # Set up input backend via KWin's EIS D-Bus interface
        time.sleep(0.5)
        try:
            self._input = InputBackend(info.dbus_address)
        except RuntimeError as exc:
            self._input = None
            result += f"\nNo input backend available ({exc})"
        else:
            result += "\nInput backend: KWin EIS"

        return result

    def session_connect(
        self,
        dbus_address: str = "",
        wayland_display: str = "",
        keep_screenshots: bool = False,
    ) -> str:
        """Connect to an existing KWin session (e.g. the real desktop)."""
        if self._session is not None and self._session.is_running:
            return "Session already running. Call session_stop first."

        # Any worker left over from an earlier session (crashed or never stopped)
        # is bound to that session's bus.
        self._teardown_atspi_worker()

        dbus_addr = dbus_address or os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
        wayland_disp = wayland_display or os.environ.get("WAYLAND_DISPLAY", "")

        if not dbus_addr:
            return (
                "No D-Bus address available. Provide dbus_address parameter "
                "or ensure $DBUS_SESSION_BUS_ADDRESS is set."
            )
        if not wayland_disp:
            return (
                "No Wayland display available. Provide wayland_display parameter "
                "or ensure $WAYLAND_DISPLAY is set."
            )

        # Validate KWin is reachable on the given D-Bus
        import dbus as dbus_module
        import dbus.bus

        try:
            bus = dbus.bus.BusConnection(dbus_addr)
            bus.get_object("org.kde.KWin", "/org/kde/KWin")
        except dbus_module.DBusException as exc:
            return f"Cannot reach KWin on D-Bus ({dbus_addr}): {exc}"

        display_path = Path(wayland_disp)
        if not display_path.is_absolute():
            runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
            if not runtime_dir:
                return f"Cannot reach Wayland display ({wayland_disp}): XDG_RUNTIME_DIR is not set"
            display_path = Path(runtime_dir) / display_path

        try:
            display_mode = display_path.stat().st_mode
        except OSError as exc:
            return f"Cannot reach Wayland display ({wayland_disp}): {exc}"
        if not stat.S_ISSOCK(display_mode):
            return (
                f"Cannot reach Wayland display ({wayland_disp}): "
                f"{display_path} is not a Unix socket"
            )

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                probe.connect(str(display_path))
        except OSError as exc:
            return f"Cannot reach Wayland display ({wayland_disp}): {exc}"

        screenshot_dir = Path(tempfile.mkdtemp(prefix="kwin-mcp-screenshots-"))

        session = LiveSession(dbus_addr, wayland_disp, screenshot_dir)
        session._keep_screenshots = keep_screenshots
        self._session = session
        self._keep_screenshots = keep_screenshots

        # Clipboard is always available on live sessions
        self._clipboard_enabled = True

        result = f"Connected to live KWin session. D-Bus: {dbus_addr}, Wayland: {wayland_disp}"

        # Set up input backend — EIS first, ydotool fallback
        time.sleep(0.3)
        try:
            self._input = InputBackend(dbus_addr)
            result += "\nInput backend: KWin EIS"
        except RuntimeError as exc:
            self._input = None
            if shutil.which("ydotool"):
                result += "\nInput backend: ydotool (EIS unavailable)"
            else:
                result += (
                    f"\nNo input backend available ({exc}; ydotool not found). "
                    "Screenshot and accessibility tools still work."
                )

        return result

    def session_stop(self) -> str:
        """Stop the current session and clean up."""
        if self._session is None:
            return "No session running."

        self._clipboard_enabled = False

        if self._input is not None:
            self._input.close()

        # The AT-SPI worker is bound to this session's bus; stop it before the bus goes away.
        self._teardown_atspi_worker()

        is_live = isinstance(self._session, LiveSession)
        if isinstance(self._session, LiveSession):
            self._session.stop(keep_screenshots=self._keep_screenshots)
        else:
            self._session.stop()
        self._session = None
        self._input = None
        self._keep_screenshots = False

        return "Disconnected from live session." if is_live else "Session stopped."

    # ── Screenshot / Accessibility ────────────────────────────────────────

    def screenshot(self, include_cursor: bool = False) -> str:
        """Capture a screenshot of the isolated session."""
        session = self._get_session()
        info = session.info
        if info is None:
            msg = "No session info available"
            raise RuntimeError(msg)

        path, mapping = capture_screenshot_to_file(
            dbus_address=info.dbus_address,
            wayland_socket=info.wayland_socket,
            include_cursor=include_cursor,
            output_dir=info.screenshot_dir,
        )
        size_kb = path.stat().st_size / 1024
        return f"Screenshot saved: {path} ({size_kb:.1f} KB)\n{mapping.describe()}"

    def accessibility_tree(self, app_name: str = "", max_depth: int = 15, role: str = "") -> str:
        """Get the accessibility tree of apps in the isolated session."""
        self._get_session()
        resp = self._run_atspi("tree", app_name=app_name, max_depth=max_depth, role=role)
        return resp["result"]

    def find_ui_elements(
        self, query: str, app_name: str = "", states: list[str] | None = None
    ) -> str:
        """Find UI elements matching a search query and/or required states."""
        self._get_session()
        resp = self._run_atspi("find", query=query, app_name=app_name, states=states)
        elements = resp["result"]

        # Build descriptive search summary
        criteria: list[str] = []
        if query:
            criteria.append(f"query='{query}'")
        if states:
            criteria.append(f"states={states}")
        search_desc = ", ".join(criteria) if criteria else "(all)"

        if not elements:
            return f"No elements found matching {search_desc}"

        lines = [f"Found {len(elements)} elements matching {search_desc}:\n"]
        for el in elements:
            actions_str = f" [actions: {', '.join(el['actions'])}]" if el["actions"] else ""
            text_str = f" text={el['text']!r}" if el.get("text") else ""
            value_str = (
                f" value={el['value']:g}/{el['value_max']:g}"
                if el.get("value") is not None and el.get("value_max") is not None
                else ""
            )
            lines.append(
                f'- [{el["role"]}] "{el["name"]}" '
                f"{_element_position(el)}{text_str}{value_str}{actions_str}"
            )
        return "\n".join(lines)

    # ── Mouse tools ───────────────────────────────────────────────────────

    def mouse_click(
        self,
        x: int,
        y: int,
        button: str = "left",
        double: bool = False,
        triple: bool = False,
        modifiers: list[str] | None = None,
        hold_ms: int = 0,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Click at coordinates in the isolated session."""
        inp = self._get_input()
        btn = MouseButton(button)
        click_count = 3 if triple else (2 if double else 1)
        inp.mouse_click(x, y, btn, click_count=click_count, modifiers=modifiers, hold_ms=hold_ms)

        desc = f"Clicked {button} at ({x}, {y})"
        if triple:
            desc += " (triple)"
        elif double:
            desc += " (double)"
        if modifiers:
            desc += f" with {'+'.join(modifiers)}"
        if hold_ms > 0:
            desc += f" held {hold_ms}ms"

        return self._with_frame_capture(desc, screenshot_after_ms)

    def mouse_move(
        self,
        x: int,
        y: int,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Move the mouse cursor to coordinates without clicking."""
        inp = self._get_input()
        inp.mouse_move(x, y)
        result = f"Mouse moved to ({x}, {y})"
        return self._with_frame_capture(result, screenshot_after_ms)

    def mouse_scroll(
        self,
        x: int,
        y: int,
        delta: int,
        horizontal: bool = False,
        discrete: bool = False,
        steps: int = 1,
    ) -> str:
        """Scroll at coordinates in the isolated session."""
        inp = self._get_input()
        inp.mouse_scroll(x, y, delta, horizontal=horizontal, discrete=discrete, steps=steps)
        direction = "horizontal" if horizontal else "vertical"
        mode = "discrete" if discrete else "smooth"
        desc = f"Scrolled {direction} ({mode}) by {delta} at ({x}, {y})"
        if steps > 1:
            desc += f" in {steps} steps"
        return desc

    def mouse_drag(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        button: str = "left",
        modifiers: list[str] | None = None,
        waypoints: list[list[int]] | None = None,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Drag from one point to another in the isolated session."""
        inp = self._get_input()
        btn = MouseButton(button)
        wp: list[tuple[int, int, int]] | None = None
        if waypoints:
            wp = [(w[0], w[1], w[2]) for w in waypoints]
        inp.mouse_drag(from_x, from_y, to_x, to_y, button=btn, modifiers=modifiers, waypoints=wp)

        desc = f"Dragged from ({from_x}, {from_y}) to ({to_x}, {to_y})"
        if modifiers:
            desc += f" with {'+'.join(modifiers)}"
        if waypoints:
            desc += f" via {len(waypoints)} waypoints"
        return self._with_frame_capture(desc, screenshot_after_ms)

    def mouse_button_down(
        self,
        x: int,
        y: int,
        button: str = "left",
    ) -> str:
        """Press a mouse button at coordinates without releasing."""
        inp = self._get_input()
        inp.mouse_button_down(x, y, MouseButton(button))
        return f"Button {button} pressed at ({x}, {y})"

    def mouse_button_up(
        self,
        x: int,
        y: int,
        button: str = "left",
    ) -> str:
        """Release a mouse button at coordinates."""
        inp = self._get_input()
        inp.mouse_button_up(x, y, MouseButton(button))
        return f"Button {button} released at ({x}, {y})"

    # ── Keyboard tools ────────────────────────────────────────────────────

    def keyboard_type(
        self,
        text: str,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Type ASCII text into the currently focused element."""
        inp = self._get_input()
        inp.keyboard_type(text)
        result = f"Typed: {text!r}"
        return self._with_frame_capture(result, screenshot_after_ms)

    def keyboard_type_unicode(
        self,
        text: str,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Type arbitrary Unicode text including non-ASCII characters."""
        inp = self._get_input()
        # Both routes are Wayland clients: without the session's WAYLAND_DISPLAY
        # they would act on whatever compositor the server inherited.
        ok = inp.keyboard_type_unicode(text, env=self._session_env())
        result = f"Typed unicode: {text!r}" if ok else f"Failed to type unicode: {text!r}"
        return self._with_frame_capture(result, screenshot_after_ms)

    def keyboard_key(
        self,
        key: str,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Press and release a key or key combination."""
        inp = self._get_input()
        inp.keyboard_key(key)
        result = f"Pressed: {key}"
        return self._with_frame_capture(result, screenshot_after_ms)

    def keyboard_key_down(self, key: str) -> str:
        """Press and hold a key without releasing."""
        inp = self._get_input()
        inp.keyboard_key_down(key)
        return f"Key down: {key}"

    def keyboard_key_up(self, key: str) -> str:
        """Release a previously held key."""
        inp = self._get_input()
        inp.keyboard_key_up(key)
        return f"Key up: {key}"

    # ── Touch tools ───────────────────────────────────────────────────────

    def touch_tap(
        self,
        x: int,
        y: int,
        hold_ms: int = 0,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Tap at coordinates using touch input."""
        inp = self._get_input()
        inp.touch_tap(x, y, hold_ms=hold_ms)
        desc = f"Touch tap at ({x}, {y})"
        if hold_ms > 0:
            desc += f" held {hold_ms}ms"
        return self._with_frame_capture(desc, screenshot_after_ms)

    def touch_swipe(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        duration_ms: int = 300,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Swipe from one point to another using single-finger touch input."""
        inp = self._get_input()
        inp.touch_swipe(from_x, from_y, to_x, to_y, duration_ms=duration_ms)
        desc = f"Touch swipe from ({from_x}, {from_y}) to ({to_x}, {to_y}) in {duration_ms}ms"
        return self._with_frame_capture(desc, screenshot_after_ms)

    def touch_pinch(
        self,
        center_x: int,
        center_y: int,
        start_distance: int,
        end_distance: int,
        duration_ms: int = 500,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Perform a two-finger pinch gesture."""
        inp = self._get_input()
        inp.touch_pinch(center_x, center_y, start_distance, end_distance, duration_ms=duration_ms)
        direction = "in" if end_distance < start_distance else "out"
        desc = f"Pinch {direction} at ({center_x}, {center_y}): {start_distance}→{end_distance}px"
        return self._with_frame_capture(desc, screenshot_after_ms)

    def touch_multi_swipe(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        fingers: int = 3,
        duration_ms: int = 300,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Perform a multi-finger swipe gesture."""
        inp = self._get_input()
        inp.touch_multi_swipe(from_x, from_y, to_x, to_y, fingers=fingers, duration_ms=duration_ms)
        desc = (
            f"{fingers}-finger swipe from ({from_x}, {from_y}) "
            f"to ({to_x}, {to_y}) in {duration_ms}ms"
        )
        return self._with_frame_capture(desc, screenshot_after_ms)

    # ── Clipboard tools ───────────────────────────────────────────────────

    def clipboard_get(self) -> str:
        """Read the current clipboard content in the isolated session."""
        if not self._clipboard_enabled:
            return (
                "Clipboard not enabled. Pass enable_clipboard=True to session_start, "
                "or use session_connect (clipboard is always enabled for live sessions)."
            )

        env = self._session_env()
        try:
            result = subprocess.run(
                ["wl-paste", "--no-newline"],
                env=env,
                capture_output=True,
                timeout=5,
            )
        except FileNotFoundError:
            return _INSTALL_HINTS["wl-paste"]
        if result.returncode != 0:
            return f"Failed to read clipboard: {result.stderr.decode(errors='replace')}"
        return result.stdout.decode(errors="replace")

    def clipboard_set(self, text: str) -> str:
        """Set the clipboard content in the isolated session."""
        if not self._clipboard_enabled:
            return (
                "Clipboard not enabled. Pass enable_clipboard=True to session_start, "
                "or use session_connect (clipboard is always enabled for live sessions)."
            )

        env = self._session_env()
        try:
            proc = subprocess.Popen(
                ["wl-copy", "--", text],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            return _INSTALL_HINTS["wl-copy"]
        # The exit is the readiness signal; the forked owner intentionally
        # outlives this call and serves the text until something replaces it.
        try:
            returncode = proc.wait(timeout=_CLIPBOARD_SET_TIMEOUT)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            return (
                "Failed to set clipboard: wl-copy did not confirm the selection "
                f"within {_CLIPBOARD_SET_TIMEOUT:g}s"
            )
        if returncode != 0:
            return f"Failed to set clipboard: wl-copy exited with status {returncode}"
        return f"Clipboard set: {text!r}"

    # ── Wait-for-UI tools ─────────────────────────────────────────────────

    def wait_for_element(
        self,
        query: str,
        app_name: str = "",
        timeout_ms: int = 5000,
        poll_interval_ms: int = 200,
        expected_states: list[str] | None = None,
    ) -> str:
        """Wait for a UI element to appear in the accessibility tree."""
        self._get_session()
        resp = self._run_atspi(
            "wait",
            query=query,
            app_name=app_name,
            timeout_ms=timeout_ms,
            poll_interval_ms=poll_interval_ms,
            states=expected_states,
        )
        if not resp["ok"]:
            return resp["error"]

        elements = resp["result"]

        # Build descriptive search summary
        criteria: list[str] = []
        if query:
            criteria.append(f"query='{query}'")
        if expected_states:
            criteria.append(f"states={expected_states}")
        search_desc = ", ".join(criteria) if criteria else "(all)"

        lines = [f"Found {len(elements)} elements matching {search_desc}:\n"]
        for el in elements:
            actions_str = f" [actions: {', '.join(el['actions'])}]" if el["actions"] else ""
            text_str = f" text={el['text']!r}" if el.get("text") else ""
            value_str = (
                f" value={el['value']:g}/{el['value_max']:g}"
                if el.get("value") is not None and el.get("value_max") is not None
                else ""
            )
            lines.append(
                f'- [{el["role"]}] "{el["name"]}" '
                f"{_element_position(el)}{text_str}{value_str}{actions_str}"
            )
        return "\n".join(lines)

    # ── Window management tools ───────────────────────────────────────────

    def launch_app(self, command: str, env: dict[str, str] | None = None) -> str:
        """Launch an application inside the running isolated session."""
        session = self._get_session()
        cmd = shlex.split(command)
        app_info = session.launch_app(cmd, extra_env=env)
        return f"App launched: {command} (PID={app_info.pid})\nApp log: {app_info.log_path}"

    def list_windows(self) -> str:
        """List accessible application windows in the isolated session."""
        self._get_session()
        resp = self._run_atspi("list_windows")
        return resp["result"]

    def _run_kwin_query(self, request: dict[str, object]) -> dict:
        """Run a KWin scripting query in a subprocess bound to the session bus."""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "kwin_mcp.geometry"],
                input=json.dumps(request),
                env=self._session_env(),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "KWin query timed out after 30s"}
        if result.returncode != 0:
            return {"ok": False, "error": f"exit {result.returncode}: {result.stderr[:200]}"}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"invalid JSON: {result.stdout[:200]}"}

    def focus_window(self, app_name: str) -> str:
        """Activate a window by application name.

        Activation goes through KWin: AT-SPI2's grab_focus neither raises nor
        activates windows on Wayland, so it reported success while the previously
        active window kept both focus and the foreground.
        """
        self._get_session()
        resp = self._run_kwin_query({"op": "activate", "app_name": app_name})
        if not resp["ok"]:
            return f"Failed to focus '{app_name}': {resp['error']}"
        activated = str(resp["result"]).strip()
        return f"Focused: {activated}" if activated else f"No window matches '{app_name}'."

    @staticmethod
    def _format_window(window: dict) -> str:
        """Render one window from the KWin geometry report."""
        frame, client = window["frame"], window["client"]
        marker = " [active]" if window["active"] else ""
        return (
            f'- {window["app"]} "{window["caption"]}"{marker}\n'
            f"    id:     {window['id']}\n"
            f"    frame:  ({frame['x']}, {frame['y']}, {frame['width']}x{frame['height']})\n"
            f"    client: ({client['x']}, {client['y']}, "
            f"{client['width']}x{client['height']})"
        )

    def window_geometry(self, app_name: str = "", window_id: str = "") -> str:
        """Report window positions in global screen coordinates.

        Element rectangles from find_ui_elements and accessibility_tree are
        already translated to this same coordinate space; this tool remains
        useful for locating whole windows and diagnosing placement. Each window
        carries its KWin id, which window_id filters on and window_close takes.
        """
        self._get_session()
        resp = self._run_kwin_query({"app_name": app_name, "window_id": window_id})
        if not resp["ok"]:
            return f"Window geometry unavailable: {resp['error']}"

        windows = resp["result"]
        if not windows:
            if window_id:
                return f"No window with id {window_id!r}."
            return "No windows found." if not app_name else f"No windows found for '{app_name}'."

        lines = [f"Windows ({len(windows)}):"]
        lines.extend(self._format_window(window) for window in windows)
        return "\n".join(lines)

    def active_window(self) -> str:
        """Report the window KWin currently treats as active (the one focus_window sets)."""
        self._get_session()
        resp = self._run_kwin_query({"op": "active"})
        if not resp["ok"]:
            return f"Active window unavailable: {resp['error']}"
        window = resp["result"]
        if window is None:
            return "No active window."
        return f"Active window:\n{self._format_window(window)}"

    def window_close(self, window_id: str) -> str:
        """Ask KWin to close one window, addressed by the id window_geometry reports.

        Refused in live sessions: closing can discard unsaved work in the user's
        own applications, and unlike input actions it cannot be watched and
        interrupted step by step.
        """
        session = self._get_session()
        if isinstance(session, LiveSession):
            return (
                "window_close is disabled in live sessions: it could discard unsaved work "
                "on the real desktop. Close the window through the app's own UI instead."
            )
        if not window_id:
            return "window_close needs a window_id (see window_geometry)."
        resp = self._run_kwin_query({"op": "close", "window_id": window_id})
        if not resp["ok"]:
            return f"Failed to close window {window_id!r}: {resp['error']}"
        result = resp["result"]
        if not result["found"]:
            return f"No window with id {window_id!r}."
        name = f'{result["app"]} "{result["caption"]}"'
        if not result["closeable"]:
            return f"Window {name} ({window_id}) cannot be closed."
        return (
            f"Close requested: {name} ({window_id}). The app may keep the window open, "
            "for example to ask about unsaved changes; check window_geometry."
        )

    # ── D-Bus tools ───────────────────────────────────────────────────────

    def dbus_call(
        self,
        service: str,
        path: str,
        interface: str,
        method: str,
        args: list[str | dict] | None = None,
    ) -> str:
        """Call a D-Bus method in the isolated session.

        ``args`` accepts dbus-send strings (``"type:value"``) and/or
        typed-JSON dicts (``{"type": ..., "value": ...}``); both shapes
        may mix in one call. When the object is introspectable, the
        arguments must match one of the method's declared input
        signatures: argument types may differ only where the signature
        expects a variant, so e.g. ``int32`` does not silently become
        ``int64``. Qt overloads and slots with default arguments declare
        several signatures; the first one that fits is used. Argument
        errors are reported as ``D-Bus call failed: ...`` and nothing is
        sent. When the object does not introspect, the call is sent with
        each argument's own signature (an explicit ``variant`` argument
        stays a variant) and the remote side validates it. The reply is
        rendered via :func:`_format_dbus_result` (single primitives
        become bare strings, containers and tuples become JSON).
        """
        import dbus
        import dbus.bus

        from kwin_mcp.dbus_args import parse_arg

        info = self._get_session().info
        if info is None or not info.dbus_address:
            return "D-Bus call failed: session has no D-Bus address"

        try:
            parsed_args = [parse_arg(a) for a in (args or [])]
        except ValueError as exc:
            return f"D-Bus call failed: {exc}"

        signature: str | None = None
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
                        return (
                            f"D-Bus call failed: {interface}.{method} takes "
                            f"{shown}, got signature '{sent}'"
                        )
            result = obj.get_dbus_method(method, interface)(
                *parsed_args, signature=signature, timeout=_DBUS_CALL_TIMEOUT
            )
        except dbus.DBusException as exc:
            name = exc.get_dbus_name() or type(exc).__name__
            msg = exc.get_dbus_message() or str(exc)
            return f"D-Bus error: {name}: {msg}"
        except (TypeError, ValueError, OverflowError) as exc:
            # dbus-python raises these while validating names or marshalling
            # arguments that do not fit the signature; nothing was sent.
            expected_sig = f" (signature '{signature}')" if signature is not None else ""
            return f"D-Bus call failed: {interface}.{method}{expected_sig}: {exc}"
        return _format_dbus_result(result)

    def read_app_log(self, pid: int, last_n_lines: int = 50) -> str:
        """Read stdout/stderr output of a launched app."""
        session = self._get_session()
        return session.read_app_log(pid, last_n_lines=last_n_lines)

    def wayland_info(self, filter_protocol: str = "") -> str:
        """List Wayland protocols available in the isolated session."""
        env = self._session_env()
        try:
            result = subprocess.run(
                ["wayland-info"],
                env=env,
                capture_output=True,
                timeout=10,
            )
        except FileNotFoundError:
            return _INSTALL_HINTS["wayland-info"]
        if result.returncode != 0:
            return f"wayland-info failed: {result.stderr.decode(errors='replace')}"

        output = result.stdout.decode(errors="replace")
        if filter_protocol:
            lines = [line for line in output.splitlines() if filter_protocol in line]
            if not lines:
                return f"No protocols matching '{filter_protocol}' found."
            return "\n".join(lines)
        return output
