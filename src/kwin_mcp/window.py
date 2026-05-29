"""KWin scripting backend for window intelligence.

Loads short-lived JavaScript probes into a specific KWin D-Bus session.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
import time
import uuid

import dbus
import dbus.bus
import dbus.mainloop.glib
import dbus.service

_gi_repository = __import__("gi.repository", fromlist=["GLib"])
GLib = _gi_repository.GLib

logger = logging.getLogger("kwin_mcp.window")

JS_LIST_WINDOWS = """
// Inspired by KWin scripting docs, written from scratch for kwin-mcp (MIT)
try {
    var windows = workspace.windowList();
    var payload = [];
    for (var i = 0; i < windows.length; i++) {
        var w = windows[i];
        var geometry = w.frameGeometry;
        payload.push({
            id: String(w.internalId),
            title: w.caption,
            appId: String(w.resourceClass),
            active: w === workspace.activeWindow,
            x: geometry.x,
            y: geometry.y,
            width: geometry.width,
            height: geometry.height
        });
    }
    callDBus(
        "__RESULT_BUS__", "__RESULT_PATH__", "__RESULT_BUS__", "Result",
        JSON.stringify(payload));
} catch (e) {
    /* error */
    var payload = {error: String(e)};
    callDBus(
        "__RESULT_BUS__", "__RESULT_PATH__", "__RESULT_BUS__", "Result",
        JSON.stringify(payload));
}
"""

JS_ACTIVE_WINDOW = """
// Inspired by KWin scripting docs, written from scratch for kwin-mcp (MIT)
try {
    var w = workspace.activeWindow;
    var payload = null;
    if (w) {
        var geometry = w.frameGeometry;
        payload = {
            id: String(w.internalId),
            title: w.caption,
            appId: String(w.resourceClass),
            active: w === workspace.activeWindow,
            x: geometry.x,
            y: geometry.y,
            width: geometry.width,
            height: geometry.height
        };
    }
    callDBus(
        "__RESULT_BUS__", "__RESULT_PATH__", "__RESULT_BUS__", "Result",
        JSON.stringify(payload));
} catch (e) {
    /* error */
    var payload = {error: String(e)};
    callDBus(
        "__RESULT_BUS__", "__RESULT_PATH__", "__RESULT_BUS__", "Result",
        JSON.stringify(payload));
}
"""

JS_GEOMETRY_BY_ID = """
// Inspired by KWin scripting docs, written from scratch for kwin-mcp (MIT)
try {
    var targetId = "__WINDOW_ID__";
    var windows = workspace.windowList();
    var payload = null;
    for (var i = 0; i < windows.length; i++) {
        var w = windows[i];
        if (String(w.internalId) === targetId) {
            var geometry = w.frameGeometry;
            payload = {
                x: geometry.x,
                y: geometry.y,
                width: geometry.width,
                height: geometry.height
            };
            break;
        }
    }
    callDBus(
        "__RESULT_BUS__", "__RESULT_PATH__", "__RESULT_BUS__", "Result",
        JSON.stringify(payload));
} catch (e) {
    /* error */
    var payload = {error: String(e)};
    callDBus(
        "__RESULT_BUS__", "__RESULT_PATH__", "__RESULT_BUS__", "Result",
        JSON.stringify(payload));
}
"""

JS_ACTIVATE_BY_ID = """
// Inspired by KWin scripting docs, written from scratch for kwin-mcp (MIT)
try {
    var targetId = "__WINDOW_ID__";
    var windows = workspace.windowList();
    var payload = "not_found";
    for (var i = 0; i < windows.length; i++) {
        var w = windows[i];
        if (String(w.internalId) === targetId) {
            workspace.activeWindow = w;
            payload = "OK";
            break;
        }
    }
    callDBus(
        "__RESULT_BUS__", "__RESULT_PATH__", "__RESULT_BUS__", "Result",
        JSON.stringify(payload));
} catch (e) {
    /* error */
    var payload = {error: String(e)};
    callDBus(
        "__RESULT_BUS__", "__RESULT_PATH__", "__RESULT_BUS__", "Result",
        JSON.stringify(payload));
}
"""

JS_CLOSE_BY_ID = """
// Inspired by KWin scripting docs, written from scratch for kwin-mcp (MIT)
try {
    var targetId = "__WINDOW_ID__";
    var windows = workspace.windowList();
    var payload = "not_found";
    for (var i = 0; i < windows.length; i++) {
        var w = windows[i];
        if (String(w.internalId) === targetId) {
            w.closeWindow();
            payload = "OK";
            break;
        }
    }
    callDBus(
        "__RESULT_BUS__", "__RESULT_PATH__", "__RESULT_BUS__", "Result",
        JSON.stringify(payload));
} catch (e) {
    /* error */
    var payload = {error: String(e)};
    callDBus(
        "__RESULT_BUS__", "__RESULT_PATH__", "__RESULT_BUS__", "Result",
        JSON.stringify(payload));
}
"""


class KWinScriptingBackend:
    """Execute short-lived KWin JavaScript probes on a specific session bus."""

    def __init__(self, dbus_address: str) -> None:
        self._dbus_address = dbus_address

    def run_script(self, js: str, timeout: float = 5.0) -> object:
        """Load, run, and unload a KWin script, returning the parsed JSON result."""
        unique_bus = f"org.kwin_mcp.script.s{uuid.uuid4().hex}"
        result_path = "/Result"
        rendered_js = js.replace("__RESULT_BUS__", unique_bus).replace(
            "__RESULT_PATH__", result_path
        )

        name = f"kwin_mcp_script_{uuid.uuid4().hex}"
        fd, temp_path = tempfile.mkstemp(prefix="kwin_mcp_script_", suffix=".js")
        try:
            os.write(fd, rendered_js.encode())
        finally:
            os.close(fd)

        # Strategy (a): DBusGMainLoop + scoped GLib.MainContext.default().iteration().
        # We never start a global mainloop (would block forever and starve other
        # Pool-spawned background activity); instead we poll non-blocking iterations
        # until the listener's threading.Event fires or the timeout elapses.
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

        event = threading.Event()
        payload_holder: list[str | None] = [None]

        class _ResultListener(dbus.service.Object):
            def __init__(self, conn: dbus.bus.BusConnection, path: str) -> None:
                super().__init__(conn, path)
                self._event = event
                self._payload_holder = payload_holder

            @dbus.service.method(unique_bus, in_signature="s", out_signature="")
            def Result(self, payload: str) -> None:  # noqa: N802
                self._payload_holder[0] = str(payload)
                self._event.set()

        listener_bus = dbus.bus.BusConnection(self._dbus_address)
        try:
            _bus_name = dbus.service.BusName(unique_bus, listener_bus)
            listener_obj = _ResultListener(listener_bus, result_path)
            try:
                bus = dbus.bus.BusConnection(self._dbus_address)
                script_id = int(
                    bus.call_blocking(
                        "org.kde.KWin",
                        "/Scripting",
                        "org.kde.kwin.Scripting",
                        "loadScript",
                        "ss",
                        (temp_path, name),
                        timeout=timeout,
                    )
                )
                if script_id == -1:
                    msg = f"KWin loadScript failed for {name}"
                    raise RuntimeError(msg)

                try:
                    bus.call_blocking(
                        "org.kde.KWin",
                        f"/{script_id}",
                        "org.kde.kwin.Script",
                        "run",
                        "",
                        (),
                        timeout=timeout,
                    )
                except dbus.DBusException as exc:
                    if exc.get_dbus_name() != "org.freedesktop.DBus.Error.UnknownObject":
                        raise
                    logger.debug("script path /%s unavailable, trying KWin 6 path", script_id)
                    bus.call_blocking(
                        "org.kde.KWin",
                        f"/Scripting/Script{script_id}",
                        "org.kde.kwin.Script",
                        "run",
                        "",
                        (),
                        timeout=timeout,
                    )

                ctx = GLib.MainContext.default()
                deadline = time.monotonic() + timeout
                while not event.is_set():
                    ctx.iteration(may_block=False)
                    if event.is_set():
                        break
                    if time.monotonic() >= deadline:
                        msg = f"KWin script {name} did not publish a result within {timeout}s"
                        raise TimeoutError(msg)
                    time.sleep(0.005)

                payload_str = payload_holder[0]
                if payload_str is None:
                    msg = f"KWin script {name} did not publish a result within {timeout}s"
                    raise TimeoutError(msg)

                parsed = json.loads(payload_str)
                if isinstance(parsed, dict) and "error" in parsed:
                    raise RuntimeError(str(parsed["error"]))
                return parsed
            finally:
                with contextlib.suppress(Exception):
                    listener_obj.remove_from_connection()
                del _bus_name
        finally:
            with contextlib.suppress(Exception):
                listener_bus.close()
            with contextlib.suppress(Exception):
                cleanup_bus = dbus.bus.BusConnection(self._dbus_address)
                cleanup_obj = cleanup_bus.get_object("org.kde.KWin", "/Scripting")
                cleanup_iface = dbus.Interface(cleanup_obj, "org.kde.kwin.Scripting")
                cleanup_iface.unloadScript(name, timeout=timeout)
            with contextlib.suppress(OSError):
                os.unlink(temp_path)


__all__ = [
    "JS_ACTIVATE_BY_ID",
    "JS_ACTIVE_WINDOW",
    "JS_CLOSE_BY_ID",
    "JS_GEOMETRY_BY_ID",
    "JS_LIST_WINDOWS",
    "KWinScriptingBackend",
]
