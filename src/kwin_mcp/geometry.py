"""Global window geometry via KWin scripting.

AT-SPI2 reports surface-local coordinates because a Wayland client cannot know
where the compositor placed it. KWin does know, and its scripting engine can
hand the numbers back over D-Bus.

Two entry points share one script round-trip:

- ``query()`` / ``active()`` / ``activate()`` / ``close()`` / ``desktops()``
  / ``set_desktop()`` / ``outputs()`` / ``active_class()`` run in a subprocess
  (`python -m kwin_mcp.geometry`) for the public ``window_geometry`` /
  ``active_window`` / ``focus_window`` / ``window_close`` /
  ``virtual_desktops`` / ``window_set_desktop`` tools, for screenshot
  coordinate mapping, and for the
  paste chord ``keyboard_type_unicode`` chooses.
- ``collect_windows()`` runs in-process inside the AT-SPI2 query subprocess so
  `accessibility.py` can translate element rectangles into screen coordinates.

Replies are always dispatched on the calling thread through the default GLib
main context. A background GLib loop thread races libatspi's use of the same
context and was observed to abort the process with heap corruption.

Usage: `echo '{"app_name": "kcalc"}' | python -m kwin_mcp.geometry`
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

import dbus
import dbus.bus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop

if TYPE_CHECKING:
    from collections.abc import Mapping

SINK_NAME = "io.github.kwin_mcp.Geometry"
SINK_PATH = "/geometry"


class KWinWindow(TypedDict):
    """One KWin window as reported by the geometry script.

    ``id`` is KWin's ``internalId``: stable for the lifetime of the window and
    never reused, so it can address one window among several of the same app.
    ``frame``/``client``/``buffer`` are ``[x, y, width, height]`` in global
    logical coordinates, rounded once from KWin's qreal values. ``buffer`` is
    the client's whole surface, including the shadow a client-side decorated
    window (Chromium, for example) draws around itself.
    """

    pid: int
    id: str
    app: str
    caption: str
    normal: bool
    popup: bool
    managed: bool
    deleted: bool
    desktop: bool
    dock: bool
    notification: bool
    active: bool
    frame: list[int]
    client: list[int]
    buffer: list[int]


# KWin scripts run in a sandboxed QJSEngine with no file or socket access, so
# callDBus back into this process is the only way out. The payload is JSON so
# that both consumers (the public geometry report and the accessibility
# coordinate mapper) can pick the fields they need without positional parsing.
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
    out.push({
        pid: w.pid, id: String(w.internalId),
        resourceClass: String(w.resourceClass), caption: w.caption,
        normal: w.normalWindow, popup: w.popupWindow,
        managed: w.managed, deleted: w.deleted,
        desktop: w.desktopWindow, dock: w.dock, notification: w.notification,
        active: w === workspace.activeWindow,
        frame: [f.x, f.y, f.width, f.height],
        client: [c.x, c.y, c.width, c.height],
        buffer: [b.x, b.y, b.width, b.height]
    });
}
callDBus("{sink}", "{path}", "{sink}", "Report", JSON.stringify(out));
"""

# Activation must go through KWin too: AT-SPI's grab_focus does not raise or
# activate windows on Wayland, so focus_window used to report success while the
# previously active window kept both focus and the foreground.
_ACTIVATE_SCRIPT_TEMPLATE = """
var needle = {needle};
var target = {window_id};
var activated = "";
var windows = workspace.windowList();
for (var i = 0; i < windows.length; i++) {
    var w = windows[i];
    if (!w.normalWindow) {
        continue;
    }
    var haystack = (w.resourceClass + " " + w.caption).toLowerCase();
    var matchesId = target && String(w.internalId) === target;
    var matchesName = !target && needle && haystack.indexOf(needle) !== -1;
    if (matchesId || matchesName) {
        workspace.activeWindow = w;
        activated = w.resourceClass + " " + w.caption;
        break;
    }
}
callDBus("{sink}", "{path}", "{sink}", "Report", activated);
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

# The active window's resource class (the Wayland app_id, or the X11 WM_CLASS
# class for Xwayland clients). keyboard_type_unicode picks its paste chord from
# it before pressing anything. An empty string means no window is active.
_ACTIVE_CLASS_SCRIPT = """
var w = workspace.activeWindow;
callDBus("{sink}", "{path}", "{sink}", "Report", w ? String(w.resourceClass) : "");
"""

# Virtual desktops in KWin's order, with the current one, addressed by their
# stable id (a UUID string), never by position. When {window_id} is not empty the
# report also carries that window's desktop ids, read in this separate script so
# window_set_desktop observes the state KWin committed after its own script ran.
# The window population is the one window_geometry reports (normal windows).
# In KWin 6 a window on all desktops has an empty desktops list.
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
        if (!w.normalWindow || String(w.internalId) !== target) {
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

# Assign the requested window, by exact internalId, to exactly one virtual desktop,
# by exact id. Nothing is assigned unless both are found. The script does not
# write currentDesktop or activate or raise the window.
#
# KWin's Window::setDesktops (checked at v6.3.6 and unchanged through 6.7) also
# sets the desktops of every transient of the window, recursively, and, when the
# window is modal, of its main windows. The script checks those relationships
# across workspace.windowList() at request time and refuses before assignment
# if an enumerated related window is not already on the requested desktop.
#
# Preflight refusal covers only relationships KWin scripting can enumerate.
# Linked but unpainted or unlisted children may follow their parent; when they
# first appear, KWin may switch the current desktop. Scripts see transientFor
# and modal but not transients() or mainWindows(); an X11 group transient
# (transient, no transientFor) links to main windows scripts cannot see, so while
# one exists the request is refused too. Nothing is moved back afterwards.
_SET_DESKTOP_SCRIPT_TEMPLATE = """
var target = {window_id};
var wanted = {desktop_id};
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
    if (!w.normalWindow || String(w.internalId) !== target) {
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
        } else {
            w.desktops = [desktop];
        }
    }
    break;
}
callDBus("{sink}", "{path}", "{sink}", "Report", JSON.stringify(result));
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
            windows.append(
                KWinWindow(
                    pid=int(record["pid"]),
                    id=str(record["id"]),
                    app=str(record["resourceClass"]),
                    caption=str(record["caption"]),
                    normal=bool(record["normal"]),
                    popup=bool(record["popup"]),
                    managed=bool(record["managed"]),
                    deleted=bool(record["deleted"]),
                    desktop=bool(record["desktop"]),
                    dock=bool(record["dock"]),
                    notification=bool(record["notification"]),
                    active=bool(record["active"]),
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
    """Ask KWin for every window in-process, for the accessibility mapper.

    Unlike ``query()`` this keeps popups, unmanaged and non-normal windows:
    the mapper needs the full list to prove that an AT-SPI top-level matches
    exactly one compositor window.
    """
    return _parse_windows(run_kwin_script(_SCRIPT_TEMPLATE, timeout))


def _report(window: KWinWindow) -> dict[str, object]:
    """Shape one window for the public geometry report."""
    return {
        "id": window["id"],
        "app": window["app"],
        "caption": window["caption"],
        "active": window["active"],
        "frame": {
            "x": window["frame"][0],
            "y": window["frame"][1],
            "width": window["frame"][2],
            "height": window["frame"][3],
        },
        "client": {
            "x": window["client"][0],
            "y": window["client"][1],
            "width": window["client"][2],
            "height": window["client"][3],
        },
    }


def query(app_name: str = "", window_id: str = "", timeout: float = 5.0) -> list[dict[str, object]]:
    """Ask KWin for the geometry of every normal window.

    ``app_name`` is a case-insensitive substring of the app name; ``window_id``
    must equal a window id exactly. Both filters apply when both are given.
    """
    windows = [w for w in collect_windows(timeout) if w["normal"]]
    if app_name:
        needle = app_name.lower()
        windows = [w for w in windows if needle in w["app"].lower()]
    if window_id:
        windows = [w for w in windows if w["id"] == window_id]
    return [_report(w) for w in windows]


def active(timeout: float = 5.0) -> dict[str, object] | None:
    """Return the window KWin currently treats as active, or None."""
    for window in collect_windows(timeout):
        if window["active"]:
            return _report(window)
    return None


def activate(
    app_name: str = "",
    window_id: str = "",
    timeout: float = 5.0,
) -> str:
    """Activate one window by exact id, or by app name/caption."""
    script = _render_script(
        _ACTIVATE_SCRIPT_TEMPLATE, {"needle": app_name.lower(), "window_id": window_id}
    )
    return run_kwin_script(script, timeout)


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
    None when ``window_id`` is empty or no window has exactly that id.
    """
    script = _render_script(_DESKTOPS_SCRIPT_TEMPLATE, {"window_id": window_id})
    payload = json.loads(run_kwin_script(script, timeout))
    if not _valid_desktops(payload):
        msg = f"KWin returned a malformed desktop list: {payload!r}"
        raise RuntimeError(msg)
    return payload


def set_desktop(window_id: str, desktop_id: str, timeout: float = 5.0) -> dict[str, object]:
    """Assign the window with exactly this id to exactly the desktop with this id.

    Returns ``{"windowFound", "desktopFound", "current"}`` and, when the window
    was found, its ``before`` desktop ids and ``beforeOnAllDesktops``. The
    assignment happens only when both were found and no enumerated related window
    would move; otherwise ``refused`` is ``"related"`` (with the ``related`` windows
    that would also move) or ``"unsupported"`` (an X11 group transient exists)
    and nothing is assigned. Preflight refusal covers only relationships KWin
    scripting can enumerate at request time. Linked but unpainted or unlisted
    children may follow their parent, and KWin may switch the current desktop
    when they first appear. Read the selected window's membership back with
    ``desktops(window_id)``; the reported current desktop is a point-in-time observation.
    """
    script = _render_script(
        _SET_DESKTOP_SCRIPT_TEMPLATE, {"window_id": window_id, "desktop_id": desktop_id}
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


def main() -> None:
    """Entry point for `python -m kwin_mcp.geometry`."""
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        request = {}
    try:
        op = request.get("op")
        if op == "activate":
            print(
                json.dumps(
                    {
                        "ok": True,
                        "result": activate(
                            app_name=str(request.get("app_name", "")),
                            window_id=str(request.get("window_id", "")),
                        ),
                    }
                )
            )
            return
        if op == "outputs":
            print(json.dumps({"ok": True, "result": outputs()}))
            return
        if op == "active":
            print(json.dumps({"ok": True, "result": active()}))
            return
        if op == "active_class":
            print(json.dumps({"ok": True, "result": active_class()}))
            return
        if op == "set_geometry":
            print(
                json.dumps(
                    {
                        "ok": True,
                        "result": set_geometry(
                            window_id=str(request.get("window_id", "")),
                            x=request.get("x"),
                            y=request.get("y"),
                            width=request.get("width"),
                            height=request.get("height"),
                        ),
                    }
                )
            )
            return
        if op == "desktops":
            print(
                json.dumps(
                    {"ok": True, "result": desktops(window_id=str(request.get("window_id", "")))}
                )
            )
            return
        if op == "set_desktop":
            print(
                json.dumps(
                    {
                        "ok": True,
                        "result": set_desktop(
                            window_id=str(request.get("window_id", "")),
                            desktop_id=str(request.get("desktop_id", "")),
                        ),
                    }
                )
            )
            return
        if op == "close":
            print(json.dumps({"ok": True, "result": close(str(request.get("window_id", "")))}))
            return
        result = query(
            app_name=str(request.get("app_name", "")),
            window_id=str(request.get("window_id", "")),
        )
        print(json.dumps({"ok": True, "result": result}))
    except (RuntimeError, ValueError, dbus.DBusException) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))


if __name__ == "__main__":
    main()
