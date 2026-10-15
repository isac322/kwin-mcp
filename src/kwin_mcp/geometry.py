"""Global window geometry, window selection and virtual desktops via KWin scripting.

AT-SPI2 reports surface-local coordinates because a Wayland client cannot know
where the compositor placed it. KWin does know, and its scripting engine can
hand the numbers back over D-Bus.

Two entry points share one script round-trip:

- ``windows()`` / ``activate()`` / ``set_geometry()`` / ``desktops()`` /
  ``set_desktop()`` / ``close()`` / ``active_class()`` / ``outputs()`` run in a
  subprocess (`python -m kwin_mcp.geometry`) for the ``window_list`` /
  ``window_focus`` / ``window_place`` / ``window_close`` / ``desktop_list``
  tools, for screenshot coordinate mapping, and for the paste chord
  ``keyboard_type(method="unicode")`` chooses.
- ``collect_windows()`` runs in-process inside the AT-SPI2 query subprocess so
  `accessibility.py` can translate element rectangles into screen coordinates.

``select_windows()`` / ``select_one()`` / ``describe_selector()`` are pure: the
engine converts the ``windows()`` report with ``window_from_kwin()`` and applies a
``WindowSelector`` to it with them. The subprocess never imports the pydantic
models: it starts for every window query and every capture, and importing them
there made each call about 0.4 s slower.

Replies are always dispatched on the calling thread through the default GLib
main context. A background GLib loop thread races libatspi's use of the same
context and was observed to abort the process with heap corruption.

Usage: `echo '{"op": "windows"}' | python -m kwin_mcp.geometry`
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from collections.abc import Mapping

    from kwin_mcp.models import Rect, Window, WindowKind, WindowSelector

SINK_NAME = "io.github.kwin_mcp.Geometry"
SINK_PATH = "/geometry"


class KWinWindow(TypedDict):
    """One KWin window as reported by the geometry script.

    ``id`` is KWin's ``internalId``: stable for the lifetime of the window and
    never reused, so it can address one window among several of the same app.
    ``frame``/``client``/``buffer`` are ``[x, y, width, height]`` in global
    logical coordinates, rounded once from KWin's qreal values. ``buffer`` is
    the client's whole surface, including the shadow a client-side decorated
    window (Chromium, for example) draws around itself. ``desktops`` holds the
    virtual desktop ids (empty in KWin 6 when ``onAllDesktops``).
    ``transientFor`` is the id of the window this one is transient for, or
    None; ``transient`` without ``transientFor`` is an X11 group transient,
    whose main windows a script cannot see.
    """

    pid: int
    id: str
    app: str
    caption: str
    normal: bool
    dialog: bool
    utility: bool
    popup: bool
    managed: bool
    deleted: bool
    desktop: bool
    dock: bool
    notification: bool
    active: bool
    closeable: bool
    moveable: bool
    resizeable: bool
    desktops: list[str]
    onAllDesktops: bool
    transient: bool
    transientFor: str | None
    modal: bool
    frame: list[int]
    client: list[int]
    buffer: list[int]


# KWin scripts run in a sandboxed QJSEngine with no file or socket access, so
# callDBus back into this process is the only way out. The payload is JSON so
# that both consumers (the window report and the accessibility coordinate
# mapper) can pick the fields they need without positional parsing.
# {sink} and {path} are substituted per call: the sink name embeds the pid so a
# query inside the AT-SPI subprocess cannot collide with a concurrent one.
_SCRIPT_TEMPLATE = """
var out = [];
var windows = workspace.windowList();
for (var i = 0; i < windows.length; i++) {
    var w = windows[i];
    var f = w.frameGeometry;
    var c = w.clientGeometry;
    var b = w.bufferGeometry;
    var desktopIds = [];
    for (var k = 0; k < w.desktops.length; k++) {
        desktopIds.push(String(w.desktops[k].id));
    }
    out.push({
        pid: w.pid, id: String(w.internalId),
        resourceClass: String(w.resourceClass), caption: w.caption,
        normal: w.normalWindow, dialog: w.dialog, utility: w.utility,
        popup: w.popupWindow,
        managed: w.managed, deleted: w.deleted,
        desktop: w.desktopWindow, dock: w.dock, notification: w.notification,
        active: w === workspace.activeWindow,
        closeable: w.closeable, moveable: w.moveable, resizeable: w.resizeable,
        desktops: desktopIds, onAllDesktops: w.onAllDesktops,
        transient: w.transient,
        transientFor: w.transientFor ? String(w.transientFor.internalId) : null,
        modal: w.modal,
        frame: [f.x, f.y, f.width, f.height],
        client: [c.x, c.y, c.width, c.height],
        buffer: [b.x, b.y, b.width, b.height]
    });
}
callDBus("{sink}", "{path}", "{sink}", "Report", JSON.stringify(out));
"""

# Activation must go through KWin too: AT-SPI's grab_focus does not raise or
# activate windows on Wayland. The window is addressed by exact id only (the
# engine resolves selectors first) and any kind can be activated: a dialog or
# a utility window is as focusable as a normal one.
_ACTIVATE_SCRIPT_TEMPLATE = """
var target = {window_id};
var found = false;
var windows = workspace.windowList();
for (var i = 0; i < windows.length; i++) {
    var w = windows[i];
    if (String(w.internalId) === target) {
        workspace.activeWindow = w;
        found = true;
        break;
    }
}
callDBus("{sink}", "{path}", "{sink}", "Report", JSON.stringify({found: found}));
"""

# Direct geometry change by exact KWin internalId.
#
# KWin's QRect wrapper is safest when copied to a plain JS object, modified,
# then assigned back as a whole. Mutating w.frameGeometry.x in place is not
# reliable across Plasma/KWin versions. The pre-request frame is copied too:
# keeping the wrapper itself would report the new frame after the assignment.
#
# A requested width/height larger than the workspace (the bounding rectangle
# of all outputs) is rejected without assigning anything; only supplied fields
# are compared, so moving an already oversized window keeps working. The check
# runs here so the live workspace is read in the same round trip.
_SET_GEOMETRY_SCRIPT_TEMPLATE = """
var target = {window_id};
var requested = {requested};
var result = {found: false};
var windows = workspace.windowList();

for (var i = 0; i < windows.length; i++) {
    var w = windows[i];
    if (String(w.internalId) !== target) {
        continue;
    }

    var before = Object.assign({}, w.frameGeometry);
    var area = workspace.virtualScreenGeometry;
    result = {
        found: true,
        app: String(w.resourceClass),
        caption: w.caption,
        moveable: w.moveable,
        resizeable: w.resizeable,
        before: [before.x, before.y, before.width, before.height]
    };

    if (requested.width !== null && requested.width > area.width) {
        result.rejected = "width";
        result.limit = area.width;
        break;
    }
    if (requested.height !== null && requested.height > area.height) {
        result.rejected = "height";
        result.limit = area.height;
        break;
    }

    var geometry = Object.assign({}, before);
    if (requested.x !== null) geometry.x = requested.x;
    if (requested.y !== null) geometry.y = requested.y;
    if (requested.width !== null) geometry.width = requested.width;
    if (requested.height !== null) geometry.height = requested.height;

    w.frameGeometry = geometry;
    break;
}

callDBus("{sink}", "{path}", "{sink}", "Report", JSON.stringify(result));
"""

# Caller-supplied values reach these scripts only as JSON string literals (see
# _render_script), so a quote or any other character in an id or app name stays
# data and can never run as KWin script.
# closeWindow() asks the client to close, exactly like the titlebar button: the
# app may still keep the window open (for example behind a "save changes?"
# prompt), so the caller observes the result instead of trusting this reply.
_CLOSE_SCRIPT_TEMPLATE = """
var target = {window_id};
var result = {found: false};
var windows = workspace.windowList();
for (var i = 0; i < windows.length; i++) {
    var w = windows[i];
    if (String(w.internalId) !== target) {
        continue;
    }
    result = {
        found: true, closeable: w.closeable,
        app: String(w.resourceClass), caption: w.caption
    };
    if (w.closeable) {
        w.closeWindow();
    }
    break;
}
callDBus("{sink}", "{path}", "{sink}", "Report", JSON.stringify(result));
"""

# Virtual desktops in KWin's order, with the current one, addressed by their
# stable id (a UUID string), never by position. When {window_id} is not empty the
# report also carries that window's desktop ids, read in this separate script so
# window_place observes the state KWin committed after its own script ran.
# Every window kind is looked up. In KWin 6 a window on all desktops has an empty
# desktops list.
_DESKTOPS_SCRIPT_TEMPLATE = """
var target = {window_id};
var current = workspace.currentDesktop;
var currentId = current ? String(current.id) : null;
var desktops = [];
var list = workspace.desktops;
for (var i = 0; i < list.length; i++) {
    var d = list[i];
    desktops.push({id: String(d.id), name: String(d.name), current: String(d.id) === currentId});
}
var found = null;
if (target) {
    var windows = workspace.windowList();
    for (var j = 0; j < windows.length; j++) {
        var w = windows[j];
        if (String(w.internalId) !== target) {
            continue;
        }
        var ids = [];
        for (var k = 0; k < w.desktops.length; k++) {
            ids.push(String(w.desktops[k].id));
        }
        found = {desktops: ids, onAllDesktops: w.onAllDesktops};
        break;
    }
}
callDBus("{sink}", "{path}", "{sink}", "Report", JSON.stringify({
    desktops: desktops, current: currentId, window: found
}));
"""

# Assign exactly one window, by exact internalId, to exactly one virtual desktop,
# by exact id. Nothing is assigned unless both are found. currentDesktop is only
# read, never written, and the window is not activated or raised.
#
# KWin's Window::setDesktops (checked at v6.3.6 and unchanged through 6.7) also
# sets the desktops of every transient of the window, recursively, and, when the
# window is modal, of its main windows. To move only the requested window, the
# script follows those relationships first and refuses, without assigning
# anything, when a related window is not already on exactly the requested
# desktop. Scripts see transientFor and modal but not transients() or
# mainWindows(); an X11 group transient (transient, no transientFor) belongs to
# main windows a script cannot see, so while one exists the outcome cannot be
# known and the request is refused too. Nothing is ever moved back afterwards.
_SET_DESKTOP_SCRIPT_TEMPLATE = """
var target = {window_id};
var wanted = {desktop_id};
var checkOnly = {check_only};
var current = workspace.currentDesktop;
var result = {
    windowFound: false, desktopFound: false,
    current: current ? String(current.id) : null
};

function desktopIds(w) {
    var ids = [];
    for (var k = 0; k < w.desktops.length; k++) {
        ids.push(String(w.desktops[k].id));
    }
    return ids;
}

function refusal(start, desktopId, windows) {
    for (var u = 0; u < windows.length; u++) {
        if (windows[u].transient && !windows[u].transientFor) {
            return {refused: "unsupported"};
        }
    }
    var closure = [start];
    var seen = {};
    seen[String(start.internalId)] = true;
    for (var c = 0; c < closure.length; c++) {
        var w = closure[c];
        var id = String(w.internalId);
        for (var t = 0; t < windows.length; t++) {
            var other = windows[t];
            var otherId = String(other.internalId);
            if (other.transientFor && String(other.transientFor.internalId) === id
                    && !seen[otherId]) {
                seen[otherId] = true;
                closure.push(other);
            }
        }
        if (w.modal && w.transientFor && !seen[String(w.transientFor.internalId)]) {
            seen[String(w.transientFor.internalId)] = true;
            closure.push(w.transientFor);
        }
    }
    var related = [];
    for (var r = 1; r < closure.length; r++) {
        var ids = desktopIds(closure[r]);
        if (ids.length !== 1 || ids[0] !== desktopId) {
            related.push({
                id: String(closure[r].internalId),
                app: String(closure[r].resourceClass),
                caption: String(closure[r].caption),
                desktops: ids
            });
        }
    }
    return related.length ? {refused: "related", related: related} : null;
}

var desktop = null;
var list = workspace.desktops;
for (var i = 0; i < list.length; i++) {
    if (String(list[i].id) === wanted) {
        desktop = list[i];
        break;
    }
}
result.desktopFound = desktop !== null;
var windows = workspace.windowList();
for (var j = 0; j < windows.length; j++) {
    var w = windows[j];
    if (String(w.internalId) !== target) {
        continue;
    }
    result.windowFound = true;
    result.before = desktopIds(w);
    result.beforeOnAllDesktops = w.onAllDesktops;
    if (desktop !== null) {
        var refused = refusal(w, String(desktop.id), windows);
        if (refused !== null) {
            result.refused = refused.refused;
            if (refused.related) {
                result.related = refused.related;
            }
        } else if (!checkOnly) {
            w.desktops = [desktop];
        }
    }
    break;
}
callDBus("{sink}", "{path}", "{sink}", "Report", JSON.stringify(result));
"""

# The active window's resource class (the Wayland app_id, or the X11 WM_CLASS
# class for Xwayland clients). keyboard_type(method="unicode") picks its paste chord from
# it before pressing anything. An empty string means no window is active.
_ACTIVE_CLASS_SCRIPT = """
var w = workspace.activeWindow;
callDBus("{sink}", "{path}", "{sink}", "Report", w ? String(w.resourceClass) : "");
"""

# Output topology for screenshot coordinate mapping. Geometry is logical (the
# space EIS input and window geometry use); devicePixelRatio is the output
# scale. virtualScreenGeometry is the exact rectangle ScreenShot2
# CaptureWorkspace renders, so it is reported separately from the union.
_OUTPUTS_SCRIPT = """
var screens = [];
var list = workspace.screens;
for (var i = 0; i < list.length; i++) {
    var s = list[i];
    var g = s.geometry;
    screens.push({
        name: String(s.name), scale: s.devicePixelRatio,
        geometry: [g.x, g.y, g.width, g.height]
    });
}
var v = workspace.virtualScreenGeometry;
callDBus("{sink}", "{path}", "{sink}", "Report", JSON.stringify({
    screens: screens,
    virtual: v ? [v.x, v.y, v.width, v.height] : null
}));
"""


def _render_script(template: str, values: Mapping[str, object]) -> str:
    """Replace each ``{name}`` placeholder in ``template`` with ``json.dumps(values[name])``.

    All placeholders are substituted in one pass, so text inserted for one value
    is never re-scanned: a value that itself contains ``{other_name}`` stays a
    literal inside its JSON string instead of being expanded into script source.
    """
    pattern = "|".join(re.escape("{" + name + "}") for name in values)
    return re.sub(pattern, lambda match: json.dumps(values[match.group(0)[1:-1]]), template)


def _parse_windows(payload: str) -> list[KWinWindow]:
    """Decode the JSON window list reported by the geometry script."""
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError:
        return []
    windows: list[KWinWindow] = []
    for record in raw:
        try:
            transient_for = record["transientFor"]
            windows.append(
                KWinWindow(
                    pid=int(record["pid"]),
                    id=str(record["id"]),
                    app=str(record["resourceClass"]),
                    caption=str(record["caption"]),
                    normal=bool(record["normal"]),
                    dialog=bool(record["dialog"]),
                    utility=bool(record["utility"]),
                    popup=bool(record["popup"]),
                    managed=bool(record["managed"]),
                    deleted=bool(record["deleted"]),
                    desktop=bool(record["desktop"]),
                    dock=bool(record["dock"]),
                    notification=bool(record["notification"]),
                    active=bool(record["active"]),
                    closeable=bool(record["closeable"]),
                    moveable=bool(record["moveable"]),
                    resizeable=bool(record["resizeable"]),
                    desktops=[str(d) for d in record["desktops"]],
                    onAllDesktops=bool(record["onAllDesktops"]),
                    transient=bool(record["transient"]),
                    transientFor=None if transient_for is None else str(transient_for),
                    modal=bool(record["modal"]),
                    # KWin geometry is qreal (e.g. 325.5 for a centred dialog);
                    # round once here so every consumer sees integers.
                    frame=[round(float(v)) for v in record["frame"]],
                    client=[round(float(v)) for v in record["client"]],
                    buffer=[round(float(v)) for v in record["buffer"]],
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return windows


def run_kwin_script(js_source: str, timeout: float = 5.0) -> str:
    """Run a KWin script and return the payload it reports back over D-Bus.

    ``{sink}`` and ``{path}`` placeholders in the source are replaced with the
    reply endpoint. The reply is dispatched on the calling thread via the
    default GLib main context: running a GLib loop on a background thread races
    libatspi's use of the same context and was observed to abort the process.
    """
    # Imported here so the pure helpers (window kinds, selector matching) load
    # without dbus-python or PyGObject, e.g. in the engine and in unit tests.
    import dbus
    import dbus.bus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    DBusGMainLoop(set_as_default=True)
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    bus = dbus.bus.BusConnection(address) if address else dbus.SessionBus()
    # The sink name embeds the pid so concurrent queries (e.g. one inside the
    # AT-SPI subprocess and one in the geometry subprocess) cannot collide.
    sink_name = f"{SINK_NAME}.p{os.getpid()}"
    # Both references must outlive the wait: dropping the BusName releases the
    # well-known name, and dropping the object unexports it, either of which
    # makes KWin's callDBus land nowhere.
    bus_name = dbus.service.BusName(sink_name, bus)
    received: list[str] = []

    class _Sink(dbus.service.Object):
        @dbus.service.method(sink_name, in_signature="s", out_signature="")
        def Report(self, payload: str) -> None:  # noqa: N802 - D-Bus method name
            received.append(str(payload))

    sink = _Sink(bus, SINK_PATH)

    script_path = Path(tempfile.mkdtemp(prefix="kwin-mcp-script-")) / "script.js"
    script_path.write_text(js_source.replace("{sink}", sink_name).replace("{path}", SINK_PATH))
    scripting = dbus.Interface(
        bus.get_object("org.kde.KWin", "/Scripting"), "org.kde.kwin.Scripting"
    )
    try:
        scripting.loadScript(str(script_path))
        scripting.start()

        context = GLib.MainContext.default()
        deadline = time.monotonic() + timeout
        while not received and time.monotonic() < deadline:
            context.iteration(False) or time.sleep(0.005)
    finally:
        with contextlib.suppress(dbus.DBusException):
            scripting.unloadScript(str(script_path))
        script_path.unlink(missing_ok=True)
        script_path.parent.rmdir()
        sink.remove_from_connection()
        del bus_name

    if not received:
        msg = f"KWin did not answer the script within {timeout:.0f}s"
        raise RuntimeError(msg)
    return received[0]


def collect_windows(timeout: float = 5.0) -> list[KWinWindow]:
    """Ask KWin for every window in-process.

    Keeps popups, unmanaged, deleted and every other kind: the accessibility
    mapper needs the full list to prove that an AT-SPI top-level matches
    exactly one compositor window.
    """
    return _parse_windows(run_kwin_script(_SCRIPT_TEMPLATE, timeout))


def window_kind(window: KWinWindow) -> WindowKind:
    """The kind of a KWin window, from the first matching KWin window type.

    normal, dialog, utility (tool palettes, toolbars, menus torn off), popup
    (menus, tooltips, combo lists), shell (desktop background, panels/docks,
    notifications) and other (anything else, e.g. splash screens).
    """
    if window["normal"]:
        return "normal"
    if window["dialog"]:
        return "dialog"
    if window["utility"]:
        return "utility"
    if window["popup"]:
        return "popup"
    if window["desktop"] or window["dock"] or window["notification"]:
        return "shell"
    return "other"


def _rect(box: list[int]) -> Rect:
    from kwin_mcp.models import Rect  # in the server only: kept out of the helper subprocess

    # Rect requires a positive size, but KWin can report a zero-sized frame or
    # client (e.g. a window that is still mapping). Clamp to 1 so such a window
    # stays listable and addressable instead of failing the whole report.
    return Rect(x=box[0], y=box[1], width=max(1, box[2]), height=max(1, box[3]))


def window_from_kwin(window: KWinWindow) -> Window:
    """The public ``Window`` of one KWin window (in the server, not the helper)."""
    from kwin_mcp.models import Window  # in the server only: kept out of the helper subprocess

    return Window(
        id=window["id"],
        app_id=window["app"],
        caption=window["caption"],
        kind=window_kind(window),
        active=window["active"],
        frame=_rect(window["frame"]),
        client=_rect(window["client"]),
        moveable=window["moveable"],
        resizeable=window["resizeable"],
        closeable=window["closeable"],
        desktops=list(window["desktops"]),
        on_all_desktops=window["onAllDesktops"],
    )


def windows(timeout: float = 5.0) -> list[KWinWindow]:
    """Every window KWin lists, of every kind, as raw KWin records in KWin's order.

    Deleted windows (closed, still animating out) are left out: they can no
    longer be focused, placed or closed. The records stay plain JSON so this
    subprocess needs no models; the engine converts them with ``window_from_kwin``.
    """
    return [w for w in collect_windows(timeout) if not w["deleted"]]


_EMPTY_KIND = (
    "kind must name at least one window kind (normal, dialog, utility, popup, shell, other)."
    "\nOmit kind to match every kind."
)
_EMPTY_SELECTOR = (
    "window needs at least one field (id, app_id, caption, active or kind)."
    "\nUse window_list to find a window id."
)


def describe_selector(selector: WindowSelector) -> str:
    """The set fields of a selector, e.g. ``{app_id='kate', kind=['normal']}``."""
    fields: list[tuple[str, object]] = [
        ("id", selector.id),
        ("app_id", selector.app_id),
        ("caption", selector.caption),
        ("active", selector.active),
        ("kind", selector.kind),
    ]
    return "{" + ", ".join(f"{name}={value!r}" for name, value in fields if value is not None) + "}"


def _matches(window: Window, selector: WindowSelector) -> bool:
    if selector.id is not None and window.id != selector.id:
        return False
    if selector.app_id is not None and selector.app_id.lower() not in window.app_id.lower():
        return False
    if selector.caption is not None and selector.caption.lower() not in window.caption.lower():
        return False
    if selector.active is not None and window.active != selector.active:
        return False
    return selector.kind is None or window.kind in selector.kind


def select_windows(windows: list[Window], selector: WindowSelector | None) -> list[Window]:
    """The windows matching every set field of ``selector``, in their original order.

    ``id`` is exact; ``app_id`` (the resource class) and ``caption`` are
    case-insensitive substrings; ``active`` is equality; ``kind`` is
    membership. An unset field, or no selector, matches every window of every
    kind. ``kind=[]`` (only constructible without validation, e.g. from the
    CLI) raises ``ValueError``.
    """
    if selector is None:
        return list(windows)
    if selector.kind is not None and not selector.kind:
        raise ValueError(_EMPTY_KIND)
    return [window for window in windows if _matches(window, selector)]


def select_one(windows: list[Window], selector: WindowSelector) -> Window:
    """The single window a mutator's selector names; ``ValueError`` otherwise.

    The messages teach the next step: an empty selector, no match (an id-only
    selector names the id), or the ambiguous candidates with their ids and
    kinds and how to narrow the selector.
    """
    if selector.is_empty():
        raise ValueError(_EMPTY_SELECTOR)
    matches = select_windows(windows, selector)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        if selector.id is not None and selector.model_copy(update={"id": None}).is_empty():
            msg = f"No window with id {selector.id!r}.\nList windows with window_list."
        else:
            described = describe_selector(selector)
            msg = f"No window matches {described}.\nList windows with window_list."
        raise ValueError(msg)
    candidates = "; ".join(f'{w.id} {w.app_id} "{w.caption}" ({w.kind})' for w in matches)
    msg = (
        f"{len(matches)} windows match {describe_selector(selector)}: {candidates}"
        '\nNarrow the selector: add kind:["normal"] or use id.'
    )
    raise ValueError(msg)


def activate(window_id: str, timeout: float = 5.0) -> dict[str, object]:
    """Activate the window with exactly this id, of any kind.

    Returns ``{"found": bool}``; nothing is activated when no window has the id.
    """
    script = _render_script(_ACTIVATE_SCRIPT_TEMPLATE, {"window_id": window_id})
    payload = json.loads(run_kwin_script(script, timeout))
    if not isinstance(payload, dict) or not isinstance(payload.get("found"), bool):
        msg = f"KWin returned a malformed activation result: {payload!r}"
        raise RuntimeError(msg)
    return payload


def set_geometry(
    window_id: str,
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Set selected frame-geometry fields on exactly one KWin window."""
    requested = {
        "x": x,
        "y": y,
        "width": width,
        "height": height,
    }
    script = _render_script(
        _SET_GEOMETRY_SCRIPT_TEMPLATE, {"window_id": window_id, "requested": requested}
    )

    payload = json.loads(run_kwin_script(script, timeout))
    if not isinstance(payload, dict) or "found" not in payload:
        msg = f"KWin returned malformed geometry result: {payload!r}"
        raise RuntimeError(msg)
    return payload


def close(window_id: str, timeout: float = 5.0) -> dict[str, object]:
    """Ask KWin to close the window with exactly this id.

    Returns ``{"found": False}`` when no window has the id, otherwise
    ``{"found": True, "closeable", "app", "caption"}``.
    """
    script = _render_script(_CLOSE_SCRIPT_TEMPLATE, {"window_id": window_id})
    payload = json.loads(run_kwin_script(script, timeout))
    if not isinstance(payload, dict) or "found" not in payload:
        msg = f"KWin returned a malformed close result: {payload!r}"
        raise RuntimeError(msg)
    return payload


def _is_id_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _valid_desktops(payload: object) -> bool:
    """Whether a desktops() payload has every field and type the engine reads."""
    if not isinstance(payload, dict) or not isinstance(payload.get("desktops"), list):
        return False
    for desktop in payload["desktops"]:
        if not (
            isinstance(desktop, dict)
            and isinstance(desktop.get("id"), str)
            and isinstance(desktop.get("name"), str)
            and isinstance(desktop.get("current"), bool)
        ):
            return False
    if not isinstance(payload.get("current"), str):
        return False
    if "window" not in payload:
        return False
    window = payload["window"]
    return window is None or (
        isinstance(window, dict)
        and _is_id_list(window.get("desktops"))
        and isinstance(window.get("onAllDesktops"), bool)
    )


def _valid_set_desktop(payload: object) -> bool:
    """Whether a set_desktop() payload has every field and type the engine reads."""
    if not (
        isinstance(payload, dict)
        and isinstance(payload.get("windowFound"), bool)
        and isinstance(payload.get("desktopFound"), bool)
        and isinstance(payload.get("current"), str)
    ):
        return False
    if not payload["windowFound"]:
        return True
    if not (
        _is_id_list(payload.get("before")) and isinstance(payload.get("beforeOnAllDesktops"), bool)
    ):
        return False
    refused = payload.get("refused")
    if refused is None:
        return "related" not in payload
    # The guard runs only after both the window and the desktop were found.
    if refused == "unsupported":
        return payload["desktopFound"] and "related" not in payload
    if refused == "related":
        related = payload.get("related")
        return (
            payload["desktopFound"]
            and isinstance(related, list)
            and bool(related)
            and all(
                isinstance(window, dict)
                and isinstance(window.get("id"), str)
                and isinstance(window.get("app"), str)
                and isinstance(window.get("caption"), str)
                and _is_id_list(window.get("desktops"))
                for window in related
            )
        )
    return False


def desktops(window_id: str = "", timeout: float = 5.0) -> dict[str, object]:
    """Ask KWin for its virtual desktops and, optionally, one window's desktops.

    Returns ``{"desktops": [{"id", "name", "current"}], "current": id,
    "window": {"desktops": [id], "onAllDesktops": bool} | None}``; ``window`` is
    None when ``window_id`` is empty or no window (of any kind) has exactly that id.
    """
    script = _render_script(_DESKTOPS_SCRIPT_TEMPLATE, {"window_id": window_id})
    payload = json.loads(run_kwin_script(script, timeout))
    if not _valid_desktops(payload):
        msg = f"KWin returned a malformed desktop list: {payload!r}"
        raise RuntimeError(msg)
    return payload


def set_desktop(
    window_id: str, desktop_id: str, timeout: float = 5.0, *, check_only: bool = False
) -> dict[str, object]:
    """Assign the window with exactly this id to exactly the desktop with this id.

    Returns ``{"windowFound", "desktopFound", "current"}`` and, when the window
    was found, its ``before`` desktop ids and ``beforeOnAllDesktops``. The
    assignment happens only when both were found and KWin would move no other
    window; otherwise ``refused`` is ``"related"`` (with the ``related`` windows
    that would also move) or ``"unsupported"`` (an X11 group transient exists)
    and nothing is assigned. Read an assignment back with ``desktops(window_id)``.
    ``check_only`` runs every check and reports the same fields without assigning,
    so a caller can refuse before changing anything else about the window.
    """
    script = _render_script(
        _SET_DESKTOP_SCRIPT_TEMPLATE,
        {"window_id": window_id, "desktop_id": desktop_id, "check_only": check_only},
    )
    payload = json.loads(run_kwin_script(script, timeout))
    if not _valid_set_desktop(payload):
        msg = f"KWin returned a malformed desktop assignment result: {payload!r}"
        raise RuntimeError(msg)
    return payload


def active_class(timeout: float = 5.0) -> str:
    """Return the active window's resource class, or "" when none is active."""
    return run_kwin_script(_ACTIVE_CLASS_SCRIPT, timeout)


def outputs(timeout: float = 5.0) -> dict[str, object]:
    """Ask KWin for every output's logical geometry and scale.

    Returns ``{"screens": [{"name", "scale", "geometry": [x, y, w, h]}],
    "virtual": [x, y, w, h] | None}`` with geometry in global logical
    coordinates exactly as KWin reports them (qreal, not rounded).
    """
    payload = json.loads(run_kwin_script(_OUTPUTS_SCRIPT, timeout))
    if not isinstance(payload, dict) or not isinstance(payload.get("screens"), list):
        msg = f"KWin returned a malformed output list: {payload!r}"
        raise RuntimeError(msg)
    return payload


def _optional_int(request: Mapping[str, object], key: str) -> int | None:
    value = request.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{key} must be an integer or null, got {value!r}"
        raise ValueError(msg)
    return value


def _dispatch(request: Mapping[str, object]) -> object:
    """Run the operation named by ``request["op"]`` and return its result."""
    op = request.get("op")
    window_id = str(request.get("window_id", ""))
    if op == "windows":
        return windows()
    if op == "activate":
        return activate(window_id)
    if op == "set_geometry":
        return set_geometry(
            window_id=window_id,
            x=_optional_int(request, "x"),
            y=_optional_int(request, "y"),
            width=_optional_int(request, "width"),
            height=_optional_int(request, "height"),
        )
    if op == "desktops":
        return desktops(window_id=window_id)
    if op == "set_desktop":
        return set_desktop(
            window_id=window_id,
            desktop_id=str(request.get("desktop_id", "")),
            check_only=request.get("check_only") is True,
        )
    if op == "close":
        return close(window_id)
    if op == "active_class":
        return active_class()
    if op == "outputs":
        return outputs()
    msg = f"Unknown geometry op: {op!r}"
    raise ValueError(msg)


def main() -> None:
    """Entry point for `python -m kwin_mcp.geometry`: one JSON request on stdin."""
    import dbus

    try:
        request = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        request = {}
    if not isinstance(request, dict):
        request = {}
    try:
        print(json.dumps({"ok": True, "result": _dispatch(request)}))
    except (RuntimeError, ValueError, dbus.DBusException) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))


if __name__ == "__main__":
    main()
