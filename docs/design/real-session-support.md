# Design: Real KWin Session Support

> **Issue**: [#1 --dangerously-skip-isolation](https://github.com/isac322/kwin-mcp/issues/1)
> **Status**: Implemented in 0.x (live sessions shipped as `session_connect`)
>
> **Historical document.** This design predates kwin-mcp 1.0 and names the 0.x tools and parameters it was written against. In 1.0 the app launcher is `app_launch`, app logs are read with `app_log`, Wayland globals with `wayland_list`, retention is `retention={artifacts, home}` and home isolation is `home="isolated"`; the ydotool fallback was never implemented. See the [CHANGELOG 1.0.0 migration tables](../../CHANGELOG.md#100---2026-10-12) for every renamed tool and parameter.

<!-- stale-name-scan: allow-begin (historical design record below uses the 0.x tool names) -->

## Context

kwin-mcp currently only supports **virtual KWin sessions** (`dbus-run-session` + `kwin_wayland --virtual`). This design adds support for connecting to **real/existing KWin sessions** for live GUI automation.

**Target use cases from the issue:**
1. **pepijndevos**: "Share my screen" with Claude — collaborate on the same screen, observe each other
2. **null-dev**: Attach to KWin+Plasma running inside a `systemd-nspawn` container (different D-Bus/Wayland from host)
3. General: Any pre-existing KWin session (real desktop, container, remote)

**Reference**: null-dev's fork ([compare](https://github.com/isac322/kwin-mcp/compare/main...null-dev:kwin-mcp:main)) adds `HostSession` + `session_attach` tool. Key differences from this design: no ydotool fallback (EIS-only), disabled `session_start` entirely, no formal session protocol, clipboard always enabled for real sessions.

---

## Design Decisions

### D1: No "list and select" — Two explicit tools instead

Cross-bus D-Bus discovery is technically impossible. Each virtual KWin lives on its own isolated D-Bus session bus created by `dbus-run-session`. There is no registry that tracks these buses. Real KWin registers as `org.kde.KWin` on the user's login session bus (`$DBUS_SESSION_BUS_ADDRESS`), which is per-bus, not globally discoverable.

A "list" tool would be incomplete (only showing the real session + kwin-mcp-created virtual sessions), causing LLM hallucinations when sessions exist that cannot be discovered.

**Decision:** `session_start` (create virtual) + `session_connect` (attach to existing). No discovery/listing.

### D2: Separate `session_connect` tool, `session_start` unchanged. Virtual is default, but configurable.

Different parameter sets:
- Virtual: `screen_width`, `screen_height`, `isolate_home`, `keep_home`, `enable_clipboard`
- Real: `dbus_address`, `wayland_display`

Separate tools = no "ignored depending on mode" params = less LLM confusion. Existing users unaffected (100% backward compatible).

**Virtual remains the default.** The LLM must use `session_start` (virtual) unless explicitly told to use a real session. `session_connect`'s MCP tool description includes "Only use when explicitly asked to interact with a real/existing desktop session."

### D3: `--default-live-session` flag to switch default session mode

CLI and MCP server both support `--default-live-session` flag. When set:
- Default behavior switches to `session_connect` (real session)
- `session_start` (virtual) requires explicit invocation
- MCP tool descriptions change dynamically: `session_connect` becomes "default", `session_start` becomes "only when explicitly asked"

### D4: Input injection strategy — EIS first, ydotool fallback

EIS (Emulated Input Server) is used for mouse/keyboard/touch input injection. On real sessions:
1. **EIS D-Bus direct connection** tried first (likely works since same user session)
2. On failure, **ydotool fallback** (uinput-level, supports mouse+keyboard+touch)
3. XDG RemoteDesktop Portal excluded (requires authorization popup every time — unsuitable for automation)

### D5: `session_stop` on real session = disconnect only

Close EIS connection + clean up screenshot directory. Never kill KWin or existing apps.

### D6: Clipboard always enabled on real sessions

Real sessions always have clipboard access. No `enable_clipboard` parameter needed — always `True`.

### D7: `launch_app` on real session = supported

Apps can be launched on real desktops via subprocess with `WAYLAND_DISPLAY` + `DBUS_SESSION_BUS_ADDRESS`.

### D8: Multi-monitor is orthogonal

Orthogonal to virtual/real split. `SessionInfo` can gain a `monitors` field later. Deferred.

---

## Implementation Plan

### Step 1: `session.py` — `SessionType` enum + `RealSession` class

- `SessionType` enum: `VIRTUAL`, `REAL`
- `SessionInfo.session_type` field (default `VIRTUAL` for backward compat)
- `RealSession` class:
  - `__init__(dbus_address, wayland_socket, screenshot_dir)`
  - `is_running`, `info` properties
  - `stop(keep_screenshots)` — screenshot dir cleanup only
  - `launch_app(command, extra_env)` — subprocess with host env

### Step 2: `core.py` — `session_connect()` method

- `_session` type: `Session | RealSession | None`
- `session_connect(dbus_address, wayland_display, keep_screenshots)`:
  1. Check no active session
  2. Default from `$DBUS_SESSION_BUS_ADDRESS` / `$WAYLAND_DISPLAY`
  3. Validate KWin reachable via D-Bus
  4. Create `RealSession`
  5. Try EIS → ydotool fallback → input unavailable warning
- `_clipboard_enabled = True` for real sessions
- Error messages: "Call session_start or session_connect first"

### Step 3: `server.py` — `session_connect` MCP tool

New tool with `dbus_address`, `wayland_display`, `keep_screenshots` parameters.

### Step 4: `input.py` — ydotool fallback

`InputBackend` falls back to ydotool subprocess when EIS connection fails.

### Step 5: `screenshot.py` — verify spectacle fallback works on real sessions

Existing D-Bus → spectacle fallback should work. Verify and add error hints if needed.

### Step 6: `cli.py` — `--default-live-session` flag

Add argparse to `main()`. Pass `live_session_mode` to `KwinMcpShell`.

### Step 7: `server.py` — `--default-live-session` flag + dynamic descriptions

Dynamic MCP tool descriptions based on mode flag.

### Step 8: Existing tools audit

**29 existing tools analyzed. Most need NO changes.**

Changes needed:

| Item | File | Change |
|------|------|--------|
| Clipboard | `core.py` | Auto-enable on `session_connect` |
| Error messages | `core.py` | Mention both `session_start` and `session_connect` |
| `session_stop` | `core.py` | Skip process killing for `RealSession` |
| `launch_app` | `session.py` | `RealSession.launch_app()` uses host env |

No changes needed (key rationale):
- **All input tools**: Abstracted through `InputBackend` (EIS or ydotool)
- **screenshot**: Reads `dbus_address`/`wayland_socket` from `SessionInfo`
- **Accessibility tools**: `_session_env()` sets `DBUS_SESSION_BUS_ADDRESS` → works on real AT-SPI2 bus
- **dbus_call, wayland_info**: Use `_session_env()` → work on real D-Bus/Wayland
- **`_session_env()`**: Already conditional on `home_dir` being `None` → no XDG override for real sessions
- **read_app_log**: Session-type agnostic

---

## Permission Model

| Component | Virtual session | Real session |
|-----------|----------------|--------------|
| Screenshot (D-Bus) | `KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1` | Not set — spectacle fallback |
| Screenshot (spectacle) | Works | Works |
| Input (EIS) | `KWIN_WAYLAND_NO_PERMISSION_CHECKS=1` | May work (same user) → ydotool fallback |
| Accessibility (AT-SPI2) | Isolated bus | Host bus (works) |
| Clipboard | Requires `enable_clipboard=True` | Always available |

<!-- stale-name-scan: allow-end -->
