"""``kwin-mcp-smoke --live``: kwin-mcp attached to a live-desktop stand-in, for client smoke runs.

The e2e image's entrypoint dispatches ``/opt/kwin-mcp-venv/bin/kwin-mcp-smoke`` here
(``docker/e2e-entrypoint.sh``). ``scripts/client_smoke.sh`` registers
``docker run -i --rm kwin-mcp-e2e /opt/kwin-mcp-venv/bin/kwin-mcp-smoke --live`` with a
real MCP client (QA-CLT-001/002/008), so the server must find a desktop to attach to:

1. ``live_kwin()`` from ``tests/e2e/session_harness.py``: a test-owned
   ``kwin_wayland --virtual`` (1280x800) with its own session and AT-SPI buses;
2. a tiny D-Bus service ``org.kwinmcp.Smoke`` (``/org/kwinmcp/Smoke``,
   ``Add(ii) -> i``) on that bus, the target of the ``dbus_call`` smoke arm;
3. ``window_zoo`` (``tests/e2e/window_zoo.py``) with its main window ``Zoo`` and the
   ``Zoo Utility`` window the live ``window_close`` arm closes.

Then ``kwin-mcp`` runs with every argument after ``--live`` (e.g.
``--live-consent=refuse``), inheriting this process's stdin and stdout, and with
``DBUS_SESSION_BUS_ADDRESS`` / ``WAYLAND_DISPLAY`` pointing at the stand-in, so a
plain ``session_connect()`` attaches to it. Everything this wrapper reports goes to
stderr: stdout belongs to the JSON-RPC stream. When kwin-mcp exits (the client closed
stdin), the zoo, the service and the compositor are stopped and kwin-mcp's exit
status is returned.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from types import FrameType

TESTS_DIR: Final = Path(os.environ.get("KWIN_MCP_SMOKE_TESTS", "/app/tests")) / "e2e"
KWIN_MCP: Final = Path(sys.executable).with_name("kwin-mcp")
SERVICE_NAME: Final = "org.kwinmcp.Smoke"
SERVICE_PATH: Final = "/org/kwinmcp/Smoke"
SERVICE_FLAG: Final = "--dbus-service"
READY_TIMEOUT_S: Final = 15.0
USAGE: Final = "usage: kwin-mcp-smoke --live [kwin-mcp flags...]"


def _log(message: str) -> None:
    print(f"kwin-mcp-smoke: {message}", file=sys.stderr, flush=True)


def _serve_dbus() -> int:
    """Own ``org.kwinmcp.Smoke`` on ``$DBUS_SESSION_BUS_ADDRESS`` until terminated."""
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()

    class Smoke(dbus.service.Object):
        @dbus.service.method(SERVICE_NAME, in_signature="ii", out_signature="i")
        def Add(self, first: int, second: int) -> int:  # noqa: N802 - D-Bus method name
            return first + second

    name = dbus.service.BusName(SERVICE_NAME, bus)
    service = Smoke(bus, SERVICE_PATH)
    print("READY", flush=True)
    loop = GLib.MainLoop()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, loop.quit)
    loop.run()
    del service, name
    return 0


def _start_service(dbus_address: str) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, __file__, SERVICE_FLAG],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": dbus_address},
        text=True,
    )
    assert process.stdout is not None
    line = process.stdout.readline().strip()
    if line != "READY":
        process.kill()
        msg = f"the {SERVICE_NAME} service did not start (read {line!r})"
        raise RuntimeError(msg)
    return process


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _run_live(server_args: list[str]) -> int:
    sys.path.insert(0, str(TESTS_DIR))
    from session_harness import live_kwin
    from window_zoo import APP_ID, MAIN_CAPTION, ZooProcess

    utility = "Zoo Utility"
    with live_kwin() as live:
        service = _start_service(live.dbus_address)
        try:
            with ZooProcess(
                wayland_display=live.wayland_display,
                dbus_address=live.dbus_address,
                open_commands=("utility",),
            ) as zoo:
                zoo.expect(f"window: {MAIN_CAPTION}", timeout_s=READY_TIMEOUT_S)
                zoo.expect(f"window: {utility}", timeout_s=READY_TIMEOUT_S)
                live.wait_for_windows(
                    lambda windows: (
                        {w.caption for w in windows if w.resource_class == APP_ID}
                        >= {MAIN_CAPTION, utility}
                    ),
                    timeout_s=READY_TIMEOUT_S,
                )
                _log(
                    f"live KWin {live.wayland_display} ready; {APP_ID} pid {zoo.pid} "
                    f"shows {MAIN_CAPTION!r} and {utility!r}; D-Bus {SERVICE_NAME} "
                    f"{SERVICE_PATH} Add(ii)"
                )
                return _run_server(server_args, live.dbus_address, live.wayland_display)
        finally:
            _stop(service)


def _run_server(server_args: list[str], dbus_address: str, wayland_display: str) -> int:
    env = {
        **os.environ,
        "DBUS_SESSION_BUS_ADDRESS": dbus_address,
        "WAYLAND_DISPLAY": wayland_display,
        "XDG_CURRENT_DESKTOP": "KDE",
        "XDG_SESSION_TYPE": "wayland",
    }
    env.pop("DISPLAY", None)
    started = time.monotonic()
    server = subprocess.Popen([str(KWIN_MCP), *server_args], env=env)

    def forward(signum: int, _frame: FrameType | None) -> None:
        if server.poll() is None:
            server.send_signal(signum)

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, forward)
    status = server.wait()
    _log(f"kwin-mcp exited with status {status} after {time.monotonic() - started:.1f}s")
    return status


def main(argv: list[str]) -> int:
    if argv[:1] == [SERVICE_FLAG]:
        return _serve_dbus()
    if argv[:1] != ["--live"]:
        print(USAGE, file=sys.stderr)
        return 64
    return _run_live(argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
