---
name: kwin-desktop-automation
description: Use when the user asks to launch, click, type, screenshot, or otherwise drive a Linux KDE Plasma / Wayland desktop app through the kwin-mcp MCP server. Trigger when kwin-mcp tools are available and the task involves desktop GUI automation, end-to-end GUI testing, kiosk / embedded device control, or live KDE Plasma session interaction. Teaches session-mode selection (virtual vs live), the observe-act-verify loop, AT-SPI2 vs screenshot tradeoffs, US-QWERTY vs Unicode typing, and platform pitfalls (surface-local coordinates, QMenu invisibility, EIS edge limits, clipboard opt-in).
---

# kwin-desktop-automation

Drive Linux KDE Plasma 6 Wayland desktops through the `kwin-mcp` MCP server. The MCP server provides 33 capabilities; this skill provides the operational discipline to use them efficiently, in the right order, and without falling into platform-specific traps.

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
- `keep_screenshots=true` — preserves PNGs in `/tmp/kwin-mcp-screenshots-*` after `session_stop` (delete the directory yourself when done).
- `isolate_home=true` — temp HOME with isolated XDG dirs; keeps host configuration untouched.

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

**Action tools:**

- Rectangles from `find_ui_elements` / `accessibility_tree` are already global screen coordinates (`@ screen (x, y, wxh)`) — the same space `mouse_click` and `touch_tap` take. Click the centre directly: `(x + width / 2, y + height / 2)`. An element reported as `@ unavailable (reason)` has no trustworthy position; do not click it.
- `window_geometry` lists every window with a KWin `id`. Ids stay stable while the window exists, so use them to tell apart several windows of the same app: `window_geometry(window_id=...)` re-reads one window, and `window_close(window_id=...)` closes exactly that one. `window_close` only asks the app to close; confirm with `window_geometry` because a "save changes?" prompt can keep it open. It is refused in live sessions.
- Screenshot pixels are logical too, but offset by the image origin. Read the `Coordinate space: logical; origin (ox, oy); ...` line in the `screenshot` result and click image pixel `(px, py)` at `(ox + px, oy + py)`. The origin can be negative on multi-monitor layouts. Never rescale by the display scale factor — kwin-mcp already did.
- `keyboard_type` is **ASCII / US-QWERTY only**. It maps characters to evdev keycodes; non-ASCII silently breaks.
- `keyboard_type_unicode` for Korean / CJK / emoji / any non-ASCII. It tries `wtype` first; when `wtype` is unavailable or unsupported by the session, it pastes through a built-in temporary clipboard owner and then restores the previous clipboard in every format. The paste chord follows the focused window: Ctrl+Shift+V in terminals known to paste on it (Konsole, GNOME Terminal, kitty, foot, and others), Ctrl+V elsewhere. A failure result means the clipboard could not be taken, nothing read the text after the paste chord, or the restore failed: verify the target field before retrying. Terminals without a clipboard paste chord (xterm, urxvt, st) and windows KWin cannot identify fail without any key being pressed; type ASCII with `keyboard_type` there. Text over 1 MiB of UTF-8 is rejected.

Branch typing by string content — never assume the input is ASCII.

**Verify after every meaningful action.** Typical pattern:

1. `find_ui_elements(query="OK", states=["enabled"])` — locate the screen-space rectangle.
2. `mouse_click(x + width / 2, y + height / 2)` — act on it directly.
3. `wait_for_element(query="Settings saved", timeout_ms=3000)` — confirm.

For animation-heavy or transient UI, pass `screenshot_after_ms=[0, 100, 300]` to a single action call instead of making three round-trips. Each frame has its own `Coordinate space` line. kwin-mcp uses the best supported capture backend. High-frequency timing requires ScreenShot2; the Docker nested visual QA explicitly selects X11/scrot for pixel verification, so do not treat its frame timing as ScreenShot2 performance.

## 3. Pitfalls

These are properties of the Wayland / AT-SPI2 / EIS stack, not bugs. Know them or get burned.

- **`keyboard_type` is US QWERTY only.** Non-ASCII text must go through `keyboard_type_unicode`. Always check the input.
- **Clipboard tools are opt-in on virtual sessions.** Pass `enable_clipboard=true` to `session_start` AND ensure `wl-clipboard` is installed before calling `clipboard_get` / `clipboard_set`. Live sessions always have clipboard.
- **`keyboard_type_unicode` briefly exposes the text on the clipboard.** Klipper keeps it out of history, but clipboard managers that ignore KDE's secret hint can record it, and Wayland cannot tell which client read it. Restoring the previous clipboard is best-effort: a copy made at the same moment can be overwritten. `clipboard_set` is different — its text intentionally stays on the clipboard.
- **Element coordinates are screen-global, or unavailable.** Rectangles returned by `find_ui_elements` and `accessibility_tree` are already in the screen space `mouse_click` and `touch_tap` take. When the element's window cannot be matched to exactly one KWin window, the element reports `@ unavailable (reason)` with no coordinates — never click a guessed position.
- **A screenshot's coordinate space can be partial or unavailable, and some layouts cannot be captured at all.** `coverage partial` (Spectacle fallback with a screen at a negative position) means only the listed regions were captured; the rest of the image is transparent, not empty desktop. On a layout the installed Spectacle cannot compose — mixed scales with a screen outside Spectacle's canvas — `screenshot` raises an error naming the output when the fallback would be needed. kwin-mcp has no per-output capture mode; make ScreenShot2 available to the session, or rearrange the outputs, instead of retrying. A frame reporting `Coordinate space: unavailable (reason)` kept its PNG but it holds the backend's raw unnormalized pixels — only its timing is meaningful. A `screenshot` error containing `coordinate mapping cannot be proven` has no trustworthy pixel-to-screen mapping at all — take a new screenshot instead of clicking a guessed position.
- **QMenu and native context menus may be invisible to AT-SPI2.** Qt's AT-SPI2 bridge has incomplete popup-menu support on Wayland. Take a `screenshot` to identify the menu item, derive its position from the parent widget's reported screen rectangle, then click.
- **Screen edge triggers (auto-hide panels, layer-shell strips) ignore EIS pointer events.** Use `dbus_call` to invoke KWin scripting or a keyboard shortcut instead of trying to hover the edge.
- **Live sessions inside containers need reachable Wayland and D-Bus endpoints.** `session_connect` rejects missing, non-socket, or unreachable Wayland sockets, as well as an unreachable KWin D-Bus. Mount the target Wayland socket under the container's `$XDG_RUNTIME_DIR` (or pass its absolute path), and propagate `DBUS_SESSION_BUS_ADDRESS`.
- **Touch is EIS-emulated, not from a real touchscreen.** Most apps handle this correctly, but a few may behave differently from a physical touch device.

## 4. Cleanup

- Always call `session_stop` once the task is complete, even if a step errored.
- If `keep_screenshots=true` was used, `/tmp/kwin-mcp-screenshots-*` survives `session_stop`. Delete it explicitly when no longer needed.
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
2. `keyboard_type_unicode(text="안녕하세요")` — never `keyboard_type`; it would silently drop the characters.

**"Find what currently has focus"**:
1. `find_ui_elements(query="", states=["focused"])` — empty query is allowed when filtering by state.
