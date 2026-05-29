#!/usr/bin/env bash
set -u
set -o pipefail

SOCKET_NAME="wayland-kwin-probe-$$"
WIDTH=1280
HEIGHT=720

echo "KWin scripting probe"
echo "kwin_version=$(kwin_wayland --version 2>&1 | tr '\n' ' ')"
echo "socket=${SOCKET_NAME}"

dbus-run-session bash -s -- "$SOCKET_NAME" "$WIDTH" "$HEIGHT" <<'SESSION'
set -u
set -o pipefail

SOCKET_NAME="$1"
WIDTH="$2"
HEIGHT="$3"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

export KDE_FULL_SESSION=true
export KDE_SESSION_VERSION=6
export XDG_SESSION_TYPE=wayland
export XDG_CURRENT_DESKTOP=KDE
export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
export QT_ACCESSIBILITY=1
export ATSPI_DBUS_IMPLEMENTATION=dbus-daemon
export KWIN_WAYLAND_NO_PERMISSION_CHECKS=1
export KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1

KWIN_PID=""
ATSPI_PID=""
KCALC_PID=""

cleanup() {
    if [ -n "${KCALC_PID}" ]; then kill "${KCALC_PID}" 2>/dev/null || true; fi
    if [ -n "${KWIN_PID}" ]; then kill "${KWIN_PID}" 2>/dev/null || true; fi
    if [ -n "${ATSPI_PID}" ]; then kill "${ATSPI_PID}" 2>/dev/null || true; fi
    if [ -n "${KCALC_PID}" ]; then wait "${KCALC_PID}" 2>/dev/null || true; fi
    if [ -n "${KWIN_PID}" ]; then wait "${KWIN_PID}" 2>/dev/null || true; fi
    if [ -n "${ATSPI_PID}" ]; then wait "${ATSPI_PID}" 2>/dev/null || true; fi
    rm -f "${RUNTIME_DIR}/${SOCKET_NAME}" "${RUNTIME_DIR}/${SOCKET_NAME}.lock"
}
trap cleanup EXIT TERM INT HUP

if [ -x /usr/lib/at-spi-bus-launcher ]; then
    /usr/lib/at-spi-bus-launcher --launch-immediately &
    ATSPI_PID=$!
    sleep 0.2
fi

dbus-update-activation-environment WAYLAND_DISPLAY="${SOCKET_NAME}" QT_QPA_PLATFORM=wayland >/dev/null 2>&1 || true

env -u WAYLAND_DISPLAY -u QT_QPA_PLATFORM \
    KWIN_WAYLAND_NO_PERMISSION_CHECKS=1 \
    KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1 \
    kwin_wayland --virtual --no-lockscreen \
    --width "${WIDTH}" --height "${HEIGHT}" \
    --socket "${SOCKET_NAME}" &
KWIN_PID=$!

echo "dbus_address=${DBUS_SESSION_BUS_ADDRESS}"
echo "kwin_pid=${KWIN_PID}"

deadline=$((SECONDS + 10))
while [ ! -e "${RUNTIME_DIR}/${SOCKET_NAME}" ]; do
    if ! kill -0 "${KWIN_PID}" 2>/dev/null; then
        echo "FAIL: kwin_wayland exited before socket creation"
        exit 1
    fi
    if [ "${SECONDS}" -ge "${deadline}" ]; then
        echo "FAIL: timed out waiting for ${RUNTIME_DIR}/${SOCKET_NAME}"
        exit 1
    fi
    sleep 0.1
done
sleep 0.5

env \
    DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS}" \
    WAYLAND_DISPLAY="${SOCKET_NAME}" \
    QT_QPA_PLATFORM=wayland \
    KWIN_WAYLAND_NO_PERMISSION_CHECKS=1 \
    KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1 \
    ATSPI_DBUS_IMPLEMENTATION=dbus-daemon \
    kcalc >/tmp/kwin-scripting-probe-kcalc-$$.log 2>&1 &
KCALC_PID=$!
echo "kcalc_pid=${KCALC_PID}"
sleep 2

python3 - <<'PY'
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass

import dbus
import dbus.bus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib


@dataclass
class ProbeState:
    payload: str | None = None
    event: threading.Event = threading.Event()


class ProbeObject(dbus.service.Object):
    def __init__(self, bus: dbus.bus.BusConnection, state: ProbeState) -> None:
        self._state = state
        super().__init__(bus, "/Probe")

    @dbus.service.method("org.kwin_mcp.Probe", in_signature="s", out_signature="")
    def Result(self, payload: str) -> None:
        self._state.payload = str(payload)
        self._state.event.set()


HEADER = "// KDE Plasma 6 KWin scripting — written from scratch, inspired by KDE scripting API docs"

TESTS = [
    (
        "basic window enumeration",
        HEADER
        + r'''
try {
    var wins = workspace.windowList();
    var results = [];
    for (var i = 0; i < wins.length; i++) {
        var w = wins[i];
        results.push({
            id: String(w.internalId),
            title: w.caption,
            appId: String(w.resourceClass),
            active: w === workspace.activeWindow,
            x: w.frameGeometry.x,
            y: w.frameGeometry.y,
            width: w.frameGeometry.width,
            height: w.frameGeometry.height
        });
    }
    callDBus("org.kwin_mcp.Probe", "/Probe", "org.kwin_mcp.Probe", "Result", JSON.stringify(results));
} catch (e) {
    callDBus("org.kwin_mcp.Probe", "/Probe", "org.kwin_mcp.Probe", "Result", JSON.stringify({error: String(e)}));
}
''',
    ),
    (
        "active window",
        HEADER
        + r'''
try {
    var w = workspace.activeWindow;
    if (w) {
        callDBus("org.kwin_mcp.Probe", "/Probe", "org.kwin_mcp.Probe", "Result",
            JSON.stringify({id: String(w.internalId), title: w.caption, appId: String(w.resourceClass)}));
    } else {
        callDBus("org.kwin_mcp.Probe", "/Probe", "org.kwin_mcp.Probe", "Result", JSON.stringify(null));
    }
} catch (e) {
    callDBus("org.kwin_mcp.Probe", "/Probe", "org.kwin_mcp.Probe", "Result", JSON.stringify({error: String(e)}));
}
''',
    ),
    (
        "close kcalc by resourceClass",
        HEADER
        + r'''
try {
    var wins = workspace.windowList();
    var closed = false;
    for (var i = 0; i < wins.length; i++) {
        if (String(wins[i].resourceClass) === "kcalc") {
            wins[i].closeWindow();
            callDBus("org.kwin_mcp.Probe", "/Probe", "org.kwin_mcp.Probe", "Result", JSON.stringify("closed"));
            closed = true;
            break;
        }
    }
    if (!closed) {
        callDBus("org.kwin_mcp.Probe", "/Probe", "org.kwin_mcp.Probe", "Result", JSON.stringify("not_found"));
    }
} catch (e) {
    callDBus("org.kwin_mcp.Probe", "/Probe", "org.kwin_mcp.Probe", "Result", JSON.stringify({error: String(e)}));
}
''',
    ),
]


def call_script_run(bus: dbus.bus.BusConnection, script_id: int) -> None:
    paths = [f"/Scripting/Script{script_id}", f"/Scripting/Script/{script_id}"]
    ifaces = ["org.kde.kwin.Script", "org.kde.KWin.Script"]
    last_error: Exception | None = None
    for path in paths:
        for iface_name in ifaces:
            try:
                obj = bus.get_object("org.kde.KWin", path)
                iface = dbus.Interface(obj, iface_name)
                iface.run()
                return
            except Exception as exc:  # noqa: BLE001 - probe reports all D-Bus variants tried
                last_error = exc
    raise RuntimeError(f"could not run Script({script_id}): {last_error}")


def main() -> int:
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.bus.BusConnection(os.environ["DBUS_SESSION_BUS_ADDRESS"])
    state = ProbeState()
    bus_name = dbus.service.BusName("org.kwin_mcp.Probe", bus)
    probe = ProbeObject(bus, state)
    loop = GLib.MainLoop()
    loop_thread = threading.Thread(target=loop.run, daemon=True)
    loop_thread.start()

    kwin_obj = bus.get_object("org.kde.KWin", "/Scripting")
    scripting = dbus.Interface(kwin_obj, "org.kde.kwin.Scripting")
    introspection = kwin_obj.Introspect(dbus_interface="org.freedesktop.DBus.Introspectable")
    method_names = [line.strip() for line in str(introspection).splitlines() if "<method" in line]
    print("scripting_methods=" + "; ".join(method_names))
    print("probe_service=org.kwin_mcp.Probe")

    failures = 0
    try:
        for index, (test_name, js_code) in enumerate(TESTS, start=1):
            script_name = f"kwin_mcp_probe_{index}_{int(time.time() * 1000)}"
            state.payload = None
            state.event.clear()
            try:
                script_id = int(scripting.loadScriptFromText(js_code, script_name))
                print(f"script_id[{test_name}]={script_id}")
                if script_id == -1:
                    print(f"FAIL: {test_name}: loadScriptFromText returned -1 (script rejected)")
                    failures += 1
                    continue
                call_script_run(bus, script_id)
                if not state.event.wait(8):
                    print(f"FAIL: {test_name}: timed out waiting for callDBus Result after 8s")
                    failures += 1
                    continue
                print(f"payload[{test_name}]={state.payload}")
                try:
                    parsed = json.loads(state.payload or "null")
                except json.JSONDecodeError as exc:
                    print(f"FAIL: {test_name}: payload is not JSON: {exc}")
                    failures += 1
                    continue
                if isinstance(parsed, dict) and "error" in parsed:
                    print(f"FAIL: {test_name}: JavaScript error: {parsed['error']}")
                    failures += 1
                    continue
                if test_name == "basic window enumeration" and not isinstance(parsed, list):
                    print(f"FAIL: {test_name}: expected JSON array, got {type(parsed).__name__}")
                    failures += 1
                    continue
                if test_name == "close kcalc by resourceClass" and parsed != "closed":
                    print(f"FAIL: {test_name}: expected closed, got {parsed!r}")
                    failures += 1
                    continue
                print(f"PASS: {test_name}")
            except Exception as exc:  # noqa: BLE001 - probe should continue across tests
                print(f"FAIL: {test_name}: {type(exc).__name__}: {exc}")
                failures += 1
            finally:
                try:
                    scripting.unloadScript(script_name)
                except Exception as exc:  # noqa: BLE001 - cleanup best effort
                    print(f"WARN: unloadScript({script_name}) failed: {type(exc).__name__}: {exc}")
    finally:
        probe.remove_from_connection()
        del bus_name
        loop.quit()
        loop_thread.join(timeout=2)

    if failures:
        print(f"SUMMARY: FAIL ({failures} failed)")
        return 0
    print("SUMMARY: PASS")
    return 0


raise SystemExit(main())
PY
SESSION
