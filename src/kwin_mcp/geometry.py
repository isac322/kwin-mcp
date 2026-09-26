"""Global window geometry via KWin scripting.

AT-SPI2 reports surface-local coordinates because a Wayland client cannot know
where the compositor placed it. KWin does know, and its scripting engine can
hand the numbers back over D-Bus.

Two entry points share one script round-trip:

- ``query()`` / ``active()`` / ``activate()`` / ``close()`` / ``outputs()``
  / ``active_class()`` run in a subprocess (`python -m kwin_mcp.geometry`)
  for the public ``window_geometry`` / ``active_window`` / ``focus_window``
  / ``window_close`` tools, for screenshot coordinate mapping, and for the
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
import sys
import tempfile
import time
from pathlib import Path
from typing import TypedDict

import dbus
import dbus.bus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop

SINK_NAME = "io.github.kwin_mcp.Geometry"
SINK_PATH = "/geometry"


class KWinWindow(TypedDict):
    """One KWin window as reported by the geometry script.

    ``id`` is KWin's ``internalId``: stable for the lifetime of the window and
    never reused, so it can address one window among several of the same app.
    ``frame``/``client`` are ``[x, y, width, height]`` in global logical
    coordinates, rounded once from KWin's qreal values.
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
    out.push({
        pid: w.pid, id: String(w.internalId),
        resourceClass: String(w.resourceClass), caption: w.caption,
        normal: w.normalWindow, popup: w.popupWindow,
        managed: w.managed, deleted: w.deleted,
        desktop: w.desktopWindow, dock: w.dock, notification: w.notification,
        active: w === workspace.activeWindow,
        frame: [f.x, f.y, f.width, f.height],
        client: [c.x, c.y, c.width, c.height]
    });
}
callDBus("{sink}", "{path}", "{sink}", "Report", JSON.stringify(out));
"""

# Activation must go through KWin too: AT-SPI's grab_focus does not raise or
# activate windows on Wayland, so focus_window used to report success while the
# previously active window kept both focus and the foreground.
_ACTIVATE_SCRIPT_TEMPLATE = """
var needle = {needle};
var activated = "";
var windows = workspace.windowList();
for (var i = 0; i < windows.length; i++) {
    var w = windows[i];
    if (!w.normalWindow) {
        continue;
    }
    var haystack = (w.resourceClass + " " + w.caption).toLowerCase();
    if (haystack.indexOf(needle) !== -1) {
        workspace.activeWindow = w;
        activated = w.resourceClass + " " + w.caption;
        break;
    }
}
callDBus("{sink}", "{path}", "{sink}", "Report", activated);
"""

# The id reaches the script only as a JSON string literal ({window_id} is
# replaced with json.dumps output), so a quote or any other character in a
# caller-supplied id stays data and can never run as KWin script.
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


def activate(app_name: str, timeout: float = 5.0) -> str:
    """Activate the first window whose app name or caption matches."""
    script = _ACTIVATE_SCRIPT_TEMPLATE.replace("{needle}", json.dumps(app_name.lower()))
    return run_kwin_script(script, timeout)


def close(window_id: str, timeout: float = 5.0) -> dict[str, object]:
    """Ask KWin to close the window with exactly this id.

    Returns ``{"found": False}`` when no window has the id, otherwise
    ``{"found": True, "closeable", "app", "caption"}``.
    """
    script = _CLOSE_SCRIPT_TEMPLATE.replace("{window_id}", json.dumps(window_id))
    payload = json.loads(run_kwin_script(script, timeout))
    if not isinstance(payload, dict) or "found" not in payload:
        msg = f"KWin returned a malformed close result: {payload!r}"
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
            print(json.dumps({"ok": True, "result": activate(str(request.get("app_name", "")))}))
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
