"""End-to-end coverage for the ``dbus_call`` argument and reply contract.

A test-owned echo service runs in a separate process on the virtual session's
bus (this file run with ``--echo-service``). It records the message signature
and every argument it received, so the assertions check what actually crossed
the bus rather than only the tool's reply. A twin service under
``SERVICE_NOINTRO`` refuses introspection, covering the path where each
argument's own signature is sent, and ``OVERLOAD_PATH`` introspects as a Qt-style
overloaded method (``Set(i)`` and ``Set(s)``).

Every call returns ``DbusReply{reply, signature}``; its text is ``render()``.
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
from _asserts import assert_render_roundtrip
from kwin_oracle import KWinOracle

from kwin_mcp.core import AutomationEngine
from kwin_mcp.models import DbusReply, TypedArg

if TYPE_CHECKING:
    from collections.abc import Iterator

SERVICE = "org.kwinmcp.E2EEcho"
SERVICE_NOINTRO = "org.kwinmcp.E2EEcho.nointro"
OBJECT_PATH = "/echo"
OVERLOAD_PATH = "/overload"
INTERFACE = "org.kwinmcp.E2EEcho"
READY_TIMEOUT_S = 15.0
ALL_TYPES_SIGNATURE = "siubodaia{si}v"
OVERLOAD_XML = f"""<node>
  <interface name="{INTERFACE}">
    <method name="Set"><arg type="i" direction="in"/></method>
    <method name="Set"><arg type="s" direction="in"/></method>
  </interface>
</node>"""

ArgList = list[str | dict[str, Any]]


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

    def method(in_sig: str | None, out_sig: str) -> Any:
        return dbus.service.method(
            INTERFACE, in_signature=in_sig, out_signature=out_sig, message_keyword="msg"
        )

    class Echo(dbus.service.Object):
        @method("", "s")
        def NoArgs(self, msg: Any) -> str:  # noqa: N802 - D-Bus method name
            record("NoArgs", msg, ())
            return "no-args"

        @method("s", "s")
        def OneStr(self, value: str, msg: Any) -> str:  # noqa: N802
            record("OneStr", msg, (value,))
            return str(value)

        @method("i", "i")
        def OneInt(self, value: int, msg: Any) -> int:  # noqa: N802
            record("OneInt", msg, (value,))
            return value

        @method("x", "x")
        def OneInt64(self, value: int, msg: Any) -> int:  # noqa: N802
            record("OneInt64", msg, (value,))
            return value

        @method("v", "s")
        def OneVariant(self, value: Any, msg: Any) -> str:  # noqa: N802
            record("OneVariant", msg, (value,))
            return "variant-ok"

        @method("a{ss}", "s")
        def StrDict(self, value: dict, msg: Any) -> str:  # noqa: N802
            record("StrDict", msg, (value,))
            return "str-dict"

        @method("a{sv}", "s")
        def VarDict(self, value: dict, msg: Any) -> str:  # noqa: N802
            record("VarDict", msg, (value,))
            return "var-dict"

        @method(ALL_TYPES_SIGNATURE, ALL_TYPES_SIGNATURE)
        def AllTypes(  # noqa: N802
            self,
            s: Any,
            i: Any,
            u: Any,
            b: Any,
            o: Any,
            d: Any,
            ai: Any,
            asi: Any,
            v: Any,
            msg: Any,
        ) -> tuple:
            args = (s, i, u, b, o, d, ai, asi, v)
            record("AllTypes", msg, args)
            return args

        @method("sasa{sv}", "s")
        def Mixed(self, text: str, items: list, mapping: dict, msg: Any) -> str:  # noqa: N802
            record("Mixed", msg, (text, items, mapping))
            return "mixed"

        @method("", "s")
        def Boom(self, msg: Any) -> str:  # noqa: N802
            record("Boom", msg, ())
            raise dbus.DBusException("boom", name="org.test.Error")

        @method("", "")
        def ReplyVoid(self, msg: Any) -> None:  # noqa: N802
            record("ReplyVoid", msg, ())

        @method("", "b")
        def ReplyBool(self, msg: Any) -> bool:  # noqa: N802
            return True

        @method("", "i")
        def ReplyInt(self, msg: Any) -> int:  # noqa: N802
            return 42

        @method("", "s")
        def ReplyStr(self, msg: Any) -> str:  # noqa: N802
            return "hello"

        @method("", "a{sv}")
        def ReplyDict(self, msg: Any) -> dict:  # noqa: N802
            return {"name": dbus.String("x", variant_level=1), "n": dbus.Int32(3, variant_level=1)}

        @method("", "is")
        def ReplyPair(self, msg: Any) -> tuple:  # noqa: N802
            return (7, "x")

        if not introspectable:

            @method(None, "s")
            def Loose(self, a: Any, b: Any, c: Any, d: Any, e: Any, msg: Any) -> str:  # noqa: N802
                record("Loose", msg, (a, b, c, d, e))
                return "any"

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

    class Overload(dbus.service.Object):
        """Introspects as Set(i) + Set(s); records whatever signature arrives."""

        @method(None, "")
        def Set(self, value: Any, msg: Any) -> None:  # noqa: N802
            record("Set", msg, (value,))

        @dbus.service.method(
            "org.freedesktop.DBus.Introspectable",
            in_signature="",
            out_signature="s",
            path_keyword="object_path",
            connection_keyword="connection",
        )
        def Introspect(self, object_path: object, connection: object) -> str:  # noqa: N802
            return OVERLOAD_XML

    bus_name = dbus.service.BusName(service_name, bus)
    objects = [Echo(bus, OBJECT_PATH), Overload(bus, OVERLOAD_PATH)]
    print("READY", flush=True)
    try:
        GLib.MainLoop().run()
    finally:
        del objects, bus_name


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


def _args(args: ArgList | None) -> list[str | TypedArg] | None:
    """Typed-JSON dicts become ``TypedArg`` (the tool's input type); strings stay."""
    if args is None:
        return None
    return [a if isinstance(a, str) else TypedArg.model_validate(a) for a in args]


class EchoService:
    """Handle for calling the echo services through ``AutomationEngine.dbus_call``."""

    def __init__(self, engine: AutomationEngine, record_path: Path, dbus_address: str) -> None:
        self.engine = engine
        self.dbus_address = dbus_address
        self._record_path = record_path

    def received(self) -> list:
        lines = self._record_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def clear(self) -> None:
        self._record_path.write_text("", encoding="utf-8")

    def call(
        self,
        method: str,
        args: ArgList | None = None,
        *,
        service: str = SERVICE,
        path: str = OBJECT_PATH,
    ) -> tuple[DbusReply, list]:
        """Call ``method``; return the reply and what the service received.

        Each received entry is ``[method_name, wire_signature, [typed_args]]``.
        """
        self.clear()
        reply = self.engine.dbus_call(service, path, INTERFACE, method, _args(args))
        assert_render_roundtrip(DbusReply, reply.model_dump(mode="json"), reply.render())
        return reply, self.received()

    def call_rejected(
        self,
        method: str,
        args: ArgList | None = None,
        *,
        service: str = SERVICE,
        path: str = OBJECT_PATH,
    ) -> tuple[str, list]:
        """Call ``method`` expecting an argument error; return its text and what was received.

        Argument errors are tool errors (``ValueError``) starting with
        ``D-Bus call failed:``.
        """
        self.clear()
        with pytest.raises(ValueError, match=r"^D-Bus call failed:") as exc_info:
            self.engine.dbus_call(service, path, INTERFACE, method, _args(args))
        return str(exc_info.value), self.received()


@pytest.fixture(scope="module")
def echo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[EchoService]:
    """Virtual session with introspectable and non-introspectable echo services."""
    engine = AutomationEngine()
    record_path = tmp_path_factory.mktemp("dbus-echo") / "calls.jsonl"
    record_path.touch()
    children: list[subprocess.Popen[str]] = []
    try:
        info = engine.session_start(screen_width=1280, screen_height=800)
        assert info.kind == "virtual" and info.dbus_address, info
        for service_name, introspectable in ((SERVICE, True), (SERVICE_NOINTRO, False)):
            children.append(
                _spawn_service(record_path, info.dbus_address, service_name, introspectable)
            )
        yield EchoService(engine, record_path, info.dbus_address)
    finally:
        for child in children:
            _stop_service(child)
        engine.session_stop()


# ── Every basic type as a dbus-send string ──────────────────────────────────


@pytest.mark.qa("QA-ESC-001")
def test_dbus_send_string_args_all_types(echo: EchoService) -> None:
    args: ArgList = [
        "string:hi",
        "int32:-5",
        "uint32:0x10",
        "boolean:TRUE",
        "objpath:/a/b",
        "double:1.5",
        "array:int32:1,2",
        "dict:string:int32:a,1",
        "variant:int32:3",
    ]
    reply, received = echo.call("AllTypes", args)

    assert received == [
        [
            "AllTypes",
            ALL_TYPES_SIGNATURE,
            [
                ["String", "hi"],
                ["Int32", -5],
                ["UInt32", 16],
                ["Boolean", True],
                ["ObjectPath", "/a/b"],
                ["Double", 1.5],
                [["Int32", 1], ["Int32", 2]],
                {"a": ["Int32", 1]},
                ["Int32", 3],
            ],
        ]
    ], (reply, received)
    assert reply == DbusReply(
        reply=["hi", -5, 16, True, "/a/b", 1.5, [1, 2], {"a": 1}, 3],
        signature=ALL_TYPES_SIGNATURE,
    )


@pytest.mark.qa("QA-ESC-002")
@pytest.mark.parametrize(
    ("arg", "fragment"),
    [
        ("byte:300", "byte"),
        ("int16:70000", "int16"),
        ("objpath:relative", "objpath"),
        ("boolean:maybe", "boolean"),
        ("int32:0777", "int32"),
    ],
    ids=["byte-range", "int16-range", "relative-objpath", "boolean-word", "octal-int32"],
)
def test_malformed_args_fail_before_send(echo: EchoService, arg: str, fragment: str) -> None:
    output, received = echo.call_rejected("OneVariant", [arg])
    assert fragment in output, output
    assert received == [], received


# ── Integers: dbus-send's strtol silently corrupted these ──────────────────


@pytest.mark.parametrize(
    "arg",
    ["int32:12x3", "int32:2147483648", "int32:notanumber"],
    ids=["trailing-garbage", "overflow", "not-a-number"],
)
def test_malformed_int32_is_rejected_before_sending(echo: EchoService, arg: str) -> None:
    output, received = echo.call_rejected("OneInt", [arg])
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
    reply, received = echo.call("OneInt", [arg])
    assert received == [["OneInt", "i", [["Int32", expected]]]], (reply, received)
    assert reply == DbusReply(reply=expected, signature="i")
    assert reply.render() == f"{expected}\n(reply signature 'i')"


def test_typed_json_int32_reaches_service(echo: EchoService) -> None:
    reply, received = echo.call("OneInt", [{"type": "int32", "value": 7}])
    assert received == [["OneInt", "i", [["Int32", 7]]]], (reply, received)
    assert reply == DbusReply(reply=7, signature="i")


# ── Dicts: dbus-send's comma key,value grammar, including a{sv} ────────────


def test_dict_uses_dbus_send_comma_grammar(echo: EchoService) -> None:
    reply, received = echo.call("StrDict", ["dict:string:string:FOO,bar,BAZ,qux"])
    assert received == [
        ["StrDict", "a{ss}", [{"FOO": ["String", "bar"], "BAZ": ["String", "qux"]}]]
    ], (reply, received)
    assert reply == DbusReply(reply="str-dict", signature="s")


def test_dict_with_variant_values_sends_asv(echo: EchoService) -> None:
    reply, received = echo.call("VarDict", ["dict:string:variant:name,string:x,n,int32:3"])
    assert received == [["VarDict", "a{sv}", [{"name": ["String", "x"], "n": ["Int32", 3]}]]], (
        reply,
        received,
    )


def test_typed_json_dict_with_variant_values_sends_asv(echo: EchoService) -> None:
    arg = {
        "type": "dict",
        "key_type": "string",
        "value_type": "variant",
        "value": {"name": {"type": "string", "value": "x"}, "n": {"type": "int32", "value": 3}},
    }
    reply, received = echo.call("VarDict", [arg])
    assert received == [["VarDict", "a{sv}", [{"name": ["String", "x"], "n": ["Int32", 3]}]]], (
        reply,
        received,
    )
    assert reply == DbusReply(reply="var-dict", signature="s")


@pytest.mark.qa("QA-ESC-003")
def test_typed_json_args_mixable(echo: EchoService) -> None:
    args: ArgList = [
        "string:x",
        {"type": "array", "element_type": "string", "value": ["a", "b"]},
        {
            "type": "dict",
            "key_type": "string",
            "value_type": "variant",
            "value": {"k": {"type": "int32", "value": 1}},
        },
    ]
    reply, received = echo.call("Mixed", args)
    assert received == [
        [
            "Mixed",
            "sasa{sv}",
            [["String", "x"], [["String", "a"], ["String", "b"]], {"k": ["Int32", 1]}],
        ]
    ], (reply, received)
    assert reply == DbusReply(reply="mixed", signature="s")


# ── Variants stay variants, with or without introspection ───────────────────


@pytest.mark.parametrize(
    "arg",
    ["variant:int32:3", {"type": "variant", "value_type": "int32", "value": 3}],
    ids=["dbus-send", "typed-json"],
)
def test_top_level_variant_arrives_as_variant(echo: EchoService, arg: str | dict) -> None:
    """The wire signature is 'v' on both services, as dbus-send sends it."""
    for service in (SERVICE, SERVICE_NOINTRO):
        reply, received = echo.call("OneVariant", [arg], service=service)
        assert received == [["OneVariant", "v", [["Int32", 3]]]], (service, reply, received)
        assert reply == DbusReply(reply="variant-ok", signature="s"), service


def test_explicit_variant_arg_does_not_match_non_variant(echo: EchoService) -> None:
    _output, received = echo.call_rejected("OneInt", ["variant:int32:5"])
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
    output, received = echo.call_rejected(method, [arg])
    assert sig in output, output
    assert received == [], received


def test_non_introspectable_service_gets_own_signatures(echo: EchoService) -> None:
    reply, received = echo.call("OneInt", ["int32:5"], service=SERVICE_NOINTRO)
    assert received == [["OneInt", "i", [["Int32", 5]]]], (reply, received)
    assert reply == DbusReply(reply=5, signature="i")


@pytest.mark.qa("QA-ESC-005")
def test_non_introspectable_send(echo: EchoService) -> None:
    """Without introspection each argument is marshalled with its own type."""
    args: ArgList = [
        "string:a",
        "int64:5",
        "boolean:true",
        {"type": "array", "element_type": "uint32", "value": [1, 2]},
        "byte:7",
    ]
    reply, received = echo.call("Loose", args, service=SERVICE_NOINTRO)
    assert received == [
        [
            "Loose",
            "sxbauy",
            [
                ["String", "a"],
                ["Int64", 5],
                ["Boolean", True],
                [["UInt32", 1], ["UInt32", 2]],
                ["Byte", 7],
            ],
        ]
    ], (reply, received)
    assert reply == DbusReply(reply="any", signature="s")


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
def test_argument_count_mismatch_is_rejected(echo: EchoService, method: str, args: ArgList) -> None:
    _output, received = echo.call_rejected(method, args)
    assert received == [], received


@pytest.mark.qa("QA-ESC-004")
def test_qt_overload_selection(echo: EchoService) -> None:
    reply, received = echo.call("Set", ["int32:1"], path=OVERLOAD_PATH)
    assert received == [["Set", "i", [["Int32", 1]]]], (reply, received)
    assert reply == DbusReply(reply=None, signature="")

    reply, received = echo.call("Set", ["string:a"], path=OVERLOAD_PATH)
    assert received == [["Set", "s", [["String", "a"]]]], (reply, received)

    output, received = echo.call_rejected("Set", ["double:1"], path=OVERLOAD_PATH)
    assert output == (f"D-Bus call failed: {INTERFACE}.Set takes 'i'/'s', got signature 'd'"), (
        output
    )
    assert received == [], received


def test_overload_error_lists_every_candidate(echo: EchoService) -> None:
    """KWin's loadScript is exported twice (s and ss); a third arg fails clearly."""
    with pytest.raises(ValueError, match=r"^D-Bus call failed:") as exc_info:
        echo.engine.dbus_call(
            "org.kde.KWin",
            "/Scripting",
            "org.kde.kwin.Scripting",
            "loadScript",
            ["string:a", "string:b", "string:c"],
        )
    output = str(exc_info.value)
    assert "'s'" in output and "'ss'" in output, output


def test_overloaded_kwin_load_script_accepts_both_arities(
    echo: EchoService, tmp_path: Path
) -> None:
    """KWin exports loadScript as (s) and (ss); both arities must succeed."""
    script = tmp_path / "probe.js"
    script.write_text("console.log('kwin-mcp dbus_call probe');\n", encoding="utf-8")
    arg_sets: list[list[str | TypedArg]] = [
        [f"string:{script}"],
        [f"string:{script}", "string:prfix23probe"],
    ]
    for args in arg_sets:
        reply = echo.engine.dbus_call(
            "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "loadScript", args
        )
        assert isinstance(reply.reply, int) and not isinstance(reply.reply, bool), reply
        assert reply.signature == "i", reply


# ── Replies and bus errors against real services ────────────────────────────


def test_void_method_with_matching_args_is_called_once(echo: EchoService) -> None:
    reply, received = echo.call("NoArgs")
    assert received == [["NoArgs", "", []]], (reply, received)
    assert reply == DbusReply(reply="no-args", signature="s")


def test_bus_get_id_returns_bare_value(echo: EchoService) -> None:
    reply = echo.engine.dbus_call(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetId"
    )
    assert reply.signature == "s", reply
    assert isinstance(reply.reply, str), reply
    assert len(reply.reply) == 32 and all(c in "0123456789abcdef" for c in reply.reply), reply
    assert reply.render() == f"{reply.reply}\n(reply signature 's')"


@pytest.mark.parametrize(
    "arg", ["boolean:false", {"type": "boolean", "value": False}], ids=["legacy", "typed"]
)
def test_kwin_void_method_returns_empty_reply(echo: EchoService, arg: str | dict) -> None:
    reply = echo.engine.dbus_call(
        "org.kde.KWin", "/KWin", "org.kde.KWin", "showDesktop", _args([arg])
    )
    assert reply == DbusReply(reply=None, signature="")
    assert reply.render() == "(no reply)"


def test_unknown_service_raises_dbus_error(echo: EchoService) -> None:
    with pytest.raises(
        RuntimeError, match=r"^D-Bus error: org\.freedesktop\.DBus\.Error\.ServiceUnknown"
    ):
        echo.engine.dbus_call("org.example.DefinitelyDoesNotExist", "/x", "org.example.X", "Foo")


@pytest.mark.qa("QA-ESC-006")
def test_remote_error_text(echo: EchoService) -> None:
    echo.clear()
    with pytest.raises(RuntimeError) as exc_info:
        echo.engine.dbus_call(SERVICE, OBJECT_PATH, INTERFACE, "Boom")
    assert str(exc_info.value) == "D-Bus error: org.test.Error: boom"
    assert [entry[0] for entry in echo.received()] == ["Boom"]


def _print_reply(echo: EchoService, method: str) -> str:
    """``dbus-send --print-reply`` of ``method``: the independent reply oracle."""
    return subprocess.run(
        [
            "dbus-send",
            f"--bus={echo.dbus_address}",
            "--print-reply",
            f"--dest={SERVICE}",
            OBJECT_PATH,
            f"{INTERFACE}.{method}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout


@pytest.mark.qa("QA-ESC-007")
def test_reply_and_signature_structured(echo: EchoService) -> None:
    # (method, reply, signature, text, value lines dbus-send prints)
    cases: list[tuple[str, Any, str, str, list[str]]] = [
        ("ReplyVoid", None, "", "(no reply)", []),
        ("ReplyBool", True, "b", "true\n(reply signature 'b')", ["boolean true"]),
        ("ReplyInt", 42, "i", "42\n(reply signature 'i')", ["int32 42"]),
        ("ReplyStr", "hello", "s", "hello\n(reply signature 's')", ['string "hello"']),
        (
            "ReplyDict",
            {"name": "x", "n": 3},
            "a{sv}",
            '{"name": "x", "n": 3}\n(reply signature \'a{sv}\')',
            ['string "name"', 'variant             string "x"', 'string "n"', "int32 3"],
        ),
        (
            "ReplyPair",
            [7, "x"],
            "is",
            "[7, \"x\"]\n(reply signature 'is')",
            ["int32 7", 'string "x"'],
        ),
    ]
    for method, value, signature, text, oracle_lines in cases:
        reply, _received = echo.call(method)
        assert reply == DbusReply(reply=value, signature=signature), method
        assert reply.render() == text, method

        printed = _print_reply(echo, method).splitlines()
        assert printed[0].startswith("method return"), printed
        values = [line.strip() for line in printed[1:] if line.strip()]
        for line in oracle_lines:
            assert any(item.endswith(line.strip()) for item in values), (method, line, values)
        if not oracle_lines:
            assert values == [], (method, values)


@pytest.mark.qa("QA-ESC-008")
def test_kwin_current_desktop_equals_oracle(echo: EchoService) -> None:
    reply = echo.engine.dbus_call("org.kde.KWin", "/KWin", "org.kde.KWin", "currentDesktop")

    current = KWinOracle(echo.dbus_address).current_desktop()
    assert current.x11_number is not None, current
    assert reply == DbusReply(reply=current.x11_number, signature="i")


@pytest.mark.qa("QA-ESC-009")
def test_dbus_args_never_shell(
    echo: EchoService, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The engine talks to the session's own bus, never the inherited one.
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    marker = tmp_path / "pwned"
    hostile = [
        f"/tmp/x.js; touch {marker}; rm -rf /",
        f"$(touch {marker})",
        f"`touch {marker}`",
    ]
    for text in hostile:
        reply, received = echo.call("OneStr", [f"string:{text}"])
        assert received == [["OneStr", "s", [["String", text]]]], (text, received)
        assert reply == DbusReply(reply=text, signature="s")

    # A hostile script path is only a string to KWin: it loads nothing or errors.
    try:
        echo.engine.dbus_call(
            "org.kde.KWin",
            "/Scripting",
            "org.kde.kwin.Scripting",
            "loadScript",
            [f"string:{hostile[0]}"],
        )
    except RuntimeError as exc:
        assert str(exc).startswith("D-Bus error: "), exc
    assert not marker.exists()

    big = "A" * 1_000_000
    try:
        reply, received = echo.call("OneStr", [f"string:{big}"])
    except RuntimeError as exc:
        assert str(exc).startswith("D-Bus error: "), str(exc)[:200]
    else:
        assert received == [["OneStr", "s", [["String", big]]]]
        assert reply.reply == big

    output, received = echo.call_rejected("OneStr", ["objpath:/a/../b"])
    assert "objpath" in output.lower() or "object path" in output.lower(), output
    assert received == [], received

    bad_targets = [
        ("org.kde.KWin\u0000x", OBJECT_PATH, INTERFACE, "OneStr"),
        (SERVICE, "relative", INTERFACE, "OneStr"),
        (SERVICE, OBJECT_PATH, "", "OneStr"),
        (SERVICE, OBJECT_PATH, INTERFACE, "a.b"),
    ]
    for service, path, interface, method in bad_targets:
        echo.clear()
        with pytest.raises((ValueError, RuntimeError)) as exc_info:
            echo.engine.dbus_call(service, path, interface, method, ["string:x"])
        assert str(exc_info.value).startswith(("D-Bus call failed: ", "D-Bus error: ")), (
            service,
            path,
            interface,
            method,
            str(exc_info.value),
        )
        assert echo.received() == [], (service, path, interface, method)

    assert not marker.exists()
    info = echo.engine.session_info()
    assert info.session is not None and info.session.dbus_address == echo.dbus_address


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--echo-service":
        _serve(Path(sys.argv[2]), sys.argv[3], sys.argv[4] == "yes")
    else:
        sys.exit(f"usage: {sys.argv[0]} --echo-service RECORD_PATH SERVICE_NAME yes|no")
