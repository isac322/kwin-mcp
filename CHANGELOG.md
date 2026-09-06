# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `dbus_call` accepts typed-JSON arguments (`{"type": "int32", "value": 42}`) alongside dbus-send strings (`"int32:42"`), mixed freely in one `args` list. Supported shapes: basic types, `array` of a basic type, `dict` with a basic key and a basic or `variant` value (so `a{sv}` is expressible as `{"type": "dict", "key_type": "string", "value_type": "variant", "value": {"k": {"type": "string", "value": "v"}}}`), and `variant` of a basic type. The tool schema's `args` items widen from `string` to `string | object`.
- `window_close` asks exactly one window to close, addressed by its KWin id, through the same KWin scripting path as `focus_window`. Other windows of the same app stay open. The id reaches KWin only as a JSON string literal, so an id containing a quote is treated as data and never runs as KWin script. Closing is refused in live sessions (`session_connect`), where it could discard unsaved work on the real desktop.
- `active_window` reports the window KWin currently treats as active, with its id, frame, and client rectangles, for example to confirm `focus_window`.

### Changed

- `window_geometry` now lists each window's KWin id (stable while the window exists) and marks the active window with `[active]`. The new optional `window_id` parameter selects one window by exact id; `app_name` keeps its behavior.
- `accessibility_tree`, `find_ui_elements`, `wait_for_element` and `list_windows` reuse one long-lived AT-SPI2 worker per session bus (`python -m kwin_mcp.accessibility --serve`, newline-delimited JSON over stdin/stdout) instead of starting a fresh interpreter and importing PyGObject/Atspi on every call. In the E2E image (KWin 6.3.6, 2 CPUs, kcalc open, 231-element tree), three interleaved rounds measured warm medians (main vs new): `accessibility_tree` 412–455 → 255–376 ms, `find_ui_elements` 374–479 → 281–566 ms (the high outlier tracks a host-load spike; the other rounds were 281–290 ms), and `list_windows` 60–66 → 1.5–2.6 ms; the first call on a new worker costs about as much as one call did before. The worker records the session-bus and a11y-bus identities it was started with and is replaced when either changes — covering `session_start`, `session_connect`, a changed environment, and an AT-SPI bus restart on an unchanged session bus — or when it has exited, so stale answers are never served. A timeout, worker exit or worker-side exception discards the worker and retries once on a fresh one, as before; `session_stop` stops it (killing it after 2 seconds if it does not exit on EOF). It uses `subprocess`, not `multiprocessing`, so it starts no `multiprocessing.resource_tracker` helper and never re-imports the caller's `__main__` module.
- `docker/e2e.Dockerfile` no longer exports hashed requirements from `uv.lock`. The built wheel is installed with standard `Requires-Dist` resolution: all Python dependencies resolve fresh from the package index within the declared ranges, so their versions can vary between image builds. PyGObject, pycairo, and dbus-python compile from source in a builder-only stage, and the runtime image adds only the `libgirepository-2.0-0` shared library. The dev dependency group is installed separately from `pyproject.toml`. The Debian base-image digest and dated APT snapshot remain pinned, and `uv.lock` continues to pin development environments outside the container.
- `keyboard_type_unicode` no longer uses `wl-clipboard`. It still tries `wtype` first; when `wtype` is unavailable or unsupported by the session, the text is pasted through a private clipboard helper (`python -m kwin_mcp.clipboard`). The helper uses the Wayland data-control protocol through `libwayland-client`, which KWin already depends on; no new package is required (#45).
- `screenshot` and `screenshot_after_ms` frame bursts on ScreenShot2 now capture the whole workspace across all outputs (`CaptureWorkspace`) instead of only the active screen, matching the Spectacle and X11/scrot backends. The ScreenShot2 path also decodes the RGBX8888 raw format (QImage format 16) that current KWin emits for workspace captures, alongside the integer RGB32/ARGB32 formats.
- `docker/e2e.Dockerfile` installs `libkscreen-bin` so visual E2E tests can set nested KWin output scales with `kscreen-doctor`; `environment.json` records its package version. It is a test-environment dependency, not a runtime requirement.
- **Breaking for clients that parse `dbus_call` text:** `dbus_call` now calls D-Bus in-process through dbus-python instead of running `dbus-send --print-reply`, and its reply text changes. A void reply is an empty string instead of a `method return time=...` header, a single basic value is returned bare (`GetNameOwner` returns `:1.2` instead of `method return ...` followed by `string ":1.2"`), and containers or multiple values are JSON. Bus and remote errors read `D-Bus error: <error-name>: <message>`; argument errors read `D-Bus call failed: <reason>`. Numbers still arrive with their D-Bus type (`int32:42` is sent as `Int32`), and a top-level `variant:` argument stays a variant on the wire (`v`). The `dbus-send` binary is no longer needed at runtime. `unixfd` arguments remain unsupported, as in dbus-send.

### Fixed

- `dbus_call` sent a different number than the one written and reported success when an integer argument was malformed: `int32:12x3` was sent as `12`, `int32:2147483648` as `-2147483648`, and `int32:notanumber` or `int32:` as `0`. `dbus-send` parses numbers with `strtol`/`strtoul` without checking the end pointer or the range, and `dbus_call` passed arguments to it unchecked. Every numeric literal must now parse completely (optional sign plus decimal, or `0x` hexadecimal, the literal forms dbus-send accepts except leading-zero octal such as `010` for 8, which is rejected rather than reinterpreted) and fit its type, or the call fails with `D-Bus call failed: dbus-send arg: ...` and nothing is sent.
- `dbus_call` now checks the arguments against the method's declared input signatures and marshals with the matching signature, so surplus or missing arguments fail with `D-Bus call failed: <interface>.<method> takes '<signature>'[/'<signature>'...], got signature '<args>'` before anything is sent. An argument whose written type differs from the declared one fails the same way instead of being silently coerced (`int32:5` for an `x` parameter, or `boolean:true` for `i`, which dbus-python would otherwise send as `1`), and only a declared variant (`v`) accepts a differently typed value. Qt exports overloaded slots and slots with default arguments as several signatures under one method name, such as KWin `org.kde.kwin.Scripting` `loadScript` with `s` and `ss`; both arities work. When the object does not describe the method, each argument is sent with its own signature and an explicit `variant:` argument still marshals as `v`, as dbus-send sends it. dbus-send's dict grammar keeps working, including variant values: dbus-send's dict grammar keeps working, including variant values: `dict:string:string:FOO,bar,BAZ,qux` and `dict:string:variant:name,string:x,n,int32:3` (an `a{sv}`).
- `keyboard_type` and `keyboard_key` in a virtual session typed the host keyboard layout's characters instead of the requested text, for example `hello` became `руддщ` on a host with `ru,us` layouts (#52). kwin-mcp sends evdev keycodes computed from a US QWERTY table, and KWin translates them through its own keymap, which it built from the host's `kxkbrc` (through the inherited `XDG_CONFIG_HOME`) or from the host's `XKB_DEFAULT_*` variables, also with `isolate_home=True`. `session_start` now pins the compositor keymap to plain US: it sets `KWIN_XKB_DEFAULT_KEYMAP=1`, which makes KWin ignore `kxkbrc` and locale1, sets `XKB_DEFAULT_LAYOUT=us`, and drops the host's other `XKB_DEFAULT_*` values. The compositor also no longer reads the host's config directory; its config dir is seeded with a disabled `kwalletrc`, so a wallet-enabled host config cannot open a kwallet popup that takes focus from the app under automation. Python API callers can still set `XKB_DEFAULT_*` values through `SessionConfig.extra_env`, which is applied last, but cannot restore `kxkbrc` or locale1; the MCP tools and the CLI do not pass `extra_env` to the compositor.
- `mouse_click` could report `Clicked left at (x, y)` without activating the target when a window had appeared or moved beneath a parked cursor, most visibly on the first click after `session_stop` and `session_connect` to the same KWin with a new window placed under the previous click position (#66). In KWin 6.3.6, no `wl_pointer.motion` is sent when a surface maps or moves under a stationary cursor, and `SeatInterface::notifyPointerMotion` (`seat.cpp`) returns early for an absolute motion to the cursor's current position, so the client kept the surface-local position from when it mapped and GTK dispatched the button there. The EIS reconnect was not a factor: the same failure occurs on one connection when a new window replaces the old one. When no button is held, `mouse_move` (and so `mouse_click`, `mouse_button_down`, `mouse_scroll`, and the start of `mouse_drag`) now first moves to the adjacent pixel, then to `(x, y)`, so the final move changes the cursor position and the focused client receives a current enter/motion before the button or scroll. This restores coordinate sync only when KWin accepts the adjacent point; output clamping, edge barriers, or pointer confinement can pin it back onto the target, and the stale position can then remain. While a button pressed by `mouse_button_down` is held, `mouse_move` and `mouse_button_up` move straight to the target, so a stationary down/up pair reports no drag motion. Hover-sensitive widgets can see one extra motion event 1 px beside the target.
- `screenshot` and `screenshot_after_ms` frames killed a Spectacle fallback capture after a fixed 10 seconds, even when the capture was healthy but slow (#65). Profiling under QEMU software emulation showed where the time goes. Spectacle 6.3 takes about 3 seconds to start, waits up to 4 seconds for KWin to answer each output's `CaptureScreen` call (the calls run in parallel), then composites the outputs and PNG-encodes the result, and that last part grows with the image size. On a mixed-scale layout, Spectacle upscales every output to the next whole scale above the largest, so a 1.45 + 1.0 layout of 3245x1080 logical pixels becomes a 6490x2160 image, about 6.8 times a single 1080p output. Those captures took 9.9 to 11.3 seconds, so the fixed bound killed them. The deadline is now computed from the output layout kwin-mcp observes just before each capture: 15 seconds plus 1 second per megapixel of Spectacle's image, capped at 120 seconds. That is 18 seconds for one 1920x1080 output and 30 seconds for the 6490x2160 layout. A capture that never finishes still fails with `spectacle timed out after <N>s`, where `<N>` is the deadline that was applied. The X11/scrot backend keeps its 10-second bound. Spectacle's own 4-second wait is separate and fixed inside Spectacle: when KWin misses it, Spectacle prints the reason and exits successfully without writing a file. The `spectacle produced no output` error now includes that reason (for example `KWin screenshot request failed: Did not receive a reply`) instead of dropping it.
- E2E test helpers now declare explicit type annotations — tuple return types, fixture keyword arguments, and startup stderr handles — so the suite type-checks under the current `ty` release; test behavior and count are unchanged.
- `screenshot` and `screenshot_after_ms` frames used whatever pixel space the capture backend produced, while `mouse_click`, `mouse_move`, `mouse_drag`, and touch tools take KWin global logical coordinates. On fractionally scaled outputs, a coordinate read from a Spectacle or X11/scrot screenshot was off by the scale factor, and the click could activate a different widget while the tool reported success (#44). Saved PNGs with a proven mapping are now normalized to the logical workspace, and each result adds a line such as `Coordinate space: logical; origin (0, 0); size 1920x1080; backend screenshot2; coverage full; topology observed stable before/after capture`. `origin` is signed: image pixel `(px, py)` is logical point `(origin_x + px, origin_y + py)`. Spectacle clips outputs at negative logical positions when it composites several screens; kwin-mcp leaves those regions transparent and reports `coverage partial` with the captured regions instead of reconstructing them. When the output topology cannot be matched to the captured image, `screenshot` fails with `coordinate mapping cannot be proven` and a burst frame reports `Coordinate space: unavailable (reason)` — its PNG is kept but holds the backend's raw unnormalized pixels — rather than returning guessed coordinates.
- `find_ui_elements`, `accessibility_tree` and `wait_for_element` reported window-local AT-SPI2 coordinates, so clicking a reported rectangle landed on whatever occupied that screen position instead of the element — and on GTK4 (which reports every element at `(0,0)`) even the documented `window_geometry` offset workaround missed. Element rectangles are now translated to global screen coordinates (`@ screen (x, y, wxh)`) by matching each AT-SPI2 top-level to exactly one KWin window by process id, caption, and client size. When no unique match exists — a masked process id, identical windows of one process, a window set that changed mid-query, or a failed KWin query — the element reports `@ unavailable (reason)` with no coordinates rather than a position that could click the wrong window.
- `window_geometry` now rounds KWin's fractional geometry values to integers instead of failing with `ValueError`. A fractional window no longer prevents queries for other windows selected with `app_name` (#60).
- `session_start` could still hang, or fail with a bare `subprocess.TimeoutExpired`, when the compositor failed to start while a descendant held the wrapper's output open (#48). The startup handshake used an unbounded `readline()`, so a killed `dbus-run-session` wrapper whose children kept stdout open, or a newline-free partial write, blocked forever. Startup output is now read with a hard deadline, a partial line counts as diagnostics, and session stderr goes to a file so no descendant can block on a full stderr pipe before or after `READY`. A failed handshake always raises `RuntimeError` with the captured stderr and stray stdout (chaining the causal exception when teardown itself times out), and session teardown runs in `finally` so it is not skipped when diagnostics collection fails. If `Popen` itself fails before a session exists, the isolated home is released before the original exception propagates.
- `session_stop` and the `session_start` failure path left compositor, D-Bus, and AT-SPI2 processes running once the session leader had been reaped: the process-group signal looked up the group through the dead leader (`getpgid` → `ESRCH`) and silently sent nothing. The group is now signalled by the leader pid, which `start_new_session` makes the group id, and `session_stop` checks whether the group is actually empty after the leader exits, escalating to `SIGKILL` for descendants that ignore `SIGTERM`. The group id is used only within the session's own start/stop lifecycle; like any pid, it can be recycled by the kernel once every member has exited, so it is not signalled after `session_stop` returns.
- `keyboard_type_unicode` left the typed text on the clipboard, so a later paste could insert it again (a problem for passwords) and the previous clipboard content was lost. It also treated a `wl-copy` still running after 100 ms as success, so a slow or failing copy pasted stale clipboard content while reporting `Typed unicode` (#45). The helper now snapshots every format of the previous selection before it changes anything, offers the text marked with `x-kde-passwordManagerHint` so Klipper and other clipboard managers that honor the hint keep it out of their history, and sends Ctrl+V only after the compositor confirms it owns the selection. After the paste it puts the previous selection back, in every format, and keeps serving it until another copy replaces it, with no time limit; this includes after `session_stop` disconnects a live session. The call reports failure when the snapshot or selection change fails or times out (nothing is pasted then), when no client reads the text within 4 seconds of Ctrl+V, or when restoring the previous selection fails or times out. Text over 1 MiB of UTF-8 is rejected before the clipboard is touched.
- `clipboard_set` waited a fixed 100 ms and reported success even if `wl-copy` had not set the selection yet or failed afterwards. It now waits for `wl-copy` to confirm the selection, up to 5 seconds, and reports `Failed to set clipboard` when it exits with an error or does not finish in time. As before, the text it sets stays on the clipboard after the call until something replaces it.
- The private clipboard helper behind `keyboard_type_unicode` checked the `COPY` length only after connecting to the compositor. While a slow compositor held up the connect roundtrips, the helper kept draining stdin and accepted about 1 MiB of payload before the generic input bound rejected an oversized `COPY` header, so a parent still writing never got `EPIPE` (#69). Command headers are now validated as soon as a complete line arrives, even during connect: an oversized, malformed or unknown header, or an overlong command line, is rejected with a single `ERR protocol <why>` after at most one read chunk past the header, and the previous clipboard is left untouched. A framing failure recorded at header time now stays terminal in every later path too — it prints exactly one `ERR` and exits 2 even when a subsequent `QUIT` or signal completes its bounded restore.
- `screenshot` and `screenshot_after_ms` frame bursts started the Spectacle fallback on mixed-scale layouts where a screen lies outside Spectacle's canvas, such as a screen at a negative position next to a fractionally scaled one. Spectacle releases before 6.7.90 compose mixed scales through OpenCV and aborted there: on Debian's Spectacle 6.3.5 with OpenCV 4.10, the off-canvas screen became a zero-width ROI and `cv::resize` failed `inv_scale_x > 0`. The error was `spectacle failed (exit -6)` plus an OpenCV trace, after several seconds and a crashed Spectacle process (#67). A partly off-canvas screen was squeezed into the clipped region instead. kwin-mcp now recognizes this layout from the KWin topology observed before the capture and does not start Spectacle. On a Spectacle older than 6.7.90 (every Gear-numbered release such as 24.12.3 counts as older), or one whose version cannot be read, the capture fails with an error naming the screens Spectacle cannot place. The version is read with `spectacle --version` only for such a layout, once per screenshot or frame burst. Uniform-scale layouts, mixed-scale layouts inside the canvas, and Spectacle 6.7.90 or later are captured as before.
- `accessibility_tree`, `find_ui_elements`, and `list_windows` returned `(no accessible applications found)` on the first call after `session_start` on distros whose AT-SPI bus launcher lies outside the paths kwin-mcp knew about, such as openSUSE Tumbleweed (`/usr/libexec/at-spi2/at-spi-bus-launcher`). The wrapper looked the launcher up in a fixed list of paths and ran it in the background, so on an unlisted layout nothing owned `org.a11y.Bus` when the app started, Qt apps did not join the accessibility bus, and the user's first query was what finally started it. The session now activates `org.a11y.Bus` synchronously over D-Bus with `dbus-send` before KWin starts, so `dbus-daemon` runs the launcher named by the distro's own service file, and no new dependency is needed. If activation fails, `session_start` reports it as a `Warning:` line instead of hiding it. Thanks to @davidselassie for reporting the launcher path problem in [#8](https://github.com/isac322/kwin-mcp/pull/8).
- The `Docs & SEO Review` workflow failed on pull requests from forks even when every check passed: the step that posts the findings as a PR comment got `403 Resource not accessible by integration`, because fork pull requests receive a read-only `GITHUB_TOKEN`. The comment step now runs only for pull requests from branches of this repository; on fork pull requests the findings still appear in the job log.
- Importing `kwin_mcp` failed on systems without libei installed even when no EIS input functionality was used: the shared library was loaded eagerly at import time. It is now loaded lazily on first use, so linting, type-checking and packaging environments work without the native library

## [0.8.0] - 2026-09-24

### Added

- Containerized installed-package E2E suite (`docker/e2e.Dockerfile`, `tests/e2e`) collecting 99 tests: engine-level virtual-session coverage plus the installed `kwin-mcp` console entry point over real MCP stdio. Protocol tests verify the exact JSON schemas and wrapper behavior of all 31 tools, including lifecycle and error propagation.
- Nested visual QA that starts Xvfb and a nested KWin Wayland compositor, connects through `session_connect`, and selects the explicit X11/scrot capture backend with `KWIN_MCP_X11_SCREENSHOT=1`. Pixel oracles verify rendered CJK text, `screenshot(include_cursor=...)` cursor localization, material repaint transitions, and `screenshot_after_ms` action-frame sequences.
- `scripts/run-e2e-docker.sh` for reproducible local runs on Docker's native architecture, with retained `environment.json`, `junit.xml`, `pytest.log`, screenshots, and nested Xvfb, KWin, MCP server, and application logs under `artifacts/e2e/`.
- `window_geometry` tool reporting window frame and client rectangles in global screen coordinates via KWin scripting. Accessibility rectangles are surface-local, so until now nothing in the public API could turn a located element into clickable coordinates — the core "find an element, then click it" loop was not expressible with the tools alone. Tool count increased from 30 to 31.
- Element text content in `find_ui_elements`, `wait_for_element` and `accessibility_tree` output (`text='...'`, capped at 200 characters). Editors and entries keep their content in the AT-SPI2 Text interface rather than in the element name, so until now the tools could locate a text widget but never read what it contained.
- Scrollbar and slider positions in element output (`value=current/max`), read from the AT-SPI2 Value interface. Without it a caller could see that a scrollbar exists but not where it sits, which is the only way to observe scrolling.

### Changed

- `docker/e2e.Dockerfile` now builds the project wheel and installs it into `/opt/kwin-mcp-venv` with hash-checked dependencies exported from `uv.lock`, including MCP 1.26.0. The uv builder and Debian trixie base are pinned by digest, and APT resolves against a dated Debian snapshot.
- The `E2E` GitHub Actions workflow now runs Docker natively on `ubuntu-24.04` amd64 and `ubuntu-24.04-arm` arm64. Neither CI nor the local runner requires privileged mode, added capabilities, GPU/DRM device access, or host display access.
- Screenshot success coverage now uses the explicit nested X11/scrot visual mode. One legacy exact-virtual KWin ScreenShot2 success test remains intentionally skipped because KWin's virtual backend does not answer capture requests; its MCP error path remains covered.

### Fixed

- The published package metadata now declares `mcp>=1.0.0,<2`. The 0.7.0 release on PyPI left `mcp` unbounded, so a fresh install could resolve `mcp` 2.x and the server failed at startup with `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`; the constraint already present in the source tree now ships in the released distribution.
- `session_connect` now validates explicit D-Bus and Wayland endpoints, including the requested `$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY` socket, before attaching to a live KWin session.
- `list_windows` now filters AT-SPI2 applications with zero top-level windows instead of reporting empty application entries.
- `keyboard_key`, `keyboard_key_down`, and `keyboard_key_up` now return explicit MCP tool errors for unknown key names without terminating the stdio server; invalid mouse button names receive the same error treatment.
- Held EIS state no longer leaks between clients: KWin clears emulated modifiers and pointer buttons when the EIS connection disconnects, as proved by reconnecting to the same nested session and observing lowercase text and an unmodified single click. `mouse_drag` also releases its own pressed button and modifiers in `finally` when an exception interrupts the drag.
- Segfault on Python 3.14 caused by missing `argtypes` on variadic `ei_seat_bind_capabilities` ctypes call
- KWin crashed on startup in minimal environments (containers, CI) because `KDE_FULL_SESSION` / `KDE_SESSION_VERSION` pushed it onto the full Plasma session path; both are now stripped from the compositor's environment only, so launched apps still see them
- `session_start` could hang forever when KWin died during startup: the wrapper script waited on the Wayland socket in an unbounded loop, so the `READY` handshake never returned. The wait is now bounded, reports `FAILED`, and the resulting error includes KWin's stderr
- `accessibility_tree` and `find_ui_elements` failed with `TypeError: 'type' object is not iterable` on newer PyGObject releases; element states now come from `state_set.get_states()` instead of iterating the `Atspi.StateType` enum
- `session_start` aborted with a `dbus.DBusException` instead of degrading gracefully when KWin exposes no EIS interface; the input backend is now reported as unavailable, as intended
- `screenshot` shelled out to spectacle unconditionally; it now uses the KWin ScreenShot2 D-Bus interface first (as documented) and falls back to spectacle, reporting the original D-Bus error when the fallback is unusable
- KWin ScreenShot2 capture read the pixel pipe only after the D-Bus call returned, so a frame larger than the pipe buffer could stall the call; the pipe is now drained concurrently while the call is in flight
- ScreenShot2 calls that never reply now have an explicit capture timeout; failed captures stop and join the pipe reader and close both pipe file descriptors, preventing thread and descriptor accumulation across retries.
- `window_geometry` and `focus_window` now use a bounded KWin geometry helper query and report a clear timeout instead of hanging when KWin does not answer.
- With `KWIN_MCP_X11_SCREENSHOT=1`, absolute EIS pointer moves are mirrored to X11 with `xdotool`, keeping the cursor captured by `scrot` synchronized with the automated Wayland pointer and surfacing mirror failures or timeouts.
- E2E artifacts are now readable across container, host, and CI UID boundaries: the runner creates a container-writable artifact root, environment evidence uses mode `0644`, and CI grants recursive read and directory traversal access before upload.
- EIS input injection started emulating before the compositor had resumed the devices, which libei rejects (`ei_device_keyboard_key: device is not emulating`) and which silently dropped every injected event; `_negotiate_devices` now waits for `EI_EVENT_DEVICE_RESUMED` on the pointer and keyboard before calling `ei_device_start_emulating`, falling back to the previous unconditional start if a device does not resume within the handshake budget
- `focus_window` reported success while doing nothing on Wayland: it asked AT-SPI2 to grab focus, which neither raises nor activates a window there. It now activates through KWin scripting, and the `[active]` marker in `list_windows` follows it
- `mouse_scroll(discrete=True)` was silently dropped: libei counts discrete scrolling in 120ths of a wheel detent, so a click count was rejected as a suspicious fraction. Detents are now scaled and split correctly, including for negative deltas with `steps`
- `keyboard_type_unicode` gave up when `wtype` failed instead of falling back to the documented wl-copy + Ctrl+V path. KWin does not implement the virtual-keyboard Wayland protocol, so `wtype` always fails there and non-ASCII input never worked on Plasma
- `session_stop` left applications started by `launch_app` running: they are children of the caller, not of the session's process group, so the group signal never reached them. A surviving app also kept writing into an isolated home and defeated its removal, which surfaced as a leaked directory on slower machines. Apps are now terminated and reaped first, and the home removal retries instead of ignoring errors
- Installation docs omitted the native build prerequisites needed when installing kwin-mcp builds `pygobject` and `dbus-python` from source, as happens in the isolated environments created by `uv tool install kwin-mcp`, `uvx kwin-mcp`, and `uv sync`. The README now lists these build dependencies alongside the runtime system dependencies, including the Debian 13 runtime package `kde-spectacle`; CONTRIBUTING.md links to that list instead of duplicating it, and the AI agent integration guide separates `uvx: command not found` from native build failures

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

[Unreleased]: https://github.com/isac322/kwin-mcp/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/isac322/kwin-mcp/compare/v0.7.0...v0.8.0
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
