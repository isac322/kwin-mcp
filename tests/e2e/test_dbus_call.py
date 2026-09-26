"""End-to-end coverage for the ``dbus_call`` argument contract.

A test-owned echo service runs in a separate process on the virtual session's
bus (this file run with ``--echo-service``). It records the message signature
and every argument it received, so the assertions check what actually crossed
the bus rather than only the tool's reply text. A twin service under
``SERVICE_NOINTRO`` refuses introspection, covering the path where each
argument's own signature is sent.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from kwin_mcp.core import AutomationEngine

if TYPE_CHECKING:
    from collections.abc import Iterator

SERVICE = "org.kwinmcp.E2EEcho"
SERVICE_NOINTRO = "org.kwinmcp.E2EEcho.nointro"
OBJECT_PATH = "/echo"
INTERFACE = "org.kwinmcp.E2EEcho"
READY_TIMEOUT_S = 15.0


def _typed(value: object) -> Any:
    """Return the [dbus-python class name, plain value] of one argument.

    Dict and array values recurse so the recorded shape keeps inner types.
    """
    if isinstance(value, dict):
        return {str(k): _typed(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_typed(v) for v in value]
    plain = value if isinstance(value, bool | int | float) else str(value)
    return [type(value).__name__, plain]


def _serve(record_path: Path, service_name: str, introspectable: bool) -> None:
    """Run the echo service until terminated (child process entry point)."""
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()

    def record(method: str, message: Any, args: tuple) -> None:
        # message is a dbus.lowlevel.MethodCallMessage; it carries the wire
        # signature the sender marshalled.
        payload = [method, message.get_signature(), [_typed(a) for a in args]]
        with record_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

    class Echo(dbus.service.Object):
        @dbus.service.method(
            INTERFACE, in_signature="", out_signature="s", message_keyword="msg"
        )
        def NoArgs(self, msg: Any) -> str:  # noqa: N802 - D-Bus method name
            record("NoArgs", msg, ())
            return "no-args"

        @dbus.service.method(
            INTERFACE, in_signature="s", out_signature="s", message_keyword="msg"
        )
        def OneStr(self, value: str, msg: Any) -> str:  # noqa: N802
            record("OneStr", msg, (value,))
            return str(value)

        @dbus.service.method(
            INTERFACE, in_signature="i", out_signature="i", message_keyword="msg"
        )
        def OneInt(self, value: int, msg: Any) -> int:  # noqa: N802
            record("OneInt", msg, (value,))
            return value

        @dbus.service.method(
            INTERFACE, in_signature="x", out_signature="x", message_keyword="msg"
        )
        def OneInt64(self, value: int, msg: Any) -> int:  # noqa: N802
            record("OneInt64", msg, (value,))
            return value

        @dbus.service.method(
            INTERFACE, in_signature="v", out_signature="s", message_keyword="msg"
        )
        def OneVariant(self, value: Any, msg: Any) -> str:  # noqa: N802
            record("OneVariant", msg, (value,))
            return "variant-ok"

        @dbus.service.method(
            INTERFACE, in_signature="a{ss}", out_signature="s", message_keyword="msg"
        )
        def StrDict(self, value: dict, msg: Any) -> str:  # noqa: N802
            record("StrDict", msg, (value,))
            return "str-dict"

        @dbus.service.method(
            INTERFACE, in_signature="a{sv}", out_signature="s", message_keyword="msg"
        )
        def VarDict(self, value: dict, msg: Any) -> str:  # noqa: N802
            record("VarDict", msg, (value,))
            return "var-dict"

        if not introspectable:

            @dbus.service.method(
                "org.freedesktop.DBus.Introspectable",
                in_signature="",
                out_signature="s",
                path_keyword="object_path",
                connection_keyword="connection",
            )
            def Introspect(self, object_path: object, connection: object) -> str:  # noqa: N802
                raise dbus.DBusException(
                    "introspection is disabled",
                    name="org.kwinmcp.E2EEcho.NoIntrospection",
                )

    bus_name = dbus.service.BusName(service_name, bus)
    service = Echo(bus, OBJECT_PATH)
    print("READY", flush=True)
    try:
        GLib.MainLoop().run()
    finally:
        del service, bus_name


def _spawn_service(
    record_path: Path, dbus_address: str, service_name: str, introspectable: bool
) -> subprocess.Popen[str]:
    env = {**os.environ, "DBUS_SESSION_BUS_ADDRESS": dbus_address}
    child = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__)),
            "--echo-service",
            str(record_path),
            service_name,
            "yes" if introspectable else "no",
        ],
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
    return child


def _stop_service(child: subprocess.Popen[str]) -> None:
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)
    if child.stdout is not None:
        child.stdout.close()


class EchoService:
    """Handle for calling the echo services through ``AutomationEngine.dbus_call``."""

    def __init__(self, engine: AutomationEngine, record_path: Path) -> None:
        self.engine = engine
        self._record_path = record_path

    def call(
        self,
        method: str,
        args: list[str | dict] | None = None,
        *,
        service: str = SERVICE,
    ) -> tuple[str, list]:
        """Call ``method``; return the tool output and what the service received.

        Each received entry is ``[method_name, wire_signature, [typed_args]]``.
        """
        self._record_path.write_text("", encoding="utf-8")
        output = self.engine.dbus_call(service, OBJECT_PATH, INTERFACE, method, args)
        lines = self._record_path.read_text(encoding="utf-8").splitlines()
        return output, [json.loads(line) for line in lines]


@pytest.fixture(scope="module")
def echo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[EchoService]:
    """Virtual session with introspectable and non-introspectable echo services."""
    engine = AutomationEngine()
    record_path = tmp_path_factory.mktemp("dbus-echo") / "calls.jsonl"
    record_path.touch()
    children: list[subprocess.Popen[str]] = []
    try:
        output = engine.session_start(screen_width=1280, screen_height=800)
        assert "Session started" in output, output
        info = engine._get_session().info
        assert info is not None and info.dbus_address, output
        for service_name, introspectable in ((SERVICE, True), (SERVICE_NOINTRO, False)):
            children.append(
                _spawn_service(record_path, info.dbus_address, service_name, introspectable)
            )
        yield EchoService(engine, record_path)
    finally:
        for child in children:
            _stop_service(child)
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
    assert received == [["OneInt", "i", [["Int32", expected]]]], (output, received)
    # Both the old dbus-send text and the new bare value end with the number.
    assert output.split()[-1] == str(expected), output


def test_typed_json_int32_reaches_service(echo: EchoService) -> None:
    output, received = echo.call("OneInt", [{"type": "int32", "value": 7}])
    assert received == [["OneInt", "i", [["Int32", 7]]]], (output, received)
    assert output == "7", output


# ── Dicts: dbus-send's comma key,value grammar, including a{sv} ────────────


def test_dict_uses_dbus_send_comma_grammar(echo: EchoService) -> None:
    output, received = echo.call("StrDict", ["dict:string:string:FOO,bar,BAZ,qux"])
    assert received == [
        ["StrDict", "a{ss}", [{"FOO": ["String", "bar"], "BAZ": ["String", "qux"]}]]
    ], (output, received)
    assert not output.startswith(("D-Bus call failed:", "D-Bus error:")), output


def test_dict_with_variant_values_sends_asv(echo: EchoService) -> None:
    output, received = echo.call("VarDict", ["dict:string:variant:name,string:x,n,int32:3"])
    assert received == [
        ["VarDict", "a{sv}", [{"name": ["String", "x"], "n": ["Int32", 3]}]]
    ], (output, received)
    assert not output.startswith(("D-Bus call failed:", "D-Bus error:")), output


def test_typed_json_dict_with_variant_values_sends_asv(echo: EchoService) -> None:
    arg = {
        "type": "dict",
        "key_type": "string",
        "value_type": "variant",
        "value": {"name": {"type": "string", "value": "x"}, "n": {"type": "int32", "value": 3}},
    }
    output, received = echo.call("VarDict", [arg])
    assert received == [
        ["VarDict", "a{sv}", [{"name": ["String", "x"], "n": ["Int32", 3]}]]
    ], (output, received)
    assert output == "var-dict", output


# ── Variants stay variants, with or without introspection ───────────────────


@pytest.mark.parametrize(
    "arg",
    ["variant:int32:3", {"type": "variant", "value_type": "int32", "value": 3}],
    ids=["dbus-send", "typed-json"],
)
def test_top_level_variant_arrives_as_variant(echo: EchoService, arg: str | dict) -> None:
    """The wire signature is 'v' on both services, as dbus-send sends it."""
    for service in (SERVICE, SERVICE_NOINTRO):
        output, received = echo.call("OneVariant", [arg], service=service)
        assert received == [["OneVariant", "v", [["Int32", 3]]]], (service, output, received)
        assert not output.startswith(("D-Bus call failed:", "D-Bus error:")), (service, output)


def test_explicit_variant_arg_does_not_match_non_variant(echo: EchoService) -> None:
    output, received = echo.call("OneInt", ["variant:int32:5"])
    assert output.startswith("D-Bus call failed:"), output
    assert received == [], received


# ── Argument types must fit the declared signature, no silent coercion ──────


@pytest.mark.parametrize(
    ("method", "arg", "sig"),
    [
        ("OneInt", "int64:5", "i"),
        ("OneInt64", "int32:5", "x"),
        ("OneInt", "boolean:true", "i"),
        ("OneStr", "int32:5", "s"),
    ],
    ids=["int64-to-i", "int32-to-x", "bool-to-i", "int-to-s"],
)
def test_type_mismatch_is_rejected_before_sending(
    echo: EchoService, method: str, arg: str, sig: str
) -> None:
    output, received = echo.call(method, [arg])
    assert output.startswith("D-Bus call failed:"), output
    assert sig in output, output
    assert received == [], received


def test_non_introspectable_service_gets_own_signatures(echo: EchoService) -> None:
    output, received = echo.call("OneInt", ["int32:5"], service=SERVICE_NOINTRO)
    assert received == [["OneInt", "i", [["Int32", 5]]]], (output, received)
    assert output == "5", output


# ── Argument count: surplus, missing, and overloaded methods ────────────────


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


def test_overload_error_lists_every_candidate(echo: EchoService) -> None:
    """KWin's loadScript is exported twice (s and ss); a third arg fails clearly."""
    output = echo.engine.dbus_call(
        "org.kde.KWin",
        "/Scripting",
        "org.kde.kwin.Scripting",
        "loadScript",
        ["string:a", "string:b", "string:c"],
    )
    assert output.startswith("D-Bus call failed:"), output
    assert "'s'" in output and "'ss'" in output, output


def test_overloaded_kwin_load_script_accepts_both_arities(
    echo: EchoService, tmp_path: Path
) -> None:
    """KWin exports loadScript as (s) and (ss); both arities must succeed."""
    script = tmp_path / "probe.js"
    script.write_text("console.log('kwin-mcp dbus_call probe');\n", encoding="utf-8")
    for args in ([f"string:{script}"], [f"string:{script}", "string:prfix23probe"]):
        output = echo.engine.dbus_call(
            "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "loadScript", args
        )
        assert not output.startswith(("D-Bus call failed:", "D-Bus error:")), output
        assert output.lstrip("-").isdigit(), output


# ── Replies and bus errors against real services ────────────────────────────


def test_void_method_with_matching_args_is_called_once(echo: EchoService) -> None:
    output, received = echo.call("NoArgs")
    assert received == [["NoArgs", "", []]], (output, received)
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
    if len(sys.argv) == 5 and sys.argv[1] == "--echo-service":
        _serve(Path(sys.argv[2]), sys.argv[3], sys.argv[4] == "yes")
    else:
        sys.exit(f"usage: {sys.argv[0]} --echo-service RECORD_PATH SERVICE_NAME yes|no")
