"""MCP server for KDE Wayland GUI automation."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

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
    app_command: Annotated[
        str,
        Field(
            description='Command to launch (e.g. "kcalc" or "/path/to/app --arg"). '
            "Leave empty to start session without an app."
        ),
    ] = "",
    screen_width: Annotated[int, Field(description="Virtual screen width in pixels.")] = 1920,
    screen_height: Annotated[int, Field(description="Virtual screen height in pixels.")] = 1080,
    enable_clipboard: Annotated[
        bool,
        Field(
            description="Enable clipboard tools (wl-copy/wl-paste). Disabled by default "
            "because wl-copy can hang in isolated sessions."
        ),
    ] = False,
    env: Annotated[
        dict[str, str] | None,
        Field(description="Extra environment variables to pass to the launched app."),
    ] = None,
) -> str:
    """Start an isolated KWin Wayland session, optionally launching an app.

    This must be called before any other tool. If a session is already running,
    call session_stop first. Returns session status including the Wayland socket
    path, launched app PID (if any), and input backend availability.
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
    """Stop the isolated KWin session and clean up.

    Terminates KWin, all launched app processes, and the D-Bus session.
    Cleans up temporary files and clipboard processes. Safe to call when
    no session is running (returns "No session running.").
    """
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
def screenshot(
    include_cursor: Annotated[
        bool,
        Field(description="If true, render the mouse cursor in the screenshot."),
    ] = False,
) -> str:
    """Capture a screenshot of the isolated session.

    Requires an active session. Returns the file path to the saved PNG image
    and its size in KB.
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
def accessibility_tree(
    app_name: Annotated[
        str,
        Field(description="Filter to a specific app name (empty string = all apps)."),
    ] = "",
    max_depth: Annotated[int, Field(description="Maximum tree traversal depth.")] = 15,
) -> str:
    """Get the accessibility tree of apps in the isolated session.

    Returns a formatted text tree with each widget's role, name, states,
    and bounding box coordinates. Use this to understand UI structure before
    interacting with elements.
    """
    _get_session()  # Ensure session is running
    return get_accessibility_tree(app_name=app_name, max_depth=max_depth)


@mcp.tool()
def find_ui_elements(
    query: Annotated[
        str,
        Field(description="Search text (case-insensitive, matches names/roles/descriptions)."),
    ],
    app_name: Annotated[
        str,
        Field(description="Filter to a specific app name (empty string = all apps)."),
    ] = "",
) -> str:
    """Find UI elements matching a search query.

    Returns a list of matching elements with their role, name, bounding box
    (x, y, width, height), and available actions. Use this to locate specific
    buttons, inputs, or labels before clicking or interacting.
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
    x: Annotated[
        int,
        Field(description="X coordinate in pixels (0 = left edge of virtual screen)."),
    ],
    y: Annotated[
        int,
        Field(description="Y coordinate in pixels (0 = top edge of virtual screen)."),
    ],
    button: Annotated[
        str, Field(description='Mouse button: "left", "right", or "middle".')
    ] = "left",
    double: Annotated[bool, Field(description="If true, double-click.")] = False,
    triple: Annotated[bool, Field(description="If true, triple-click (overrides double).")] = False,
    modifiers: Annotated[
        list[str] | None,
        Field(description='Modifier keys to hold during click (e.g. ["ctrl"], ["shift", "alt"]).'),
    ] = None,
    hold_ms: Annotated[
        int,
        Field(description="Duration to hold button pressed before release (ms, for long-press)."),
    ] = 0,
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(
            description="Capture screenshots at these delays (ms) after the click. "
            "Example: [0, 50, 200] captures 3 frames showing the click effect."
        ),
    ] = None,
) -> str:
    """Click at coordinates in the isolated session.

    Coordinates use the virtual screen pixel grid where (0, 0) is the top-left
    corner. Returns a description of the click performed. Optionally captures
    screenshot frames after the click for visual feedback.
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
    x: Annotated[int, Field(description="X coordinate in pixels.")],
    y: Annotated[int, Field(description="Y coordinate in pixels.")],
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(
            description="Capture screenshots at these delays (ms) after moving. "
            "Useful for observing hover effects and tooltip animations."
        ),
    ] = None,
) -> str:
    """Move the mouse cursor to coordinates without clicking.

    Use this to trigger hover effects, reveal tooltips, or position the
    cursor before a separate button-down action.
    """
    inp = _get_input()
    inp.mouse_move(x, y)
    result = f"Mouse moved to ({x}, {y})"
    return _with_frame_capture(result, screenshot_after_ms)


@mcp.tool()
def mouse_scroll(
    x: Annotated[int, Field(description="X coordinate in pixels.")],
    y: Annotated[int, Field(description="Y coordinate in pixels.")],
    delta: Annotated[
        int,
        Field(
            description="Scroll amount (positive = down/right, negative = up/left). "
            "Typical values: 1-5 for discrete wheel ticks, 50-200 for smooth pixel scrolling."
        ),
    ],
    horizontal: Annotated[
        bool, Field(description="If true, scroll horizontally instead of vertically.")
    ] = False,
    discrete: Annotated[
        bool,
        Field(
            description="If true, use discrete scroll (wheel ticks) instead of smooth pixels. "
            "Most desktop apps expect discrete scrolling."
        ),
    ] = False,
    steps: Annotated[
        int,
        Field(
            description="Split total delta into this many increments for smooth animation. "
            "Only useful with discrete=false."
        ),
    ] = 1,
) -> str:
    """Scroll at coordinates in the isolated session.

    Moves the cursor to (x, y) and performs a scroll action. Returns a
    description of the scroll performed.
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
    from_x: Annotated[int, Field(description="Starting X coordinate in pixels.")],
    from_y: Annotated[int, Field(description="Starting Y coordinate in pixels.")],
    to_x: Annotated[int, Field(description="Ending X coordinate in pixels.")],
    to_y: Annotated[int, Field(description="Ending Y coordinate in pixels.")],
    button: Annotated[
        str, Field(description='Mouse button: "left", "right", or "middle".')
    ] = "left",
    modifiers: Annotated[
        list[str] | None,
        Field(description='Modifier keys to hold during drag (e.g. ["alt"], ["ctrl"]).'),
    ] = None,
    waypoints: Annotated[
        list[list[int]] | None,
        Field(
            description="Intermediate points as [[x, y, dwell_ms], ...]. "
            "The cursor pauses at each waypoint for dwell_ms milliseconds."
        ),
    ] = None,
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(description="Capture screenshots at these delays (ms) after the drag completes."),
    ] = None,
) -> str:
    """Drag from one point to another in the isolated session.

    Presses the mouse button at (from_x, from_y), moves to (to_x, to_y)
    optionally through waypoints, then releases. Returns a description of
    the drag performed.
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
def mouse_button_down(
    x: Annotated[int, Field(description="X coordinate in pixels.")],
    y: Annotated[int, Field(description="Y coordinate in pixels.")],
    button: Annotated[
        str, Field(description='Mouse button: "left", "right", or "middle".')
    ] = "left",
) -> str:
    """Press a mouse button at coordinates without releasing.

    Use with mouse_button_up to perform custom drag sequences or
    hold-and-interact patterns. The button stays pressed until
    mouse_button_up is called.
    """
    inp = _get_input()
    inp.mouse_button_down(x, y, MouseButton(button))
    return f"Button {button} pressed at ({x}, {y})"


@mcp.tool()
def mouse_button_up(
    x: Annotated[int, Field(description="X coordinate in pixels.")],
    y: Annotated[int, Field(description="Y coordinate in pixels.")],
    button: Annotated[
        str, Field(description='Mouse button: "left", "right", or "middle".')
    ] = "left",
) -> str:
    """Release a mouse button at coordinates.

    Pair with mouse_button_down. The release happens at the specified
    coordinates, which may differ from where the button was pressed.
    """
    inp = _get_input()
    inp.mouse_button_up(x, y, MouseButton(button))
    return f"Button {button} released at ({x}, {y})"


# ── Keyboard tools ───────────────────────────────────────────────────────


@mcp.tool()
def keyboard_type(
    text: Annotated[
        str,
        Field(
            description="ASCII text to type. For non-ASCII (Korean, CJK, emoji), "
            "use keyboard_type_unicode instead."
        ),
    ],
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(
            description="Capture screenshots at these delays (ms) after typing. "
            "Useful for observing autocomplete popups and input validation."
        ),
    ] = None,
) -> str:
    """Type ASCII text into the currently focused element.

    Simulates individual key presses for each character. Only supports ASCII
    characters. For non-ASCII text (Korean, CJK, emoji, accented characters),
    use keyboard_type_unicode instead.
    """
    inp = _get_input()
    inp.keyboard_type(text)
    result = f"Typed: {text!r}"
    return _with_frame_capture(result, screenshot_after_ms)


@mcp.tool()
def keyboard_type_unicode(
    text: Annotated[
        str,
        Field(description="Unicode text to type (supports any script: Korean, CJK, emoji, etc.)."),
    ],
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(description="Capture screenshots at these delays (ms) after typing."),
    ] = None,
) -> str:
    """Type arbitrary Unicode text including non-ASCII characters.

    Uses wtype if available, otherwise falls back to clipboard injection
    (wl-copy + Ctrl+V). Requires wtype or wl-clipboard to be installed.
    Use this instead of keyboard_type when the text contains non-ASCII
    characters (e.g. Korean, CJK, emoji, accented characters).
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
    key: Annotated[
        str,
        Field(
            description='Key or combination to press (e.g. "Return", "ctrl+c", '
            '"alt+F4", "Tab", "shift+ctrl+z").'
        ),
    ],
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(
            description="Capture screenshots at these delays (ms) after the key press. "
            "Useful for observing menu openings and dialog transitions."
        ),
    ] = None,
) -> str:
    """Press and release a key or key combination.

    Supports single keys and modifier combinations joined with "+".
    Returns a confirmation of the key pressed.
    """
    inp = _get_input()
    inp.keyboard_key(key)
    result = f"Pressed: {key}"
    return _with_frame_capture(result, screenshot_after_ms)


@mcp.tool()
def keyboard_key_down(
    key: Annotated[
        str,
        Field(description='Key to press and hold (e.g. "ctrl", "shift", "alt").'),
    ],
) -> str:
    """Press and hold a key without releasing.

    Use with keyboard_key_up to hold modifier keys across multiple actions
    (e.g. hold Ctrl while clicking multiple items). The key stays pressed
    until keyboard_key_up is called with the same key.
    """
    inp = _get_input()
    inp.keyboard_key_down(key)
    return f"Key down: {key}"


@mcp.tool()
def keyboard_key_up(
    key: Annotated[
        str,
        Field(description='Key to release (e.g. "ctrl", "shift", "alt").'),
    ],
) -> str:
    """Release a previously held key.

    Pair with keyboard_key_down. Must be called to release keys that
    were pressed with keyboard_key_down.
    """
    inp = _get_input()
    inp.keyboard_key_up(key)
    return f"Key up: {key}"


# ── Touch tools ──────────────────────────────────────────────────────────


@mcp.tool()
def touch_tap(
    x: Annotated[int, Field(description="X coordinate in pixels.")],
    y: Annotated[int, Field(description="Y coordinate in pixels.")],
    hold_ms: Annotated[
        int,
        Field(
            description="Duration to hold before lifting (ms). Use >500 for long-press gestures."
        ),
    ] = 0,
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(description="Capture screenshots at these delays (ms) after the tap."),
    ] = None,
) -> str:
    """Tap at coordinates using touch input.

    Simulates a single finger touch-and-release. Set hold_ms > 0 for
    long-press gestures. Returns a description of the tap performed.
    """
    inp = _get_input()
    inp.touch_tap(x, y, hold_ms=hold_ms)
    desc = f"Touch tap at ({x}, {y})"
    if hold_ms > 0:
        desc += f" held {hold_ms}ms"
    return _with_frame_capture(desc, screenshot_after_ms)


@mcp.tool()
def touch_swipe(
    from_x: Annotated[int, Field(description="Starting X coordinate in pixels.")],
    from_y: Annotated[int, Field(description="Starting Y coordinate in pixels.")],
    to_x: Annotated[int, Field(description="Ending X coordinate in pixels.")],
    to_y: Annotated[int, Field(description="Ending Y coordinate in pixels.")],
    duration_ms: Annotated[int, Field(description="Duration of the swipe in milliseconds.")] = 300,
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(description="Capture screenshots at these delays (ms) after the swipe."),
    ] = None,
) -> str:
    """Swipe from one point to another using single-finger touch input.

    Returns a description of the swipe performed.
    """
    inp = _get_input()
    inp.touch_swipe(from_x, from_y, to_x, to_y, duration_ms=duration_ms)
    desc = f"Touch swipe from ({from_x}, {from_y}) to ({to_x}, {to_y}) in {duration_ms}ms"
    return _with_frame_capture(desc, screenshot_after_ms)


@mcp.tool()
def touch_pinch(
    center_x: Annotated[int, Field(description="Center X coordinate of the pinch gesture.")],
    center_y: Annotated[int, Field(description="Center Y coordinate of the pinch gesture.")],
    start_distance: Annotated[
        int, Field(description="Initial distance between two fingers in pixels.")
    ],
    end_distance: Annotated[
        int,
        Field(
            description="Final distance between two fingers in pixels. "
            "Smaller than start_distance = pinch in (zoom out), "
            "larger = pinch out (zoom in)."
        ),
    ],
    duration_ms: Annotated[
        int, Field(description="Duration of the gesture in milliseconds.")
    ] = 500,
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(description="Capture screenshots at these delays (ms) after the pinch."),
    ] = None,
) -> str:
    """Perform a two-finger pinch gesture.

    Simulates two fingers moving symmetrically toward or away from the
    center point. Returns a description of the pinch performed.
    """
    inp = _get_input()
    inp.touch_pinch(center_x, center_y, start_distance, end_distance, duration_ms=duration_ms)
    direction = "in" if end_distance < start_distance else "out"
    desc = f"Pinch {direction} at ({center_x}, {center_y}): {start_distance}→{end_distance}px"
    return _with_frame_capture(desc, screenshot_after_ms)


@mcp.tool()
def touch_multi_swipe(
    from_x: Annotated[
        int, Field(description="Starting X coordinate (center of finger group) in pixels.")
    ],
    from_y: Annotated[
        int, Field(description="Starting Y coordinate (center of finger group) in pixels.")
    ],
    to_x: Annotated[int, Field(description="Ending X coordinate in pixels.")],
    to_y: Annotated[int, Field(description="Ending Y coordinate in pixels.")],
    fingers: Annotated[int, Field(description="Number of fingers (2-5).")] = 3,
    duration_ms: Annotated[int, Field(description="Duration of the swipe in milliseconds.")] = 300,
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(description="Capture screenshots at these delays (ms) after the swipe."),
    ] = None,
) -> str:
    """Perform a multi-finger swipe gesture.

    All fingers move in parallel from the start to end coordinates.
    Returns a description of the swipe performed.
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

    Requires enable_clipboard=true in session_start and wl-clipboard
    installed. Returns the clipboard text or an error message if clipboard
    is not enabled or empty.
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
def clipboard_set(
    text: Annotated[str, Field(description="Text to copy to clipboard.")],
) -> str:
    """Set the clipboard content in the isolated session.

    Requires enable_clipboard=true in session_start and wl-clipboard
    installed. The content remains available until replaced by another
    clipboard_set call or the session ends.
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
    query: Annotated[
        str,
        Field(description="Search text (case-insensitive, matches names/roles/descriptions)."),
    ],
    app_name: Annotated[
        str,
        Field(description="Filter to a specific app name (empty string = all apps)."),
    ] = "",
    timeout_ms: Annotated[int, Field(description="Maximum wait time in milliseconds.")] = 5000,
    poll_interval_ms: Annotated[int, Field(description="Polling interval in milliseconds.")] = 200,
) -> str:
    """Wait for a UI element to appear in the accessibility tree.

    Polls repeatedly until a matching element is found or the timeout expires.
    Returns matching elements in the same format as find_ui_elements, or a
    timeout error message.
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
def launch_app(
    command: Annotated[
        str,
        Field(description='Command to launch (e.g. "kcalc" or "/path/to/app --arg").'),
    ],
    env: Annotated[
        dict[str, str] | None,
        Field(description="Extra environment variables to pass to the app."),
    ] = None,
) -> str:
    """Launch an application inside the running isolated session.

    Requires an active session. Returns the app PID (for use with read_app_log)
    and the log file path.
    """
    session = _get_session()
    cmd = shlex.split(command)
    app_info = session.launch_app(cmd, extra_env=env)
    return f"App launched: {command} (PID={app_info.pid})\nApp log: {app_info.log_path}"


@mcp.tool()
def list_windows() -> str:
    """List accessible application windows in the isolated session.

    Uses AT-SPI2 to enumerate top-level applications and their window count.
    Applications that do not support accessibility (AT-SPI2) may not appear.
    Returns a formatted list of app names with window counts.
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
def focus_window(
    app_name: Annotated[
        str,
        Field(description="Application name to focus (case-insensitive substring match)."),
    ],
) -> str:
    """Attempt to focus a window by application name.

    Searches for an application whose name contains the given string
    (case-insensitive) and activates its first focusable window via AT-SPI2.
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
    service: Annotated[str, Field(description='D-Bus service name (e.g. "org.kde.KWin").')],
    path: Annotated[str, Field(description='Object path (e.g. "/org/kde/KWin").')],
    interface: Annotated[str, Field(description='Interface name (e.g. "org.kde.KWin.Scripting").')],
    method: Annotated[str, Field(description="Method name to call.")],
    args: Annotated[
        list[str] | None,
        Field(
            description="Method arguments in dbus-send format "
            '(e.g. ["string:hello", "int32:42", "boolean:true"]).'
        ),
    ] = None,
) -> str:
    """Call a D-Bus method in the isolated session using dbus-send.

    Executes a D-Bus method call and returns the reply. Arguments must use
    dbus-send type notation (e.g. "string:value", "int32:42", "boolean:true").
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
def read_app_log(
    pid: Annotated[
        int,
        Field(description="PID of the app (returned by launch_app or session_start)."),
    ],
    last_n_lines: Annotated[
        int,
        Field(description="Number of trailing lines to return (0 = all output)."),
    ] = 50,
) -> str:
    """Read stdout/stderr output of a launched app.

    Returns the combined stdout and stderr text captured since the app was
    launched. Use the PID from launch_app or session_start to identify the app.
    """
    session = _get_session()
    return session.read_app_log(pid, last_n_lines=last_n_lines)


@mcp.tool()
def wayland_info(
    filter_protocol: Annotated[
        str,
        Field(
            description="Substring to filter protocol names "
            '(e.g. "plasma_window_management"). Empty = show all.'
        ),
    ] = "",
) -> str:
    """List Wayland protocols available in the isolated session.

    Runs wayland-info to enumerate all exposed Wayland globals. Useful for
    verifying that restricted protocols are accessible. Returns the full
    output or only lines matching the filter.
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
