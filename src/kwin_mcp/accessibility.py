"""AT-SPI2 accessibility tree reader."""

from __future__ import annotations

from dataclasses import dataclass

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402


@dataclass
class ElementInfo:
    """Information about a single UI element."""

    role: str
    name: str
    description: str
    states: list[str]
    x: int
    y: int
    width: int
    height: int
    actions: list[str]
    children_count: int
    depth: int


def get_accessibility_tree(
    app_name: str = "",
    max_depth: int = 15,
) -> str:
    """Get the accessibility tree as a formatted text string.

    Args:
        app_name: Filter to a specific application (empty = all apps).
        max_depth: Maximum tree depth to traverse.

    Returns:
        Formatted text representation of the accessibility tree.
    """
    desktop = Atspi.get_desktop(0)
    lines: list[str] = []
    total = 0

    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue

        name = app.get_name() or ""
        if app_name and app_name.lower() not in name.lower():
            continue

        count = _format_element(app, lines, depth=0, max_depth=max_depth)
        total += count

    if not lines:
        return "(no accessible applications found)"

    header = f"# Accessibility Tree ({total} elements)\n\n"
    return header + "\n".join(lines)


def find_elements(query: str, app_name: str = "") -> list[ElementInfo]:
    """Find elements matching a query string.

    Searches element names, roles, and descriptions.

    Args:
        query: Search string (case-insensitive).
        app_name: Filter to a specific application.

    Returns:
        List of matching ElementInfo objects.
    """
    desktop = Atspi.get_desktop(0)
    results: list[ElementInfo] = []
    query_lower = query.lower()

    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue

        name = app.get_name() or ""
        if app_name and app_name.lower() not in name.lower():
            continue

        _search_element(app, query_lower, results, depth=0, max_depth=15)

    return results


def _format_element(
    element: Atspi.Accessible,
    lines: list[str],
    depth: int,
    max_depth: int,
) -> int:
    """Recursively format an element and its children. Returns element count."""
    if depth > max_depth:
        return 0

    info = _extract_info(element, depth)
    indent = "  " * depth

    # Build display line
    states_str = f" ({', '.join(info.states)})" if info.states else ""
    pos_str = f" @ ({info.x}, {info.y}, {info.width}x{info.height})"
    actions_str = f" [actions: {', '.join(info.actions)}]" if info.actions else ""

    line = f'{indent}- [{info.role}] "{info.name}"{states_str}{pos_str}{actions_str}'
    lines.append(line)

    count = 1
    for i in range(info.children_count):
        child = element.get_child_at_index(i)
        if child is not None:
            count += _format_element(child, lines, depth + 1, max_depth)

    return count


def _search_element(
    element: Atspi.Accessible,
    query: str,
    results: list[ElementInfo],
    depth: int,
    max_depth: int,
) -> None:
    """Recursively search for elements matching the query."""
    if depth > max_depth:
        return

    info = _extract_info(element, depth)

    # Check if element matches
    if (
        query in info.name.lower()
        or query in info.role.lower()
        or query in info.description.lower()
    ):
        results.append(info)

    # Search children
    for i in range(info.children_count):
        child = element.get_child_at_index(i)
        if child is not None:
            _search_element(child, query, results, depth + 1, max_depth)


def _extract_info(element: Atspi.Accessible, depth: int) -> ElementInfo:
    """Extract information from an AT-SPI accessible element."""
    role = element.get_role_name() or "unknown"
    name = element.get_name() or ""
    description = element.get_description() or ""

    # Get states
    state_set = element.get_state_set()
    states: list[str] = []
    for state in Atspi.StateType:
        if state_set.contains(state):
            state_name = state.value_nick
            if state_name:
                states.append(state_name)

    # Get position and size
    x, y, width, height = 0, 0, 0, 0
    try:
        component = element.get_component_iface()
        if component is not None:
            rect = component.get_extents(Atspi.CoordType.SCREEN)
            x, y, width, height = rect.x, rect.y, rect.width, rect.height
    except Exception:
        pass

    # Get available actions
    actions: list[str] = []
    try:
        action_iface = element.get_action_iface()
        if action_iface is not None:
            for i in range(action_iface.get_n_actions()):
                action_name = action_iface.get_action_name(i)
                if action_name:
                    actions.append(action_name)
    except Exception:
        pass

    return ElementInfo(
        role=role,
        name=name,
        description=description,
        states=states,
        x=x,
        y=y,
        width=width,
        height=height,
        actions=actions,
        children_count=element.get_child_count(),
        depth=depth,
    )
