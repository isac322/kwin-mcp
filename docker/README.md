# Containerized KWin session for end-to-end tests

`e2e.Dockerfile` builds the installed-package end-to-end environment used locally and in `.github/workflows/e2e.yml`. The suite collects every test under `tests/e2e` across engine-level virtual sessions, a real installed MCP server over stdio, and nested visual pixel QA. `e2e-arch.Dockerfile` builds the same environment on Arch Linux; see [Arch Linux variant](#arch-linux-variant).

`e2e-fedora.Dockerfile` (Fedora 44) and `e2e-opensuse.Dockerfile` (openSUSE Tumbleweed) build the same layout from those distributions' packages and run the same suite. Their base images are pinned by digest, but neither distribution offers a dated package snapshot for both amd64 and arm64, so their packages resolve from the live repositories at build time and can change between builds; `environment.json` records the installed versions. The Reproducibility section below applies to the Debian image.

## Reproducibility

The image fixes the inputs that define the test environment:

- the build helper is `ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424`;
- the runtime base is `debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a`;
- APT reads Debian trixie, trixie-updates, and trixie-security from snapshot `20260926T000000Z`;
- the project is built as a wheel and installed into `/opt/kwin-mcp-venv` with standard `Requires-Dist` resolution. Tests import that installed distribution and launch its `kwin-mcp` and `kwin-mcp-cli` console entry points.

All Python dependencies — `mcp` within the declared 2.x range, PyGObject, pycairo, dbus-python, Pillow, and their transitives — resolve fresh from the package index at image-build time, so Python dependency versions vary within the declared ranges between builds. PyGObject, pycairo, and dbus-python compile from source in the isolated `venv-builder` stage; the runtime image adds only the `libgirepository-2.0-0` shared library they need and carries no compiler or development headers. The virtual environment keeps system site packages so distro-only modules such as NumPy stay importable, and the dev dependency group is installed separately from `pyproject.toml`; `uv.lock` is not used inside the image. `environment.json` records the architecture, base digest, package snapshot, Python and KWin versions, the installed version of every distro package the image lists in `KWIN_MCP_SYSTEM_PACKAGES` (the same list its package-manager step installs), and installed `kwin-mcp` and `mcp` distribution metadata for each run, so the resolved versions remain part of the retained provenance.

## Arch Linux variant

`e2e-arch.Dockerfile` keeps the contract of `e2e.Dockerfile`: the same build stages, `/opt/kwin-mcp-venv`, `tester` user (UID 1000), entrypoint, `/artifacts` directory, and `pytest tests/e2e` command. Only the distro layer differs, so the unchanged suite checks kwin-mcp against Arch's KWin, libei, Qt, Mesa, and tool versions, which are much newer than Debian stable's (for the pinned date: KWin 6.7.5, libei 1.6.0, Python 3.14).

- The runtime base is `archlinux:base-20260920.0.596911@sha256:f3691b4dde62ba4c4b6f0ae2c1fbf28e8c0c8c4b9a35c7e06dc1f70e21aa29f6`.
- pacman reads the Arch Linux Archive snapshot for `2026/09/26` (build argument `ARCH_ARCHIVE_DATE`), the equivalent of the Debian snapshot pin, and `pacman -Syyuu` moves the base packages to exactly that snapshot.
- The image is `linux/amd64` only. The official Arch image has no arm64 build, and no maintained Arch-family arm64 image tracks current packages: Manjaro ARM's `arm-stable` branch still ships KWin 5.27 and Python 3.11. On an arm64 host, build and run it with `--platform linux/amd64` under emulation, which is several times slower.
- pacman's download sandbox restricts syscalls with seccomp, which fails with `EINVAL` under qemu-user emulation, so the image enables `DisableSandboxSyscalls`. The base image already disables the filesystem part of the sandbox.
- Package names differ from Debian: `kate` provides `/usr/bin/kwrite`, `libkscreen` provides `kscreen-doctor`, `xorg-server-xvfb` provides `Xvfb`, and `xorg-xwininfo`/`xorg-xdpyinfo` replace `x11-utils`.
- `kwin_wayland` ships with the `cap_sys_nice=ep` file capability. Docker's `NoNewPrivileges` makes the kernel refuse to execute it for the unprivileged test user, so the image strips it with `setcap -r`, as the Debian image does.

### Multi-touch oracles and Qt versions

KWrite cannot show that a multi-finger touch reached it, so the multi-finger assertions in `test_touch.py` and in the MCP touch tests read the GTK interaction probe (`interaction_probe.py`) instead. The probe records each touch sequence (`fingers`, `motions`, `release`) and runs a `GestureZoom`. KWrite remains the oracle for single-finger swipe scrolling; the pinch delivery check still reads KWrite's clipboard response, which the last bullet caveats.

- Both KWin 6.3.6 and 6.7.5 deliver the same `wl_touch` stream for a two-finger swipe (two `down`, paired `motion`, two `up`, no `cancel`). `GlobalShortcutFilter::touchDown` in KWin's `src/input.cpp` claims a touchscreen gesture only at three or more touch points, and both versions have that code.
- KTextEditor scrolls through `QScroller::grabGesture` (`kateviewinternal.cpp`). `QFlickGestureRecognizer::recognize` in Qt's `qflickgesture.cpp` ignores any touchscreen `QTouchEvent` without exactly one point, the same in Qt 6.8.2 and 6.11.2.
- In Qt 6.8.2 (Debian trixie), `QApplicationPrivate::findClosestTouchPointTarget` could pick a point with no target, so the second finger was never grouped with the first. KWrite's view then received one-point `TouchUpdate` events, and the flick recognizer scrolled on the first finger. Qt fixed this in f66a34b9 and 8976c4cd (QTBUG-125197). Qt 6.11.2 (Arch, Fedora 44, openSUSE Tumbleweed) delivers two-point events, and the recognizer ignores them. A two-finger swipe no longer scrolls KWrite, and it only places the cursor where the first finger landed.
- KWrite has no pinch. A pinch only moves its cursor, and Ctrl+C with no selection copies the whole current line, so a clipboard comparison measures cursor lines, not a selection.

## Fedora and openSUSE Tumbleweed variants

`e2e-fedora.Dockerfile` (Fedora 44) and `e2e-opensuse.Dockerfile` (openSUSE Tumbleweed) keep the same contract as `e2e.Dockerfile` (same stages, `/opt/kwin-mcp-venv`, `tester` user (UID 1000), entrypoint, `/artifacts` directory, and `pytest tests/e2e` command), so the unchanged suite checks kwin-mcp against KDE Gear 26.08 components (KWin 6.7.5, libei 1.6.0, Qt 6.11). Unlike the Debian and Arch images, neither distribution offers a dated package snapshot, so packages resolve from the live `fedora`/`updates` or Tumbleweed repositories at build time and can change between builds; `environment.json` records the installed versions through `rpm --query`.

- Package names differ from Debian: Fedora `dbus-tools` provides `dbus-run-session`/`dbus-send`, `xdpyinfo`/`xwininfo` replace `x11-utils`, `xorg-x11-server-Xvfb` provides `Xvfb`, `xorg-x11-server-Xwayland` provides Xwayland, and `mesa-demos` provides `glxinfo`/`eglinfo`. On Tumbleweed the equivalents are `dbus-1-tools`, `Mesa-demo-x`, `xwayland`, and the Python stack is `python313-*`.
- Tumbleweed needs `imlib2-loaders` for `scrot` to write PNGs, `typelib-1_0-Atspi-2_0`/`typelib-1_0-Gtk-3_0` plus `python313-gobject-Gdk` for the AT-SPI2 and GTK probe paths, and `libkscreen6-plugin` for `kscreen-doctor`; Fedora needs `gobject-introspection` for the AT-SPI2 typelibs and `libkscreen` for `kscreen-doctor`.
- Both install `konsole` for the terminal Unicode-paste test plus `spectacle`, `wtype`, `wl-clipboard`, `scrot`, and `xdotool` for the same suite coverage as Debian.

## Process topology

```text
scripts/run-e2e-docker.sh
  |-- docker build
  |     |-- build the kwin-mcp wheel
  |     +-- install wheel with resolved dependencies in /opt/kwin-mcp-venv
  |
  +-- docker run (tester, UID 1000)
        |-- e2e-entrypoint.sh
        |     +-- write /artifacts/environment.json
        |
        +-- pytest tests/e2e
              |-- engine-level virtual tests
              |     +-- AutomationEngine
              |           +-- dbus-run-session
              |                 |-- AT-SPI2 bus
              |                 +-- kwin_wayland --virtual
              |                       +-- KCalc, KWrite, Konsole, and test probes
              |
              |-- installed MCP stdio tests
              |     +-- MCP ClientSession <-> installed `kwin-mcp`
              |           +-- AutomationEngine and the virtual or connected session
              |
              +-- visual tests
                    |-- Xvfb inside the container
                    +-- dbus-run-session
                          |-- AT-SPI2 bus
                          +-- nested kwin_wayland --x11-display ...
                                |-- Wayland applications
                                +-- installed `kwin-mcp` via session_connect
                                      +-- explicit X11/scrot pixel capture
                                          (output scale set by kscreen-doctor)
```

The virtual and nested compositors use Mesa llvmpipe software rendering. KWin >= 6.8 also requires `/dev/udmabuf`, the kernel software-render-device allocator, for its virtual software render device. CI detects the KWin version, then passes that host character device and its group only to actual E2E test containers that require it; quality and collect-only containers remain device-free. Local actual-test runs must arrange equivalent access themselves when using KWin >= 6.8. No `--privileged`, `--cap-add`, physical GPU, or `/dev/dri` passthrough is required. Xvfb is installed and started inside the container only for nested visual tests; the Docker host does not need an X server. Nested Xvfb/DRI3 compatibility remains unresolved separately; this allocator requirement does not claim that all KWin 6.8 nested QA passes.

## Commands

Run commands from the repository root. The local runner is the recommended interface because it creates a writable artifact directory, names and removes the container, and collects diagnostics after failures.

Full suite:

```bash
scripts/run-e2e-docker.sh
```

One test file:

```bash
scripts/run-e2e-docker.sh -- tests/e2e/test_visual_qa.py -v
```

A pytest selection:

```bash
scripts/run-e2e-docker.sh -- -k "mcp and not screenshot" -v
```

A custom image tag:

```bash
scripts/run-e2e-docker.sh --image-tag kwin-mcp-e2e-local
```

The other image variants (default tags `kwin-mcp-e2e-fedora`, `kwin-mcp-e2e-opensuse`, `kwin-mcp-e2e-arch`):

```bash
scripts/run-e2e-docker.sh --distro fedora
scripts/run-e2e-docker.sh --distro opensuse
scripts/run-e2e-docker.sh --distro archlinux -- tests/e2e/test_pointer_click.py -v
```

To invoke Docker directly:

```bash
artifact_dir="$PWD/artifacts/e2e/manual"
mkdir -p "$artifact_dir"
chmod 0777 "$artifact_dir"
docker build --file docker/e2e.Dockerfile --tag kwin-mcp-e2e .
docker run --rm \
  --volume "$artifact_dir:/artifacts" \
  kwin-mcp-e2e \
  /opt/kwin-mcp-venv/bin/python -m pytest tests/e2e -v \
  --junitxml=/artifacts/junit.xml
```

Additional arguments after the image command can select a file, node ID, marker, or `-k` expression. Replace `docker/e2e.Dockerfile` with `docker/e2e-arch.Dockerfile`, `docker/e2e-fedora.Dockerfile`, or `docker/e2e-opensuse.Dockerfile` to build another distro's image.

### Direct AT-SPI startup coverage

Virtual sessions normally activate AT-SPI2 through D-Bus. On a host whose SELinux policy denies that activation, `KWIN_MCP_ATSPI_DIRECT_START=1` in the **server environment** opts into direct startup; no other value enables it, and an application's `env` does not opt in. The helper (`dbus_service_file.py`) validates both service commands before launching, bounds service-file acquisition and rollback, and runs only after normal activation fails. The [README accessibility section](../README.md#accessibility-tree) describes the trust and confinement limits.

Run the controlled activation-denial and execution-security tests with:

```bash
scripts/run-e2e-docker.sh -- tests/e2e/test_session_atspi_direct_start.py tests/e2e/test_atspi_service_acquisition.py tests/e2e/test_atspi_exec_security.py -v
```

These tests simulate activation denial and stalled helpers; a successful container run does not verify behavior on an SELinux-enforcing Fedora host or real NFS/FUSE stalls.

## Test-file inventory

The suite is organized by tool: one file per 1.0 tool for engine-level behavior, cross-cutting files for rules shared by several tools, and `test_mcp_*` files that drive the installed server over stdio through `mcp_harness.py`. Oracles are independent of the tool under test (`kwin_oracle.py` reads KWin through its own scripts, `atspi_oracle.py` reads AT-SPI2 directly, and the probes record the events they receive). Tests that cover a QA item carry `@pytest.mark.qa("QA-...")`; `tests/qa_trace.toml` maps every QA id to its tests.

**Per-tool files**

| Test file | Coverage |
|---|---|
| `test_session_start.py` | `session_start`: virtual kind, sockets, screen size, backends; double start refused; `retention.home` without `home="isolated"` refused; startup progress. |
| `test_session_connect.py` | `session_connect`: live kind, clipboard always on, EIS, the four accessibility states with their remedy lines, `retention.home` refused on live, startup progress. |
| `test_session_stop.py` | `session_stop`: `kind`/`kept`/`released`; stop without a session is `stopped: false`, not an error; held buttons and keys released and reported; a live stop kills only launched apps and restores the accessibility switch. |
| `test_session_info.py` | `session_info`: `null` without a session; launched apps with pid, command, running state, exit code and log path; held input reported after revalidation. |
| `test_app_launch_log.py` | `app_launch`/`app_log`: a failed launch keeps the session and names `session_stop`; `env` reaches only the launched process; virtual `DISPLAY` stripped, live kept; tail, `0` = whole log, unknown pid, empty log; the log URI matches the resource. |
| `test_screen_capture.py`, `test_screen_capture_visual.py` | `screen_capture`: `Frame` fields, `region` inside/outside/partial, `max_edge` `0`/null/server default, virtual-backend failure as a tool error with the server surviving; pixel oracles at output scales 1.0 and 1.45 in the nested X11/scrot fixture. |
| `test_element_query.py` | `element_query`: empty-locator and filtered headers, case-sensitive states, `max_depth` `0`, childless applications, `window_id`, `wait` present/absent/timeout/`timeout_ms=0`, scrollbar values. |
| `test_element_refs.py` | Ref generation prefix, stale refs after a worker restart, role change rejected, `rect: null` target refused with the reason, `{ref, dx, dy}` hitting the neighbour. |
| `test_element_act.py` | `element_act` by ref and by strict locator (depth 64, fail-closed), exact/spoken name matching, ordered fallback actions, zero candidates listing actions, `confirmed`/`sent_unconfirmed`, app-reported failure, `capture` after the action. |
| `test_window_list.py` | `window_list`: every window kind with `kind`, `filter={kind:["normal"]}`, `{active:true}`, capability flags that match KWin, `desktops`, frame contains client, unknown app gives `[]`, a KWin timeout as a tool error. |
| `test_desktop_list.py` | `desktop_list`: stable ids, names, the current desktop. |
| `test_window_focus.py` | `window_focus` by id, dialog focus allowed, a vanished window as `window: null`, `capture` after focus, a window on another desktop or minimized. |
| `test_window_place.py`, `test_window_place_desktop.py` | `window_place`: partial frames, applied vs pending, `before` with moveable/resizeable, `window: null`, `observation_error`, beyond-workspace refusal; desktop moves, idempotent assignment with divergent transients, propagation stopping at a child already on the target desktop, nested transient and modal refusal with `nothing was changed` (GTK fixtures in `transient_probe.py`). |
| `test_window_close.py`, `test_window_close_consent.py` | `window_close`: virtual `not_required` closes only the target; ambiguous selectors list ids and kinds; `closeable=false` gives `not_closeable`; live accept, accept-unchecked, decline and cancel texts; `--live-consent=refuse`; no prompt for an unknown id. |
| `test_window_selector_errors.py` | Window-selector teaching errors over MCP without a session and with one: empty selector, zero and several matches, `kind: []`. |
| `test_pointer_click.py` | `pointer_click`: hits on KCalc, `count` 2 and 3, `KeyChord` modifiers, `hold_ms` long-press and its progress, `hold_ms=0`. |
| `test_pointer_move.py` | `pointer_move` hover; refused while a button is held, with the literal remedy and no motion injected. |
| `test_pointer_scroll.py` | `pointer_scroll`: smooth 15 px per unit, detents, horizontal, `steps`, `capture`, `delta=0` sending nothing. |
| `test_pointer_drag.py` | `pointer_drag`: KWrite selection, waypoints dwelling in order, `dwell_ms` at `to`, ref endpoints, release in `finally` on error. |
| `test_pointer_hold.py` | `pointer_hold` press/move/release equal to a manual drag; press on a held button sends nothing; move on an unheld button is a teaching error; release of an untracked button still injects; every result reports `held`. |
| `test_keyboard_press.py` | `keyboard_press` chord grammar, unknown keys rejected before input, Return/Tab as real keys, `capture`. |
| `test_keyboard_type.py` | `keyboard_type`: method `keys` independent of the host layout, non-QWERTY text rejected with the unicode remedy, `route` reported, echo bounds, empty text, progress. |
| `test_keyboard_hold.py` | `keyboard_hold`: a held `shift` plus a click reports the modifier, a second press sends nothing, reverse-order release, untracked release injects, any key or full chord accepted. |
| `test_touch.py` | `touch_tap` without moving the pointer, tap hold progress, swipe scrolling KWrite, 2-finger delivery to the probe, 3 to 5 fingers consumed by KWin, pinch direction. |
| `test_clipboard.py` | `clipboard_read`/`clipboard_write`: structured text, echo and length, NUL and 1 MiB text, the disabled-virtual gate remedy, live always enabled, empty selection error. |
| `test_dbus_call.py` | `dbus_call` arguments as received by a test D-Bus service, `{reply, signature}`. |
| `test_wayland_list.py` | `wayland_list`: raw-line substring filter (case-sensitive), parsed globals, missing `wayland-info` hint. |
| `test_terminal_unicode_paste.py` | `keyboard_type(method="unicode")` in Konsole running `cat`: byte-for-byte delivery through Ctrl+Shift+V (`route` `paste:ctrl+shift+v`), and a failed call leaves the next Return unaltered (#53). |
| `test_unicode_clipboard_lifecycle.py` | Unicode clipboard paste: byte-for-byte restoration of every prior MIME type, empty prior selections, the secret hint, a concurrent user copy that must survive, a stalled prior owner, a single restore owner, survival across a live-session disconnect. |
| `test_clipboard_helper_protocol.py` | The private `kwin_mcp.clipboard` helper driven directly against a test-owned KWin over data-control: READY timeout, framing errors, and restoring the prior selection. |
| `test_spectacle_composition.py` | Spectacle fallback decisions for synthetic KWin topologies (#67): refused layouts before Spectacle starts on affected versions; controls that still capture. |
| `test_virtual_session_smoke.py` | The virtual-session smoke run, including the exact-virtual ScreenShot2 success probe `test_screen_capture_exact_virtual_backend` (skipped with its reason, see below) and the error path of that backend. |
| `test_session_accessibility_switch.py` | The `org.a11y.Status.IsEnabled` switch: on in virtual sessions; reported, left off by default, enabled by `enable_accessibility=true`, and restored at `session_stop` and on a server exit, including one that lands while a tool runs. |
| `test_atspi_worker.py` | The long-lived AT-SPI2 worker: answers from the current bus after a replaced session, respawn of a killed worker, reaping of a stopped worker, no helper left after `session_stop`. |
| `test_session_atspi_direct_start.py` | Exact server-environment opt-in, activation-denial warnings, `XDG_DATA_DIRS`, missing/invalid services, stalled-helper rollback, and direct-start cleanup. |
| `test_atspi_service_acquisition.py` | Child-only service lookup, the 128 KiB limit, descriptor guards, nonregular candidates, symlinks, inaccessible roots, both-service preflight, registry revalidation, and literal argv/environment handoff. |
| `test_atspi_exec_security.py` | Direct helper execution rejects shell builtins, implicit `PATH` search, function evaluation, and `ENOEXEC` fallback; permits valid shebang scripts and explicit interpreters; isolates Python customization and startup-handshake stdout. |
| `test_eis_device_replacement.py`, `test_eis_handshake.py`, `test_lazy_libei.py` | Input after KWin replaces its EIS devices (#76), the scripted-libei handshake and device lifecycle, and a missing `libei.so.1` reported in `session_info` with the session still stoppable. |
| `test_pointer_reconnect.py` | A `pointer_click` after `session_stop` and `session_connect` to the same KWin reaches a new window under the parked cursor (#66). |
| `test_environment_evidence.py`, `test_installed_package.py` | Allowlisted `environment.json` provenance; the installed distribution: version 1.0.0 equal to `pyproject.toml` and `serverInfo.version`, the `Production/Stable` classifier, entry points, `kwin-mcp --help` equal to the shipped usage text, usage errors with exit status 2. |

**Cross-cutting files**

| Test file | Coverage |
|---|---|
| `test_tool_surface_cutover.py` | Exactly the 29 tool names over stdio, none of the 28 removed names, and the CLI's 29 commands. |
| `test_capture.py` | `capture` on all 15 carriers in one KCalc session: sorted frames with duplicates kept, `[]` = no frames, a failure after the action as a warning, options equal to `screen_capture`. |
| `test_held_input.py`, `test_eis_held_revalidation.py` | The held-input rule table (gestures refused on a held control, other controls allowed) and revalidation after an EIS disconnect or device replacement. |
| `test_window_kinds.py` | Dialogs, popups and tooltips listed with `kind`; an `app_id` selector ambiguous while a menu is open; `kind: ["normal"]` disambiguates; ids and `active` reach every kind. |
| `test_session_retention.py` | The artifacts × home retention matrix on start and connect, honored by the server-exit path; `artifacts=false` plus `SIGKILL` leaks the directory and the next session does not reuse it. |
| `test_session_processes.py` | Sockets, process groups, reaping, and `PATH`-stub lifecycle regressions (bounded startup failure with stderr diagnostics, a reaped leader, a `SIGTERM`-ignoring descendant, waiting for `org.kde.KWin`). |
| `test_exit_cleanup_virtual.py`, `test_exit_cleanup_live.py` | Server and CLI exit stop the session like `session_stop` on EOF, `SIGTERM`, `SIGHUP` and `SIGINT`, within the 2 s drain and the owned-process registry fallback, honoring `retention` and releasing held input; on a live session KWin and pre-existing apps survive. |
| `test_a11y_text_formats.py` | Element text: the tree header for an empty locator, the found header, `NOT SATISFIED:` first, the unavailable-rect marker, text equal to the structured result's rendering. |
| `test_cli_surface.py`, `test_cli_errors.py` | `kwin-mcp-cli` output equal to the server text for the same call; JSON model parameters; one `Error:` line per tool error, naming the replacement for an unknown argument. |
| `test_input_options_matrix.py` | Every input option (`count`, `axis`, `unit`, `KeyChord`, `Target`, `Waypoint`, `capture`) against the GTK probe's recorded events and status pixels. |
| `test_oracles.py` | Harness self-test: `kwin_oracle`, `atspi_oracle` and the probes validated independently of the tools. |
| `test_visual_qa.py` | Pixel-backed GUI probe and KCalc oracles: hover repaint, cursor localization, animation bursts, CJK-versus-tofu rendering, binary-value transitions. |

**Installed MCP stdio files**

| Test file | Coverage |
|---|---|
| `test_mcp_protocol.py` | Initialization, the 29 names and schemas from the committed snapshot, two-line `ToolError`s, server survival. |
| `test_structured_output.py` | For each of the 29 tools: an object-rooted `outputSchema`, `structuredContent` that validates, text equal to the rendering of the validated model, no `structuredContent` on errors. |
| `test_mcp_annotations.py` | The per-tool annotation profiles, titles, and the server icon. |
| `test_mcp_progress.py` | Progress notifications: `session_start`, `session_connect`, `element_query` with `wait`, `pointer_drag`, `keyboard_type`, touch gestures, capture bursts and holds; monotonic and never past the total. |
| `test_mcp_images_flag.py` | Under `--screenshot-images`, one `image/png` block per frame in order, byte-equal to the frame's file, for `screen_capture` and each of the 15 carriers; none without the flag or on errors. |
| `test_mcp_resources.py` | `kwin://session`, `kwin://windows`, `kwin://desktops`, `kwin://apps/{pid}/log` with completion, `kwin://screenshots/{name}` with path traversal refused. |
| `test_mcp_errors.py` | The unified no-session and no-input texts for every gated tool, and the shutdown gate. |
| `test_mcp_schema_wire.py` | The stdio `tools/list` equal to the committed snapshot, with the listing budget and forbidden strings re-checked on the wire bytes. |
| `test_mcp_unknown_args.py` | Per tool: an unknown key is an `isError` naming it plus the closest key; each removed argument names its replacement. |
| `test_mcp_input_tools.py`, `test_mcp_session_observation.py`, `test_mcp_clipboard.py`, `test_mcp_stdin_isolation.py` | Input, session, observation, window, log, Wayland, D-Bus and clipboard tools changing application state over stdio; the server's JSON-RPC stdin stays private from launched apps. |
| `test_mcp_scenarios.py` | Agent tasks as 1.0 call sequences over stdio, each checked through an independent oracle. |

**Parser and fixture support**

| File | Coverage |
|---|---|
| `tests/unit/test_spoken_name.py` | `spoken_name.py`: NFC/case folding, significant compatibility characters, Unicode whitespace, the seven allowed terminal punctuation marks, and word boundaries. |
| `tests/unit/test_dbus_service_file.py` | `dbus_service_file.py`: `Exec=` argument splitting compared with real dbus-daemon activation, first-group/first-`Exec` rules, malformed files, line endings, and no expansion. |
| `tests/unit/test_e2e_shards.py` | Behavioral subprocess tests for row-local E2E shard aggregation: exact five-file coverage, successful shard results, collected/full count agreement, and isolation from other rows. |
| `tests/e2e/transient_probe.py` | GTK parent/child/nested transient windows for independent desktop-propagation and modal-refusal checks. |

KWin-free tests live in `tests/unit/` and run on the host with `uv run pytest tests/unit` (CI's `unit` job, Python 3.12 to 3.14; dbus-python 1.4.0 does not import on CPython 3.15 yet). They cover the schemas against the design, unknown-argument and removed-tool errors, the renderers (`test_render.py` goldens), held-input state machines, selector and locator matching, KWin script rendering, CLI value parsing, row-local shard-status aggregation behavior, and the documentation gates.

The CI workflow (`.github/workflows/e2e.yml`) runs `python -m pytest tests -v -m "not slow"` in the image, sharded across jobs, so it also collects `tests/unit`. `.github/workflows/e2e-slow.yml` runs the tests marked `slow`. The image's default command runs `tests/e2e`; `scripts/run-e2e-docker.sh` runs the whole `tests` tree unless you name a test path.

## Visual pixel oracles

Visual tests do not infer success from process exit alone. They preserve PNGs and compare material pixel differences with accessibility state:

- KCalc's displayed binary value changes from the initial state to `111`, then to `1000110`, with corresponding screenshot transitions;
- moving the pointer over the GUI probe changes both its AT-SPI2 status and the hover target's pixels;
- captures with and without `include_cursor` must differ near the requested pointer coordinates rather than elsewhere on the frame;
- a `capture={delays_ms:[...]}` burst returns distinct animation frames for the requested delays;
- at output scales 1.0 and 1.45, a probe button located by pixel color in a screenshot and clicked at the reported origin plus that pixel must activate, the image size must equal the logical workspace, burst frames must report the same mapping, and the `include_cursor` difference must appear at the logical pointer position;
- the CJK text sample must render with materially different pixels and color content from the same-sized missing-glyph tofu control.

The screen-capture visual tests also drive frame-producing pointer, keyboard, touch, and drag tools through installed MCP stdio.

## Evidence layout

The local runner creates one directory per invocation:

```text
artifacts/e2e/<UTC-timestamp>-<pid>/
  environment.json
  junit.xml
  pytest.log
  quality.log
  visual-kwin-*/
    capture-backend.txt
    dbus-address.txt
    xvfb.stdout.log
    xvfb.stderr.log
    kwin.stdout.log
    kwin.stderr.log
    mcp-server.stderr.log
    kscreen-doctor.log        (fractional-scale fixtures only)
    *.png
    additional probe logs and text evidence
```

When the run fails, the runner also writes:

```text
docker-inspect.json
docker-inspect.stderr
docker.log
docker-processes.txt
```

CI stores shard evidence under `artifacts/e2e/<label>-s<N>/` and uploads one diagnostic artifact and one status artifact per shard, plus one compressed image artifact per row. Lint and type checks run only in the Debian rows because they do not depend on the distro. The native runner matrix is:

| Label | Image | Runner |
|---|---|---|
| `amd64` | `e2e.Dockerfile` (Debian) | `ubuntu-26.04` |
| `arm64` | `e2e.Dockerfile` (Debian) | `ubuntu-26.04-arm` |
| `fedora-amd64` | `e2e-fedora.Dockerfile` (Fedora 44) | `ubuntu-26.04` |
| `fedora-arm64` | `e2e-fedora.Dockerfile` (Fedora 44) | `ubuntu-26.04-arm` |
| `opensuse-amd64` | `e2e-opensuse.Dockerfile` (openSUSE Tumbleweed) | `ubuntu-26.04` |
| `opensuse-arm64` | `e2e-opensuse.Dockerfile` (openSUSE Tumbleweed) | `ubuntu-26.04-arm` |
| `archlinux-amd64` | `e2e-arch.Dockerfile` (Arch Linux) | `ubuntu-26.04` |

The Arch Linux image has no arm64 row because no current Arch-family arm64 base image exists.

CI builds one image for each row and shares it with that row's five test shards. The 35 shards
feed seven independent required checks, one per row. Each check first verifies through the
current workflow run's latest jobs API that its row's build and all five shard jobs completed
successfully, then reads its five status files and requires their summed test count to equal
the full collection count. A failure in another row does not change that result. The shard
command keeps the `not slow` selection, strict skip handling, layer-shell requirement, and
existing timeouts. Image artifacts are retained for one day; diagnostic and status artifacts
keep the repository's default retention.

Image artifacts use stable same-run names `e2e-image-LABEL`; status artifacts use
`e2e-status-LABEL-sN`. Reruns overwrite these artifacts, allowing partial reruns to reuse
successful jobs from the same workflow run. Diagnostic archives remain attempt-specific.
Checking the latest row job states before reading statuses prevents an older success from
masking a newer failed, missing, or cancelled job.

One test, the missing-command case of `test_app_launch_log.py`, fails only when the container runs on an emulated kernel such as an arm64 Docker host: qemu-user lets `execve` of a missing file report as exit 127 in the child instead of raising `FileNotFoundError`, so `app_launch` reports a launched process that immediately died. On a native amd64 runner the assertion holds.

## Screenshot backends and virtual/nested limitations

ScreenShot2 and Spectacle remain the normal Wayland screenshot backends. The nested visual fixture explicitly sets `KWIN_MCP_X11_SCREENSHOT=1` and supplies its Xvfb `DISPLAY`, selecting the test-only X11/scrot path so pixel assertions do not depend on KWin's exact virtual screenshot behavior. That path finds each KWin output's X window with `xwininfo` and rescales it to logical pixels, and it mirrors the EIS pointer to the matching physical X position so cursor captures line up. For fractional-scale tests, the fixture sets every nested output's scale through `kscreen-doctor` (package `libkscreen-bin`) and waits until KWin reports it; the command and its output are kept in `kscreen-doctor.log` in the visual artifact directory.

On KWin versions 6.3 and 6.7, the `kwin_wayland --virtual` backend in this image has the known legacy limitation that it does not return ScreenShot2 capture data. On KWin >= 6.8, exposing `/dev/udmabuf` enables its virtual software render device and exact ScreenShot2 capture; CI detects the version, passes that allocator only to actual test containers that require it, and opts those containers into the exact-backend probe. Native KWin 6.8 proof run 39251748324 passed clipboard and exact virtual ScreenShot2 PNG capture at 1280x800 (16,420 bytes). That proof does not claim the separate nested Xvfb/DRI3 path is green. Older rows keep the legacy opt-in behavior and do not receive the allocator. The suite still verifies that ScreenShot2/Spectacle failures become actionable tool errors without killing the engine or installed MCP server. Because the image's Spectacle refuses the headless compositor immediately, the Spectacle fallback is covered with a `spectacle` stub placed first on the server's `PATH`. A stub that answers after 12 seconds, beyond the former fixed 10-second bound, must yield one full logical-space screenshot. A stub that never answers must fail at the deadline the error reports. A stub that exits 0 without writing a file must have its stderr reason included in the error.

Screenshot success is covered through the nested KWin/Xvfb fixture with explicit scrot selection, including cursor pixels, action frame bursts, retention, image oracles, and the reported `Coordinate space` mapping. This keeps the nested Xvfb/DRI3 compatibility question separate from the exact virtual ScreenShot2 path. Because the nested fixture has one output, real multi-output captures, negative origins, ScreenShot2 `CaptureWorkspace` normalization, and Spectacle partial coverage are not captured here; `test_spectacle_composition.py` checks only the fallback decisions for such topologies with stubbed inputs.
