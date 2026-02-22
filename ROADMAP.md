# kwin-mcp Roadmap

## Purpose

This MCP server exists to enable **Claude Code to autonomously test the GUI of KDE Plasma apps (especially the krema dock)**.

What Claude Code should be able to do through this MCP:
1. Launch apps in an isolated KWin Wayland session
2. Perform all interactions: mouse hover, click, scroll, drag, etc.
3. Take screenshots to verify visual changes
4. Read the accessibility tree to understand widget structure
5. Repeat the above to autonomously perform **visual/functional/UX feedback loops**

This allows Claude Code to modify code, then directly launch the app, manipulate the GUI, and verify results to autonomously complete the development cycle.

### Key Use Scenarios (krema dock)
- Verify parabolic zoom effect on icon hover
- Verify workspace switching via mouse scroll
- Verify right-click context menu display and item actions
- Verify icon reordering via drag
- Verify auto-hide behavior

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

### M3: AT-SPI2 Accessibility Tree (`accessibility.py`) ⚠️ Partial
- [x] Widget tree traversal via gi.repository.Atspi
- [x] Extract role, name, state, coordinates, size
- [x] Return as text format from MCP tool
- [ ] Verify AT-SPI2 can access apps in isolated session
- **Note**: AT-SPI2 registry not activating in isolated session. Needs further investigation.

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
- **Done**: Server code complete, .mcp.json registered in kwin-mcp and krema projects

### M5.1: Extended Input Features for E2E Testing ✅
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

### M6: krema Integration Test
- [ ] Launch krema dock app in isolated environment
- [ ] Test hover zoom, scroll, right-click, drag
- [ ] Verify UX via feedback loop
- **Completion criteria**: Claude Code can launch krema and autonomously test core UX
