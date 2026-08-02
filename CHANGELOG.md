# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Containerized end-to-end test environment (`docker/e2e.Dockerfile`) running a virtual KWin Wayland session on Debian trixie, plus the `E2E` GitHub Actions workflow that builds it and runs `tests/e2e` on every push and pull request. No `--privileged`, `--cap-add` or X server required.
- `tests/e2e` QA suite covering session lifecycle (double start, teardown, isolated home, live `session_connect`), AT-SPI2 observation (tree role filter, query and state filters, multi-window listing, app logs, `wayland_info`), window geometry, and EIS input injection. Screenshot capture is covered by an opt-in test (`KWIN_MCP_E2E_SCREENSHOT=1`); see `docker/README.md` for what the container cannot exercise.
- `window_geometry` tool reporting window frame and client rectangles in global screen coordinates via KWin scripting. Accessibility rectangles are surface-local, so until now nothing in the public API could turn a located element into clickable coordinates — the core "find an element, then click it" loop was not expressible with the tools alone. Tool count increased from 30 to 31.
- Element text content in `find_ui_elements`, `wait_for_element` and `accessibility_tree` output (`text='...'`, capped at 200 characters). Editors and entries keep their content in the AT-SPI2 Text interface rather than in the element name, so until now the tools could locate a text widget but never read what it contained.
- Scrollbar and slider positions in element output (`value=current/max`), read from the AT-SPI2 Value interface. Without it a caller could see that a scrollbar exists but not where it sits, which is the only way to observe scrolling.

### Fixed

- Segfault on Python 3.14 caused by missing `argtypes` on variadic `ei_seat_bind_capabilities` ctypes call
- KWin crashed on startup in minimal environments (containers, CI) because `KDE_FULL_SESSION` / `KDE_SESSION_VERSION` pushed it onto the full Plasma session path; both are now stripped from the compositor's environment only, so launched apps still see them
- `session_start` could hang forever when KWin died during startup: the wrapper script waited on the Wayland socket in an unbounded loop, so the `READY` handshake never returned. The wait is now bounded, reports `FAILED`, and the resulting error includes KWin's stderr
- `accessibility_tree` and `find_ui_elements` failed with `TypeError: 'type' object is not iterable` on newer PyGObject releases; element states now come from `state_set.get_states()` instead of iterating the `Atspi.StateType` enum
- `session_start` aborted with a `dbus.DBusException` instead of degrading gracefully when KWin exposes no EIS interface; the input backend is now reported as unavailable, as intended
- `screenshot` shelled out to spectacle unconditionally; it now uses the KWin ScreenShot2 D-Bus interface first (as documented) and falls back to spectacle, reporting the original D-Bus error when the fallback is unusable
- KWin ScreenShot2 capture read the pixel pipe only after the D-Bus call returned, so a frame larger than the pipe buffer could stall the call; the pipe is now drained concurrently while the call is in flight
- EIS input injection started emulating before the compositor had resumed the devices, which libei rejects (`ei_device_keyboard_key: device is not emulating`) and which silently dropped every injected event; `_negotiate_devices` now waits for `EI_EVENT_DEVICE_RESUMED` on the pointer and keyboard before calling `ei_device_start_emulating`, falling back to the previous unconditional start if a device does not resume within the handshake budget
- `focus_window` reported success while doing nothing on Wayland: it asked AT-SPI2 to grab focus, which neither raises nor activates a window there. It now activates through KWin scripting, and the `[active]` marker in `list_windows` follows it
- `mouse_scroll(discrete=True)` was silently dropped: libei counts discrete scrolling in 120ths of a wheel detent, so a click count was rejected as a suspicious fraction. Detents are now scaled and split correctly, including for negative deltas with `steps`
- `keyboard_type_unicode` gave up when `wtype` failed instead of falling back to the documented wl-copy + Ctrl+V path. KWin does not implement the virtual-keyboard Wayland protocol, so `wtype` always fails there and non-ASCII input never worked on Plasma
- `session_stop` left applications started by `launch_app` running: they are children of the caller, not of the session's process group, so the group signal never reached them. A surviving app also kept writing into an isolated home and defeated its removal, which surfaced as a leaked directory on slower machines. Apps are now terminated and reaped first, and the home removal retries instead of ignoring errors

## [0.7.0] - 2026-03-29

### Added

- `session_connect` tool for attaching to an existing KWin session (real desktop or container) instead of creating an isolated virtual one. Defaults to `$DBUS_SESSION_BUS_ADDRESS` and `$WAYLAND_DISPLAY` from the environment. Clipboard is always enabled for live sessions.
- `--default-live-session` flag for both MCP server (`kwin-mcp`) and CLI (`kwin-mcp-cli`) to switch the default session mode from virtual to live. When active, `session_connect` becomes the recommended tool and `session_start` requires explicit invocation.
- `LiveSession` class in `session.py` for managing connections to existing KWin compositors without lifecycle management
- `SessionType` enum (`VIRTUAL` / `LIVE`) and `session_type` field on `SessionInfo` for distinguishing session types

### Changed

- `session_stop` now only disconnects (without killing KWin or pre-existing apps) when used with live sessions
- Error messages for missing sessions now mention both `session_start` and `session_connect`
- Clipboard error messages now mention `session_connect` as an alternative (clipboard is always enabled for live sessions)
- Tool count increased from 29 to 30

## [0.6.0] - 2026-02-25

### Added

- `isolate_home` option in `session_start` to create a temporary HOME directory with isolated XDG directories (`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME`), preventing apps from reading/writing host user settings
- `keep_home` option in `session_start` to preserve the isolated home directory after `session_stop`, useful for inspecting app-generated config/data files
- `list_windows` now shows per-window titles and `[active]`/`[focused]` state markers using AT-SPI2 `ACTIVE`/`FOCUSED` states
- `states` parameter for `find_ui_elements` to filter elements by AT-SPI2 states (e.g. `["focused"]`, `["active", "visible"]`). Query can be empty when filtering by states only.
- `expected_states` parameter for `wait_for_element` to wait until elements have specific AT-SPI2 states (e.g. wait for a window to become `["active"]`)
- `role` parameter for `accessibility_tree` to filter the tree to specific element types (e.g. `"button"`, `"check box"`). Non-matching elements are hidden but their children are still traversed.

### Changed

- AT-SPI2 subprocess queries (`_run_atspi`) now retry once on failure with a 0.5s delay, improving resilience against transient AT-SPI2 bus instability
- `find_ui_elements` and `wait_for_element` result messages now include a descriptive search summary with all filter criteria (query, states)

## [0.5.1] - 2026-02-23

### Fixed

- `session_start` `screen_width`/`screen_height` parameters were being ignored — now correctly passed as `--width`/`--height` flags to `kwin_wayland`

### Added

- `keep_screenshots` option in `session_start` to preserve screenshot files after `session_stop`, useful for debugging and CI artifact collection
- SEO documentation guidelines in `CLAUDE.md`, `docs-seo` custom agent, `release-notes` skill, GitHub issue/PR templates, and `CONTRIBUTING.md`

## [0.5.0] - 2026-02-23

### Added

- **`AutomationEngine` (`core.py`)**: MCP-independent automation logic extracted from `server.py` into a standalone reusable class covering session, input, screenshot, and accessibility operations
- **Interactive CLI (`kwin-mcp-cli`)**: New entry point with REPL and pipe mode for testing all 29 tools without an MCP client

### Changed

- `server.py` simplified to thin MCP wrappers delegating to `AutomationEngine`
- Improved AT-SPI2 bus address propagation and reduced launcher sleep time

## [0.4.2] - 2026-02-22

### Changed

- Added JSON Schema `description` fields to all parameters across all 29 MCP tools for improved discoverability and client-side documentation
- Rewrote `README.md` with complete tool reference tables, architecture diagram, and SEO-optimized metadata

## [0.4.1] - 2026-02-22

### Fixed

- Explicitly pass `KWIN_WAYLAND_NO_PERMISSION_CHECKS` and `KWIN_SCREENSHOT_NO_PERMISSION_CHECKS` env vars directly to the KWin process in the wrapper script — environment inheritance through `dbus-run-session` was unreliable, causing restricted Wayland protocols (e.g. `org_kde_plasma_window_management`) and `X-KDE-Wayland-Interfaces` desktop file declarations to not take effect

## [0.4.0] - 2026-02-22

### Added

- **Restricted Wayland protocol access**: Set `KWIN_WAYLAND_NO_PERMISSION_CHECKS=1` in isolated sessions, enabling clients to bind `org_kde_plasma_window_management` and other KWin-restricted protocols — critical for testing apps that use Plasma's TasksModel / window management APIs
- **App stdout/stderr capture**: `launch_app` and `session_start` now redirect app output to per-app log files, with a new `read_app_log` MCP tool to retrieve logs by PID
- **Wayland protocol diagnostics**: New `wayland_info` MCP tool runs `wayland-info` inside the session to enumerate exposed Wayland globals (useful for verifying protocol availability)
- **Environment variable passthrough**: `session_start` and `launch_app` now accept an `env` parameter for passing extra environment variables to launched apps
- **Shell-aware command parsing**: Commands are now parsed with `shlex.split()` instead of `str.split()`, correctly handling quoted arguments (e.g. `bash -c 'echo hello world'`)

### Changed

- `Session.launch_app()` now returns `AppInfo` (with pid, command, log_path) instead of a bare `int` PID
- `SessionInfo` now tracks all launched apps via an `apps` dict keyed by PID

## [0.3.0] - 2026-02-22

### Added

- M5.1 E2E input features: touch input (tap, swipe, pinch, multi-finger swipe), clipboard (get/set), Unicode text input (wtype/wl-copy fallback), window management (launch_app, list_windows, focus_window), `dbus_call`, and `wait_for_element` — 17 new MCP tools total

### Fixed

- External binary missing errors now return helpful install instructions instead of raw `FileNotFoundError` (affects `wl-clipboard`, `wtype`, `dbus-send`, `spectacle`)

## [0.2.0] - 2026-02-20

### Added

- **Composite frame capture**: Action tools (`mouse_click`, `mouse_move`, `mouse_drag`, `keyboard_type`, `keyboard_key`) now accept an optional `screenshot_after_ms` parameter to capture screenshots at specified delays (in milliseconds) after the action completes
- Fast D-Bus screenshot capture via KWin ScreenShot2 interface (~30-70ms per frame vs ~200-300ms with spectacle CLI)
- Optimized burst capture with two-phase pipeline: raw frame capture with accurate timing, then deferred PNG encoding
- `KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1` environment variable automatically set for isolated sessions to enable direct D-Bus screenshot access

## [0.1.0] - 2026-02-20

### Added

- Isolated KWin Wayland session management (`session_start`, `session_stop`)
- Screenshot capture via KWin's ScreenShot2 D-Bus interface
- Accessibility tree inspection using AT-SPI2
- UI element search by name, role, or description
- Mouse input: click, move, scroll, drag via KWin EIS (Emulated Input Server)
- Keyboard input: text typing and key combinations via KWin EIS
- FastMCP-based MCP server with stdio transport

[Unreleased]: https://github.com/isac322/kwin-mcp/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/isac322/kwin-mcp/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/isac322/kwin-mcp/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/isac322/kwin-mcp/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/isac322/kwin-mcp/compare/v0.4.2...v0.5.0
[0.4.2]: https://github.com/isac322/kwin-mcp/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/isac322/kwin-mcp/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/isac322/kwin-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/isac322/kwin-mcp/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/isac322/kwin-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/isac322/kwin-mcp/releases/tag/v0.1.0
