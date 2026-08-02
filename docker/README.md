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

| Test | Covers |
|---|---|
| `test_launched_app_is_visible_to_accessibility` | virtual session startup, app launch, window listing over AT-SPI2 |
| `test_widgets_of_the_launched_app_are_queryable` | AT-SPI2 tree traversal: roles, names, geometry |
| `test_keyboard_input_reaches_the_app` | EIS input injection: a typed key reaches the app and changes its state |
| `test_screenshot_captures_the_session` | KWin ScreenShot2 capture (opt-in, see below) |

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

- **Pointer input**: keyboard injection over KWin EIS is verified by the suite,
  but `mouse_click` / `mouse_move` / `touch_tap` change nothing in the app.
  KWin's own debug log shows the events arriving
  (`kwin_libeis: "kwin-mcp eis absolute device" pointer motion absolute 529 233`,
  `button 272 true/false`), so they are lost inside KWin rather than on the
  client side. Excluded by experiment: wrong coordinates (a full-screen click
  sweep changed nothing), a missing device region (the absolute device reports
  `0,0 1280x800`), a missing button capability, the relative pointer device, a
  dead EIS connection (typing still works after a click), and a missing cursor
  theme. Keyboard events reach the app because they follow keyboard focus
  instead of pointer routing.
