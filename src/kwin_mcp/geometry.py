"""Global window geometry via KWin scripting.

AT-SPI2 reports surface-local coordinates because a Wayland client cannot know
where the compositor placed it, so element rectangles from `find_ui_elements`
and `accessibility_tree` cannot be clicked directly. KWin does know, and its
scripting engine can hand the numbers back over D-Bus.

The query runs in a subprocess (like the AT-SPI2 queries in `accessibility.py`)
so the GLib main loop it needs never touches the caller's process.

Usage: `echo '{"app_name": "kcalc"}' | python -m kwin_mcp.geometry`
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import dbus
import dbus.bus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop

SINK_NAME = "io.github.kwin_mcp.Geometry"
SINK_PATH = "/geometry"
_FIELD_SEPARATOR = "\x1f"
_RECORD_SEPARATOR = "\x1e"

# KWin scripts run in a sandboxed QJSEngine with no file or socket access, so
# callDBus back into this process is the only way out.
_SCRIPT = f"""
var out = [];
var windows = workspace.windowList();
for (var i = 0; i < windows.length; i++) {{
    var w = windows[i];
    if (!w.normalWindow) {{
        continue;
    }}
    var f = w.frameGeometry;
    var c = w.clientGeometry;
    out.push([
        w.resourceClass, w.caption,
        f.x, f.y, f.width, f.height,
        c.x, c.y, c.width, c.height
    ].join({_FIELD_SEPARATOR!r}));
}}
callDBus("{SINK_NAME}", "{SINK_PATH}", "{SINK_NAME}", "Report",
         out.join({_RECORD_SEPARATOR!r}));
"""

# Activation must go through KWin too: AT-SPI's grab_focus does not raise or
# activate windows on Wayland, so focus_window used to report success while the
# previously active window kept both focus and the foreground.
_ACTIVATE_SCRIPT_TEMPLATE = """
var needle = {needle};
var activated = "";
var windows = workspace.windowList();
for (var i = 0; i < windows.length; i++) {{
    var w = windows[i];
    if (!w.normalWindow) {{
        continue;
    }}
    var haystack = (w.resourceClass + " " + w.caption).toLowerCase();
    if (haystack.indexOf(needle) !== -1) {{
        workspace.activeWindow = w;
        activated = w.resourceClass + " " + w.caption;
        break;
    }}
}}
callDBus("{sink}", "{path}", "{sink}", "Report", activated);
"""


def _parse(payload: str) -> list[dict[str, object]]:
    windows: list[dict[str, object]] = []
    for record in payload.split(_RECORD_SEPARATOR):
        if not record:
            continue
        fields = record.split(_FIELD_SEPARATOR)
        if len(fields) != 10:
            continue
        app, caption, fx, fy, fw, fh, cx, cy, cw, ch = fields
        windows.append(
            {
                "app": app,
                "caption": caption,
                "frame": {"x": int(fx), "y": int(fy), "width": int(fw), "height": int(fh)},
                "client": {"x": int(cx), "y": int(cy), "width": int(cw), "height": int(ch)},
            }
        )
    return windows


def run_kwin_script(js_source: str, timeout: float = 5.0) -> str:
    """Run a KWin script and return the payload it reports back over D-Bus."""
    from gi.repository import GLib

    DBusGMainLoop(set_as_default=True)
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    bus = dbus.bus.BusConnection(address) if address else dbus.SessionBus()
    # Both references must outlive the wait: dropping the BusName releases the
    # well-known name, and dropping the object unexports it, either of which
    # makes KWin's callDBus land nowhere.
    bus_name = dbus.service.BusName(SINK_NAME, bus)
    received: list[str] = []

    class _Sink(dbus.service.Object):
        @dbus.service.method(SINK_NAME, in_signature="s", out_signature="")
        def Report(self, payload: str) -> None:  # noqa: N802 - D-Bus method name
            received.append(str(payload))

    sink = _Sink(bus, SINK_PATH)
    loop = GLib.MainLoop()
    threading.Thread(target=loop.run, daemon=True).start()

    script_path = Path(tempfile.mkdtemp(prefix="kwin-mcp-script-")) / "script.js"
    script_path.write_text(js_source)
    scripting = dbus.Interface(
        bus.get_object("org.kde.KWin", "/Scripting"), "org.kde.kwin.Scripting"
    )
    try:
        scripting.loadScript(str(script_path))
        scripting.start()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not received:
            time.sleep(0.05)
    finally:
        loop.quit()
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


def query(app_name: str = "", timeout: float = 5.0) -> list[dict[str, object]]:
    """Ask KWin for the geometry of every normal window."""
    windows = _parse(run_kwin_script(_SCRIPT, timeout))
    if app_name:
        needle = app_name.lower()
        windows = [w for w in windows if needle in str(w["app"]).lower()]
    return windows


def activate(app_name: str, timeout: float = 5.0) -> str:
    """Activate the first window whose app name or caption matches."""
    script = _ACTIVATE_SCRIPT_TEMPLATE.format(
        needle=json.dumps(app_name.lower()), sink=SINK_NAME, path=SINK_PATH
    )
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
