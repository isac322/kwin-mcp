"""MCP server for KDE Wayland GUI automation."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time

from mcp.server.fastmcp import FastMCP

from kwin_mcp.accessibility import find_elements, get_accessibility_tree
from kwin_mcp.input import InputBackend, MouseButton
from kwin_mcp.screenshot import capture_frame_burst, capture_screenshot_to_file
from kwin_mcp.session import Session, SessionConfig

mcp = FastMCP("kwin-mcp")

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

# Global session state
_session: Session | None = None
_input: InputBackend | None = None
_clipboard_enabled: bool = False
_wl_copy_proc: subprocess.Popen[bytes] | None = None


def _get_session() -> Session:
    if _session is None or not _session.is_running:
        msg = "No active session. Call session_start first."
        raise RuntimeError(msg)
    return _session


def _get_input() -> InputBackend:
    if _input is None:
        msg = "No input backend. Call session_start first."
        raise RuntimeError(msg)
    return _input


def _session_env() -> dict[str, str]:
    """Build environment dict for tools that need the isolated session."""
    session = _get_session()
    env = {**os.environ}
    info = session.info
    if info:
        if info.dbus_address:
            env["DBUS_SESSION_BUS_ADDRESS"] = info.dbus_address
        env["WAYLAND_DISPLAY"] = info.wayland_socket
    env["QT_QPA_PLATFORM"] = "wayland"
    env.pop("DISPLAY", None)
    return env


def _with_frame_capture(
    action_result: str,
    screenshot_after_ms: list[int] | None,
) -> str:
    """Append frame captures to an action result if requested.

    Captures screenshots at specified delays (in ms) after the action
    using the fast D-Bus capture method (~15-30ms per frame).
    """
    if not screenshot_after_ms:
        return action_result

    session = _get_session()
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


# ── Session management ──────────────────────────────────────────────────


@mcp.tool()
def session_start(
    app_command: str = "",
    screen_width: int = 1920,
    screen_height: int = 1080,
    enable_clipboard: bool = False,
    env: dict[str, str] | None = None,
) -> str:
    """Start an isolated KWin Wayland session, optionally launching an app.

    Args:
        app_command: Command to launch (e.g., "kcalc" or "/path/to/app --arg").
                    Leave empty to start session without an app.
        screen_width: Virtual screen width in pixels.
        screen_height: Virtual screen height in pixels.
        enable_clipboard: Enable clipboard tools (wl-copy/wl-paste). Disabled by default
                         because wl-copy can hang in isolated sessions.
        env: Extra environment variables to pass to the launched app.

    Returns:
        Session status information.
    """
    global _session, _input, _clipboard_enabled

    if _session is not None and _session.is_running:
        return "Session already running. Call session_stop first."

    _clipboard_enabled = enable_clipboard

    _session = Session()
    config = SessionConfig(
        screen_width=screen_width,
        screen_height=screen_height,
        enable_clipboard=enable_clipboard,
    )
    info = _session.start(config)

    result = f"Session started. Wayland socket: {info.wayland_socket}"

    if app_command:
        cmd = shlex.split(app_command)
        app_info = _session.launch_app(cmd, extra_env=env)
        result += f"\nApp launched: {app_command} (PID={app_info.pid})"
        result += f"\nApp log: {app_info.log_path}"

    # Set up input backend via KWin's EIS D-Bus interface
    time.sleep(0.5)
    try:
        _input = InputBackend(info.dbus_address)
    except RuntimeError:
        _input = None

    input_status = "Input backend: KWin EIS" if _input else "No input backend available"
    result += f"\n{input_status}"

    return result


@mcp.tool()
def session_stop() -> str:
    """Stop the isolated KWin session and clean up."""
    global _session, _input, _wl_copy_proc, _clipboard_enabled

    if _session is None:
        return "No session running."

    # Clean up wl-copy process if active
    if _wl_copy_proc is not None:
        _wl_copy_proc.terminate()
        try:
            _wl_copy_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _wl_copy_proc.kill()
        _wl_copy_proc = None
    _clipboard_enabled = False

    if _input is not None:
        _input.close()
    _session.stop()
    _session = None
    _input = None
    return "Session stopped."


# ── Screenshot / Accessibility ───────────────────────────────────────────


@mcp.tool()
def screenshot(include_cursor: bool = False) -> str:
    """Capture a screenshot of the isolated session.

    Returns the file path of the saved PNG image.
    """
    session = _get_session()
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


@mcp.tool()
def accessibility_tree(app_name: str = "", max_depth: int = 15) -> str:
    """Get the accessibility tree of apps in the isolated session.

    Args:
        app_name: Filter to a specific app (empty = all apps).
        max_depth: Maximum tree traversal depth.

    Returns:
        Formatted text of the widget tree with roles, names, states, and coordinates.
    """
    _get_session()  # Ensure session is running
    return get_accessibility_tree(app_name=app_name, max_depth=max_depth)


@mcp.tool()
def find_ui_elements(query: str, app_name: str = "") -> str:
    """Find UI elements matching a search query.

    Args:
        query: Search text (case-insensitive, matches names/roles/descriptions).
        app_name: Filter to a specific app.

    Returns:
        List of matching elements with positions and actions.
    """
    _get_session()
    elements = find_elements(query, app_name=app_name)
    if not elements:
        return f"No elements found matching '{query}'"

    lines = [f"Found {len(elements)} elements matching '{query}':\n"]
    for el in elements:
        actions_str = f" [actions: {', '.join(el.actions)}]" if el.actions else ""
        lines.append(
            f'- [{el.role}] "{el.name}" @ ({el.x}, {el.y}, {el.width}x{el.height}){actions_str}'
        )
    return "\n".join(lines)


# ── Mouse tools ──────────────────────────────────────────────────────────


@mcp.tool()
def mouse_click(
    x: int,
    y: int,
    button: str = "left",
    double: bool = False,
    triple: bool = False,
    modifiers: list[str] | None = None,
    hold_ms: int = 0,
    screenshot_after_ms: list[int] | None = None,
) -> str:
    """Click at coordinates in the isolated session.

    Args:
        x: X coordinate.
        y: Y coordinate.
        button: "left", "right", or "middle".
        double: If True, double-click.
        triple: If True, triple-click (overrides double).
        modifiers: Modifier keys to hold during click (e.g. ["ctrl"], ["shift", "alt"]).
        hold_ms: Duration to hold button pressed before release (ms, for long-press).
        screenshot_after_ms: If provided, capture screenshots at these delays
            (in milliseconds) after the click. Example: [0, 50, 100, 200, 500]
            captures 5 frames showing the click animation over 500ms.
    """
    inp = _get_input()
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

    return _with_frame_capture(desc, screenshot_after_ms)


@mcp.tool()
def mouse_move(
    x: int,
    y: int,
    screenshot_after_ms: list[int] | None = None,
) -> str:
    """Move the mouse (hover) to coordinates without clicking.

    Args:
        x: X coordinate.
        y: Y coordinate.
        screenshot_after_ms: If provided, capture screenshots at these delays
            (in milliseconds) after moving. Useful for observing hover effects
            and tooltip animations.
    """
    inp = _get_input()
    inp.mouse_move(x, y)
    result = f"Mouse moved to ({x}, {y})"
    return _with_frame_capture(result, screenshot_after_ms)


@mcp.tool()
def mouse_scroll(
    x: int,
    y: int,
    delta: int,
    horizontal: bool = False,
    discrete: bool = False,
    steps: int = 1,
) -> str:
    """Scroll at coordinates.

    Args:
        x: X coordinate.
        y: Y coordinate.
        delta: Scroll amount (positive = down/right, negative = up/left).
        horizontal: If True, scroll horizontally.
        discrete: If True, use discrete scroll (wheel ticks) instead of smooth pixels.
        steps: Split total delta into this many increments (for smooth animation).
    """
    inp = _get_input()
    inp.mouse_scroll(x, y, delta, horizontal=horizontal, discrete=discrete, steps=steps)
    direction = "horizontal" if horizontal else "vertical"
    mode = "discrete" if discrete else "smooth"
    desc = f"Scrolled {direction} ({mode}) by {delta} at ({x}, {y})"
    if steps > 1:
        desc += f" in {steps} steps"
    return desc


@mcp.tool()
def mouse_drag(
    from_x: int,
    from_y: int,
    to_x: int,
    to_y: int,
    button: str = "left",
    modifiers: list[str] | None = None,
    waypoints: list[list[int]] | None = None,
    screenshot_after_ms: list[int] | None = None,
) -> str:
    """Drag from one point to another.

    Args:
        from_x, from_y: Starting coordinates.
        to_x, to_y: Ending coordinates.
        button: Mouse button to use ("left", "right", "middle").
        modifiers: Modifier keys to hold during drag (e.g. ["alt"], ["ctrl"]).
        waypoints: Intermediate points as [[x, y, dwell_ms], ...].
        screenshot_after_ms: If provided, capture screenshots at these delays
            (in milliseconds) after the drag completes. Useful for observing
            drop animations and visual feedback.
    """
    inp = _get_input()
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
    return _with_frame_capture(desc, screenshot_after_ms)


@mcp.tool()
def mouse_button_down(x: int, y: int, button: str = "left") -> str:
    """Press a mouse button at coordinates without releasing.

    Args:
        x: X coordinate.
        y: Y coordinate.
        button: "left", "right", or "middle".
    """
    inp = _get_input()
    inp.mouse_button_down(x, y, MouseButton(button))
    return f"Button {button} pressed at ({x}, {y})"


@mcp.tool()
def mouse_button_up(x: int, y: int, button: str = "left") -> str:
    """Release a mouse button at coordinates.

    Args:
        x: X coordinate.
        y: Y coordinate.
        button: "left", "right", or "middle".
    """
    inp = _get_input()
    inp.mouse_button_up(x, y, MouseButton(button))
    return f"Button {button} released at ({x}, {y})"


# ── Keyboard tools ───────────────────────────────────────────────────────


@mcp.tool()
def keyboard_type(
    text: str,
    screenshot_after_ms: list[int] | None = None,
) -> str:
    """Type text in the focused element.

    Args:
        text: Text to type.
        screenshot_after_ms: If provided, capture screenshots at these delays
            (in milliseconds) after typing. Useful for observing autocomplete
            popups and input validation feedback.
    """
    inp = _get_input()
    inp.keyboard_type(text)
    result = f"Typed: {text!r}"
    return _with_frame_capture(result, screenshot_after_ms)


@mcp.tool()
def keyboard_type_unicode(
    text: str,
    screenshot_after_ms: list[int] | None = None,
) -> str:
    """Type arbitrary Unicode text (non-ASCII, e.g. Korean, CJK).

    Uses wtype or clipboard fallback (wl-copy + Ctrl+V).

    Args:
        text: Text to type.
        screenshot_after_ms: If provided, capture screenshots at these delays
            (in milliseconds) after typing.
    """
    if not shutil.which("wtype") and not shutil.which("wl-copy"):
        return (
            "Neither wtype nor wl-copy found. Install at least one: "
            "wtype (e.g. 'sudo pacman -S wtype') or "
            "wl-clipboard (e.g. 'sudo pacman -S wl-clipboard')."
        )
    inp = _get_input()
    session = _get_session()
    dbus_addr = session.info.dbus_address if session.info else None
    ok = inp.keyboard_type_unicode(text, dbus_address=dbus_addr)
    result = f"Typed unicode: {text!r}" if ok else f"Failed to type unicode: {text!r}"
    return _with_frame_capture(result, screenshot_after_ms)


@mcp.tool()
def keyboard_key(
    key: str,
    screenshot_after_ms: list[int] | None = None,
) -> str:
    """Press a key combination.

    Args:
        key: Key to press (e.g., "Return", "ctrl+c", "alt+F4", "Tab").
        screenshot_after_ms: If provided, capture screenshots at these delays
            (in milliseconds) after the key press. Useful for observing menu
            openings, dialog transitions, and keyboard-triggered animations.
    """
    inp = _get_input()
    inp.keyboard_key(key)
    result = f"Pressed: {key}"
    return _with_frame_capture(result, screenshot_after_ms)


@mcp.tool()
def keyboard_key_down(key: str) -> str:
    """Press and hold a key combination without releasing.

    Useful for holding modifier keys across multiple actions
    (e.g., hold Ctrl while clicking multiple items).

    Args:
        key: Key to press (e.g., "ctrl", "shift+a", "alt").
    """
    inp = _get_input()
    inp.keyboard_key_down(key)
    return f"Key down: {key}"


@mcp.tool()
def keyboard_key_up(key: str) -> str:
    """Release a previously pressed key combination.

    Args:
        key: Key to release (e.g., "ctrl", "shift+a", "alt").
    """
    inp = _get_input()
    inp.keyboard_key_up(key)
    return f"Key up: {key}"


# ── Touch tools ──────────────────────────────────────────────────────────


@mcp.tool()
def touch_tap(
    x: int,
    y: int,
    hold_ms: int = 0,
    screenshot_after_ms: list[int] | None = None,
) -> str:
    """Tap at coordinates (touch input).

    Args:
        x: X coordinate.
        y: Y coordinate.
        hold_ms: Duration to hold before lifting (ms, for long-press).
        screenshot_after_ms: If provided, capture screenshots at these delays.
    """
    inp = _get_input()
    inp.touch_tap(x, y, hold_ms=hold_ms)
    desc = f"Touch tap at ({x}, {y})"
    if hold_ms > 0:
        desc += f" held {hold_ms}ms"
    return _with_frame_capture(desc, screenshot_after_ms)


@mcp.tool()
def touch_swipe(
    from_x: int,
    from_y: int,
    to_x: int,
    to_y: int,
    duration_ms: int = 300,
    screenshot_after_ms: list[int] | None = None,
) -> str:
    """Swipe from one point to another (touch input).

    Args:
        from_x, from_y: Starting coordinates.
        to_x, to_y: Ending coordinates.
        duration_ms: Duration of the swipe in milliseconds.
        screenshot_after_ms: If provided, capture screenshots at these delays.
    """
    inp = _get_input()
    inp.touch_swipe(from_x, from_y, to_x, to_y, duration_ms=duration_ms)
    desc = f"Touch swipe from ({from_x}, {from_y}) to ({to_x}, {to_y}) in {duration_ms}ms"
    return _with_frame_capture(desc, screenshot_after_ms)


@mcp.tool()
def touch_pinch(
    center_x: int,
    center_y: int,
    start_distance: int,
    end_distance: int,
    duration_ms: int = 500,
    screenshot_after_ms: list[int] | None = None,
) -> str:
    """Pinch gesture with two fingers.

    Args:
        center_x, center_y: Center point of the pinch.
        start_distance: Initial distance between fingers (pixels).
        end_distance: Final distance between fingers (pixels).
        duration_ms: Duration of the gesture.
        screenshot_after_ms: If provided, capture screenshots at these delays.
    """
    inp = _get_input()
    inp.touch_pinch(center_x, center_y, start_distance, end_distance, duration_ms=duration_ms)
    direction = "in" if end_distance < start_distance else "out"
    desc = f"Pinch {direction} at ({center_x}, {center_y}): {start_distance}→{end_distance}px"
    return _with_frame_capture(desc, screenshot_after_ms)


@mcp.tool()
def touch_multi_swipe(
    from_x: int,
    from_y: int,
    to_x: int,
    to_y: int,
    fingers: int = 3,
    duration_ms: int = 300,
    screenshot_after_ms: list[int] | None = None,
) -> str:
    """Multi-finger swipe gesture.

    Args:
        from_x, from_y: Starting coordinates (center of finger group).
        to_x, to_y: Ending coordinates.
        fingers: Number of fingers (2-5).
        duration_ms: Duration of the swipe.
        screenshot_after_ms: If provided, capture screenshots at these delays.
    """
    inp = _get_input()
    inp.touch_multi_swipe(from_x, from_y, to_x, to_y, fingers=fingers, duration_ms=duration_ms)
    desc = (
        f"{fingers}-finger swipe from ({from_x}, {from_y}) to ({to_x}, {to_y}) in {duration_ms}ms"
    )
    return _with_frame_capture(desc, screenshot_after_ms)


# ── Clipboard tools ──────────────────────────────────────────────────────


@mcp.tool()
def clipboard_get() -> str:
    """Read the current clipboard content in the isolated session.

    Returns:
        The clipboard text content.
    """
    if not _clipboard_enabled:
        return "Clipboard not enabled. Pass enable_clipboard=True to session_start."

    env = _session_env()
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


@mcp.tool()
def clipboard_set(text: str) -> str:
    """Set the clipboard content in the isolated session.

    Args:
        text: Text to copy to clipboard.
    """
    global _wl_copy_proc

    if not _clipboard_enabled:
        return "Clipboard not enabled. Pass enable_clipboard=True to session_start."

    # Terminate previous wl-copy process (replaced by new content)
    if _wl_copy_proc is not None:
        _wl_copy_proc.terminate()
        try:
            _wl_copy_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _wl_copy_proc.kill()
        _wl_copy_proc = None

    env = _session_env()
    # wl-copy forks: parent exits immediately, child serves clipboard.
    # Using Popen + DEVNULL avoids the pipe-blocking issue that
    # subprocess.run(capture_output=True) causes with forked children.
    try:
        _wl_copy_proc = subprocess.Popen(
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


# ── Wait-for-UI tools ───────────────────────────────────────────────────


@mcp.tool()
def wait_for_element(
    query: str,
    app_name: str = "",
    timeout_ms: int = 5000,
    poll_interval_ms: int = 200,
) -> str:
    """Wait for a UI element to appear.

    Polls the accessibility tree until an element matching the query is found
    or the timeout expires.

    Args:
        query: Search text (case-insensitive, matches names/roles/descriptions).
        app_name: Filter to a specific app.
        timeout_ms: Maximum wait time in milliseconds.
        poll_interval_ms: Polling interval in milliseconds.

    Returns:
        Matching elements (same format as find_ui_elements) or error on timeout.
    """
    _get_session()
    deadline = time.monotonic() + timeout_ms / 1000.0
    interval = poll_interval_ms / 1000.0

    while True:
        elements = find_elements(query, app_name=app_name)
        if elements:
            lines = [f"Found {len(elements)} elements matching '{query}':\n"]
            for el in elements:
                actions_str = f" [actions: {', '.join(el.actions)}]" if el.actions else ""
                lines.append(
                    f'- [{el.role}] "{el.name}" @ '
                    f"({el.x}, {el.y}, {el.width}x{el.height}){actions_str}"
                )
            return "\n".join(lines)

        if time.monotonic() >= deadline:
            return f"Timeout after {timeout_ms}ms: no elements matching '{query}'"

        time.sleep(interval)


# ── Window management tools ──────────────────────────────────────────────


@mcp.tool()
def launch_app(command: str, env: dict[str, str] | None = None) -> str:
    """Launch an application inside the running isolated session.

    Args:
        command: Command to launch (e.g., "kcalc" or "/path/to/app --arg").
        env: Extra environment variables to pass to the app.

    Returns:
        Launch status with PID.
    """
    session = _get_session()
    cmd = shlex.split(command)
    app_info = session.launch_app(cmd, extra_env=env)
    return f"App launched: {command} (PID={app_info.pid})\nApp log: {app_info.log_path}"


@mcp.tool()
def list_windows() -> str:
    """List windows in the isolated session via AT-SPI2.

    Returns:
        List of top-level application windows.
    """
    _get_session()
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    desktop = Atspi.get_desktop(0)
    lines: list[str] = []
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        name = app.get_name() or "(unnamed)"
        child_count = app.get_child_count()
        lines.append(f"- {name} ({child_count} windows)")
    if not lines:
        return "(no accessible applications found)"
    return f"Applications ({len(lines)}):\n" + "\n".join(lines)


@mcp.tool()
def focus_window(app_name: str) -> str:
    """Attempt to focus a window by application name.

    Uses AT-SPI2 to find the window and activate it.

    Args:
        app_name: Application name to focus.
    """
    _get_session()
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        name = app.get_name() or ""
        if app_name.lower() in name.lower():
            # Try to find a focusable window child
            for j in range(app.get_child_count()):
                win = app.get_child_at_index(j)
                if win is None:
                    continue
                try:
                    component = win.get_component_iface()
                    if component is not None:
                        component.grab_focus()
                        return f"Focused: {name}"
                except Exception:
                    continue
            return f"Found '{name}' but could not focus it"
    return f"No application matching '{app_name}' found"


# ── D-Bus tools ──────────────────────────────────────────────────────────


@mcp.tool()
def dbus_call(
    service: str,
    path: str,
    interface: str,
    method: str,
    args: list[str] | None = None,
) -> str:
    """Call a D-Bus method in the isolated session.

    Args:
        service: D-Bus service name (e.g., "org.kde.KWin").
        path: Object path (e.g., "/org/kde/KWin").
        interface: Interface name (e.g., "org.kde.KWin.Scripting").
        method: Method name.
        args: Method arguments as strings.

    Returns:
        Method return value as string.
    """
    env = _session_env()
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


@mcp.tool()
def read_app_log(pid: int, last_n_lines: int = 50) -> str:
    """Read stdout/stderr output of a launched app.

    Args:
        pid: PID of the app (returned by launch_app or session_start).
        last_n_lines: Number of trailing lines to return (0 = all).

    Returns:
        The app's captured stdout/stderr output.
    """
    session = _get_session()
    return session.read_app_log(pid, last_n_lines=last_n_lines)


@mcp.tool()
def wayland_info(filter_protocol: str = "") -> str:
    """List Wayland protocols available in the isolated session.

    Runs wayland-info inside the session to enumerate all exposed globals.
    Useful for verifying that restricted protocols (e.g. plasma_window_management)
    are accessible.

    Args:
        filter_protocol: If provided, only show lines matching this substring.

    Returns:
        wayland-info output (optionally filtered).
    """
    env = _session_env()
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


def main() -> None:
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
