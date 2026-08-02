# Containerized KWin session for end-to-end tests

`e2e.Dockerfile` builds a self-contained KDE Plasma 6 / KWin Wayland environment
so the end-to-end tests in `tests/e2e/` produce the same result on a laptop and
in CI. It is based on Debian trixie: its KWin, AT-SPI2 and libei packages are
version-pinned by the stable release and published for amd64 and arm64.

## Usage

```bash
docker build -f docker/e2e.Dockerfile -t kwin-mcp-e2e .   # from the repo root
docker run --rm kwin-mcp-e2e                              # runs pytest tests/e2e
docker run --rm kwin-mcp-e2e python3 -m pytest tests/e2e -k accessibility -v
```

No `--privileged`, `--cap-add` or X server is needed: the image drops KWin's
`cap_sys_nice` file capability at build time, and KWin runs on its virtual
(headless) backend with llvmpipe software rendering.

## What the tests cover

| Test file | Covers |
|---|---|
| `test_virtual_session_smoke.py` | the minimum contract: session starts, app launches, AT-SPI2 sees it, a keystroke reaches it |
| `test_session_lifecycle.py` | double start, teardown and socket cleanup, bad app command, isolated HOME, live `session_connect` ownership |
| `test_observation_tools.py` | `accessibility_tree` role filter, query and state filters, multi-window `list_windows`, `read_app_log`, `wayland_info` |
| `test_window_geometry.py` | global frame/client rectangles and the surface-local offset they correct |
| `test_window_control.py` | `focus_window` activation, `mouse_scroll` (smooth and discrete), `mouse_drag` selection, touch swipe / multi-swipe / pinch, scrollbar values |
| `test_input_injection.py` | EIS mouse click and press/release with verified aim, touch tap, key combos, modifier hold, unicode input, clipboard roundtrip |

## Known limitations in the container

- **Screenshots**: KWin registers `org.kde.KWin.ScreenShot2` only when the
  screenshot effect loads, which requires OpenGL compositing and therefore a DRM
  render node (`--device /dev/dri`). Even with one, KWin's virtual backend never
  answers a capture request (no reply, zero bytes), so the screenshot test is
  skipped unless `KWIN_MCP_E2E_SCREENSHOT=1` is set. CI deliberately passes
  neither the device nor the flag: every run then takes the same llvmpipe
  software path, and enabling the test would only turn a known-unsupported
  capture into a red build. The combination below **fails today** — it is the
  command to re-check with once the compositor answers captures, not a working
  recipe:

  ```bash
  docker run --rm --device /dev/dri -e KWIN_MCP_E2E_SCREENSHOT=1 kwin-mcp-e2e
  ```

- **Pointer coordinates are not screen coordinates**: `mouse_click(x, y)` takes
  global screen coordinates, while `find_ui_elements` and `accessibility_tree`
  report surface-local ones — a Wayland client cannot know its own position, so
  its frame always starts at `(0, 0)`. Clicking a reported rect verbatim lands
  on whatever happens to sit at that screen position. Add the client origin from
  `window_geometry` first: for a centred 640x480 kcalc window on a 1280x800
  screen it is `(320, 172)`, so the "Seven" button at surface-local
  `(8, 204, 100x59)` is clicked at global `(378, 405)`, after which the visible
  display reads `7` and the AT-SPI2 tree reports the hidden base-conversion
  label `111` that the tests assert on.

- **Not covered**: `touch_pinch` is asserted only as multi-touch delivery, since
  KWrite has no pinch-zoom to observe. Three- and four-finger `touch_multi_swipe`
  never reaches the application because KWin claims those as global gestures, so
  the test uses two fingers.
