"""Global window geometry via KWin scripting.

AT-SPI2 reports surface-local coordinates because a Wayland client cannot know
where the compositor placed it. KWin does know, and its scripting engine can
hand the numbers back over D-Bus.

Two entry points share one script round-trip:

- ``query()`` / ``activate()`` run in a subprocess (`python -m kwin_mcp.geometry`)
  for the public ``window_geometry`` / ``focus_window`` tools.
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


def query(app_name: str = "", timeout: float = 5.0) -> list[dict[str, object]]:
    """Ask KWin for the geometry of every normal window."""
    windows: list[dict[str, object]] = [
        {
            "app": w["app"],
            "caption": w["caption"],
            "frame": {
                "x": w["frame"][0],
                "y": w["frame"][1],
                "width": w["frame"][2],
                "height": w["frame"][3],
            },
            "client": {
                "x": w["client"][0],
                "y": w["client"][1],
                "width": w["client"][2],
                "height": w["client"][3],
            },
        }
        for w in collect_windows(timeout)
        if w["normal"]
    ]
    if app_name:
        needle = app_name.lower()
        windows = [w for w in windows if needle in str(w["app"]).lower()]
    return windows


def activate(app_name: str, timeout: float = 5.0) -> str:
    """Activate the first window whose app name or caption matches."""
    script = _ACTIVATE_SCRIPT_TEMPLATE.replace("{needle}", json.dumps(app_name.lower()))
    return run_kwin_script(script, timeout)


def main() -> None:
    """Entry point for `python -m kwin_mcp.geometry`."""
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        request = {}
    try:
        if request.get("op") == "activate":
            print(json.dumps({"ok": True, "result": activate(str(request.get("app_name", "")))}))
            return
        print(json.dumps({"ok": True, "result": query(app_name=str(request.get("app_name", "")))}))
    except (RuntimeError, dbus.DBusException) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))


if __name__ == "__main__":
    main()
