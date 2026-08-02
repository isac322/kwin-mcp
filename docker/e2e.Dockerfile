# Reproducible KWin Wayland environment for kwin-mcp end-to-end tests.
#
# Debian trixie is used because its KWin 6 / AT-SPI2 / libei packages are
# version-pinned by the stable release (identical bits on every machine) and
# published for both amd64 and arm64, so the same image runs on CI and on
# developer laptops.
#
# Build (from the repository root):
#   docker build -f docker/e2e.Dockerfile -t kwin-mcp-e2e .
# Run:
#   docker run --rm kwin-mcp-e2e
FROM debian:trixie-slim

# Runtime dependencies of a virtual KWin session plus the automation stack:
#   kwin-wayland          - the compositor itself (virtual/headless backend)
#   kwin-common           - KWin plugins, notably eis.so (EIS input injection)
#   dbus                  - dbus-run-session, dbus-send, dbus-daemon (AT-SPI bus)
#   at-spi2-core          - at-spi-bus-launcher, AT-SPI2 registry
#   gir1.2-atspi-2.0      - GObject introspection data for gi.repository.Atspi
#   libei1                - EIS input injection (loaded via ctypes by input.py)
#   libgl1-mesa-dri,      - llvmpipe software rendering: the container has no
#   libegl-mesa0            /dev/dri, so KWin's OpenGL backend needs them
#   libcap2-bin           - setcap, used below to drop KWin's file capability
#   kcalc, kwrite         - small Qt/KF6 apps used as test subjects
#   wayland-utils         - wayland-info, backing the wayland_info tool
#   fonts-dejavu-core     - without a font kcalc renders empty boxes
#   python3-{gi,dbus,pil} - runtime deps as system packages (no wheel builds)
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        kwin-wayland \
        kwin-common \
        dbus \
        at-spi2-core \
        gir1.2-atspi-2.0 \
        libei1 \
        libgl1-mesa-dri \
        libegl-mesa0 \
        libcap2-bin \
        wl-clipboard \
        wtype \
        qt6-wayland \
        kwrite \
        wayland-utils \
        kcalc \
        fonts-dejavu-core \
        python3 \
        python3-gi \
        python3-dbus \
        python3-pil \
        python3-pytest \
    && rm -rf /var/lib/apt/lists/*

# Debian ships kwin_wayland with cap_sys_nice=ep. Docker's default capability
# bounding set excludes CAP_SYS_NICE, so exec'ing the binary fails with EPERM.
# Dropping the file capability avoids requiring --cap-add on every run; KWin
# only loses realtime scheduling priority, which tests do not need.
RUN setcap -r /usr/bin/kwin_wayland

# KWin refuses to run as root, and AT-SPI expects a real user account.
RUN useradd --create-home --uid 1000 tester

# session.py falls back to /run/user/$(id -u) when XDG_RUNTIME_DIR is unset;
# that path does not exist in a container, so provide an explicit one.
# LIBGL_ALWAYS_SOFTWARE keeps KWin on llvmpipe instead of probing for a GPU.
ENV XDG_RUNTIME_DIR=/tmp/xdg-runtime \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    LIBGL_ALWAYS_SOFTWARE=1
RUN install -d -m 0700 -o tester -g tester /tmp/xdg-runtime

RUN install -d -m 0755 -o tester -g tester /app
WORKDIR /app
COPY --chown=tester:tester pyproject.toml README.md ./
COPY --chown=tester:tester src/ ./src/
COPY --chown=tester:tester tests/ ./tests/

USER tester
CMD ["python3", "-m", "pytest", "tests/e2e", "-v"]
