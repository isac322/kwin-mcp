"""The kwin-mcp 1.0 tool-name contract, shared by the unit and end-to-end trees.

Both sets are written out here instead of being read from ``kwin_mcp.server`` so
that a test comparing the server's ``tools/list`` against them is checking the
server, not the server against itself.
"""

from __future__ import annotations

from typing import Final

# The 29 tools of the 1.0 surface, in the order of the design's tool table.
TOOL_NAMES: Final[tuple[str, ...]] = (
    "session_start",
    "session_connect",
    "session_stop",
    "session_info",
    "app_launch",
    "app_log",
    "screen_capture",
    "element_query",
    "element_act",
    "window_list",
    "desktop_list",
    "window_focus",
    "window_place",
    "window_close",
    "pointer_click",
    "pointer_move",
    "pointer_scroll",
    "pointer_drag",
    "pointer_hold",
    "keyboard_press",
    "keyboard_type",
    "keyboard_hold",
    "touch_tap",
    "touch_swipe",
    "touch_pinch",
    "clipboard_read",
    "clipboard_write",
    "dbus_call",
    "wayland_list",
)

# Every tool name that 0.11 (35 tools) or PR #103 (virtual_desktops,
# window_set_desktop) exposed and 1.0 does not keep: 35 + 2 - 9 kept = 28.
REMOVED_TOOL_NAMES: Final[tuple[str, ...]] = (
    "accessibility_tree",
    "active_window",
    "clipboard_get",
    "clipboard_set",
    "find_ui_elements",
    "focus_window",
    "invoke_ui_action",
    "keyboard_key",
    "keyboard_key_down",
    "keyboard_key_up",
    "keyboard_type_unicode",
    "launch_app",
    "list_windows",
    "mouse_button_down",
    "mouse_button_up",
    "mouse_click",
    "mouse_drag",
    "mouse_move",
    "mouse_scroll",
    "read_app_log",
    "screenshot",
    "touch_multi_swipe",
    "virtual_desktops",
    "wait_for_element",
    "wayland_info",
    "window_geometry",
    "window_set_desktop",
    "window_set_geometry",
)
