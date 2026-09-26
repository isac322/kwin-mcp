"""Parser for dbus-send-style argument strings.

Converts the textual format used by ``dbus-send(1)`` into typed
``dbus.types.*`` instances suitable for passing through dbus-python's
``Interface.<method>(...)`` calls. This replaces the historical reliance
on ``subprocess.run(["dbus-send", ...])`` for the generic ``dbus_call``
MCP tool while preserving the wire format the agent already knows.

Supported types follow the ``dbus-send(1)`` man page, plus recursive
array nesting:

    string, int16, uint16, int32, uint32, int64, uint64, double, byte,
    boolean, objpath, signature
    array:TYPE:V1,V2,...
    dict:KTYPE:VTYPE:K1,V1,K2,V2,...
    dict:KTYPE:variant:K1,TYPE:V1,K2,TYPE:V2,...
    variant:TYPE:VALUE

Dict entries use dbus-send's grammar: one comma-separated list that
alternates keys and values. Dict keys must be basic types and dict values
must be basic types or ``variant``; a variant value carries its own
``TYPE:VALUE`` (for example ``dict:string:variant:name,string:x,n,int32:3``
builds an ``a{sv}``). Integers accept the same literal prefixes as
dbus-send's ``strtol(value, NULL, 0)`` (decimal and ``0x`` hexadecimal),
but unlike dbus-send the whole literal must parse and fit the type.
``unixfd`` is not supported, matching dbus-send.

When an array's element type is itself a container, elements are
separated by ``':'`` instead of ``','`` to disambiguate the inner
comma-separated values.

Examples:
    >>> parse_dbus_send_arg("string:hello")
    dbus.String('hello')
    >>> parse_dbus_send_arg("int32:42")
    dbus.Int32(42)
    >>> parse_dbus_send_arg("array:string:a,b,c")
    dbus.Array([dbus.String('a'), ...], signature=dbus.Signature('s'))

All malformed input raises :class:`ValueError` with a message that
starts with ``"dbus-send arg:"``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import dbus

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["parse_arg", "parse_dbus_send_arg", "parse_typed_arg"]


# ── Type tables ────────────────────────────────────────────────────────────

_BASIC_SIGNATURES: dict[str, str] = {
    "string": "s",
    "int16": "n",
    "uint16": "q",
    "int32": "i",
    "uint32": "u",
    "int64": "x",
    "uint64": "t",
    "byte": "y",
    "double": "d",
    "boolean": "b",
    "objpath": "o",
    "signature": "g",
}

_BASIC_TYPES: frozenset[str] = frozenset(_BASIC_SIGNATURES.keys())
_CONTAINER_TYPES: frozenset[str] = frozenset({"array", "dict", "variant"})


# ── Helpers ────────────────────────────────────────────────────────────────


def _err(msg: str) -> ValueError:
    return ValueError(f"dbus-send arg: {msg}")


def _split_once(s: str) -> tuple[str, str]:
    parts = s.split(":", 1)
    if len(parts) != 2:
        raise _err(f"expected 'type:value' format, got {s!r}")
    return parts[0], parts[1]


def _parse_int(value: str, type_name: str, *, signed: bool, bits: int) -> int:
    """Parse an integer literal and bounds-check it against the dbus type."""
    try:
        v = int(value, 0)
    except ValueError as exc:
        raise _err(f"invalid {type_name} value {value!r}") from exc
    if signed:
        lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    else:
        lo, hi = 0, (1 << bits) - 1
    if not (lo <= v <= hi):
        raise _err(f"{type_name} value {v} out of range [{lo}, {hi}]")
    return v


def _parse_double(value: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise _err(f"invalid double value {value!r}") from exc


def _parse_boolean(value: str) -> bool:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise _err(f"boolean must be 'true' or 'false', got {value!r}")


def _parse_basic(type_name: str, value: str) -> object:
    if type_name == "string":
        return dbus.String(value)
    if type_name == "objpath":
        if not value.startswith("/"):
            raise _err(f"objpath must start with '/', got {value!r}")
        return dbus.ObjectPath(value)
    if type_name == "signature":
        return dbus.Signature(value)
    if type_name == "boolean":
        return dbus.Boolean(_parse_boolean(value))
    if type_name == "double":
        return dbus.Double(_parse_double(value))
    if type_name == "byte":
        return dbus.Byte(_parse_int(value, "byte", signed=False, bits=8))
    if type_name == "int16":
        return dbus.Int16(_parse_int(value, "int16", signed=True, bits=16))
    if type_name == "uint16":
        return dbus.UInt16(_parse_int(value, "uint16", signed=False, bits=16))
    if type_name == "int32":
        return dbus.Int32(_parse_int(value, "int32", signed=True, bits=32))
    if type_name == "uint32":
        return dbus.UInt32(_parse_int(value, "uint32", signed=False, bits=32))
    if type_name == "int64":
        return dbus.Int64(_parse_int(value, "int64", signed=True, bits=64))
    if type_name == "uint64":
        return dbus.UInt64(_parse_int(value, "uint64", signed=False, bits=64))
    raise _err(f"unknown type {type_name!r}")


def _peel_element_type(s: str) -> tuple[str, str]:
    """Peel the element type-prefix off the front of a container value-spec.

    Returns ``(type_prefix, remaining_values_str)`` where ``type_prefix``
    is the colon-joined type chain describing one element (e.g.
    ``"string"``, ``"array:string"``, ``"dict:string:int32"``), and
    ``remaining_values_str`` is whatever follows it.
    """
    type_, rest = _split_once(s)
    if type_ in _BASIC_TYPES:
        return type_, rest
    if type_ == "array":
        sub, rest2 = _peel_element_type(rest)
        return f"array:{sub}", rest2
    if type_ == "variant":
        sub, rest2 = _peel_element_type(rest)
        return f"variant:{sub}", rest2
    if type_ == "dict":
        ktype, rest2 = _peel_element_type(rest)
        if ktype not in _BASIC_TYPES:
            raise _err(f"dict key type must be basic, got {ktype!r}")
        vtype, rest3 = _peel_element_type(rest2)
        return f"dict:{ktype}:{vtype}", rest3
    raise _err(f"unknown type {type_!r}")


def _signature_of(type_prefix: str) -> str:
    return _signature_of_parts(type_prefix.split(":"))


def _signature_of_parts(parts: list[str]) -> str:
    if not parts:
        raise _err("empty type")
    head = parts[0]
    if head in _BASIC_SIGNATURES:
        if len(parts) != 1:
            raise _err(f"basic type {head!r} cannot have sub-types")
        return _BASIC_SIGNATURES[head]
    if head == "array":
        return "a" + _signature_of_parts(parts[1:])
    if head == "dict":
        if len(parts) < 3:
            raise _err("dict needs key and value types")
        if parts[1] not in _BASIC_SIGNATURES:
            raise _err(f"dict key type must be basic, got {parts[1]!r}")
        return "a{" + _BASIC_SIGNATURES[parts[1]] + _signature_of_parts(parts[2:]) + "}"
    if head == "variant":
        return "v"
    raise _err(f"unknown type {head!r}")


def _is_container_prefix(prefix: str) -> bool:
    return prefix.split(":", 1)[0] in _CONTAINER_TYPES


# ── Public API ─────────────────────────────────────────────────────────────


def parse_dbus_send_arg(s: str) -> object:
    """Parse a single ``dbus-send``-style argument into a typed value.

    Args:
        s: A string like ``"string:hello"`` or ``"array:int32:1,2,3"``.

    Returns:
        A ``dbus.types.*`` instance. For ``variant:...`` the inner typed
        value is returned directly; dbus-python infers the surrounding
        variant from the introspection signature.

    Raises:
        ValueError: If the input is malformed. The message always begins
            with ``"dbus-send arg:"``.
    """
    type_, rest = _split_once(s)

    if type_ in _BASIC_TYPES:
        return _parse_basic(type_, rest)
    if type_ == "array":
        return _parse_array_rest(rest)
    if type_ == "dict":
        return _parse_dict_rest(rest)
    if type_ == "variant":
        return parse_dbus_send_arg(rest)
    raise _err(f"unknown type {type_!r}")


# ── Container parsers ──────────────────────────────────────────────────────


def _parse_array_rest(rest: str) -> object:
    """Parse the substring after ``array:`` into a ``dbus.Array``."""
    elem_prefix, values_str = _peel_element_type(rest)
    sig = _signature_of(elem_prefix)
    if values_str == "":
        return dbus.Array([], signature=sig)
    sep = ":" if _is_container_prefix(elem_prefix) else ","
    pieces = values_str.split(sep)
    items = [parse_dbus_send_arg(f"{elem_prefix}:{piece}") for piece in pieces]
    return dbus.Array(items, signature=sig)


def _parse_variant_basic(spec: str) -> object:
    """Parse a ``TYPE:VALUE`` variant payload whose type must be basic."""
    type_, value = _split_once(spec)
    if type_ not in _BASIC_TYPES:
        raise _err(f"dict variant value must have a basic type, got {type_!r}")
    return _parse_basic(type_, value)


def _parse_dict_rest(rest: str) -> object:
    """Parse the substring after ``dict:`` into a ``dbus.Dictionary``.

    Follows dbus-send's ``append_dict``: ``KTYPE:VTYPE:`` then a single
    comma-separated list alternating keys and values. A ``variant`` value
    type makes every value a ``TYPE:VALUE`` payload with a basic type.
    """
    ktype, after_k = _split_once(rest)
    if ktype not in _BASIC_TYPES:
        raise _err(f"dict key type must be basic, got {ktype!r}")
    vtype, values_str = _split_once(after_k)
    if vtype == "variant":
        vsig = "v"
    elif vtype in _BASIC_TYPES:
        vsig = _BASIC_SIGNATURES[vtype]
    else:
        raise _err(f"dict value type must be basic or variant, got {vtype!r}")
    full_sig = _BASIC_SIGNATURES[ktype] + vsig
    if values_str == "":
        return dbus.Dictionary({}, signature=full_sig)
    items = values_str.split(",")
    if len(items) % 2:
        raise _err(f"dict entries must alternate key,value separated by ',', got {values_str!r}")
    out: dict[object, object] = {}
    for raw_key, raw_value in zip(items[::2], items[1::2], strict=True):
        key = _parse_basic(ktype, raw_key)
        if vtype == "variant":
            out[key] = _parse_variant_basic(raw_value)
        else:
            out[key] = _parse_basic(vtype, raw_value)
    return dbus.Dictionary(out, signature=full_sig)


# ── Typed JSON args (option C: backward-compatible widening) ───────────────
#
# Accepts arguments shaped as ``{"type": "<type-name>", "value": <python>}``
# in addition to the legacy ``"type:value"`` strings. ``parse_arg`` is the
# top-level dispatcher that routes each arg to the right backend based on
# its Python type.
#
# Supported typed-JSON shapes:
#
#   Basic:    {"type": "<basic>", "value": <primitive>}
#   Array:    {"type": "array", "element_type": "<basic>",
#              "value": [<primitive>, ...]}
#   Dict:     {"type": "dict", "key_type": "<basic>",
#              "value_type": "<basic>", "value": {<key>: <value>, ...}}
#   a{Kv}:    {"type": "dict", "key_type": "<basic>", "value_type": "variant",
#              "value": {<key>: {"type": "<basic>", "value": <primitive>}}}
#   Variant:  {"type": "variant", "value_type": "<basic>",
#              "value": <primitive>}
#
# Array elements, dict keys and variant payloads are restricted to basic
# types, and dict values to basic types or variants of basic types, which is
# the same set the dbus-send string syntax can express.


def _coerce_basic(type_name: str, value: object) -> object:
    """Validate-and-wrap a Python primitive for a basic D-Bus type."""
    if type_name == "string":
        if not isinstance(value, str):
            raise _err(f"string value must be str, got {type(value).__name__}")
        return dbus.String(value)
    if type_name == "objpath":
        if not isinstance(value, str):
            raise _err(f"objpath value must be str, got {type(value).__name__}")
        if not value.startswith("/"):
            raise _err(f"objpath must start with '/', got {value!r}")
        return dbus.ObjectPath(value)
    if type_name == "signature":
        if not isinstance(value, str):
            raise _err(f"signature value must be str, got {type(value).__name__}")
        return dbus.Signature(value)
    if type_name == "boolean":
        if not isinstance(value, bool):
            raise _err(f"boolean value must be bool, got {type(value).__name__}")
        return dbus.Boolean(value)
    if type_name == "double":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise _err(f"double value must be number, got {type(value).__name__}")
        return dbus.Double(float(value))
    if type_name in _BASIC_TYPES:
        if not isinstance(value, int) or isinstance(value, bool):
            raise _err(f"{type_name} value must be int, got {type(value).__name__}")
        return _parse_basic(type_name, str(value))
    raise _err(f"unknown type {type_name!r}")


def _typed_variant_payload(value: object) -> object:
    """Parse a typed-JSON dict value stored in a variant: ``{"type", "value"}``."""
    if not isinstance(value, dict) or "type" not in value or "value" not in value:
        raise _err(
            "typed-JSON dict with value_type 'variant' needs values shaped "
            f'{{"type": <basic>, "value": ...}}; got {value!r}'
        )
    type_name = value["type"]
    if not isinstance(type_name, str) or type_name not in _BASIC_TYPES:
        raise _err(f"typed-JSON variant payload type must be basic, got {type_name!r}")
    return _coerce_basic(type_name, value["value"])


def parse_typed_arg(d: Mapping[str, object]) -> object:
    """Parse a single typed-JSON arg into a ``dbus.types.*`` instance."""
    if not isinstance(d, dict):
        raise _err(f"typed-JSON arg must be a dict, got {type(d).__name__}")
    if "type" not in d or "value" not in d:
        raise _err(
            f"typed-JSON arg must have 'type' and 'value' keys; got keys {sorted(d.keys())!r}"
        )
    type_name = d["type"]
    value = d["value"]
    if not isinstance(type_name, str):
        raise _err(f"'type' must be a string, got {type(type_name).__name__}")

    if type_name in _BASIC_TYPES:
        return _coerce_basic(type_name, value)

    if type_name == "array":
        elem_type = d.get("element_type")
        if not isinstance(elem_type, str) or elem_type not in _BASIC_TYPES:
            raise _err(
                f"typed-JSON array requires 'element_type' = a basic type name; got {elem_type!r}"
            )
        if not isinstance(value, list):
            raise _err(f"typed-JSON array 'value' must be a list, got {type(value).__name__}")
        items = [_coerce_basic(elem_type, v) for v in value]
        return dbus.Array(items, signature=_BASIC_SIGNATURES[elem_type])

    if type_name == "dict":
        key_type = d.get("key_type")
        val_type = d.get("value_type")
        if not isinstance(key_type, str) or key_type not in _BASIC_TYPES:
            raise _err(f"typed-JSON dict requires 'key_type' = a basic type name; got {key_type!r}")
        if not isinstance(val_type, str) or (
            val_type not in _BASIC_TYPES and val_type != "variant"
        ):
            raise _err(
                "typed-JSON dict requires 'value_type' = a basic type name or 'variant'; "
                f"got {val_type!r}"
            )
        if not isinstance(value, dict):
            raise _err(f"typed-JSON dict 'value' must be a dict, got {type(value).__name__}")
        out: dict[object, object] = {}
        for k, v in value.items():
            key = _coerce_basic(key_type, k)
            if val_type == "variant":
                out[key] = _typed_variant_payload(v)
            else:
                out[key] = _coerce_basic(val_type, v)
        vsig = "v" if val_type == "variant" else _BASIC_SIGNATURES[val_type]
        return dbus.Dictionary(out, signature=_BASIC_SIGNATURES[key_type] + vsig)

    if type_name == "variant":
        val_type = d.get("value_type")
        if not isinstance(val_type, str) or val_type not in _BASIC_TYPES:
            raise _err(
                f"typed-JSON variant requires 'value_type' = a basic type name; got {val_type!r}"
            )
        return _coerce_basic(val_type, value)

    raise _err(f"unknown typed-JSON type {type_name!r}")


def parse_arg(arg: str | Mapping[str, object]) -> object:
    """Top-level per-arg dispatcher: legacy string OR typed-JSON dict.

    Strings are routed to :func:`parse_dbus_send_arg`. Dicts are routed to
    :func:`parse_typed_arg`. Anything else raises :class:`ValueError`.
    """
    if isinstance(arg, str):
        return parse_dbus_send_arg(arg)
    if isinstance(arg, dict):
        return parse_typed_arg(arg)
    raise _err(f"arg must be str or dict, got {type(arg).__name__}")
