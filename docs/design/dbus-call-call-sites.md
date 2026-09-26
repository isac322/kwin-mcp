# dbus_call argument reference

`dbus_call` has exactly one internal call site: `server.py` wraps
`AutomationEngine.dbus_call`. All arguments are pass-through from MCP clients,
so the accepted grammar is the full public contract.

| Location | Role | Args type |
|---|---|---|
| `src/kwin_mcp/core.py` `AutomationEngine.dbus_call` | Implementation | `args: list[str \| dict] \| None`, parsed by `kwin_mcp.dbus_args.parse_arg` and sent in-process with dbus-python |
| `src/kwin_mcp/server.py` `dbus_call` | MCP tool schema + wrapper | Annotated `list[str \| dict] \| None`; the description documents both argument shapes |

## Argument grammar

Legacy arguments follow the `dbus-send(1)` syntax. Because agents may use any
of it, `kwin_mcp.dbus_args` accepts all dbus-send types:

| Type prefix | Example |
|---|---|
| `string:` | `string:hello` |
| `int16:` `int32:` `int64:` | `int32:42` |
| `uint16:` `uint32:` `uint64:` | `uint32:1234` |
| `byte:` | `byte:255` |
| `double:` | `double:3.14` |
| `boolean:` | `boolean:true` |
| `objpath:` | `objpath:/org/kde/KWin` |
| `signature:` | `signature:s` |
| `array:` | `array:string:a,b,c` (nested arrays separate elements with `:`) |
| `dict:` | `dict:string:int32:k1,1,k2,2`, `dict:string:variant:k,string:v` |
| `variant:` | `variant:string:hello` |

Dict entries follow dbus-send's grammar: one comma-separated list alternating
keys and values, and a `variant` value type makes each value a `TYPE:VALUE`
pair with a basic type (an `a{sv}`). Integer literals accept an optional sign,
`0x` hexadecimal, and decimals that do not start with `0`; the whole literal
must parse and fit the type (dbus-send instead sends `12x3` as `12` and wraps
overflows). Leading-zero octal such as `010` is rejected rather than
reinterpreted. `unixfd` is not supported: dbus-send has no such type, and an
fd number from an MCP client would not refer to a descriptor in the server
process.

Typed JSON is the second shape: `{"type": <basic>, "value": ...}` for basic
types, plus `array` (`element_type` + list `value`), `dict` (`key_type` +
`value_type` + object `value`), and `variant` (`value_type` + `value`). Dict
values may also be variants when `value_type` is `"variant"` and each entry is
a `{"type", "value"}` pair.

## Argument checking

`dbus_call` introspects the target object once per call. When the method is
described, the arguments must match one of its declared input signatures and
are marshalled with that signature: the count must fit, and each argument's
written type must equal the declared complete type, except that a declared
variant (`v`) accepts any value and an explicit `variant` argument only fits a
`v` position. Qt exports overloaded slots and slots with default arguments as
several `<method>` entries under one name (KWin's `org.kde.kwin.Scripting`
`loadScript` is `s` and `ss`), so every signature is a candidate and the first
fitting one is used; when none fits, the error lists every candidate. A
top-level `variant` argument is marshalled as `v` (`variant_level` = 1), so it
stays a variant on the wire even without introspection. When the object does
not describe the method, each argument is sent with its own signature and the
remote side validates it. Each D-Bus round trip (introspection, then the call)
is bounded by `_DBUS_CALL_TIMEOUT` (10 s), so a call against a non-introspectable
object may take up to two timeouts before it fails.
