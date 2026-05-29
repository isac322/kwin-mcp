# kwin-mcp Roadmap

## Purpose

This MCP server enables **Claude Code to autonomously test and automate KDE Plasma desktop GUIs** — both in isolated virtual sessions and on live real desktops.

### Virtual Session Mode (Isolated Testing)

What Claude Code can do through this MCP in isolated mode:
1. Launch apps in an isolated KWin Wayland session (no impact on the host desktop)
2. Perform all interactions: mouse hover, click, scroll, drag, etc.
3. Take screenshots to verify visual changes
4. Read the accessibility tree to understand widget structure
5. Repeat the above to autonomously perform **visual/functional/UX feedback loops**

This allows Claude Code to modify code, then directly launch the app, manipulate the GUI, and verify results to autonomously complete the development cycle.

### Live Session Mode (Real Desktop Automation)

What Claude Code can do through this MCP on a live desktop:
1. Connect to an existing KWin Wayland session via `session_connect`
2. Automate the real desktop — interact with running apps without launching a new session
3. Take screenshots of the live screen
4. Read the accessibility tree of live app windows
5. Enable **"share my screen" collaboration** and **container-based agent desktops**

This allows Claude Code to act as a desktop automation agent on real user desktops, CI/CD environments with a display, or containerized desktops — expanding use cases beyond pure development testing.

---

## Architecture

```
Claude Code
  └── kwin-mcp (MCP server)
        ├── Environment management: dbus-run-session + kwin_wayland --virtual
        ├── Screen observation: spectacle CLI + AT-SPI2
        └── Input injection: KWin EIS D-Bus + libei
```

Triple isolation ensures no impact on the host desktop:
1. dbus-run-session → D-Bus session isolation
2. kwin_wayland --virtual → Display isolation
3. EIS (Emulated Input Server) → Input isolation (inside isolated session only)

---

## Milestones

### M0: Project Initialization ✅
- [x] Create uv-based project structure
- [x] pyproject.toml (dependencies, ruff, ty config)
- [x] ROADMAP.md, CLAUDE.md, README.md
- [x] Git initialization

### M1: Isolated Environment Management (`session.py`) ✅
- [x] Start/stop dbus-run-session + kwin_wayland --virtual
- [x] Auto-start AT-SPI daemon
- [x] Launch apps (auto-set env vars: QT_LINUX_ACCESSIBILITY_ALWAYS_ON, etc.)
- [x] Process tree cleanup
- **Done**: Verified kcalc running in isolated KWin

### M2: Screenshot Capture (`screenshot.py`) ✅
- [x] Screenshot capture from isolated session using spectacle CLI
- [x] PNG → base64 conversion
- [x] Return image via MCP tool
- **Done**: Verified capturing app screen from isolated session as 52KB+ PNG

### M3: AT-SPI2 Accessibility Tree (`accessibility.py`) ✅
- [x] Widget tree traversal via gi.repository.Atspi
- [x] Extract role, name, state, coordinates, size
- [x] Return as text format from MCP tool
- [x] Verify AT-SPI2 can access apps in isolated session
- **Done**: Fixed by running AT-SPI2 queries in subprocess with isolated session's D-Bus address

### M4: Input Injection (`input.py`) ✅
- [x] Obtain EIS fd via KWin's org.kde.KWin.EIS.RemoteDesktop D-Bus interface
- [x] EIS protocol handshake + device negotiation via libei (ctypes)
- [x] Absolute coordinate pointer: move, click (left/right/middle, single/double), scroll, drag
- [x] Keyboard: type text (US QWERTY evdev keycodes), key combos (Ctrl+C, etc.)
- **Done**: E2E tests confirmed keyboard input, mouse hover, right-click all produce visual changes
- **Technical history**: fake-input (removed in Plasma 6) → RemoteDesktop Portal (requires permission dialog) → KWin EIS direct connection (final)

### M5: MCP Server Integration (`server.py`) ✅
- [x] Register 10 tools and run MCP server
- [x] Register server in Claude Code settings (.mcp.json)
- [ ] Full feedback loop test (launch → interact → verify)
- **Done**: Server code complete, .mcp.json registered

### M6: Extended Input Features for E2E Testing ✅
- [x] Fix `ei_device_start_emulating` argument count bug (2 → 3)
- [x] Modifier + Click (`mouse_click(modifiers=["ctrl"])`)
- [x] Long-Press (`mouse_click(hold_ms=1000)`)
- [x] Triple-Click (`mouse_click(triple=True)` or `click_count=3`)
- [x] Key Hold/Release separation (`keyboard_key_down`, `keyboard_key_up`)
- [x] Modifier + Drag (`mouse_drag(modifiers=["alt"])`)
- [x] Mouse Button Hold/Release separation (`mouse_button_down`, `mouse_button_up`)
- [x] Discrete Scroll (`mouse_scroll(discrete=True)`)
- [x] Smooth Scroll (`mouse_scroll(steps=5)`)
- [x] Drag waypoints (`mouse_drag(waypoints=[[x, y, dwell_ms], ...])`)
- [x] Drag button selection (`mouse_drag(button="right")`)
- [x] Clipboard read/write (`clipboard_get`, `clipboard_set` via wl-clipboard)
- [x] Wait-for-UI-State (`wait_for_element` — AT-SPI2 polling)
- [x] Touch basic input (`touch_tap`, `touch_swipe`)
- [x] Multi-touch gestures (`touch_pinch`, `touch_multi_swipe`)
- [x] IME / non-ASCII text input (`keyboard_type_unicode` via wtype/clipboard)
- [x] Window management (`launch_app`, `list_windows`, `focus_window`)
- [x] Generic D-Bus call (`dbus_call` via dbus-send)
- **Total tools**: 10 → 27

### M7: Session Enhancements for E2E Testing ✅
- [x] Expose restricted Wayland protocols (`KWIN_WAYLAND_NO_PERMISSION_CHECKS=1`)
- [x] App stdout/stderr capture (`AppInfo` + log file redirect + `read_app_log` tool)
- [x] Shell-aware command parsing (`shlex.split()` for proper quoting support)
- [x] Extra environment variables for `session_start` and `launch_app` (`env` parameter)
- [x] Wayland protocol diagnostics (`wayland_info` tool)
- **Total tools**: 27 → 29

### M8: Code Separation + AT-SPI2 Cleanup ✅
- [x] Extract `AutomationEngine` class from `server.py` into `core.py` (MCP-independent logic)
- [x] Slim down `server.py` to thin MCP wrappers delegating to `AutomationEngine`
- [x] Create `cli.py` — interactive REPL + pipe mode via `cmd.Cmd` for rapid testing
- [x] Add `kwin-mcp-cli` entry point to `pyproject.toml`
- [x] Verify and remove unnecessary `AT_SPI_BUS_ADDRESS` propagation chain (was not needed)
- [x] Confirm `ATSPI_DBUS_IMPLEMENTATION=dbus-daemon` is required (verified)
- [x] Reduce AT-SPI bus launcher sleep from 0.5s to 0.2s (verified)
- **Architecture**: `core.py` (AutomationEngine) ← `server.py` (MCP wrapper) + `cli.py` (CLI wrapper)

### M9: Home Directory Isolation ✅
- [x] Optional `isolate_home` parameter to create a temporary HOME with isolated XDG directories
- [x] `keep_home` parameter to preserve the isolated home after session stop
- [x] XDG Base Directory Specification compliance (`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME`)
- [x] `XDG_RUNTIME_DIR` intentionally not isolated (Wayland socket resides there)
- [x] DRY `_xdg_isolation_env()` helper shared across `_build_env()`, `launch_app()`, and `_session_env()`
- **Goal**: Prevent apps in isolated sessions from reading/writing host user settings for reproducible and safe testing

### M10: AT-SPI2 Query Improvements + Stability ✅
- [x] `list_windows` shows per-window titles and `[active]`/`[focused]` state markers
- [x] `find_ui_elements` `states` parameter for AT-SPI2 state-based filtering (e.g. `["focused"]`)
- [x] `wait_for_element` `expected_states` parameter for waiting on state changes
- [x] `accessibility_tree` `role` parameter for role-based tree filtering (e.g. `"button"`)
- [x] `_run_atspi()` retry logic (1 retry with 0.5s delay) for transient AT-SPI2 bus failures
- **Goal**: Address feedback from E2E PoC testing — enable state-aware UI queries and improve AT-SPI2 reliability

### M11: Live Session Support (Real Desktop / Container Attachment)
- [x] `SessionType` enum (`VIRTUAL` / `LIVE`) and `LiveSession` class in `session.py`
- [x] `session_connect` MCP tool and engine method for attaching to existing KWin sessions
- [x] Clipboard always enabled for live sessions (no `enable_clipboard` parameter needed)
- [x] `session_stop` disconnects without killing KWin or pre-existing apps on live sessions
- [x] `launch_app` supported on live sessions (subprocess with host env)
- [x] `--default-live-session` CLI/server flag to switch default session mode
- [x] Dynamic MCP tool descriptions based on session mode
- [ ] ydotool fallback when EIS permission is denied on real sessions
- **Goal**: Support "share my screen" collaboration and container-based agent desktops ([#1](https://github.com/isac322/kwin-mcp/issues/1))

### M12: Pluggable CLI Backend (Auto-detect Alternatives)
- [ ] Research functionally equivalent alternatives for each external CLI (`wl-copy`/`wl-paste`, `wtype`, `spectacle`, `dbus-send`)
- [ ] Implement auto-detection: discover available CLIs at runtime and select the best match
- [ ] Ensure all alternatives are functionally identical (no behavioral differences)
- [ ] Update `_INSTALL_HINTS` to suggest multiple options
- **Goal**: Users on non-KDE or minimal setups don't need to install KDE-specific tools if equivalent alternatives are already present

### M13: Multi-distro test harness ✅
- [x] Manjaro Docker smoke test harness (local; see [docs/docker-testing.md](docs/docker-testing.md))
- [ ] Ubuntu Docker smoke test harness (future; validate apt-based container parity)
- [ ] Debian Docker smoke test harness (future; validate apt-based container parity)
- [ ] Fedora Docker smoke test harness (future; validate dnf-based container parity)
- [ ] openSUSE Docker smoke test harness (future; validate zypper-based container parity)
- **Goal**: Extend the Docker smoke harness beyond Manjaro so distro-specific regressions stay visible.
