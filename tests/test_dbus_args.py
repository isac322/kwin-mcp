from __future__ import annotations

import typing

import dbus
import pytest

from kwin_mcp.dbus_args import (
    parse_arg,
    parse_dbus_send_arg,
    parse_typed_arg,
    to_dbus_send_string,
)

# ── Basic types ────────────────────────────────────────────────────────────


def test_string() -> None:
    result = parse_dbus_send_arg("string:hello")
    assert isinstance(result, dbus.String)
    assert result == "hello"


def test_string_empty() -> None:
    result = parse_dbus_send_arg("string:")
    assert isinstance(result, dbus.String)
    assert result == ""


def test_string_with_colons_kept_in_value() -> None:
    result = parse_dbus_send_arg("string:hello:world:!")
    assert isinstance(result, dbus.String)
    assert result == "hello:world:!"


def test_int16() -> None:
    result = parse_dbus_send_arg("int16:-32000")
    assert isinstance(result, dbus.Int16)
    assert result == -32000


def test_int32() -> None:
    result = parse_dbus_send_arg("int32:42")
    assert isinstance(result, dbus.Int32)
    assert result == 42


def test_int64() -> None:
    result = parse_dbus_send_arg("int64:-9223372036854775808")
    assert isinstance(result, dbus.Int64)
    assert result == -9223372036854775808


def test_uint16() -> None:
    result = parse_dbus_send_arg("uint16:65535")
    assert isinstance(result, dbus.UInt16)
    assert result == 65535


def test_uint32() -> None:
    result = parse_dbus_send_arg("uint32:4294967295")
    assert isinstance(result, dbus.UInt32)
    assert result == 4294967295


def test_uint64() -> None:
    result = parse_dbus_send_arg("uint64:18446744073709551615")
    assert isinstance(result, dbus.UInt64)
    assert result == 18446744073709551615


def test_byte() -> None:
    result = parse_dbus_send_arg("byte:255")
    assert isinstance(result, dbus.Byte)
    assert result == 255


def test_double() -> None:
    result = parse_dbus_send_arg("double:3.14")
    assert isinstance(result, dbus.Double)
    assert result == pytest.approx(3.14)


def test_boolean_true() -> None:
    result = parse_dbus_send_arg("boolean:true")
    assert isinstance(result, dbus.Boolean)
    assert bool(result) is True


def test_boolean_false() -> None:
    result = parse_dbus_send_arg("boolean:false")
    assert isinstance(result, dbus.Boolean)
    assert bool(result) is False


def test_boolean_uppercase() -> None:
    result = parse_dbus_send_arg("boolean:TRUE")
    assert isinstance(result, dbus.Boolean)
    assert bool(result) is True


def test_boolean_mixed_case() -> None:
    result = parse_dbus_send_arg("boolean:False")
    assert isinstance(result, dbus.Boolean)
    assert bool(result) is False


def test_objpath() -> None:
    result = parse_dbus_send_arg("objpath:/org/kde/KWin")
    assert isinstance(result, dbus.ObjectPath)
    assert result == "/org/kde/KWin"


def test_signature() -> None:
    result = parse_dbus_send_arg("signature:s")
    assert isinstance(result, dbus.Signature)
    assert result == "s"


# ── Arrays ─────────────────────────────────────────────────────────────────


def test_array_strings() -> None:
    result = parse_dbus_send_arg("array:string:a,b,c")
    assert isinstance(result, dbus.Array)
    assert result.signature == "s"
    assert list(result) == ["a", "b", "c"]
    assert all(isinstance(item, dbus.String) for item in result)


def test_array_int32() -> None:
    result = parse_dbus_send_arg("array:int32:1,2,3")
    assert isinstance(result, dbus.Array)
    assert result.signature == "i"
    assert list(result) == [1, 2, 3]
    assert all(isinstance(item, dbus.Int32) for item in result)


def test_array_int64() -> None:
    result = parse_dbus_send_arg("array:int64:100,200,300")
    assert isinstance(result, dbus.Array)
    assert result.signature == "x"
    assert list(result) == [100, 200, 300]
    assert all(isinstance(item, dbus.Int64) for item in result)


def test_array_boolean() -> None:
    result = parse_dbus_send_arg("array:boolean:true,false,true")
    assert isinstance(result, dbus.Array)
    assert result.signature == "b"
    assert [bool(x) for x in result] == [True, False, True]


def test_array_empty() -> None:
    result = parse_dbus_send_arg("array:string:")
    assert isinstance(result, dbus.Array)
    assert result.signature == "s"
    assert list(result) == []


def test_array_single_element() -> None:
    result = parse_dbus_send_arg("array:string:only")
    assert isinstance(result, dbus.Array)
    assert list(result) == ["only"]


# ── Dictionaries ───────────────────────────────────────────────────────────


def test_dict_string_string() -> None:
    result = parse_dbus_send_arg("dict:string:string:k1:v1,k2:v2")
    assert isinstance(result, dbus.Dictionary)
    assert result.signature == "ss"
    assert dict(result) == {"k1": "v1", "k2": "v2"}


def test_dict_string_int32() -> None:
    result = parse_dbus_send_arg("dict:string:int32:key1:1,key2:2")
    assert isinstance(result, dbus.Dictionary)
    assert result.signature == "si"
    assert dict(result) == {"key1": 1, "key2": 2}
    for v in result.values():
        assert isinstance(v, dbus.Int32)


def test_dict_empty() -> None:
    result = parse_dbus_send_arg("dict:string:int32:")
    assert isinstance(result, dbus.Dictionary)
    assert result.signature == "si"
    assert dict(result) == {}


def test_dict_single_pair() -> None:
    result = parse_dbus_send_arg("dict:string:string:only:one")
    assert isinstance(result, dbus.Dictionary)
    assert dict(result) == {"only": "one"}


# ── Variants ───────────────────────────────────────────────────────────────


def test_variant_string() -> None:
    result = parse_dbus_send_arg("variant:string:hello")
    assert isinstance(result, dbus.String)
    assert result == "hello"


def test_variant_int32() -> None:
    result = parse_dbus_send_arg("variant:int32:42")
    assert isinstance(result, dbus.Int32)
    assert result == 42


def test_variant_boolean() -> None:
    result = parse_dbus_send_arg("variant:boolean:true")
    assert isinstance(result, dbus.Boolean)
    assert bool(result) is True


# ── Nested containers ──────────────────────────────────────────────────────


def test_nested_array_of_arrays() -> None:
    result = parse_dbus_send_arg("array:array:string:a,b:c,d")
    assert isinstance(result, dbus.Array)
    assert result.signature == "as"
    assert len(result) == 2
    assert list(result[0]) == ["a", "b"]
    assert list(result[1]) == ["c", "d"]


def test_array_of_variants() -> None:
    result = parse_dbus_send_arg("array:variant:string:one:two")
    assert isinstance(result, dbus.Array)
    assert result.signature == "v"
    assert list(result) == ["one", "two"]


# ── Return type contract ───────────────────────────────────────────────────


def test_returns_dbus_type_not_python_int() -> None:
    result = parse_dbus_send_arg("int32:7")
    assert type(result) is not int
    assert isinstance(result, dbus.Int32)


def test_returns_dbus_type_not_python_str() -> None:
    result = parse_dbus_send_arg("string:foo")
    assert type(result) is not str
    assert isinstance(result, dbus.String)


def test_returns_dbus_type_not_python_bool() -> None:
    result = parse_dbus_send_arg("boolean:true")
    assert type(result) is not bool
    assert isinstance(result, dbus.Boolean)


# ── Errors ─────────────────────────────────────────────────────────────────


def test_error_unknown_type() -> None:
    with pytest.raises(ValueError, match=r"dbus-send arg: unknown type 'foobar'"):
        parse_dbus_send_arg("foobar:42")


def test_error_missing_colon() -> None:
    with pytest.raises(ValueError, match=r"dbus-send arg: expected 'type:value' format"):
        parse_dbus_send_arg("nocolon")


def test_error_invalid_int32() -> None:
    with pytest.raises(ValueError, match=r"dbus-send arg: invalid int32 value 'notanint'"):
        parse_dbus_send_arg("int32:notanint")


def test_error_invalid_int64() -> None:
    with pytest.raises(ValueError, match=r"dbus-send arg: invalid int64 value"):
        parse_dbus_send_arg("int64:abc")


def test_error_invalid_boolean() -> None:
    with pytest.raises(
        ValueError,
        match=r"dbus-send arg: boolean must be 'true' or 'false', got 'yes'",
    ):
        parse_dbus_send_arg("boolean:yes")


def test_error_invalid_double() -> None:
    with pytest.raises(ValueError, match=r"dbus-send arg: invalid double value"):
        parse_dbus_send_arg("double:notafloat")


def test_error_int_out_of_range() -> None:
    with pytest.raises(ValueError, match=r"dbus-send arg: byte value .* out of range"):
        parse_dbus_send_arg("byte:300")


def test_error_uint_negative() -> None:
    with pytest.raises(ValueError, match=r"dbus-send arg: uint32 value .* out of range"):
        parse_dbus_send_arg("uint32:-1")


def test_error_objpath_no_leading_slash() -> None:
    with pytest.raises(ValueError, match=r"dbus-send arg: objpath must start with '/'"):
        parse_dbus_send_arg("objpath:no-slash")


def test_error_dict_pair_missing_colon() -> None:
    with pytest.raises(ValueError, match=r"dbus-send arg: dict pair must be 'key:value'"):
        parse_dbus_send_arg("dict:string:string:keyonly")


def test_error_dict_container_value_unsupported() -> None:
    with pytest.raises(ValueError, match=r"dict with container value type is not supported"):
        parse_dbus_send_arg("dict:string:array:string:k1:a,b")


def test_error_message_prefix_consistent() -> None:
    cases = [
        "foobar:42",
        "nocolon",
        "int32:notanint",
        "boolean:yes",
        "byte:300",
        "objpath:relative",
    ]
    for case in cases:
        with pytest.raises(ValueError) as exc_info:
            parse_dbus_send_arg(case)
        assert str(exc_info.value).startswith("dbus-send arg:"), case


# ── Typed-JSON parser ──────────────────────────────────────────────────────


def test_typed_string() -> None:
    result = parse_typed_arg({"type": "string", "value": "hello"})
    assert isinstance(result, dbus.String)
    assert result == "hello"


def test_typed_int32() -> None:
    result = parse_typed_arg({"type": "int32", "value": 42})
    assert isinstance(result, dbus.Int32)
    assert result == 42


def test_typed_boolean() -> None:
    result = parse_typed_arg({"type": "boolean", "value": True})
    assert isinstance(result, dbus.Boolean)
    assert bool(result) is True


def test_typed_double() -> None:
    result = parse_typed_arg({"type": "double", "value": 3.14})
    assert isinstance(result, dbus.Double)
    assert result == pytest.approx(3.14)


def test_typed_objpath() -> None:
    result = parse_typed_arg({"type": "objpath", "value": "/org/kde/KWin"})
    assert isinstance(result, dbus.ObjectPath)
    assert result == "/org/kde/KWin"


def test_typed_array() -> None:
    result = parse_typed_arg({"type": "array", "element_type": "string", "value": ["a", "b", "c"]})
    assert isinstance(result, dbus.Array)
    assert result.signature == "s"
    assert list(result) == ["a", "b", "c"]


def test_typed_array_int32() -> None:
    result = parse_typed_arg({"type": "array", "element_type": "int32", "value": [1, 2, 3]})
    assert isinstance(result, dbus.Array)
    assert result.signature == "i"
    assert list(result) == [1, 2, 3]


def test_typed_array_empty() -> None:
    result = parse_typed_arg({"type": "array", "element_type": "string", "value": []})
    assert isinstance(result, dbus.Array)
    assert result.signature == "s"
    assert list(result) == []


def test_typed_dict() -> None:
    result = parse_typed_arg(
        {
            "type": "dict",
            "key_type": "string",
            "value_type": "int32",
            "value": {"k1": 1, "k2": 2},
        }
    )
    assert isinstance(result, dbus.Dictionary)
    assert result.signature == "si"
    assert dict(result) == {"k1": 1, "k2": 2}


def test_typed_variant() -> None:
    result = parse_typed_arg({"type": "variant", "value_type": "string", "value": "hello"})
    assert isinstance(result, dbus.String)
    assert result == "hello"


def test_typed_error_missing_value_key() -> None:
    with pytest.raises(ValueError, match=r"must have 'type' and 'value'"):
        parse_typed_arg({"type": "string"})


def test_typed_error_missing_type_key() -> None:
    with pytest.raises(ValueError, match=r"must have 'type' and 'value'"):
        parse_typed_arg({"value": "hello"})


def test_typed_error_unknown_type() -> None:
    with pytest.raises(ValueError, match=r"unknown typed-JSON type 'mystery'"):
        parse_typed_arg({"type": "mystery", "value": 1})


def test_typed_error_string_with_int_value() -> None:
    with pytest.raises(ValueError, match=r"string value must be str"):
        parse_typed_arg({"type": "string", "value": 42})


def test_typed_error_int_with_string_value() -> None:
    with pytest.raises(ValueError, match=r"int32 value must be int"):
        parse_typed_arg({"type": "int32", "value": "not-an-int"})


def test_typed_error_boolean_rejects_int() -> None:
    with pytest.raises(ValueError, match=r"boolean value must be bool"):
        parse_typed_arg({"type": "boolean", "value": 1})


def test_typed_error_array_missing_element_type() -> None:
    with pytest.raises(ValueError, match=r"array requires 'element_type'"):
        parse_typed_arg({"type": "array", "value": []})


def test_typed_error_array_value_not_list() -> None:
    with pytest.raises(ValueError, match=r"array 'value' must be a list"):
        parse_typed_arg({"type": "array", "element_type": "string", "value": "not-a-list"})


def test_typed_error_dict_value_not_dict() -> None:
    with pytest.raises(ValueError, match=r"dict 'value' must be a dict"):
        parse_typed_arg(
            {
                "type": "dict",
                "key_type": "string",
                "value_type": "int32",
                "value": ["k", 1],
            }
        )


# ── Dispatcher (parse_arg) ─────────────────────────────────────────────────


def test_dispatch_legacy_string() -> None:
    assert type(parse_arg("string:hello")) is dbus.String
    assert parse_arg("string:hello") == "hello"


def test_dispatch_legacy_int32() -> None:
    assert type(parse_arg("int32:42")) is dbus.Int32
    assert parse_arg("int32:42") == 42


def test_dispatch_typed_dict() -> None:
    result = parse_arg({"type": "int32", "value": 42})
    assert type(result) is dbus.Int32
    assert result == 42


def test_dispatch_mixed_per_arg() -> None:
    args: list[str | dict] = [
        "string:hello",
        {"type": "int32", "value": 42},
        "boolean:true",
    ]
    parsed = [parse_arg(a) for a in args]
    assert isinstance(parsed[0], dbus.String) and parsed[0] == "hello"
    assert isinstance(parsed[1], dbus.Int32) and parsed[1] == 42
    assert isinstance(parsed[2], dbus.Boolean) and bool(parsed[2]) is True


def test_dispatch_rejects_non_str_non_dict() -> None:
    bad: object = 42
    with pytest.raises(ValueError, match=r"arg must be str or dict, got int"):
        parse_arg(typing.cast("str", bad))


def test_dispatch_rejects_list() -> None:
    bad: object = ["string:hello"]
    with pytest.raises(ValueError, match=r"arg must be str or dict, got list"):
        parse_arg(typing.cast("str", bad))


# ── Bridge (to_dbus_send_string) ───────────────────────────────────────────


def test_bridge_passthrough_legacy() -> None:
    assert to_dbus_send_string("string:hello") == "string:hello"
    assert to_dbus_send_string("int32:42") == "int32:42"


def test_bridge_typed_basic() -> None:
    assert to_dbus_send_string({"type": "string", "value": "hello"}) == "string:hello"
    assert to_dbus_send_string({"type": "int32", "value": 42}) == "int32:42"
    assert to_dbus_send_string({"type": "boolean", "value": True}) == "boolean:true"
    assert to_dbus_send_string({"type": "boolean", "value": False}) == "boolean:false"


def test_bridge_typed_array() -> None:
    out = to_dbus_send_string({"type": "array", "element_type": "string", "value": ["a", "b", "c"]})
    assert out == "array:string:a,b,c"


def test_bridge_typed_dict() -> None:
    out = to_dbus_send_string(
        {
            "type": "dict",
            "key_type": "string",
            "value_type": "int32",
            "value": {"k1": 1, "k2": 2},
        }
    )
    assert out.startswith("dict:string:int32:")
    parts = sorted(out[len("dict:string:int32:") :].split(","))
    assert parts == ["k1:1", "k2:2"]


def test_bridge_typed_variant() -> None:
    assert (
        to_dbus_send_string({"type": "variant", "value_type": "string", "value": "x"})
        == "variant:string:x"
    )


def test_bridge_round_trip() -> None:
    typed = {"type": "int32", "value": 7}
    rendered = to_dbus_send_string(typed)
    via_string = parse_dbus_send_arg(rendered)
    via_typed = parse_typed_arg(typed)
    assert type(via_string) is type(via_typed) is dbus.Int32
    assert via_string == via_typed == 7


def test_bridge_rejects_string_with_comma() -> None:
    with pytest.raises(ValueError, match=r"contains ',' or ':'"):
        to_dbus_send_string({"type": "string", "value": "has,comma"})


def test_bridge_rejects_invalid_legacy() -> None:
    with pytest.raises(ValueError, match=r"unknown type"):
        to_dbus_send_string("foobar:42")
