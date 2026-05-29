# KWin Scripting Feasibility — KDE Plasma 6

## KWin version

- `kwin_wayland --version`: `kwin 6.6.4`
- Probe command: `bash scripts/kwin_scripting_probe.sh`
- Session: isolated `dbus-run-session` + `kwin_wayland --virtual --no-lockscreen --width 1280 --height 720`

## Result-passing mechanism comparison

| Mechanism | Result | Notes |
|---|---|---|
| `callDBus` to `org.kwin_mcp.Probe` | Selected for the spike | Structured JSON callback over the isolated session bus. The probe registers a temporary Python `dbus.service.BusName` + `dbus.service.Object`, exposes `Result(payload: str)`, stores the payload, and unblocks a `threading.Event`. The synchronous spike listener uses `dbus.mainloop.glib` + GLib `MainLoop`; this is not a production MCP pattern. |
| `print` / compositor log scraping | Rejected | KWin and portal logs are noisy in virtual sessions, output ordering is not a reliable request/response channel, and parsing stdout/journal output would be fragile. |
| Temporary files | Rejected for this spike | File output would require choosing and securing a writable path inside the isolated session and still needs polling. D-Bus callbacks are simpler and closer to the intended automation control plane. |

## Test results

The rerun reached KWin's `/Scripting` D-Bus object and introspected it. The object exposed `loadScript`, `loadDeclarativeScript`, `isScriptLoaded`, and `unloadScript`, but not `loadScriptFromText`. Therefore all required JavaScript tests failed before execution.

| Test | JS Snippet | Result |
|---|---|---|
| Basic window enumeration | `workspace.windowList()` and `callDBus(... JSON.stringify(results))` | Failed before execution: `org.freedesktop.DBus.Error.UnknownMethod: No such method 'loadScriptFromText' in interface 'org.kde.kwin.Scripting' at object path '/Scripting' (signature 'ss')` |
| Active window | `workspace.activeWindow` and `callDBus(... JSON.stringify(windowOrNull))` | Failed before execution with the same `loadScriptFromText` `UnknownMethod` error |
| Close `kcalc` window | Iterate `workspace.windowList()`, match `resourceClass === "kcalc"`, call `closeWindow()` | Failed before execution with the same `loadScriptFromText` `UnknownMethod` error |

The log still contains transient `fork: retry: 자원이 일시적으로 사용 불가능함` lines from the local Python wrapper, but the Python probe eventually ran, introspected the interface, and captured the concrete D-Bus failure above. This is not classified as reproduction-blocked because actual scripting-interface results were obtained.

## Working JS snippets

No snippet is confirmed working on KWin `6.6.4` in this virtual-session probe because the required in-memory loader, `loadScriptFromText(script, name)`, is unavailable.

## Attempted JS snippets

### Basic window enumeration

```javascript
// KDE Plasma 6 KWin scripting — written from scratch, inspired by KDE scripting API docs
try {
    var wins = workspace.windowList();
    var results = [];
    for (var i = 0; i < wins.length; i++) {
        var w = wins[i];
        results.push({
            id: String(w.internalId),
            title: w.caption,
            appId: String(w.resourceClass),
            active: w === workspace.activeWindow,
            x: w.frameGeometry.x,
            y: w.frameGeometry.y,
            width: w.frameGeometry.width,
            height: w.frameGeometry.height
        });
    }
    callDBus("org.kwin_mcp.Probe", "/Probe", "org.kwin_mcp.Probe", "Result", JSON.stringify(results));
} catch (e) {
    callDBus("org.kwin_mcp.Probe", "/Probe", "org.kwin_mcp.Probe", "Result", JSON.stringify({error: String(e)}));
}
```

### Active window

```javascript
// KDE Plasma 6 KWin scripting — written from scratch, inspired by KDE scripting API docs
try {
    var w = workspace.activeWindow;
    if (w) {
        callDBus("org.kwin_mcp.Probe", "/Probe", "org.kwin_mcp.Probe", "Result",
            JSON.stringify({id: String(w.internalId), title: w.caption, appId: String(w.resourceClass)}));
    } else {
        callDBus("org.kwin_mcp.Probe", "/Probe", "org.kwin_mcp.Probe", "Result", JSON.stringify(null));
    }
} catch (e) {
    callDBus("org.kwin_mcp.Probe", "/Probe", "org.kwin_mcp.Probe", "Result", JSON.stringify({error: String(e)}));
}
```

### Close `kcalc`

```javascript
// KDE Plasma 6 KWin scripting — written from scratch, inspired by KDE scripting API docs
try {
    var wins = workspace.windowList();
    var closed = false;
    for (var i = 0; i < wins.length; i++) {
        if (String(wins[i].resourceClass) === "kcalc") {
            wins[i].closeWindow();
            callDBus("org.kwin_mcp.Probe", "/Probe", "org.kwin_mcp.Probe", "Result", JSON.stringify("closed"));
            closed = true;
            break;
        }
    }
    if (!closed) {
        callDBus("org.kwin_mcp.Probe", "/Probe", "org.kwin_mcp.Probe", "Result", JSON.stringify("not_found"));
    }
} catch (e) {
    callDBus("org.kwin_mcp.Probe", "/Probe", "org.kwin_mcp.Probe", "Result", JSON.stringify({error: String(e)}));
}
```

## Decision

KWin scripting blocked on KWin 6.x

Specific reason: KWin `6.6.4` in an isolated virtual session exposes the `/Scripting` D-Bus object, but the interface lacks the required `loadScriptFromText(script, name)` method. Because in-memory JavaScript cannot be loaded through that API, Test 1 did not pass and `callDBus` result-passing could not be verified from an executed script.

## Reproduction

```bash
bash scripts/kwin_scripting_probe.sh
```

Expected current result on the tested host: KWin starts, `kcalc` launches, `/Scripting` introspection succeeds, and all three tests report `UnknownMethod` for `loadScriptFromText`.

## License note

The JavaScript snippets above were written from scratch from the KDE Plasma 6 scripting API shape and carry the required attribution header. No incompatible script source was copied.
