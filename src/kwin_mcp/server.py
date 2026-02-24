"""MCP server for KDE Wayland GUI automation.

Thin wrapper that registers MCP tools with parameter descriptions,
delegating all logic to AutomationEngine in core.py.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from kwin_mcp.core import AutomationEngine

mcp = FastMCP("kwin-mcp")
_engine = AutomationEngine()


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
    keep_screenshots: Annotated[
        bool,
        Field(
            description="Keep screenshot files after session_stop instead of deleting them. "
            "Useful for debugging. Files must be cleaned up manually when enabled."
        ),
    ] = False,
    isolate_home: Annotated[
        bool,
        Field(
            description="Create a temporary HOME directory with isolated XDG directories "
            "(config, data, cache, state). Prevents apps from reading/writing host user settings."
        ),
    ] = False,
    keep_home: Annotated[
        bool,
        Field(
            description="Keep the isolated home directory after session_stop "
            "instead of deleting it. Only effective when isolate_home=true. "
            "Files must be cleaned up manually when enabled."
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
    return _engine.session_start(
        app_command=app_command,
        screen_width=screen_width,
        screen_height=screen_height,
        enable_clipboard=enable_clipboard,
        keep_screenshots=keep_screenshots,
        isolate_home=isolate_home,
        keep_home=keep_home,
        env=env,
    )


@mcp.tool()
def session_stop() -> str:
    """Stop the isolated KWin session and clean up.

    Terminates KWin, all launched app processes, and the D-Bus session.
    Cleans up temporary files and clipboard processes. Safe to call when
    no session is running (returns "No session running.").
    """
    return _engine.session_stop()


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
    return _engine.screenshot(include_cursor=include_cursor)


@mcp.tool()
def accessibility_tree(
    app_name: Annotated[
        str,
        Field(description="Filter to a specific app name (empty string = all apps)."),
    ] = "",
    max_depth: Annotated[int, Field(description="Maximum tree traversal depth.")] = 15,
    role: Annotated[
        str,
        Field(
            description='Filter to elements with this role (e.g. "push button", "text", '
            '"check box"). Empty string = show all roles. Non-matching elements are hidden '
            "but their children are still traversed to find deeper matches."
        ),
    ] = "",
) -> str:
    """Get the accessibility tree of apps in the isolated session.

    Returns a formatted text tree with each widget's role, name, states,
    and bounding box coordinates. Use this to understand UI structure before
    interacting with elements.
    """
    return _engine.accessibility_tree(app_name=app_name, max_depth=max_depth, role=role)


@mcp.tool()
def find_ui_elements(
    query: Annotated[
        str,
        Field(
            description="Search text (case-insensitive, matches names/roles/descriptions). "
            "Can be empty string when filtering by states only."
        ),
    ],
    app_name: Annotated[
        str,
        Field(description="Filter to a specific app name (empty string = all apps)."),
    ] = "",
    states: Annotated[
        list[str] | None,
        Field(
            description='Filter by AT-SPI2 states (e.g. ["focused"], ["active", "visible"]). '
            "Only elements matching ALL specified states are returned. "
            "Common states: active, focused, visible, enabled, checked, selected, expanded."
        ),
    ] = None,
) -> str:
    """Find UI elements matching a search query and/or required AT-SPI2 states.

    Returns a list of matching elements with their role, name, bounding box
    (x, y, width, height), and available actions. Use this to locate specific
    buttons, inputs, or labels before clicking or interacting.
    """
    return _engine.find_ui_elements(query=query, app_name=app_name, states=states)


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
    return _engine.mouse_click(
        x=x,
        y=y,
        button=button,
        double=double,
        triple=triple,
        modifiers=modifiers,
        hold_ms=hold_ms,
        screenshot_after_ms=screenshot_after_ms,
    )


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
    return _engine.mouse_move(x=x, y=y, screenshot_after_ms=screenshot_after_ms)


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
    return _engine.mouse_scroll(
        x=x, y=y, delta=delta, horizontal=horizontal, discrete=discrete, steps=steps
    )


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
    return _engine.mouse_drag(
        from_x=from_x,
        from_y=from_y,
        to_x=to_x,
        to_y=to_y,
        button=button,
        modifiers=modifiers,
        waypoints=waypoints,
        screenshot_after_ms=screenshot_after_ms,
    )


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
    return _engine.mouse_button_down(x=x, y=y, button=button)


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
    return _engine.mouse_button_up(x=x, y=y, button=button)


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
    return _engine.keyboard_type(text=text, screenshot_after_ms=screenshot_after_ms)


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
    return _engine.keyboard_type_unicode(text=text, screenshot_after_ms=screenshot_after_ms)


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
    return _engine.keyboard_key(key=key, screenshot_after_ms=screenshot_after_ms)


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
    return _engine.keyboard_key_down(key=key)


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
    return _engine.keyboard_key_up(key=key)


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
    return _engine.touch_tap(x=x, y=y, hold_ms=hold_ms, screenshot_after_ms=screenshot_after_ms)


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
    return _engine.touch_swipe(
        from_x=from_x,
        from_y=from_y,
        to_x=to_x,
        to_y=to_y,
        duration_ms=duration_ms,
        screenshot_after_ms=screenshot_after_ms,
    )


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
    return _engine.touch_pinch(
        center_x=center_x,
        center_y=center_y,
        start_distance=start_distance,
        end_distance=end_distance,
        duration_ms=duration_ms,
        screenshot_after_ms=screenshot_after_ms,
    )


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
    return _engine.touch_multi_swipe(
        from_x=from_x,
        from_y=from_y,
        to_x=to_x,
        to_y=to_y,
        fingers=fingers,
        duration_ms=duration_ms,
        screenshot_after_ms=screenshot_after_ms,
    )


# ── Clipboard tools ──────────────────────────────────────────────────────


@mcp.tool()
def clipboard_get() -> str:
    """Read the current clipboard content in the isolated session.

    Requires enable_clipboard=true in session_start and wl-clipboard
    installed. Returns the clipboard text or an error message if clipboard
    is not enabled or empty.
    """
    return _engine.clipboard_get()


@mcp.tool()
def clipboard_set(
    text: Annotated[str, Field(description="Text to copy to clipboard.")],
) -> str:
    """Set the clipboard content in the isolated session.

    Requires enable_clipboard=true in session_start and wl-clipboard
    installed. The content remains available until replaced by another
    clipboard_set call or the session ends.
    """
    return _engine.clipboard_set(text=text)


# ── Wait-for-UI tools ───────────────────────────────────────────────────


@mcp.tool()
def wait_for_element(
    query: Annotated[
        str,
        Field(
            description="Search text (case-insensitive, matches names/roles/descriptions). "
            "Can be empty string when waiting for state changes only."
        ),
    ],
    app_name: Annotated[
        str,
        Field(description="Filter to a specific app name (empty string = all apps)."),
    ] = "",
    timeout_ms: Annotated[int, Field(description="Maximum wait time in milliseconds.")] = 5000,
    poll_interval_ms: Annotated[int, Field(description="Polling interval in milliseconds.")] = 200,
    expected_states: Annotated[
        list[str] | None,
        Field(
            description='Wait until elements also have these AT-SPI2 states (e.g. ["active"]). '
            "Useful for waiting until a window becomes active or a checkbox becomes checked. "
            "Common states: active, focused, visible, enabled, checked, selected, expanded."
        ),
    ] = None,
) -> str:
    """Wait for a UI element matching query and/or states to appear.

    Polls repeatedly until a matching element is found or the timeout expires.
    Returns matching elements in the same format as find_ui_elements, or a
    timeout error message.
    """
    return _engine.wait_for_element(
        query=query,
        app_name=app_name,
        timeout_ms=timeout_ms,
        poll_interval_ms=poll_interval_ms,
        expected_states=expected_states,
    )


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
    return _engine.launch_app(command=command, env=env)


@mcp.tool()
def list_windows() -> str:
    """List accessible application windows in the isolated session.

    Uses AT-SPI2 to enumerate top-level applications and their window count.
    Applications that do not support accessibility (AT-SPI2) may not appear.
    Returns a formatted list of app names with window counts.
    """
    return _engine.list_windows()


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
    return _engine.focus_window(app_name=app_name)


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
    return _engine.dbus_call(
        service=service, path=path, interface=interface, method=method, args=args
    )


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
    return _engine.read_app_log(pid=pid, last_n_lines=last_n_lines)


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
    return _engine.wayland_info(filter_protocol=filter_protocol)


def main() -> None:
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
