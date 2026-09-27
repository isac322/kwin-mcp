"""Typed results of the observation tools and their human-readable text.

The data models are the single source of truth: MCP publishes them as the tool
``outputSchema``/``structuredContent``, and the text that both the MCP server
and the CLI print is derived from them by the ``format_*`` functions.

Kept free of gi/dbus imports so the server, engine and AT-SPI worker can all
import it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ── shared ───────────────────────────────────────────────────────────────


class Rect(BaseModel):
    """Axis-aligned rectangle in global logical screen coordinates."""

    x: int
    y: int
    width: int
    height: int


# ── window_geometry ──────────────────────────────────────────────────────


class WindowGeometry(BaseModel):
    """One KWin window as reported by window_geometry."""

    id: str = Field(
        description="KWin internalId; pass to window_geometry(window_id=) or window_close."
    )
    app: str = Field(description="KWin resource class (Wayland app_id).")
    caption: str
    active: bool
    frame: Rect = Field(description="Outer frame including decorations.")
    client: Rect = Field(description="Client area; element coordinates share this space.")


class WindowGeometryResult(BaseModel):
    """Result of window_geometry."""

    windows: list[WindowGeometry] = Field(default_factory=list)
    error: str | None = Field(default=None, description="Set when KWin could not be queried.")


def format_window(w: WindowGeometry) -> str:
    """Render one window (shared by window_geometry and active_window)."""
    marker = " [active]" if w.active else ""
    f, c = w.frame, w.client
    return (
        f'- {w.app} "{w.caption}"{marker}\n'
        f"    id:     {w.id}\n"
        f"    frame:  ({f.x}, {f.y}, {f.width}x{f.height})\n"
        f"    client: ({c.x}, {c.y}, {c.width}x{c.height})"
    )


def format_window_geometry(r: WindowGeometryResult, app_name: str = "", window_id: str = "") -> str:
    """Render a window_geometry result for the given filters."""
    if r.error is not None:
        return f"Window geometry unavailable: {r.error}"
    if not r.windows:
        if window_id:
            return f"No window with id {window_id!r}."
        return "No windows found." if not app_name else f"No windows found for '{app_name}'."
    return "\n".join([f"Windows ({len(r.windows)}):", *(format_window(w) for w in r.windows)])


# ── find_ui_elements / accessibility_tree ────────────────────────────────


class UIElement(BaseModel):
    """One AT-SPI element with its position in global screen coordinates."""

    role: str
    name: str
    description: str
    states: list[str]
    depth: int = Field(description="0 = application node, 1 = top-level window, ...")
    rect: Rect | None = Field(description="Global screen rect; null when unavailable.")
    unavailable: str | None = Field(
        description="Why rect is null (e.g. no-extents, ambiguous); null when rect is set."
    )
    actions: list[str]
    text: str = Field(description="Text-interface content, capped at 200 chars + '…'.")
    value: float | None
    value_max: float | None

    @classmethod
    def from_worker(cls, el: dict[str, Any]) -> UIElement:
        """Build from the AT-SPI worker's ``asdict(ElementInfo)`` payload."""
        mapped = bool(el.get("mapped"))
        return cls(
            role=el["role"],
            name=el["name"],
            description=el["description"],
            states=el["states"],
            depth=el["depth"],
            rect=(
                Rect(x=el["x"], y=el["y"], width=el["width"], height=el["height"])
                if mapped
                else None
            ),
            unavailable=None if mapped else (el.get("unavailable") or "unmapped"),
            actions=el["actions"],
            text=el["text"],
            value=el["value"],
            value_max=el["value_max"],
        )


def _position(e: UIElement) -> str:
    """Screen rect, or the reason it is unavailable, so callers never click a guess."""
    if e.rect is not None:
        r = e.rect
        return f"@ screen ({r.x}, {r.y}, {r.width}x{r.height})"
    return f"@ unavailable ({e.unavailable})"


def _suffix(e: UIElement) -> str:
    text_str = f" text={e.text!r}" if e.text else ""
    has_value = e.value is not None and e.value_max is not None
    value_str = f" value={e.value:g}/{e.value_max:g}" if has_value else ""
    actions_str = f" [actions: {', '.join(e.actions)}]" if e.actions else ""
    return f"{text_str}{value_str}{actions_str}"


def search_description(query: str, states: list[str] | None) -> str:
    """Describe find/wait criteria, e.g. ``query='7', states=['focused']``."""
    criteria: list[str] = []
    if query:
        criteria.append(f"query='{query}'")
    if states:
        criteria.append(f"states={states}")
    return ", ".join(criteria) if criteria else "(all)"


def format_found(elements: list[UIElement], search_desc: str) -> str:
    """Render a non-empty find_ui_elements / wait_for_element match list."""
    lines = [f"Found {len(elements)} elements matching {search_desc}:\n"]
    lines.extend(f'- [{e.role}] "{e.name}" {_position(e)}{_suffix(e)}' for e in elements)
    return "\n".join(lines)


class FindUIElementsResult(BaseModel):
    """Result of find_ui_elements."""

    query: str
    states: list[str] | None
    elements: list[UIElement]


def format_find(r: FindUIElementsResult) -> str:
    """Render a find_ui_elements result."""
    search_desc = search_description(r.query, r.states)
    if not r.elements:
        return f"No elements found matching {search_desc}"
    return format_found(r.elements, search_desc)


class AccessibilityTreeResult(BaseModel):
    """Result of accessibility_tree."""

    elements: list[UIElement] = Field(description="Pre-order walk; nesting is given by depth.")


def format_tree(r: AccessibilityTreeResult) -> str:
    """Render an accessibility_tree result as an indented tree."""
    if not r.elements:
        return "(no accessible applications found)"
    lines = []
    for e in r.elements:
        states_str = f" ({', '.join(e.states)})" if e.states else ""
        lines.append(
            f'{"  " * e.depth}- [{e.role}] "{e.name}"{states_str} {_position(e)}{_suffix(e)}'
        )
    return f"# Accessibility Tree ({len(r.elements)} elements)\n\n" + "\n".join(lines)


# ── list_windows ─────────────────────────────────────────────────────────


class AccessibleWindow(BaseModel):
    """One AT-SPI top-level of an application."""

    title: str = Field(description='AT-SPI name; "(untitled)" when empty.')
    active: bool
    focused: bool


class AccessibleApplication(BaseModel):
    """One AT-SPI application with at least one child."""

    name: str = Field(description='AT-SPI name; "(unnamed)" when empty.')
    window_count: int = Field(
        description="AT-SPI child count (may exceed len(windows) if a child vanished)."
    )
    windows: list[AccessibleWindow]


class ListWindowsResult(BaseModel):
    """Result of list_windows."""

    applications: list[AccessibleApplication]


def format_list_windows(r: ListWindowsResult) -> str:
    """Render a list_windows result."""
    lines: list[str] = []
    for app in r.applications:
        lines.append(f"- {app.name} ({app.window_count} windows)")
        for w in app.windows:
            markers = [m for m, on in (("active", w.active), ("focused", w.focused)) if on]
            marker_str = f" [{', '.join(markers)}]" if markers else ""
            lines.append(f'    - "{w.title}"{marker_str}')
    if not lines:
        return "(no accessible applications found)"
    return f"Applications ({len(r.applications)}):\n" + "\n".join(lines)
