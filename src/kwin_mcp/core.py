"""Core automation engine for KDE Wayland GUI automation.

Contains all tool logic independent of the MCP transport layer.
Can be used directly from the CLI or wrapped by the MCP server.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time

from kwin_mcp.input import InputBackend, MouseButton
from kwin_mcp.screenshot import capture_frame_burst, capture_screenshot_to_file
from kwin_mcp.session import Session, SessionConfig

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


class AutomationEngine:
    """Core automation engine encapsulating all tool logic.

    Manages session lifecycle, input injection, screenshot capture,
    accessibility queries, and clipboard operations.
    """

    def __init__(self) -> None:
        self._session: Session | None = None
        self._input: InputBackend | None = None
        self._clipboard_enabled: bool = False
        self._wl_copy_proc: subprocess.Popen[bytes] | None = None

    # ── Private helpers ───────────────────────────────────────────────────

    def _get_session(self) -> Session:
        if self._session is None or not self._session.is_running:
            msg = "No active session. Call session_start first."
            raise RuntimeError(msg)
        return self._session

    def _get_input(self) -> InputBackend:
        if self._input is None:
            msg = "No input backend. Call session_start first."
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

    def _run_atspi(self, op: str, **kwargs: object) -> dict:
        """Run an AT-SPI2 query in a subprocess with the isolated session's D-Bus address.

        The gi.repository.Atspi library caches the D-Bus connection process-wide,
        so we must run queries in a fresh subprocess that inherits the correct
        DBUS_SESSION_BUS_ADDRESS from the isolated dbus-run-session.

        Retries once on failure to handle transient AT-SPI2 bus instability.
        """
        env = self._session_env()
        payload = json.dumps({"op": op, **kwargs})

        last_error = ""
        for attempt in range(2):
            if attempt > 0:
                time.sleep(0.5)
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "kwin_mcp.accessibility"],
                    input=payload,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except subprocess.TimeoutExpired:
                last_error = f"AT-SPI2 query timed out after 30s (op={op})"
                continue

            if result.returncode != 0:
                last_error = f"AT-SPI2 query failed (exit {result.returncode}): {result.stderr}"
                continue

            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                last_error = f"AT-SPI2 query returned invalid JSON: {result.stdout[:200]}"
                continue

        msg = f"{last_error}. Retried once but still failed — the AT-SPI2 bus may be unstable."
        raise RuntimeError(msg)

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

    def session_stop(self) -> str:
        """Stop the isolated KWin session and clean up."""
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
        self._session.stop()
        self._session = None
        self._input = None
        return "Session stopped."

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
            return "Clipboard not enabled. Pass enable_clipboard=True to session_start."

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
            return "Clipboard not enabled. Pass enable_clipboard=True to session_start."

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
        args: list[str] | None = None,
    ) -> str:
        """Call a D-Bus method in the isolated session using dbus-send."""
        env = self._session_env()
        cmd = [
            "dbus-send",
            "--session",
            "--print-reply",
            f"--dest={service}",
            f"{path}",
            f"{interface}.{method}",
        ]
        if args:
            cmd.extend(args)

        try:
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                timeout=10,
            )
        except FileNotFoundError:
            return _INSTALL_HINTS["dbus-send"]
        if result.returncode != 0:
            return f"D-Bus call failed: {result.stderr.decode(errors='replace')}"
        return result.stdout.decode(errors="replace")

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
