# ScreenShot2 Virtual Session Feasibility

This spike evaluates the feasibility of using KWin ScreenShot2 pipe-based D-Bus capture as the primary screenshot path inside `kwin_wayland --virtual`.

- **KWin version**: `6.6.4`
- **Session model**: fresh `dbus-run-session` + `kwin_wayland --virtual` per probe row
- **Geometry**: `1280x720`
- **Required environment**:
  - `KWIN_WAYLAND_NO_PERMISSION_CHECKS=1`
  - `KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1`
  - `ATSPI_DBUS_IMPLEMENTATION=dbus-daemon`
- **Output format**: `label|method|status|width|height|stride|png_path|png_size|error`
- **Reproduction script**: `scripts/screenshot2_probe.sh`

## Phase 1: Normal virtual-session baseline

This section evaluates ScreenShot2 across various software-rendering environment combinations in an empty virtual session.

| Combination | Extra Env Vars | CaptureActiveScreen | CaptureWorkspace | CaptureArea(0,0,1,1) |
|---|---|---|---|---|
| baseline | none | OK · 1280x720 · 3653 B | OK · 1280x720 · 3653 B | OK · 1x1 · 70 B |
| kwin_prefer_sw_qpainter | `KWIN_PREFER_SW_QPAINTER=1` | OK · 1280x720 · 3653 B | OK · 1280x720 · 3653 B | OK · 1x1 · 70 B |
| libgl_always_software | `LIBGL_ALWAYS_SOFTWARE=1` | OK · 1280x720 · 3653 B | OK · 1280x720 · 3653 B | OK · 1x1 · 70 B |
| mesa_swrast | `MESA_LOADER_DRIVER_OVERRIDE=swrast` | OK · 1280x720 · 3653 B | OK · 1280x720 · 3653 B | OK · 1x1 · 70 B |
| egl_surfaceless | `EGL_PLATFORM=surfaceless` | OK · 1280x720 · 3653 B | OK · 1280x720 · 3653 B | OK · 1x1 · 70 B |
| libgl_mesa_swrast | `LIBGL_ALWAYS_SOFTWARE=1 MESA_LOADER_DRIVER_OVERRIDE=swrast` | OK · 1280x720 · 3653 B | OK · 1280x720 · 3653 B | OK · 1x1 · 70 B |
| libgl_egl_surfaceless | `LIBGL_ALWAYS_SOFTWARE=1 EGL_PLATFORM=surfaceless` | OK · 1280x720 · 3653 B | OK · 1280x720 · 3653 B | OK · 1x1 · 70 B |

## Phase 2: App scenarios

This section evaluates ScreenShot2 with active applications to verify capture under realistic workloads.

| Scenario | Apps | CaptureActiveScreen | CaptureWorkspace | CaptureArea(0,0,1,1) | Sample PNG |
|---|---|---|---|---|---|
| phase2_empty | none | OK · 1280x720 · 3653 B | OK · 1280x720 · 3653 B | OK · 1x1 · 70 B | `/tmp/screenshot2_probe_376122/phase2_empty__CaptureWorkspace.png` |
| phase2_kcalc | `kcalc` | OK · 1280x720 · 78262 B | OK · 1280x720 · 78262 B | OK · 1x1 · 70 B | `/tmp/screenshot2_probe_376122/phase2_kcalc__CaptureWorkspace.png` |
| phase2_multi | `kcalc`, `Dialog-One`, `Dialog-Two` | OK · 1280x720 · 91155 B | OK · 1280x720 · 91155 B | OK · 1x1 · 70 B | `/tmp/screenshot2_probe_376122/phase2_multi__CaptureWorkspace.png` |

## Phase 3: Forced compositor stress

These are stress tests that force the compositor mode. They are NOT representative of normal virtual KWin sessions and MUST NOT be used as primary feasibility evidence.

| Combination | Extra Env Vars | CaptureActiveScreen | CaptureWorkspace | CaptureArea(0,0,1,1) |
|---|---|---|---|---|
| kwin_compose_o | `KWIN_COMPOSE=O` | FAIL: org.kde.KWin.ScreenShot2.Error.Cancelled: Screenshot got cancelled | FAIL: org.kde.KWin.ScreenShot2.Error.Cancelled: Screenshot got cancelled | FAIL: org.kde.KWin.ScreenShot2.Error.Cancelled: Screenshot got cancelled |
| kwin_compose_q | `KWIN_COMPOSE=Q` | FAIL: org.kde.KWin.ScreenShot2.Error.Cancelled: Screenshot got cancelled | FAIL: org.kde.KWin.ScreenShot2.Error.Cancelled: Screenshot got cancelled | FAIL: org.kde.KWin.ScreenShot2.Error.Cancelled: Screenshot got cancelled |
| libgl_mesa_swrast_compose_o | `LIBGL_ALWAYS_SOFTWARE=1 MESA_LOADER_DRIVER_OVERRIDE=swrast KWIN_COMPOSE=O` | FAIL: org.kde.KWin.ScreenShot2.Error.Cancelled: Screenshot got cancelled | FAIL: org.kde.KWin.ScreenShot2.Error.Cancelled: Screenshot got cancelled | FAIL: org.kde.KWin.ScreenShot2.Error.Cancelled: Screenshot got cancelled |

## Action

Use ScreenShot2 as the primary fast path for normal virtual KWin sessions. Keep spectacle as a compatibility fallback. Treat `KWIN_COMPOSE=O`/`KWIN_COMPOSE=Q` ScreenShot2 cancellation as an environment-specific backend failure, not as a general virtual-session limitation.

## Reproduction

To reproduce these results, run the following command:

```bash
bash scripts/screenshot2_probe.sh
```

This script writes PNG artifacts to `/tmp/screenshot2_probe_$$/` and prints CSV-style rows to stdout in the documented `output_format`.
