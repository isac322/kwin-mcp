"""Core automation engine for KDE Wayland GUI automation.

Contains all tool logic independent of the MCP transport layer.
Can be used directly from the CLI or wrapped by the MCP server.
"""

from __future__ import annotations

import contextlib
import json
import logging
import multiprocessing
import multiprocessing.pool
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from kwin_mcp.input import InputBackend, MouseButton
from kwin_mcp.screenshot import capture_frame_burst, capture_screenshot_to_file
from kwin_mcp.session import LiveSession, Session, SessionConfig

_atspi_logger = logging.getLogger("kwin_mcp.atspi")

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
    "dbus-send": (
        "dbus-send not found. Install dbus (e.g. 'sudo pacman -S dbus' or 'sudo apt install dbus')."
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
        self._wl_copy_proc: subprocess.Popen[bytes] | None = None
        self._keep_screenshots: bool = False
        self._atspi_pool: multiprocessing.pool.Pool | None = None
        self._atspi_worker_pids: tuple[int, ...] = ()

    def __del__(self) -> None:
        try:
            if getattr(self, "_atspi_pool", None) is not None:
                self._teardown_atspi_pool()
        except Exception:
            pass

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

    def _ensure_atspi_pool(self) -> multiprocessing.pool.Pool:
        """Lazily build the spawn-context Pool that hosts AT-SPI ops.

        The worker module is imported here, NOT at module top, so the parent
        process never loads ``gi.repository.Atspi`` (which caches its D-Bus
        connection process-wide).
        """
        if self._atspi_pool is not None:
            return self._atspi_pool

        from kwin_mcp.accessibility_worker import _init_atspi_worker

        dbus_addr = self._session_env().get("DBUS_SESSION_BUS_ADDRESS", "")
        ctx = multiprocessing.get_context("spawn")
        self._atspi_pool = ctx.Pool(
            processes=1,
            initializer=_init_atspi_worker,
            initargs=(dbus_addr,),
        )
        _atspi_logger.info("atspi pool created (DBUS=%s)", dbus_addr)
        return self._atspi_pool

    @staticmethod
    def _atspi_pool_worker_pids(pool: multiprocessing.pool.Pool) -> tuple[int, ...]:
        return tuple(proc.pid for proc in getattr(pool, "_pool", []) if proc.pid is not None)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def _atspi_pool_has_dead_or_replaced_worker(
        self,
        pool: multiprocessing.pool.Pool,
        recorded_pids: tuple[int, ...],
    ) -> bool:
        if not recorded_pids:
            return False

        current_pids = self._atspi_pool_worker_pids(pool)
        if current_pids != recorded_pids:
            return True

        return any(not self._pid_alive(pid) for pid in recorded_pids)

    def _run_atspi(self, op: str, **kwargs: object) -> dict:
        """Run an AT-SPI op via the spawn-context worker Pool.

        Long-lived worker amortises the ~700 ms PyGObject + Atspi startup;
        warm calls land near the round-trip cost of the bus query itself.

        On TimeoutError or IPC death (the multiprocessing.pool surface for
        dead workers — NOT ``concurrent.futures.process.BrokenProcessPool``),
        tear down the Pool and retry exactly once.
        """
        from kwin_mcp.accessibility_worker import do_atspi_op

        attempts = 0
        deadline = time.monotonic() + 30.0
        poll_quantum = 0.05
        health_check_grace = 2.0
        retry_backoff = 0.05
        call_worker_pids = self._atspi_worker_pids
        while True:
            attempts += 1
            attempt_started = time.monotonic()
            pool = self._ensure_atspi_pool()
            async_result = pool.apply_async(do_atspi_op, kwds={"op": op, **kwargs})
            timeout_count = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _atspi_logger.warning(
                        "atspi call timeout (30s total, op=%s, attempt=%d)",
                        op,
                        attempts,
                    )
                    self._teardown_atspi_pool()
                    if attempts >= 2:
                        raise multiprocessing.TimeoutError() from None
                    break

                wait_seconds = min(poll_quantum, remaining)
                try:
                    result = async_result.get(timeout=wait_seconds)
                except multiprocessing.TimeoutError:
                    timeout_count += 1
                    if timeout_count < 3:
                        continue

                    if not call_worker_pids:
                        continue

                    if time.monotonic() - attempt_started < health_check_grace:
                        continue

                    worker_failed = self._atspi_pool_has_dead_or_replaced_worker(
                        pool,
                        call_worker_pids,
                    )

                    if worker_failed:
                        _atspi_logger.warning(
                            "atspi worker died or was replaced (op=%s, attempt=%d, workers=%s)",
                            op,
                            attempts,
                            call_worker_pids,
                        )
                        self._teardown_atspi_pool()
                        if attempts >= 2:
                            raise multiprocessing.TimeoutError(
                                "AT-SPI worker died or was replaced during call"
                            ) from None
                        break
                    continue
                except (EOFError, BrokenPipeError, ConnectionResetError) as exc:
                    _atspi_logger.warning(
                        "atspi worker IPC death (%s, op=%s, attempt=%d)",
                        type(exc).__name__,
                        op,
                        attempts,
                    )
                    self._teardown_atspi_pool()
                    if attempts >= 2:
                        raise
                    break
                except OSError as exc:
                    if exc.errno in (32, 104):  # EPIPE, ECONNRESET
                        _atspi_logger.warning(
                            "atspi worker IPC death (OSError errno=%d, op=%s, attempt=%d)",
                            exc.errno,
                            op,
                            attempts,
                        )
                        self._teardown_atspi_pool()
                        if attempts >= 2:
                            raise
                        break
                    raise
                else:
                    self._atspi_worker_pids = self._atspi_pool_worker_pids(pool)
                    return result

            if attempts < 2:
                time.sleep(retry_backoff)

    @staticmethod
    def _join_pool_with_timeout(pool: multiprocessing.pool.Pool, timeout: float) -> bool:
        """Wrap ``Pool.join`` (which has no timeout kwarg) in a daemon thread."""
        t = threading.Thread(target=pool.join, daemon=True)
        t.start()
        t.join(timeout=timeout)
        return not t.is_alive()

    @staticmethod
    def _terminate_pool_with_timeout(pool: multiprocessing.pool.Pool, timeout: float) -> bool:
        """Wrap ``Pool.terminate`` in a daemon thread so dead workers cannot hang teardown."""
        t = threading.Thread(target=pool.terminate, daemon=True)
        t.start()
        t.join(timeout=timeout)
        return not t.is_alive()

    def _teardown_atspi_pool(self) -> None:
        """Tear down the AT-SPI Pool with bounded shutdown latency.

        Escalation: ``close → join(5s) → terminate → join(2s) → SIGKILL → join(1s)``.

        ``multiprocessing.pool.Pool.join`` does not accept a timeout, so we
        wrap it in a daemon thread. Worker PIDs are captured BEFORE shutdown
        because ``terminate`` may reap them before SIGKILL escalation.
        """
        pool = self._atspi_pool
        if pool is None:
            return
        worker_pids = [p.pid for p in getattr(pool, "_pool", []) if p.pid is not None]
        try:
            pool.close()
            if self._join_pool_with_timeout(pool, 5.0):
                _atspi_logger.info("atspi pool torn down gracefully")
                return
            _atspi_logger.warning("atspi pool graceful shutdown timed out, terminating")
            if self._terminate_pool_with_timeout(pool, 2.0) and self._join_pool_with_timeout(
                pool, 2.0
            ):
                _atspi_logger.warning("atspi pool terminated after graceful timeout")
                return
            _atspi_logger.error(
                "atspi pool terminate timed out, escalating to SIGKILL pids=%s",
                worker_pids,
            )
            for pid in worker_pids:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            self._join_pool_with_timeout(pool, 1.0)
        finally:
            self._atspi_pool = None
            self._atspi_worker_pids = ()

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
            screenshot_backend=info.screenshot_backend,
        )

        lines = [action_result, f"Captured {len(frames)} frames:"]
        for delay_ms, path in zip(sorted(screenshot_after_ms), frames, strict=True):
            size_kb = path.stat().st_size / 1024
            lines.append(f"  {delay_ms}ms: {path} ({size_kb:.1f} KB)")
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

        if app_command:
            cmd = shlex.split(app_command)
            app_info = self._session.launch_app(cmd, extra_env=env)
            result += f"\nApp launched: {app_command} (PID={app_info.pid})"
            result += f"\nApp log: {app_info.log_path}"

        # Set up input backend via KWin's EIS D-Bus interface
        time.sleep(0.5)
        try:
            self._input = InputBackend(info.dbus_address)
        except RuntimeError:
            self._input = None

        input_status = "Input backend: KWin EIS" if self._input else "No input backend available"
        result += f"\n{input_status}"

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
        except RuntimeError:
            self._input = None
            if shutil.which("ydotool"):
                result += "\nInput backend: ydotool (EIS unavailable)"
            else:
                result += (
                    "\nNo input backend available (EIS connection failed and ydotool not found). "
                    "Screenshot and accessibility tools still work."
                )

        return result

    def session_stop(self) -> str:
        """Stop the current session and clean up."""
        if self._session is None:
            return "No session running."

        # Clean up wl-copy process if active
        if self._wl_copy_proc is not None:
            self._wl_copy_proc.terminate()
            try:
                self._wl_copy_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._wl_copy_proc.kill()
            self._wl_copy_proc = None
        self._clipboard_enabled = False

        if self._input is not None:
            self._input.close()

        # Pool worker holds a D-Bus connection to this session — tear down BEFORE session.stop().
        self._teardown_atspi_pool()

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

        path = capture_screenshot_to_file(
            dbus_address=info.dbus_address,
            wayland_socket=info.wayland_socket,
            include_cursor=include_cursor,
            output_dir=info.screenshot_dir,
            screenshot_backend=info.screenshot_backend,
        )
        size_kb = path.stat().st_size / 1024
        return f"Screenshot saved: {path} ({size_kb:.1f} KB)"

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
            lines.append(
                f'- [{el["role"]}] "{el["name"]}" '
                f"@ ({el['x']}, {el['y']}, {el['width']}x{el['height']}){actions_str}"
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
        if not shutil.which("wtype") and not shutil.which("wl-copy"):
            return (
                "Neither wtype nor wl-copy found. Install at least one: "
                "wtype (e.g. 'sudo pacman -S wtype') or "
                "wl-clipboard (e.g. 'sudo pacman -S wl-clipboard')."
            )
        inp = self._get_input()
        session = self._get_session()
        dbus_addr = session.info.dbus_address if session.info else None
        ok = inp.keyboard_type_unicode(text, dbus_address=dbus_addr)
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

        # Terminate previous wl-copy process (replaced by new content)
        if self._wl_copy_proc is not None:
            self._wl_copy_proc.terminate()
            try:
                self._wl_copy_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._wl_copy_proc.kill()
            self._wl_copy_proc = None

        env = self._session_env()
        try:
            self._wl_copy_proc = subprocess.Popen(
                ["wl-copy", "--", text],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return _INSTALL_HINTS["wl-copy"]
        time.sleep(0.1)  # Wait for fork to complete
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
            lines.append(
                f'- [{el["role"]}] "{el["name"]}" '
                f"@ ({el['x']}, {el['y']}, {el['width']}x{el['height']}){actions_str}"
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

    def focus_window(self, app_name: str) -> str:
        """Attempt to focus a window by application name."""
        self._get_session()
        resp = self._run_atspi("focus_window", app_name=app_name)
        return resp["result"]

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
        may mix in one call. The reply value is rendered via
        :func:`_format_dbus_result` (single primitives become bare strings,
        containers and tuples become JSON).
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

        try:
            bus = dbus.bus.BusConnection(info.dbus_address)
            obj = bus.get_object(service, path)
            iface = dbus.Interface(obj, interface)
            result = iface.get_dbus_method(method)(*parsed_args)
        except dbus.DBusException as exc:
            name = exc.get_dbus_name() or type(exc).__name__
            msg = exc.get_dbus_message() or str(exc)
            return f"D-Bus error: {name}: {msg}"
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
