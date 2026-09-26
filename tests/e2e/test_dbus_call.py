"""End-to-end coverage for the ``dbus_call`` argument contract.

A test-owned echo service runs in a separate process on the virtual session's
bus (this file run with ``--echo-service``) and records every call it receives,
so the assertions check what actually crossed the bus rather than only the
tool's reply text.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from kwin_mcp.core import AutomationEngine

if TYPE_CHECKING:
    from collections.abc import Iterator

SERVICE = "org.kwinmcp.E2EEcho"
OBJECT_PATH = "/echo"
INTERFACE = "org.kwinmcp.E2EEcho"
READY_TIMEOUT_S = 15.0


def _serve(record_path: Path) -> None:
    """Run the echo service until terminated (child process entry point)."""
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()

    def record(method: str, payload: object) -> None:
        with record_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps([method, payload]) + "\n")

    def typed(value: object) -> list[object]:
        plain = value if isinstance(value, bool | int | float) else str(value)
        return [type(value).__name__, plain]

    class Echo(dbus.service.Object):
        @dbus.service.method(INTERFACE, in_signature="", out_signature="s")
        def NoArgs(self) -> str:  # noqa: N802 - D-Bus method name
            record("NoArgs", [])
            return "no-args"

        @dbus.service.method(INTERFACE, in_signature="s", out_signature="s")
        def OneStr(self, value: str) -> str:  # noqa: N802 - D-Bus method name
            record("OneStr", [typed(value)])
            return str(value)

        @dbus.service.method(INTERFACE, in_signature="i", out_signature="i")
        def OneInt(self, value: int) -> int:  # noqa: N802 - D-Bus method name
            record("OneInt", [typed(value)])
            return value

        @dbus.service.method(INTERFACE, in_signature="a{ss}", out_signature="s")
        def StrDict(self, value: dict) -> str:  # noqa: N802 - D-Bus method name
            record("StrDict", {str(k): typed(v) for k, v in value.items()})
            return "str-dict"

        @dbus.service.method(INTERFACE, in_signature="a{sv}", out_signature="s")
        def VarDict(self, value: dict) -> str:  # noqa: N802 - D-Bus method name
            record("VarDict", {str(k): typed(v) for k, v in value.items()})
            return "var-dict"

    bus_name = dbus.service.BusName(SERVICE, bus)
    service = Echo(bus, OBJECT_PATH)
    print("READY", flush=True)
    try:
        GLib.MainLoop().run()
    finally:
        del service, bus_name


class EchoService:
    """Handle for calling the echo service through ``AutomationEngine.dbus_call``."""

    def __init__(self, engine: AutomationEngine, record_path: Path) -> None:
        self.engine = engine
        self._record_path = record_path

    def call(self, method: str, args: list[str | dict] | None = None) -> tuple[str, list]:
        """Call ``method`` and return the tool output plus what the service received."""
        self._record_path.write_text("", encoding="utf-8")
        output = self.engine.dbus_call(SERVICE, OBJECT_PATH, INTERFACE, method, args)
        lines = self._record_path.read_text(encoding="utf-8").splitlines()
        return output, [json.loads(line) for line in lines]


@pytest.fixture(scope="module")
def echo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[EchoService]:
    """Virtual session with the echo service registered on its session bus."""
    engine = AutomationEngine()
    record_path = tmp_path_factory.mktemp("dbus-echo") / "calls.jsonl"
    record_path.touch()
    child: subprocess.Popen[str] | None = None
    try:
        output = engine.session_start(screen_width=1280, screen_height=800)
        assert "Session started" in output, output
        info = engine._get_session().info
        assert info is not None and info.dbus_address, output
        env = {**os.environ, "DBUS_SESSION_BUS_ADDRESS": info.dbus_address}
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__)), "--echo-service", str(record_path)],
            env=env,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert child.stdout is not None
        deadline = time.monotonic() + READY_TIMEOUT_S
        ready_line = ""
        while time.monotonic() < deadline and child.poll() is None:
            readable, _, _ = select.select([child.stdout], [], [], 0.2)
            if readable:
                ready_line = child.stdout.readline().strip()
                break
        assert ready_line == "READY", f"echo service not ready: {ready_line!r}"
        yield EchoService(engine, record_path)
    finally:
        if child is not None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            if child.stdout is not None:
                child.stdout.close()
        engine.session_stop()


# ── Integers: dbus-send's strtol silently corrupted these ──────────────────


@pytest.mark.parametrize(
    "arg",
    ["int32:12x3", "int32:2147483648", "int32:notanumber"],
    ids=["trailing-garbage", "overflow", "not-a-number"],
)
def test_malformed_int32_is_rejected_before_sending(echo: EchoService, arg: str) -> None:
    output, received = echo.call("OneInt", [arg])
    assert output.startswith("D-Bus call failed:"), output
    assert "int32" in output, output
    assert received == [], received


@pytest.mark.parametrize(
    ("arg", "expected"),
    [("int32:42", 42), ("int32:-5", -5), ("int32:0x10", 16)],
    ids=["positive", "negative", "hex"],
)
def test_well_formed_int32_reaches_service_unchanged(
    echo: EchoService, arg: str, expected: int
) -> None:
    output, received = echo.call("OneInt", [arg])
    assert received == [["OneInt", [["Int32", expected]]]], (output, received)
    # Both the old dbus-send text and the new bare value end with the number.
    assert output.split()[-1] == str(expected), output


def test_typed_json_int32_reaches_service(echo: EchoService) -> None:
    output, received = echo.call("OneInt", [{"type": "int32", "value": 7}])
    assert received == [["OneInt", [["Int32", 7]]]], (output, received)
    assert output == "7", output


# ── Dicts: dbus-send's comma key,value grammar, including a{sv} ────────────


def test_dict_uses_dbus_send_comma_grammar(echo: EchoService) -> None:
    output, received = echo.call("StrDict", ["dict:string:string:FOO,bar,BAZ,qux"])
    assert received == [["StrDict", {"FOO": ["String", "bar"], "BAZ": ["String", "qux"]}]], (
        output,
        received,
    )
    assert not output.startswith(("D-Bus call failed:", "D-Bus error:")), output


def test_dict_with_variant_values_sends_asv(echo: EchoService) -> None:
    output, received = echo.call("VarDict", ["dict:string:variant:name,string:x,n,int32:3"])
    assert received == [["VarDict", {"name": ["String", "x"], "n": ["Int32", 3]}]], (
        output,
        received,
    )
    assert not output.startswith(("D-Bus call failed:", "D-Bus error:")), output


def test_typed_json_dict_with_variant_values_sends_asv(echo: EchoService) -> None:
    arg = {
        "type": "dict",
        "key_type": "string",
        "value_type": "variant",
        "value": {"name": {"type": "string", "value": "x"}, "n": {"type": "int32", "value": 3}},
    }
    output, received = echo.call("VarDict", [arg])
    assert received == [["VarDict", {"name": ["String", "x"], "n": ["Int32", 3]}]], (
        output,
        received,
    )
    assert output == "var-dict", output


# ── Arity and type mismatches ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("NoArgs", ["string:extra"]),
        ("OneStr", ["string:a", "string:b"]),
        ("OneStr", []),
    ],
    ids=["surplus-on-no-args", "surplus", "missing"],
)
def test_argument_count_mismatch_is_rejected(
    echo: EchoService, method: str, args: list[str | dict]
) -> None:
    output, received = echo.call(method, args)
    assert output.startswith("D-Bus call failed:"), output
    assert received == [], received


def test_argument_type_mismatch_is_reported(echo: EchoService) -> None:
    output, received = echo.call("OneStr", ["int32:5"])
    assert output.startswith("D-Bus call failed:"), output
    assert received == [], received


# ── Replies and bus errors against real services ────────────────────────────


def test_void_method_with_matching_args_is_called_once(echo: EchoService) -> None:
    output, received = echo.call("NoArgs")
    assert received == [["NoArgs", []]], (output, received)
    assert output == "no-args", output


def test_bus_get_id_returns_bare_value(echo: EchoService) -> None:
    output = echo.engine.dbus_call(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetId"
    )
    assert len(output) == 32 and all(c in "0123456789abcdef" for c in output), output


@pytest.mark.parametrize(
    "arg", ["boolean:false", {"type": "boolean", "value": False}], ids=["legacy", "typed"]
)
def test_kwin_void_method_returns_empty_reply(echo: EchoService, arg: str | dict) -> None:
    output = echo.engine.dbus_call("org.kde.KWin", "/KWin", "org.kde.KWin", "showDesktop", [arg])
    assert output == "", output


def test_unknown_service_returns_dbus_error(echo: EchoService) -> None:
    output = echo.engine.dbus_call(
        "org.example.DefinitelyDoesNotExist", "/x", "org.example.X", "Foo"
    )
    assert output.startswith("D-Bus error: org.freedesktop.DBus.Error.ServiceUnknown"), output


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--echo-service":
        _serve(Path(sys.argv[2]))
    else:
        sys.exit(f"usage: {sys.argv[0]} --echo-service RECORD_PATH")
