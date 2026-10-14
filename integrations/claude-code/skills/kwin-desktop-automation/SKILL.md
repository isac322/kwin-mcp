---
name: kwin-desktop-automation
description: Use when the user asks to launch, click, type, screenshot, or otherwise drive a Linux KDE Plasma / Wayland desktop app through the kwin-mcp MCP server. Trigger when kwin-mcp tools are available and the task involves desktop GUI automation, end-to-end GUI testing, kiosk / embedded device control, or live KDE Plasma session interaction. Teaches session-mode selection (virtual vs live), the observe-act-verify loop, AT-SPI2 vs screenshot tradeoffs, US-QWERTY vs Unicode typing, and platform pitfalls (surface-local coordinates, QMenu invisibility, EIS edge limits, clipboard opt-in).
---

# kwin-desktop-automation

Drive Linux KDE Plasma 6 Wayland desktops through the `kwin-mcp` MCP server. The MCP server provides 37 capabilities; this skill provides the operational discipline to use them efficiently, in the right order, and without falling into platform-specific traps.

## When to apply

Activate this skill whenever the user wants to:

- Launch, click, type, or screenshot any KDE / Qt / GTK / Electron app on Wayland.
- Run end-to-end GUI tests in a headless / virtual KWin session (CI, regression, reproducibility).
- Drive a live KDE Plasma session (their actual desktop) or a KWin instance inside a container.
- Inspect or operate a kiosk / embedded Linux device exposing AT-SPI2.

If no kwin-mcp tools are available, this skill does not apply.

## 1. Pick the session mode

Every other tool requires a session. Two mutually exclusive modes:

**Virtual — `session_start`**
Opens an isolated `dbus-run-session + kwin_wayland --virtual` compositor. Nothing reaches the host display. Use when the user says "test", "headless", "CI", "isolated", or names a specific app to launch fresh.

Useful arguments:
- `app_command="..."` — launch the target app inside the session.
- `enable_clipboard=true` — required for `clipboard_get` / `clipboard_set` (not for `keyboard_type_unicode`). Off by default because `wl-copy` can hang on a freshly minted bus.
- `keep_screenshots=true` — preserves the `/tmp/kwin-mcp-screenshots-*` directory (screenshots and launched apps' logs) after `session_stop`, also with `isolate_home=true` (delete the directory yourself when done).
- `isolate_home=true` — temp HOME with isolated XDG dirs; apps use fresh settings but keep normal filesystem access.

**Live — `session_connect`**
Attaches to an already-running KWin: the user's real desktop, or a KWin running inside a container / kiosk / embedded device. Use when the user says "my", "current", "this window", "what I'm looking at", "container", "kiosk", or "live". Defaults to `$DBUS_SESSION_BUS_ADDRESS` and `$WAYLAND_DISPLAY`; clipboard is always enabled. `session_stop` only disconnects — it never kills the live KWin or its apps.

If the kwin-mcp server was launched with `--default-live-session`, the descriptions of `session_start` and `session_connect` swap roles; in that mode `session_connect` is the default.

End every successful turn that opened a session with `session_stop`. Virtual sessions leak kwin processes otherwise; live sessions just disconnect.

## 2. Observe → Act → Verify

Each interaction is three steps. Cheap observation **before** action prevents acting on an unfocused window or stale UI.

**Observation tools, cheapest first:**

1. `list_windows` — window titles + active/focused markers. Free. `active_window` answers "which window has focus?" straight from KWin, with its id.
2. `accessibility_tree` — full AT-SPI2 widget tree. Always pass `app_name=` and/or `role=` (e.g. `"button"`, `"check box"`) and/or `max_depth=` to keep it small. Don't fetch the whole tree just to find one button.
3. `find_ui_elements` — query by name/role/states. Use this when you know what you are looking for. `query=""` + `states=["focused"]` answers "what currently has focus?".
4. `wait_for_element` — same matching as `find_ui_elements` but polls until the element appears (or `timeout_ms` elapses). Use after launching an app or after any click that triggers async UI.
5. `screenshot` — last resort for visual inspection or when AT-SPI2 fails to expose an element (see Pitfalls).

Pick the cheapest tool that answers the question. Do not start with `screenshot` if `find_ui_elements("Save")` would suffice.

`screenshot` and `screenshot_after_ms` results name the saved PNG path. If the kwin-mcp server runs with `--screenshot-images`, the result also includes each captured image directly, after the text, so you can look at it without opening the file. Otherwise, read the PNG at the reported path. Each attached image costs context, so request only the frames you need.

**Action tools:**

- Rectangles from `find_ui_elements` / `accessibility_tree` are already global screen coordinates (`@ screen (x, y, wxh)`) — the same space `mouse_click` and `touch_tap` take. Click the centre directly: `(x + width / 2, y + height / 2)`. An element reported as `@ unavailable (reason)` has no trustworthy position; do not click it.
- Prefer `invoke_ui_action` when the target exposes the intended AT-SPI action (for example `Press`). It operates semantically without pointer coordinates, so it still works when the control is obscured or another window is foreground. Scope with `app_name` whenever practical. The call runs only when exactly one matching element exposes the requested action; ambiguous matches fail closed. Discovery fails closed too: when part of the tree cannot be read, nothing is invoked and the error names the application — retry with `app_name` set to the target app. A result saying the application did not confirm means the action may have run; check the UI before retrying. Because actions can change application state, kwin-mcp does not automatically retry them. Verify the resulting state before considering the action complete.
- `window_geometry` lists every window with a KWin `id`. Ids stay stable while the window exists, so use them to tell apart several windows of the same app: `window_geometry(window_id=...)` re-reads one window, `focus_window(window_id=...)` activates exactly that one, `window_set_geometry(window_id=..., x=..., y=..., width=..., height=...)` moves and/or resizes exactly that window through KWin (supply only the fields that need changing, then verify the observed frame with `window_geometry`), and `window_close(window_id=...)` closes exactly that one. `window_set_geometry` width/height must be at most the KWin workspace size (start the session with a larger `screen_width`/`screen_height` for bigger windows), every field must fit a signed 32-bit integer, fields the client has not applied yet are listed as `not yet applied`, and `observed: window no longer exists` means the window closed before the readback. `window_close` only asks the app to close; confirm with `window_geometry` because a "save changes?" prompt can keep it open. It is refused in live sessions.
- Route a window to another virtual desktop with `virtual_desktops` and `window_set_desktop`, never with keyboard shortcuts (users rebind them). `virtual_desktops` lists desktops in order with stable ids and marks the current one; address desktops by id, never by position. `window_set_desktop(window_id=..., desktop_id=...)` assigns the selected window to only that desktop, including windows previously on all desktops, without explicitly selecting the current desktop or activating the window. Missing or unknown ids are tool errors and change nothing. Preflight refuses requests that would move related windows (transients or modal main windows) KWin scripting can enumerate at request time, and requests while an X11 group transient exists; close the dialog first, or move it first when it is not modal. Linked but unpainted or unlisted children may follow their parent; when they first appear, KWin may switch the current desktop. `applied` verifies only the selected window's observed membership; `current_desktop_id_before` and `current_desktop_id_after` are point-in-time observations, not future desktop or focus guarantees. If a dialog may appear after routing, call `virtual_desktops` again when it appears, before further input; use `active_window` to re-check the input target. An error saying the desktop could not be confirmed, or an `observation_error`, means the window may already have moved. Keep `before` to restore it later (`[]` means it was on all desktops).
- `window_geometry`, `find_ui_elements`, `list_windows`, `accessibility_tree`, `virtual_desktops`, and `window_set_desktop` also return structured data when your client exposes `structuredContent`: window `id`, `active`, and `frame`/`client` rects; element `role`, `name`, `states`, and `rect` (`{x, y, width, height}`, global screen coordinates, `null` with a reason in `unavailable` when there is no trustworthy position). Read coordinates and ids from these fields directly instead of parsing the text.
- Screenshot pixels are logical too, but offset by the image origin. Read the `Coordinate space: logical; origin (ox, oy); ...` line in the `screenshot` result and click image pixel `(px, py)` at `(ox + px, oy + py)`. The origin can be negative on multi-monitor layouts. Never rescale by the display scale factor — kwin-mcp already did. If the line goes on with `image WxH downscaled: pixel (px, py) shows (...)`, the image was shrunk: apply that formula instead.
- On a large desktop, small text is unreadable in a downscaled screenshot. Take the overview, then call `screenshot` with `region=[x, y, width, height]` (logical coordinates) and `max_edge=0` to read that area at full resolution; the crop's line reports its own origin.
- `keyboard_type` is **US-QWERTY only**: letters, digits, ASCII punctuation, space, tab, and newline. Text containing any other character (non-ASCII, `\r`, other control characters) is rejected before anything is typed, with an error naming the characters.
- `keyboard_type_unicode` for Korean / CJK / emoji / any non-ASCII. It tries `wtype` first; when `wtype` is unavailable or unsupported by the session, it pastes through a built-in temporary clipboard owner and then restores the previous clipboard in every format. The paste chord follows the focused window: Ctrl+Shift+V in terminals known to paste on it (Konsole, GNOME Terminal, kitty, foot, st, and others), Ctrl+V elsewhere. A tool error means the clipboard could not be taken, nothing read the text after the paste chord, or the restore failed: verify the target field before retrying. Recognized terminals without a clipboard paste chord (xterm, urxvt) and windows KWin cannot identify fail without any key being pressed; an unrecognized terminal still gets Ctrl+V; type ASCII with `keyboard_type` there. Text over 1 MiB of UTF-8 or containing NUL is rejected.

Branch typing by string content — never assume the input is ASCII.

**Verify after every meaningful action.** Typical pattern:

1. `find_ui_elements(query="OK", states=["enabled"])` — locate the screen-space rectangle.
2. `mouse_click(x + width / 2, y + height / 2)` — act on it directly.
3. `wait_for_element(query="Settings saved", timeout_ms=3000)` — confirm.

For animation-heavy or transient UI, pass `screenshot_after_ms=[0, 100, 300]` to a single action call instead of making three round-trips. Each frame has its own `Coordinate space` line. kwin-mcp uses the best supported capture backend. High-frequency timing requires ScreenShot2; the Docker nested visual QA explicitly selects X11/scrot for pixel verification, so do not treat its frame timing as ScreenShot2 performance.

## 3. Pitfalls

These are properties of the Wayland / AT-SPI2 / EIS stack, not bugs. Know them or get burned.

- **Failures come back as tool errors.** Invalid arguments, refusals (session already running, clipboard disabled, `window_close` in a live session), missing or ambiguous targets, and failed operations are reported with `isError: true` (`Error executing tool <name>: ...`). Check for it before continuing; an empty query answer, a `wait_for_element` timeout, and an unconfirmed action (`not yet applied`, `Close requested`, "did not confirm") are normal results that say so in their text.
- **`keyboard_type` is US QWERTY only.** Non-ASCII text must go through `keyboard_type_unicode`; `keyboard_type` rejects it as an error without typing anything. Always check the input.
- **Clipboard tools are opt-in on virtual sessions.** Pass `enable_clipboard=true` to `session_start` AND ensure `wl-clipboard` is installed before calling `clipboard_get` / `clipboard_set`. Live sessions always have clipboard.
- **`keyboard_type_unicode` briefly exposes the text on the clipboard.** Klipper keeps it out of history, but clipboard managers that ignore KDE's secret hint can record it, and Wayland cannot tell which client read it. Restoring the previous clipboard is best-effort: a copy made at the same moment can be overwritten. `clipboard_set` is different — its text intentionally stays on the clipboard.
- **Element coordinates are screen-global, or unavailable.** Rectangles returned by `find_ui_elements` and `accessibility_tree` are already in the screen space `mouse_click` and `touch_tap` take. When the element's window cannot be matched to exactly one KWin window, the element reports `@ unavailable (reason)` with no coordinates — never click a guessed position.
- **A screenshot's coordinate space can be partial or unavailable, and some layouts cannot be captured at all.** `coverage partial` (Spectacle fallback with a screen at a negative position) means only the listed regions were captured; the rest of the image is transparent, not empty desktop. On a layout the installed Spectacle cannot compose — mixed scales with a screen outside Spectacle's canvas — `screenshot` raises an error naming the output when the fallback would be needed. kwin-mcp has no per-output capture mode; make ScreenShot2 available to the session, or rearrange the outputs, instead of retrying. A frame reporting `Coordinate space: unavailable (reason)` kept its PNG but it holds the backend's raw unnormalized pixels — only its timing is meaningful. A `screenshot` error containing `coordinate mapping cannot be proven` has no trustworthy pixel-to-screen mapping at all — take a new screenshot instead of clicking a guessed position.
- **`screenshot` sees only the current virtual desktop.** It captures the whole workspace, so a window on another virtual desktop is absent — and `dbus_call` cannot capture one either, because it cannot pass the unix file descriptor that ScreenShot2's `CaptureWindow` requires. There is no per-window capture tool.
- **QMenu and native context menus may be invisible to AT-SPI2.** Qt's AT-SPI2 bridge has incomplete popup-menu support on Wayland. Take a `screenshot` to identify the menu item, derive its position from the parent widget's reported screen rectangle, then click.
- **Screen edge triggers (auto-hide panels, layer-shell strips) ignore EIS pointer events.** Use `dbus_call` to invoke KWin scripting or a keyboard shortcut instead of trying to hover the edge.
- **In a live session, browsers may expose no accessibility tree.** Read the `Accessibility:` line of the `session_connect` result. When it says off, Firefox shows up in no AT-SPI2 query. Only if the user asked to automate such an app, reconnect with `enable_accessibility=true`; it changes a desktop-wide setting until `session_stop`. Launch Chromium with `--force-renderer-accessibility` to expose web page content; without the flag its tree may hold only empty window frames.
- **Live sessions inside containers need reachable Wayland and D-Bus endpoints.** `session_connect` rejects missing, non-socket, or unreachable Wayland sockets, as well as an unreachable KWin D-Bus. Mount the target Wayland socket under the container's `$XDG_RUNTIME_DIR` (or pass its absolute path), and propagate `DBUS_SESSION_BUS_ADDRESS`.
- **Touch is EIS-emulated, not from a real touchscreen.** Most apps handle this correctly, but a few may behave differently from a physical touch device.

## 4. Cleanup

- Always call `session_stop` once the task is complete, even if a step errored.
- If `keep_screenshots=true` was used, `/tmp/kwin-mcp-screenshots-*` (screenshots and app logs) survives `session_stop`. Delete it explicitly when no longer needed.
- If `isolate_home=true` + `keep_home=true` were both used, the temp HOME under `/tmp/` also survives — delete it manually.

## Quick recipes

**"Screenshot my desktop"** (live):
1. `session_connect()`
2. `screenshot()` → report the file path.
3. `session_stop()`.

**"Click the Save button in kate"** (virtual):
1. `session_start(app_command="kate")`
2. `wait_for_element(query="Save", app_name="kate", timeout_ms=5000)` — get the screen-space rectangle.
3. `mouse_click(x + width / 2, y + height / 2)`.
4. `wait_for_element(query="Save File", timeout_ms=3000)` to confirm the dialog appeared.
5. `session_stop()`.

**"Type 안녕하세요 into the active text field"**:
1. (Session already open.)
2. `keyboard_type_unicode(text="안녕하세요")` — never `keyboard_type`; it rejects non-ASCII text.

**"Find what currently has focus"**:
1. `find_ui_elements(query="", states=["focused"])` — empty query is allowed when filtering by state.
