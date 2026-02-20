"""MCP server for KDE Wayland GUI automation."""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from kwin_mcp.accessibility import find_elements, get_accessibility_tree
from kwin_mcp.input import InputBackend, MouseButton
from kwin_mcp.screenshot import capture_screenshot_to_file
from kwin_mcp.session import Session, SessionConfig

mcp = FastMCP("kwin-mcp")

# Global session state
_session: Session | None = None
_input: InputBackend | None = None


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


@mcp.tool()
def session_start(
    app_command: str = "",
    screen_width: int = 1920,
    screen_height: int = 1080,
) -> str:
    """Start an isolated KWin Wayland session, optionally launching an app.

    Args:
        app_command: Command to launch (e.g., "kcalc" or "/path/to/app --arg").
                    Leave empty to start session without an app.
        screen_width: Virtual screen width in pixels.
        screen_height: Virtual screen height in pixels.

    Returns:
        Session status information.
    """
    global _session, _input

    if _session is not None and _session.is_running:
        return "Session already running. Call session_stop first."

    _session = Session()
    config = SessionConfig(
        screen_width=screen_width,
        screen_height=screen_height,
    )
    info = _session.start(config)

    result = f"Session started. Wayland socket: {info.wayland_socket}"

    if app_command:
        cmd = app_command.split()
        pid = _session.launch_app(cmd)
        result += f"\nApp launched: {app_command} (PID={pid})"

    # Set up input backend via KWin's EIS D-Bus interface
    import time

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
    global _session, _input

    if _session is None:
        return "No session running."

    if _input is not None:
        _input.close()
    _session.stop()
    _session = None
    _input = None
    return "Session stopped."


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


@mcp.tool()
def mouse_click(
    x: int,
    y: int,
    button: str = "left",
    double: bool = False,
) -> str:
    """Click at coordinates in the isolated session.

    Args:
        x: X coordinate.
        y: Y coordinate.
        button: "left", "right", or "middle".
        double: If True, double-click.
    """
    inp = _get_input()
    btn = MouseButton(button)
    inp.mouse_click(x, y, btn, double=double)
    return f"Clicked {button} at ({x}, {y})" + (" (double)" if double else "")


@mcp.tool()
def mouse_move(x: int, y: int) -> str:
    """Move the mouse (hover) to coordinates without clicking.

    Args:
        x: X coordinate.
        y: Y coordinate.
    """
    inp = _get_input()
    inp.mouse_move(x, y)
    return f"Mouse moved to ({x}, {y})"


@mcp.tool()
def mouse_scroll(x: int, y: int, delta: int, horizontal: bool = False) -> str:
    """Scroll at coordinates.

    Args:
        x: X coordinate.
        y: Y coordinate.
        delta: Scroll amount (positive = down/right, negative = up/left).
        horizontal: If True, scroll horizontally.
    """
    inp = _get_input()
    inp.mouse_scroll(x, y, delta, horizontal=horizontal)
    direction = "horizontal" if horizontal else "vertical"
    return f"Scrolled {direction} by {delta} at ({x}, {y})"


@mcp.tool()
def mouse_drag(from_x: int, from_y: int, to_x: int, to_y: int) -> str:
    """Drag from one point to another.

    Args:
        from_x, from_y: Starting coordinates.
        to_x, to_y: Ending coordinates.
    """
    inp = _get_input()
    inp.mouse_drag(from_x, from_y, to_x, to_y)
    return f"Dragged from ({from_x}, {from_y}) to ({to_x}, {to_y})"


@mcp.tool()
def keyboard_type(text: str) -> str:
    """Type text in the focused element.

    Args:
        text: Text to type.
    """
    inp = _get_input()
    inp.keyboard_type(text)
    return f"Typed: {text!r}"


@mcp.tool()
def keyboard_key(key: str) -> str:
    """Press a key combination.

    Args:
        key: Key to press (e.g., "Return", "ctrl+c", "alt+F4", "Tab").
    """
    inp = _get_input()
    inp.keyboard_key(key)
    return f"Pressed: {key}"


def main() -> None:
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
