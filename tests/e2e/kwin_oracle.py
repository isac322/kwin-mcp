"""Independent KWin oracle: window, desktop and cursor state read by the test itself.

Expected values in window tests must not come from the tool under test. This
module therefore shares no code with ``kwin_mcp`` (``tests/unit/test_suite_policy.py``
enforces that): it loads its own KWin script through ``org.kde.kwin.Scripting`` on
a test-owned D-Bus connection in a child process, and the script reports back
with ``callDBus`` to a well-known name that child owns.

Readers: ``windows()``, ``window(id)``, ``active_window()``, ``desktops()``,
``current_desktop()``, ``cursor_pos()``. Scene setters (``activate``,
``clear_active``, ``set_frame``, ``close``) exist only to arrange a situation
before the tool under test runs; their effect is again verified through the
readers, never through kwin-mcp.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# Runs in a child process so the GLib main loop, the owned bus name and the
# PyGObject state never touch the test process (or an engine living in it).
_RUNNER = r"""
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

address = sys.argv[1]
timeout_s = float(sys.argv[2])
request = json.loads(sys.stdin.read())

flags = (
    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
    | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
)
conn = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
token = uuid.uuid4().hex[:12]
name = f"org.kwinmcp.TestOracle.p{os.getpid()}x{token}"
object_path = "/org/kwinmcp/TestOracle"
owned = conn.call_sync(
    "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
    "RequestName", GLib.Variant("(su)", (name, 4)), GLib.VariantType.new("(u)"),
    Gio.DBusCallFlags.NONE, 5000, None,
).unpack()[0]
if owned != 1:
    sys.exit(f"could not own {name}: RequestName returned {owned}")

xml = (
    f'<node><interface name="{name}"><method name="Report">'
    '<arg type="s" direction="in"/></method></interface></node>'
)
interface = Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
received = []


def on_call(connection, sender, path, iface, method, parameters, invocation):
    received.append(parameters.unpack()[0])
    invocation.return_value(None)


registration = conn.register_object(object_path, interface, on_call, None, None)

# The helpers stay outside the try block: function declarations inside a block
# are not portable across QJSEngine versions.
script = (
    "(function () {\n"
    f"var __sink = {json.dumps(name)};\n"
    f"var __path = {json.dumps(object_path)};\n"
    "function report(value) {\n"
    '    callDBus(__sink, __path, __sink, "Report", JSON.stringify({ok: true, value: value}));\n'
    "}\n"
    + request["helpers"]
    + "\ntry {\n"
    + request["body"]
    + "\n} catch (e) {\n"
    '    callDBus(__sink, __path, __sink, "Report",\n'
    "        JSON.stringify({ok: false, error: String(e)}));\n"
    "}\n"
    "})();\n"
)
directory = Path(tempfile.mkdtemp(prefix="kwin-mcp-oracle-"))
script_path = directory / "oracle.js"
script_path.write_text(script, encoding="utf-8")
plugin = f"kwin-mcp-test-oracle-{token}"


def scripting(method, params, reply):
    return conn.call_sync(
        "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", method, params,
        None if reply is None else GLib.VariantType.new(reply),
        Gio.DBusCallFlags.NONE, 5000, None,
    )


try:
    script_id = scripting("loadScript", GLib.Variant("(ss)", (str(script_path), plugin)), "(i)")
    if script_id.unpack()[0] < 0:
        sys.exit("KWin refused to load the oracle script")
    scripting("start", None, None)
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout_s
    while not received and time.monotonic() < deadline:
        if not context.iteration(False):
            time.sleep(0.005)
finally:
    try:
        scripting("unloadScript", GLib.Variant("(s)", (plugin,)), "(b)")
    except GLib.Error:
        pass
    conn.unregister_object(registration)
    script_path.unlink(missing_ok=True)
    directory.rmdir()

if not received:
    sys.exit(f"KWin did not run the oracle script within {timeout_s:.1f} s")
sys.stdout.write(received[0])
"""

_HELPERS = r"""
function prop(o, key) {
    try {
        var v = o[key];
        return v === undefined ? null : v;
    } catch (e) {
        return null;
    }
}
function rect(r) {
    return r ? {x: r.x, y: r.y, width: r.width, height: r.height} : null;
}
var FLAGS = [
    "normalWindow", "dialog", "utility", "popupWindow", "desktopWindow", "dock",
    "notification", "criticalNotification", "onScreenDisplay", "appletPopup", "tooltip",
    "popupMenu", "dropdownMenu", "comboBox", "splash", "toolbar", "specialWindow",
    "transient", "modal", "minimized", "moveable", "resizeable", "closeable", "managed",
    "deleted", "onAllDesktops", "keepAbove", "fullScreen", "skipTaskbar"
];
function describe(w) {
    var flags = [];
    for (var i = 0; i < FLAGS.length; i++) {
        if (prop(w, FLAGS[i])) {
            flags.push(FLAGS[i]);
        }
    }
    var desktops = [];
    var list = prop(w, "desktops") || [];
    for (var j = 0; j < list.length; j++) {
        desktops.push(String(list[j].id));
    }
    var parent = prop(w, "transientFor");
    var pid = prop(w, "pid");
    var type = prop(w, "windowType");
    return {
        id: String(w.internalId),
        resourceClass: String(prop(w, "resourceClass") || ""),
        resourceName: String(prop(w, "resourceName") || ""),
        caption: String(prop(w, "caption") || ""),
        pid: pid === null ? null : Number(pid),
        active: w === workspace.activeWindow,
        transientFor: parent ? String(parent.internalId) : null,
        frame: rect(prop(w, "frameGeometry")),
        client: rect(prop(w, "clientGeometry")),
        desktops: desktops,
        windowType: type === null ? null : Number(type),
        flags: flags
    };
}
function findWindow(id) {
    var windows = workspace.windowList();
    for (var i = 0; i < windows.length; i++) {
        if (String(windows[i].internalId) === id) {
            return windows[i];
        }
    }
    return null;
}
"""


class KWinOracleError(RuntimeError):
    """The oracle script could not run or reported an exception."""


@dataclass(frozen=True)
class Rect:
    """A KWin rectangle in global logical coordinates (KWin may report fractions)."""

    x: float
    y: float
    width: float
    height: float

    def as_ints(self) -> tuple[int, int, int, int]:
        return round(self.x), round(self.y), round(self.width), round(self.height)


@dataclass(frozen=True)
class OracleWindow:
    """One entry of ``workspace.windowList()`` as KWin's scripting API reports it.

    ``id`` is ``String(window.internalId)`` verbatim. ``flags`` holds every
    truthy boolean property among the window-type and state properties read by
    the script (``normalWindow``, ``dialog``, ``closeable``, ...).
    """

    id: str
    resource_class: str
    resource_name: str
    caption: str
    pid: int | None
    active: bool
    transient_for: str | None
    frame: Rect | None
    client: Rect | None
    desktops: tuple[str, ...]
    window_type: int | None
    flags: frozenset[str]

    @property
    def normal(self) -> bool:
        return "normalWindow" in self.flags

    @property
    def dialog(self) -> bool:
        return "dialog" in self.flags

    @property
    def utility(self) -> bool:
        return "utility" in self.flags

    @property
    def popup(self) -> bool:
        return "popupWindow" in self.flags

    @property
    def shell(self) -> bool:
        return bool(self.flags & {"desktopWindow", "dock", "notification"})

    @property
    def modal(self) -> bool:
        return "modal" in self.flags

    @property
    def moveable(self) -> bool:
        return "moveable" in self.flags

    @property
    def resizeable(self) -> bool:
        return "resizeable" in self.flags

    @property
    def closeable(self) -> bool:
        return "closeable" in self.flags

    @property
    def on_all_desktops(self) -> bool:
        return "onAllDesktops" in self.flags

    @property
    def kind(self) -> str:
        """The 1.0 ``Window.kind`` this window must have, from the design's mapping.

        normalWindow -> normal, dialog -> dialog, utility -> utility,
        popupWindow -> popup, desktopWindow | dock | notification -> shell,
        anything else -> other; checked in that order.
        """
        if self.normal:
            return "normal"
        if self.dialog:
            return "dialog"
        if self.utility:
            return "utility"
        if self.popup:
            return "popup"
        if self.shell:
            return "shell"
        return "other"


@dataclass(frozen=True)
class OracleDesktop:
    """A virtual desktop: KWin's stable id, its name and the X11 number (1-based)."""

    id: str
    name: str
    x11_number: int | None


def _rect(raw: object) -> Rect | None:
    if not isinstance(raw, dict):
        return None
    return Rect(float(raw["x"]), float(raw["y"]), float(raw["width"]), float(raw["height"]))


def _window(raw: object) -> OracleWindow:
    if not isinstance(raw, dict):
        msg = f"unexpected window record from KWin: {raw!r}"
        raise KWinOracleError(msg)
    pid = raw.get("pid")
    window_type = raw.get("windowType")
    transient_for = raw.get("transientFor")
    return OracleWindow(
        id=str(raw["id"]),
        resource_class=str(raw["resourceClass"]),
        resource_name=str(raw["resourceName"]),
        caption=str(raw["caption"]),
        pid=int(pid) if isinstance(pid, int | float) else None,
        active=bool(raw["active"]),
        transient_for=str(transient_for) if transient_for is not None else None,
        frame=_rect(raw.get("frame")),
        client=_rect(raw.get("client")),
        desktops=tuple(str(d) for d in raw.get("desktops") or []),
        window_type=int(window_type) if isinstance(window_type, int | float) else None,
        flags=frozenset(str(flag) for flag in raw.get("flags") or []),
    )


class KWinOracle:
    """Reads KWin state on the session bus at ``dbus_address`` with test-owned scripts."""

    def __init__(self, dbus_address: str, *, timeout_s: float = 10.0) -> None:
        self._dbus_address = dbus_address
        self._timeout_s = timeout_s

    def run(self, body: str) -> object:
        """Run ``body`` as a KWin script and return the JSON value it passed to ``report``.

        The script body must call ``report(value)`` exactly once. The helper
        functions ``prop``, ``rect``, ``describe`` and ``findWindow`` are in scope.
        An exception inside the script raises ``KWinOracleError``.
        """
        try:
            result = subprocess.run(
                [sys.executable, "-c", _RUNNER, self._dbus_address, str(self._timeout_s)],
                input=json.dumps({"helpers": _HELPERS, "body": body}),
                capture_output=True,
                text=True,
                timeout=self._timeout_s + 15,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            msg = f"KWin oracle runner hung for {exc.timeout:.0f} s"
            raise KWinOracleError(msg) from exc
        if result.returncode != 0:
            msg = f"KWin oracle runner failed ({result.returncode}): {result.stderr.strip()}"
            raise KWinOracleError(msg)
        envelope = json.loads(result.stdout)
        if not isinstance(envelope, dict) or not envelope.get("ok"):
            error = envelope.get("error") if isinstance(envelope, dict) else envelope
            msg = f"KWin oracle script raised: {error}"
            raise KWinOracleError(msg)
        return envelope.get("value")

    def windows(self) -> list[OracleWindow]:
        """Every window in ``workspace.windowList()``, in KWin's order."""
        raw = self.run(
            "var out = [];\n"
            "var windows = workspace.windowList();\n"
            "for (var i = 0; i < windows.length; i++) { out.push(describe(windows[i])); }\n"
            "report(out);"
        )
        if not isinstance(raw, list):
            msg = f"unexpected window list from KWin: {raw!r}"
            raise KWinOracleError(msg)
        return [_window(entry) for entry in raw]

    def window(self, window_id: str) -> OracleWindow | None:
        """The window whose ``internalId`` string equals ``window_id``, or None."""
        raw = self.run(
            f"var w = findWindow({json.dumps(window_id)});\nreport(w ? describe(w) : null);"
        )
        return None if raw is None else _window(raw)

    def active_window(self) -> OracleWindow | None:
        raw = self.run("var w = workspace.activeWindow;\nreport(w ? describe(w) : null);")
        return None if raw is None else _window(raw)

    def _desktops(self) -> tuple[list[OracleDesktop], str]:
        raw = self.run(
            "var out = [];\n"
            "var list = workspace.desktops;\n"
            "for (var i = 0; i < list.length; i++) {\n"
            "    var d = list[i];\n"
            "    out.push({id: String(d.id), name: String(d.name),"
            " x11: prop(d, 'x11DesktopNumber')});\n"
            "}\n"
            "var current = workspace.currentDesktop;\n"
            "report({desktops: out, current: current ? String(current.id) : null});"
        )
        if not isinstance(raw, dict) or not isinstance(raw.get("desktops"), list):
            msg = f"unexpected desktop list from KWin: {raw!r}"
            raise KWinOracleError(msg)
        desktops = [
            OracleDesktop(
                id=str(entry["id"]),
                name=str(entry["name"]),
                x11_number=int(entry["x11"]) if isinstance(entry.get("x11"), int | float) else None,
            )
            for entry in raw["desktops"]
        ]
        return desktops, str(raw.get("current"))

    def desktops(self) -> list[OracleDesktop]:
        """``workspace.desktops`` in KWin's order."""
        return self._desktops()[0]

    def current_desktop(self) -> OracleDesktop:
        desktops, current = self._desktops()
        for desktop in desktops:
            if desktop.id == current:
                return desktop
        msg = f"KWin's current desktop {current!r} is not in its desktop list"
        raise KWinOracleError(msg)

    def cursor_pos(self) -> tuple[float, float]:
        """``workspace.cursorPos`` in global logical coordinates."""
        raw = self.run("var p = workspace.cursorPos;\nreport({x: p.x, y: p.y});")
        if not isinstance(raw, dict):
            msg = f"unexpected cursor position from KWin: {raw!r}"
            raise KWinOracleError(msg)
        return float(raw["x"]), float(raw["y"])

    def wait_for_windows(
        self,
        predicate: Callable[[list[OracleWindow]], bool],
        *,
        timeout_s: float = 15.0,
        interval_s: float = 0.2,
    ) -> list[OracleWindow]:
        """Poll ``windows()`` until ``predicate`` holds; raise ``AssertionError`` on timeout."""
        deadline = time.monotonic() + timeout_s
        current = self.windows()
        while not predicate(current):
            if time.monotonic() >= deadline:
                listing = [(w.resource_class, w.caption, w.kind) for w in current]
                msg = f"KWin windows never satisfied the predicate: {listing}"
                raise AssertionError(msg)
            time.sleep(interval_s)
            current = self.windows()
        return current

    # ── scene setters (arrange only; verify through the readers) ──────────

    def activate(self, window_id: str) -> bool:
        """Ask KWin to activate a window; False when no such window exists."""
        raw = self.run(
            f"var w = findWindow({json.dumps(window_id)});\n"
            "if (w) { workspace.activeWindow = w; }\n"
            "report(w !== null);"
        )
        return bool(raw)

    def clear_active(self) -> None:
        """Set ``workspace.activeWindow`` to null (KWin may refuse; read it back)."""
        self.run("workspace.activeWindow = null;\nreport(true);")

    def set_frame(self, window_id: str, frame: Rect) -> bool:
        """Assign a window's frame geometry; False when no such window exists."""
        geometry = json.dumps(
            {"x": frame.x, "y": frame.y, "width": frame.width, "height": frame.height}
        )
        raw = self.run(
            f"var w = findWindow({json.dumps(window_id)});\n"
            f"if (w) {{ w.frameGeometry = {geometry}; }}\n"
            "report(w !== null);"
        )
        return bool(raw)

    def close(self, window_id: str) -> bool:
        """Ask a window to close like its titlebar button; False when it does not exist."""
        raw = self.run(
            f"var w = findWindow({json.dumps(window_id)});\n"
            "if (w) { w.closeWindow(); }\n"
            "report(w !== null);"
        )
        return bool(raw)
