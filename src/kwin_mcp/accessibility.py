"""AT-SPI2 accessibility tree reader.

Can be run as a subprocess CLI for isolated D-Bus session support.
Reads a JSON request from stdin and writes a JSON response to stdout.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

import gi

if TYPE_CHECKING:
    from kwin_mcp.geometry import KWinWindow

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

# Elements such as editors expose their whole document through the Text
# interface; cap it so a tree dump stays readable.
_MAX_TEXT_CHARS = 200


@dataclass
class ElementInfo:
    """Information about a single UI element.

    ``x``/``y``/``width``/``height`` are global screen coordinates when
    ``mapped`` is true. When ``mapped`` is false the rectangle could not be
    placed on the screen with certainty; ``unavailable`` carries the reason
    and the coordinates must not be used for input.
    """

    role: str
    name: str
    description: str
    states: list[str]
    x: int
    y: int
    width: int
    height: int
    actions: list[str]
    text: str
    value: float | None
    value_max: float | None
    children_count: int
    depth: int
    has_extents: bool
    mapped: bool
    unavailable: str


@dataclass
class _WindowMapping:
    """How one AT-SPI top-level maps to a KWin window.

    ``window`` is the matched KWin window dict; ``offset`` is the translation
    from AT-SPI window-local coordinates to screen coordinates. ``reason`` is
    set whenever the mapping failed.
    """

    window: KWinWindow | None
    reason: str
    rect: tuple[int, int, int, int] | None = None
    offset: tuple[int, int] | None = None


# Shared mapping for elements that are not windows at all (the application
# root node, or AT-SPI children that vanished mid-walk).
_UNMAPPED = _WindowMapping(None, "not-a-window")


def get_accessibility_tree(
    app_name: str = "",
    max_depth: int = 15,
    role: str = "",
) -> str:
    """Get the accessibility tree as a formatted text string.

    Args:
        app_name: Filter to a specific application (empty = all apps).
        max_depth: Maximum tree depth to traverse.
        role: Filter to elements with this role (empty = all roles).
            Non-matching elements are hidden but their children are still traversed.

    Returns:
        Formatted text representation of the accessibility tree.
    """
    kwin_before, kwin_error = _kwin_windows()
    desktop = Atspi.get_desktop(0)
    entries: list[tuple[ElementInfo, _WindowMapping]] = []
    mappings_by_pid: dict[int, list[_WindowMapping]] = {}
    role_filter = role.lower()

    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue

        name = app.get_name() or ""
        if app_name and app_name.lower() not in name.lower():
            continue

        pid, mappings, children = _resolve_app(app, kwin_before, kwin_error)
        if pid is not None:
            mappings_by_pid.setdefault(pid, []).extend(mappings)
        entries.append((_extract_info(app, 0), _UNMAPPED))
        for child, mapping in zip(children, mappings, strict=True):
            if child is not None:
                _collect_tree(child, mapping, entries, depth=1, max_depth=max_depth)

    _apply_stability(mappings_by_pid, kwin_before, kwin_error)

    lines: list[str] = []
    total = 0
    for info, mapping in entries:
        if role_filter and role_filter != info.role.lower():
            continue
        lines.append(_format_line(_finalize(info, mapping)))
        total += 1

    if not lines:
        return "(no accessible applications found)"

    header = f"# Accessibility Tree ({total} elements)\n\n"
    return header + "\n".join(lines)


def find_elements(
    query: str, app_name: str = "", states: list[str] | None = None
) -> list[ElementInfo]:
    """Find elements matching a query string and/or required states.

    Searches element names, roles, and descriptions. Optionally filters
    by AT-SPI2 states.

    Args:
        query: Search string (case-insensitive). Empty string matches all elements.
        app_name: Filter to a specific application.
        states: If provided, only return elements that have ALL of these states.

    Returns:
        List of matching ElementInfo objects.
    """
    kwin_before, kwin_error = _kwin_windows()
    desktop = Atspi.get_desktop(0)
    results: list[tuple[ElementInfo, _WindowMapping]] = []
    mappings_by_pid: dict[int, list[_WindowMapping]] = {}
    query_lower = query.lower()

    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue

        name = app.get_name() or ""
        if app_name and app_name.lower() not in name.lower():
            continue

        pid, mappings, children = _resolve_app(app, kwin_before, kwin_error)
        if pid is not None:
            mappings_by_pid.setdefault(pid, []).extend(mappings)
        app_info = _extract_info(app, 0)
        if _matches(app_info, query_lower, states):
            results.append((app_info, _UNMAPPED))
        for child, mapping in zip(children, mappings, strict=True):
            if child is not None:
                _search_element(
                    child, mapping, query_lower, results, depth=1, required_states=states
                )

    _apply_stability(mappings_by_pid, kwin_before, kwin_error)
    return [_finalize(info, mapping) for info, mapping in results]


def list_windows() -> str:
    """List accessible application windows with titles and active/focused state.

    Returns:
        Formatted list of apps with per-window title and state markers.
    """
    desktop = Atspi.get_desktop(0)
    lines: list[str] = []
    app_count = 0
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        app_name = app.get_name() or "(unnamed)"
        child_count = app.get_child_count()
        if child_count == 0:
            continue
        app_count += 1
        lines.append(f"- {app_name} ({child_count} windows)")
        for j in range(child_count):
            win = app.get_child_at_index(j)
            if win is None:
                continue
            win_title = win.get_name() or "(untitled)"
            state_set = win.get_state_set()
            markers: list[str] = []
            if state_set.contains(Atspi.StateType.ACTIVE):
                markers.append("active")
            if state_set.contains(Atspi.StateType.FOCUSED):
                markers.append("focused")
            marker_str = f" [{', '.join(markers)}]" if markers else ""
            lines.append(f'    - "{win_title}"{marker_str}')
    if not lines:
        return "(no accessible applications found)"
    return f"Applications ({app_count}):\n" + "\n".join(lines)


def wait_for_elements(
    query: str,
    app_name: str = "",
    timeout_ms: int = 5000,
    poll_interval_ms: int = 200,
    states: list[str] | None = None,
) -> list[ElementInfo]:
    """Poll for elements matching a query and/or states until found or timeout.

    Args:
        query: Search string (case-insensitive). Empty string matches all elements.
        app_name: Filter to a specific application.
        timeout_ms: Maximum wait time in milliseconds.
        poll_interval_ms: Polling interval in milliseconds.
        states: If provided, only match elements that have ALL of these states.

    Returns:
        List of matching elements.

    Raises:
        TimeoutError: If no elements found within timeout.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    interval = poll_interval_ms / 1000.0

    while True:
        elements = find_elements(query, app_name=app_name, states=states)
        if elements:
            return elements

        if time.monotonic() >= deadline:
            criteria = f"query='{query}'"
            if states:
                criteria += f", states={states}"
            msg = f"Timeout after {timeout_ms}ms: no elements matching {criteria}"
            raise TimeoutError(msg)

        time.sleep(interval)


def _collect_tree(
    element: Atspi.Accessible,
    mapping: _WindowMapping,
    entries: list[tuple[ElementInfo, _WindowMapping]],
    depth: int,
    max_depth: int,
) -> None:
    """Collect an element and its children for later formatting."""
    if depth > max_depth:
        return

    info = _extract_info(element, depth)
    entries.append((info, mapping))

    for i in range(info.children_count):
        child = element.get_child_at_index(i)
        if child is not None:
            _collect_tree(child, mapping, entries, depth + 1, max_depth)


def _search_element(
    element: Atspi.Accessible,
    mapping: _WindowMapping,
    query: str,
    results: list[tuple[ElementInfo, _WindowMapping]],
    depth: int,
    max_depth: int = 15,
    required_states: list[str] | None = None,
) -> None:
    """Recursively search for elements matching the query and/or required states."""
    if depth > max_depth:
        return

    info = _extract_info(element, depth)

    if _matches(info, query, required_states):
        results.append((info, mapping))

    # Search children
    for i in range(info.children_count):
        child = element.get_child_at_index(i)
        if child is not None:
            _search_element(child, mapping, query, results, depth + 1, max_depth, required_states)


def _matches(info: ElementInfo, query: str, required_states: list[str] | None) -> bool:
    """Query (name/role/description substring) and required-states check."""
    query_match = (
        query in info.name.lower()
        or query in info.role.lower()
        or query in info.description.lower()
    )
    states_match = required_states is None or all(s in info.states for s in required_states)
    return query_match and states_match


def _finalize(info: ElementInfo, mapping: _WindowMapping) -> ElementInfo:
    """Turn window-local extents into screen coordinates, or mark unavailable.

    Element-level problems (no extents at all, empty rectangle) win over
    mapping problems: an element without a rectangle has no position to
    translate regardless of whether its window was identified.
    """
    if not info.has_extents:
        info.mapped = False
        info.unavailable = "no-extents"
    elif info.width <= 0 or info.height <= 0:
        info.mapped = False
        info.unavailable = "empty-extents"
    elif mapping.offset is not None:
        info.x += mapping.offset[0]
        info.y += mapping.offset[1]
        info.mapped = True
        info.unavailable = ""
    else:
        info.mapped = False
        info.unavailable = mapping.reason or "unmapped"
    return info


def _format_line(info: ElementInfo) -> str:
    """Format one collected element as a tree line."""
    indent = "  " * info.depth
    states_str = f" ({', '.join(info.states)})" if info.states else ""
    if info.mapped:
        pos_str = f" @ screen ({info.x}, {info.y}, {info.width}x{info.height})"
    else:
        pos_str = f" @ unavailable ({info.unavailable})"
    actions_str = f" [actions: {', '.join(info.actions)}]" if info.actions else ""
    text_str = f" text={info.text!r}" if info.text else ""
    has_value = info.value is not None and info.value_max is not None
    value_str = f" value={info.value:g}/{info.value_max:g}" if has_value else ""

    return (
        f'{indent}- [{info.role}] "{info.name}"{states_str}{pos_str}'
        f"{text_str}{value_str}{actions_str}"
    )


# ── AT-SPI top-level → KWin window mapping ───────────────────────────────
#
# AT-SPI reports window-local coordinates (CoordType.WINDOW); a Wayland
# client cannot know where the compositor placed it. KWin does know, so each
# AT-SPI top-level is matched to exactly one KWin window and the whole
# subtree is translated by that window's client origin. Any uncertainty —
# the KWin query failing, no candidate, several candidates, a window set
# that changed mid-walk — fails closed: elements report "unavailable" with
# a reason instead of coordinates that would click the wrong place.


def _kwin_windows() -> tuple[list[KWinWindow], str]:
    """Snapshot every KWin window. Returns (windows, error)."""
    try:
        from kwin_mcp import geometry

        return geometry.collect_windows(), ""
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def _window_key(window: KWinWindow) -> tuple[object, ...]:
    """Identity + geometry of a KWin window for before/after comparison."""
    return (
        window["id"],
        window["pid"],
        window["caption"],
        *window["client"],
    )


def _apply_stability(
    mappings_by_pid: dict[int, list[_WindowMapping]],
    kwin_before: list[KWinWindow],
    kwin_error: str,
) -> None:
    """Unmap every top-level of a pid whose KWin window set changed mid-walk.

    The AT-SPI traversal takes ~200ms; a window opening, closing or moving
    during it would otherwise translate elements against stale geometry.
    """
    if kwin_error:
        return
    kwin_after, after_error = _kwin_windows()
    if after_error:
        # Cannot prove the snapshot was stable; fail closed for everything.
        for mappings in mappings_by_pid.values():
            for mapping in mappings:
                if mapping.offset is not None:
                    mapping.window, mapping.offset, mapping.reason = (
                        None,
                        None,
                        "windows-changed",
                    )
        return
    before_by_pid: dict[int, set[tuple[object, ...]]] = {}
    for window in kwin_before:
        before_by_pid.setdefault(window["pid"], set()).add(_window_key(window))
    after_by_pid: dict[int, set[tuple[object, ...]]] = {}
    for window in kwin_after:
        after_by_pid.setdefault(window["pid"], set()).add(_window_key(window))
    for pid, mappings in mappings_by_pid.items():
        if before_by_pid.get(pid, set()) != after_by_pid.get(pid, set()):
            for mapping in mappings:
                mapping.window, mapping.offset, mapping.reason = None, None, "windows-changed"


def _resolve_app(
    app: Atspi.Accessible,
    kwin: list[KWinWindow],
    kwin_error: str,
) -> tuple[int | None, list[_WindowMapping], list[Atspi.Accessible | None]]:
    """Match every AT-SPI top-level of one application to a KWin window.

    Returns (pid, mappings aligned with children, children). The mapping must
    be a bijection: a child that vanished mid-walk, a top-level with no unique
    match, or two top-levels claiming the same KWin window unmaps the whole
    application — a partial mapping could silently click the wrong window.
    """
    try:
        pid: int | None = int(app.get_process_id())
    except Exception:
        pid = None

    children: list[Atspi.Accessible | None] = []
    for j in range(app.get_child_count()):
        children.append(app.get_child_at_index(j))

    def _unmapped(reason: str) -> list[_WindowMapping]:
        return [_WindowMapping(None, reason) for _ in children]

    if pid is None or pid <= 0:
        return pid, _unmapped("pid-unavailable"), children
    if kwin_error:
        return pid, _unmapped("kwin-query-failed"), children
    if any(child is None for child in children):
        return pid, _unmapped("window-set-mismatch"), children

    mappings: list[_WindowMapping] = []
    poisoned = False
    for child in children:
        assert child is not None
        name = child.get_name() or ""
        rect = _window_rect(child)
        if rect is None:
            # No extents at all: nothing to translate, and nothing to match.
            mappings.append(_WindowMapping(None, "no-extents"))
            continue
        window, reason = _match_toplevel(pid, name, rect, kwin)
        if window is None:
            poisoned = True
        mappings.append(_WindowMapping(window, reason, rect))

    claimed: dict[str, int] = {}
    for mapping in mappings:
        if mapping.window is not None:
            claimed[mapping.window["id"]] = claimed.get(mapping.window["id"], 0) + 1
    if poisoned or any(count > 1 for count in claimed.values()):
        for mapping in mappings:
            if mapping.window is not None:
                mapping.window, mapping.reason = None, "ambiguous-window-match"
        return pid, mappings, children

    for mapping in mappings:
        if mapping.window is not None and mapping.rect is not None:
            client = mapping.window["client"]
            mapping.offset = (
                client[0] - mapping.rect[0],
                client[1] - mapping.rect[1],
            )
    return pid, mappings, children


def _match_toplevel(
    pid: int,
    name: str,
    rect: tuple[int, int, int, int],
    kwin: list[KWinWindow],
) -> tuple[KWinWindow | None, str]:
    """Match one AT-SPI top-level to exactly one KWin window.

    No singleton shortcut: every candidate must survive the caption and size
    checks even when it is the only window of the process.
    """
    candidates = [w for w in kwin if w["pid"] == pid and _eligible(w)]
    candidates = [w for w in candidates if _caption_consistent(w["caption"], name)]
    size = (rect[2], rect[3])
    candidates = [w for w in candidates if (w["client"][2], w["client"][3]) == size]
    if len(candidates) == 1:
        return candidates[0], ""
    return None, "ambiguous" if candidates else "no-kwin-window"


def _eligible(window: KWinWindow) -> bool:
    """KWin windows that can own an AT-SPI top-level.

    Popups stay eligible: GTK3 menus are separate AT-SPI top-levels backed by
    KWin popup windows, and excluding them would leave a wrong singleton
    match. They still must pass the caption and size checks.
    """
    return bool(
        window["managed"]
        and not window["deleted"]
        and not window["desktop"]
        and not window["dock"]
        and not window["notification"]
    )


def _caption_consistent(caption: str, name: str) -> bool:
    """Caption check applies only when both sides are non-empty.

    Accepts exact equality or the KDE " — App" suffix KWin shows for KDE apps
    (AT-SPI 'Open File' vs KWin 'Open File — KWrite'). A plain prefix is never
    accepted: 'Document' must not match 'Document 2 — KWrite'.
    """
    n, c = name.strip(), caption.strip()
    if not n or not c:
        return True
    return c == n or c.startswith(n + " — ")


def _window_rect(element: Atspi.Accessible) -> tuple[int, int, int, int] | None:
    """Window-local extents of an AT-SPI element, or None when unavailable."""
    try:
        component = element.get_component_iface()
        if component is None:
            return None
        rect = component.get_extents(Atspi.CoordType.WINDOW)
        return rect.x, rect.y, rect.width, rect.height
    except Exception:
        return None


def _extract_info(element: Atspi.Accessible, depth: int) -> ElementInfo:
    """Extract information from an AT-SPI accessible element."""
    role = element.get_role_name() or "unknown"
    name = element.get_name() or ""
    description = element.get_description() or ""

    # Get states. get_states() returns only the active members, which is both
    # cheaper than probing every enum value and portable: GI enum types are not
    # iterable in newer PyGObject releases.
    states: list[str] = []
    for state in element.get_state_set().get_states():
        state_name = state.value_nick
        if state_name:
            states.append(state_name)

    # Get position and size. WINDOW extents are window-local and reliable for
    # both Qt and GTK; SCREEN extents are (0,0) on GTK4 and already include the
    # output origin for Qt on a second output, so they cannot be used here.
    # _finalize() adds the matched KWin window's client origin.
    x, y, width, height = 0, 0, 0, 0
    has_extents = False
    try:
        component = element.get_component_iface()
        if component is not None:
            rect = component.get_extents(Atspi.CoordType.WINDOW)
            x, y, width, height = rect.x, rect.y, rect.width, rect.height
            has_extents = True
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

    # Editors and entries carry their content in the Text interface rather than
    # in the name, so without this their contents are invisible to callers. The
    # cap keeps a whole document out of a tree dump that walks every element.
    text = ""
    try:
        text_iface = element.get_text_iface()
        if text_iface is not None:
            # Call through the interface class: the bound method on the
            # accessible returns an empty string on this PyGObject version.
            count = Atspi.Text.get_character_count(text_iface)
            if count:
                text = Atspi.Text.get_text(text_iface, 0, min(count, _MAX_TEXT_CHARS))
                if count > _MAX_TEXT_CHARS:
                    text += "…"
    except Exception:
        pass

    # Scrollbars and sliders keep their position in the Value interface; without
    # it a caller can see that a scrollbar exists but not where it sits.
    value: float | None = None
    value_max: float | None = None
    try:
        value_iface = element.get_value_iface()
        if value_iface is not None:
            value = float(Atspi.Value.get_current_value(value_iface))
            value_max = float(Atspi.Value.get_maximum_value(value_iface))
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
        text=text,
        value=value,
        value_max=value_max,
        children_count=element.get_child_count(),
        depth=depth,
        has_extents=has_extents,
        mapped=False,
        unavailable="",
    )


# ── CLI entrypoint for subprocess execution ──────────────────────────────


def _handle_request(request: dict) -> dict:
    """Dispatch a JSON request to the appropriate function."""
    op = request.get("op", "")

    if op == "tree":
        result = get_accessibility_tree(
            app_name=request.get("app_name", ""),
            max_depth=request.get("max_depth", 15),
            role=request.get("role", ""),
        )
        return {"ok": True, "result": result}

    if op == "find":
        elements = find_elements(
            query=request.get("query", ""),
            app_name=request.get("app_name", ""),
            states=request.get("states"),
        )
        return {"ok": True, "result": [asdict(e) for e in elements]}

    if op == "wait":
        try:
            elements = wait_for_elements(
                query=request.get("query", ""),
                app_name=request.get("app_name", ""),
                timeout_ms=request.get("timeout_ms", 5000),
                poll_interval_ms=request.get("poll_interval_ms", 200),
                states=request.get("states"),
            )
        except TimeoutError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "result": [asdict(e) for e in elements]}

    if op == "list_windows":
        return {"ok": True, "result": list_windows()}

    return {"ok": False, "error": f"Unknown operation: {op}"}


if __name__ == "__main__":
    raw = sys.stdin.read()
    try:
        req = json.loads(raw)
    except json.JSONDecodeError as exc:
        json.dump({"ok": False, "error": f"Invalid JSON: {exc}"}, sys.stdout)
        sys.exit(1)

    resp = _handle_request(req)
    json.dump(resp, sys.stdout)
