# dbus_call Call Site Map

## Summary

- **Total internal code call sites**: 1 (`server.py` → `core.py`)
- **Hard-coded arg patterns in source**: 0 (all args are pass-through from external callers)
- **External API callers**: unlimited (LLM agents / MCP clients pass arbitrary dbus-send syntax)

## Definition Sites

| Location | Role | Current Args Type |
|---|---|---|
| `src/kwin_mcp/core.py` `AutomationEngine.dbus_call` | Implementation | `args: list[str \| dict] \| None`, parsed by `kwin_mcp.dbus_args.parse_arg` and sent in-process with dbus-python |
| `src/kwin_mcp/server.py` `dbus_call` | MCP tool schema + wrapper | Annotated `list[str \| dict] \| None`; the description documents both argument shapes |

## Internal Call Sites

| Location | Caller | Service | Path | Interface | Method | Args | Type Complexity |
|---|---|---|---|---|---|---|---|
| `src/kwin_mcp/server.py` `dbus_call` | MCP tool handler | pass-through | pass-through | pass-through | pass-through | pass-through from MCP user | varies |

**There are no internal call sites with hard-coded arg patterns.** All args originate from external MCP clients (LLM agents).

## External Arg Patterns (from MCP tool docstring)

Before the in-process rewrite, the `dbus_call` MCP tool description documented:
```
Method arguments in dbus-send format (e.g. "string:value", "int32:42", "boolean:true")
```

Examples that MCP clients are known to supply (from plan QA scenarios and typical agent behavior):
- `[]` — no args (e.g., `org.freedesktop.DBus.GetId`, `org.kde.KWin.showDesktop`)
- `["string:hello"]` — single basic string arg
- `["int32:42"]` — integer arg
- `["boolean:true"]` — boolean arg
- `["string:hello", "int32:42"]` — mixed basic args

## Required Parser Features

Based on the zero internal hard-coded call sites and the MCP tool's documented usage, the parser in `dbus_args.py` **MUST support** all standard dbus-send basic types because external LLM agents may use any of them:

| Type prefix | Example | Priority |
|---|---|---|
| `string:` | `string:hello` | **Required** — most common |
| `int32:` | `int32:42` | **Required** — most common integer |
| `boolean:` | `boolean:true` | **Required** — frequently used |
| `uint32:` | `uint32:1234` | **Required** — KWin D-Bus methods use uint32 |
| `uint64:` | `uint64:999` | **Required** — timestamps, PIDs |
| `int64:` | `int64:-1` | **Required** — standard type |
| `byte:` | `byte:255` | **Required** — protocol completeness |
| `int16:` | `int16:10` | **Required** — protocol completeness |
| `uint16:` | `uint16:10` | **Required** — protocol completeness |
| `double:` | `double:3.14` | **Required** — used by KWin geometry |
| `objpath:` | `objpath:/org/kde/KWin` | **Required** — object references |
| `signature:` | `signature:s` | **Required** — introspection |
| `array:` | `array:string:a,b,c` | **Required** — lists |
| `dict:` | `dict:string:int32:k1,1,k2,2`, `dict:string:variant:k,string:v` | **Required** — D-Bus dicts, including `a{sv}` |
| `variant:` | `variant:string:hello` | **Required** — polymorphic values |

**No types from the dbus-send man page are deferred** — the MCP tool is a public API and agents may use any valid dbus-send syntax. Dict entries follow dbus-send's grammar: one comma-separated list alternating keys and values, and a `variant` value type makes each value a `TYPE:VALUE` pair with a basic type. `unixfd` is not supported: dbus-send has no such type either, and an fd number from an MCP client would not refer to a descriptor in the server process.

## Scope Impact on Task 7 (Parser)

Task 7 must implement a full recursive-descent parser for the dbus-send argument syntax. The scope is NOT reducible based on current callers because:

1. The tool is an external API with no internal call sites to scope-cap from
2. LLM agents generate diverse arg patterns based on D-Bus introspection output
3. Supporting only 2-3 types would break real-world agent workflows

**Minimum viable parser scope**: all basic types + `array:`, `dict:`, `variant:` containers + nested arrays.

## Argument checking

`dbus_call` introspects the target object once per call. When the method is described, the argument count must match its input signature and the arguments are marshalled with that signature; mismatches and marshalling errors return `D-Bus call failed: ...` before anything is sent. When the object is not introspectable, the call goes out with dbus-python's inferred signature and the remote side validates it.
