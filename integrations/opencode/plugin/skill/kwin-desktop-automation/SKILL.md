---
name: kwin-desktop-automation
description: Use when the user asks to launch, click, type, screenshot, or otherwise drive a Linux KDE Plasma / Wayland desktop app through the kwin-mcp MCP server. Trigger when kwin-mcp tools are available and the task involves desktop GUI automation, end-to-end GUI testing, kiosk / embedded device control, or live KDE Plasma session interaction. Teaches session-mode selection (virtual vs live), the observe-act-verify loop, AT-SPI2 vs screenshot tradeoffs, US-QWERTY vs Unicode typing, and platform pitfalls (surface-local coordinates, QMenu invisibility, EIS edge limits, clipboard opt-in).
---

# kwin-desktop-automation

Drive Linux KDE Plasma 6 Wayland desktops through the `kwin-mcp` MCP server. The MCP server provides 30 capabilities; this skill provides the operational discipline to use them efficiently, in the right order, and without falling into platform-specific traps.

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
- `enable_clipboard=true` — required for `clipboard_get` / `clipboard_set` and the Unicode-via-clipboard fallback. Off by default because `wl-copy` can hang on a freshly minted bus.
- `keep_screenshots=true` — preserves PNGs in `/tmp/kwin-mcp-screenshots-*` after `session_stop` (delete the directory yourself when done).
- `isolate_home=true` — temp HOME with isolated XDG dirs; keeps host configuration untouched.

**Live — `session_connect`**
Attaches to an already-running KWin: the user's real desktop, or a KWin running inside a container / kiosk / embedded device. Use when the user says "my", "current", "this window", "what I'm looking at", "container", "kiosk", or "live". Defaults to `$DBUS_SESSION_BUS_ADDRESS` and `$WAYLAND_DISPLAY`; clipboard is always enabled. `session_stop` only disconnects — it never kills the live KWin or its apps.

If the kwin-mcp server was launched with `--default-live-session`, the descriptions of `session_start` and `session_connect` swap roles; in that mode `session_connect` is the default.

End every successful turn that opened a session with `session_stop`. Virtual sessions leak kwin processes otherwise; live sessions just disconnect.

## 2. Observe → Act → Verify

Each interaction is three steps. Cheap observation **before** action prevents acting on an unfocused window or stale UI.

**Observation tools, cheapest first:**

1. `list_windows` — window titles + active/focused markers. Free.
2. `accessibility_tree` — full AT-SPI2 widget tree. Always pass `app_name=` and/or `role=` (e.g. `"button"`, `"check box"`) and/or `max_depth=` to keep it small. Don't fetch the whole tree just to find one button.
3. `find_ui_elements` — query by name/role/states. Use this when you know what you are looking for. `query=""` + `states=["focused"]` answers "what currently has focus?".
4. `wait_for_element` — same matching as `find_ui_elements` but polls until the element appears (or `timeout_ms` elapses). Use after launching an app or after any click that triggers async UI.
5. `screenshot` — last resort for visual inspection or when AT-SPI2 fails to expose an element (see Pitfalls).

Pick the cheapest tool that answers the question. Do not start with `screenshot` if `find_ui_elements("Save")` would suffice.

**Action tools:**

- Click coordinates returned by `find_ui_elements` / `accessibility_tree` directly with `mouse_click`.
- `keyboard_type` is **ASCII / US-QWERTY only**. It maps characters to evdev keycodes; non-ASCII silently breaks.
- `keyboard_type_unicode` for Korean / CJK / emoji / any non-ASCII. Internally uses `wtype` first, falls back to `wl-copy` + Ctrl+V. Requires `wtype` or `wl-clipboard` installed.

Branch typing by string content — never assume the input is ASCII.

**Verify after every meaningful action.** Typical pattern:

1. `find_ui_elements(query="OK", states=["enabled"])` — locate.
2. `mouse_click(x, y)` — act.
3. `wait_for_element(query="Settings saved", timeout_ms=3000)` — confirm.

For animation-heavy or transient UI, pass `screenshot_after_ms=[0, 100, 300]` to a single action call instead of three round-trips — kwin-mcp captures frames server-side via the fast ScreenShot2 D-Bus interface (~30–70 ms per frame).

## 3. Pitfalls

These are properties of the Wayland / AT-SPI2 / EIS stack, not bugs. Know them or get burned.

- **`keyboard_type` is US QWERTY only.** Non-ASCII text must go through `keyboard_type_unicode`. Always check the input.
- **Clipboard is opt-in on virtual sessions.** Pass `enable_clipboard=true` to `session_start` AND ensure `wl-clipboard` is installed. Live sessions always have clipboard.
- **AT-SPI2 coordinates are surface-local on Wayland.** Each app's coordinates are relative to its own window's top-left, not the virtual screen — Wayland clients do not know their global position by design. Single-window scenarios are fine. For multi-window layouts, cross-reference with `screenshot` to disambiguate.
- **QMenu and native context menus may be invisible to AT-SPI2.** Qt's AT-SPI2 bridge has incomplete popup-menu support on Wayland. Workaround: take a `screenshot`, locate the menu item visually, click by coordinates.
- **Screen edge triggers (auto-hide panels, layer-shell strips) ignore EIS pointer events.** Use `dbus_call` to invoke KWin scripting or a keyboard shortcut instead of trying to hover the edge.
- **Live sessions inside containers need Wayland + D-Bus mounted in.** Symptom: `session_connect` fails with "no Wayland display" or "no session bus". The user must mount `$XDG_RUNTIME_DIR/wayland-*` and propagate `DBUS_SESSION_BUS_ADDRESS` into the container.
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
2. `wait_for_element(query="Save", app_name="kate", timeout_ms=5000)`
3. `mouse_click(x, y)` using coords from step 2.
4. `wait_for_element(query="Save File", timeout_ms=3000)` to confirm the dialog appeared.
5. `session_stop()`.

**"Type 안녕하세요 into the active text field"**:
1. (Session already open.)
2. `keyboard_type_unicode(text="안녕하세요")` — never `keyboard_type`; it would silently drop the characters.

**"Find what currently has focus"**:
1. `find_ui_elements(query="", states=["focused"])` — empty query is allowed when filtering by state.
