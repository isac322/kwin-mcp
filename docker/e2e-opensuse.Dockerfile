# openSUSE Tumbleweed variant of the kwin-mcp end-to-end test image
# (see docker/e2e.Dockerfile).
#
# Build from the repository root:
#   docker build -f docker/e2e-opensuse.Dockerfile -t kwin-mcp-e2e-opensuse .
# Run:
#   docker run --rm kwin-mcp-e2e-opensuse
#
# The base image is pinned by digest, but Tumbleweed is a rolling release with no
# dated snapshot for every architecture, so packages resolve from the current
# repositories at build time. environment.json records the installed versions of
# KWIN_MCP_SYSTEM_PACKAGES.

ARG TUMBLEWEED_IMAGE=opensuse/tumbleweed:latest@sha256:007fde2c1e6b3de48681c765b3d6905d4e59d2f7a26f4585ec49dd11305c3fe7

FROM ghcr.io/astral-sh/uv:0.10.8@sha256:88234bc9e09c2b2f6d176a3daf411419eb0370d450a08129257410de9cfafd2a AS uv-bin

FROM ${TUMBLEWEED_IMAGE} AS wheel-builder

COPY --from=uv-bin /uv /usr/local/bin/uv
RUN zypper --non-interactive install --no-recommends python313 \
    && zypper clean --all

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv build --wheel --out-dir /build/dist

FROM ${TUMBLEWEED_IMAGE} AS runtime-base

# The recorder in docker/e2e-environment.py reports the installed version of
# every package named here, so this list is the single source for both.
ENV KWIN_MCP_SYSTEM_PACKAGES="\
        at-spi2-core \
        breeze6-cursors \
        ca-certificates \
        dbus-1-daemon \
        dbus-1-tools \
        dejavu-fonts \
        google-noto-coloremoji-fonts \
        google-noto-sans-cjk-fonts \
        imlib2-loaders \
        kcalc \
        konsole \
        kwin6 \
        kwrite \
        libcap-progs \
        libei1 \
        libkscreen6-plugin \
        Mesa-demo-x \
        Mesa-dri \
        Mesa-libEGL1 \
        Mesa-libGL1 \
        python313 \
        python313-dbus-python \
        python313-gobject \
        python313-gobject-Gdk \
        python313-numpy \
        python313-Pillow \
        qt6-wayland \
        scrot \
        shadow \
        spectacle \
        typelib-1_0-Atspi-2_0 \
        typelib-1_0-Gtk-3_0 \
        wayland-utils \
        wl-clipboard \
        wtype \
        xauth \
        xdotool \
        xdpyinfo \
        xorg-x11-server-Xvfb \
        xwayland \
        xwininfo"

RUN zypper --non-interactive install --no-recommends $KWIN_MCP_SYSTEM_PACKAGES \
    && zypper clean --all \
    && setcap -r /usr/bin/kwin_wayland \
    && groupadd --gid 1000 tester \
    && useradd --create-home --uid 1000 --gid 1000 --shell /bin/sh tester \
    && install -d -m 1777 -o root -g root /tmp/.X11-unix \
    && install -d -m 0755 -o tester -g tester /app \
    && install -d -m 0755 -o tester -g tester /artifacts \
    && install -d -m 0700 -o tester -g tester /tmp/xdg-runtime

FROM runtime-base AS venv-builder

COPY --from=uv-bin /uv /usr/local/bin/uv
RUN zypper --non-interactive install --no-recommends \
        cairo-devel \
        dbus-1-devel \
        gcc \
        gobject-introspection-devel \
        pkgconf-pkg-config \
        python313-devel \
    && zypper clean --all

COPY --from=wheel-builder /build/pyproject.toml /tmp/pyproject.toml
COPY --from=wheel-builder /build/dist/ /tmp/wheels/
RUN --mount=type=cache,target=/root/.cache/uv \
    python3 -m venv --system-site-packages /opt/kwin-mcp-venv \
    && uv pip install \
        --python /opt/kwin-mcp-venv/bin/python \
        /tmp/wheels/*.whl \
    && uv pip install \
        --python /opt/kwin-mcp-venv/bin/python \
        --group /tmp/pyproject.toml:dev

FROM runtime-base AS runtime
ARG TUMBLEWEED_IMAGE

LABEL org.opencontainers.image.title="kwin-mcp E2E (openSUSE Tumbleweed)" \
      org.opencontainers.image.description="Virtual KWin environment for kwin-mcp end-to-end tests on openSUSE Tumbleweed" \
      org.opencontainers.image.source="https://github.com/isac322/kwin-mcp" \
      org.opencontainers.image.base.name="opensuse/tumbleweed:latest" \
      org.opencontainers.image.base.digest="sha256:007fde2c1e6b3de48681c765b3d6905d4e59d2f7a26f4585ec49dd11305c3fe7"

ENV PATH="/opt/kwin-mcp-venv/bin:${PATH}" \
    HOME=/home/tester \
    XDG_RUNTIME_DIR=/tmp/xdg-runtime \
    KWIN_MCP_ARTIFACT_DIR=/artifacts \
    KWIN_MCP_BASE_IMAGE="${TUMBLEWEED_IMAGE}" \
    KWIN_MCP_BASE_SNAPSHOT="Tumbleweed repositories at image build time" \
    PYTHONUNBUFFERED=1 \
    LIBGL_ALWAYS_SOFTWARE=1

COPY --from=venv-builder /opt/kwin-mcp-venv/ /opt/kwin-mcp-venv/
COPY --chown=1000:1000 pyproject.toml /app/pyproject.toml
COPY --chown=1000:1000 tests/ /app/tests/
COPY --chmod=0755 --chown=1000:1000 \
    docker/e2e-entrypoint.sh \
    docker/e2e-environment.py \
    /app/docker/

WORKDIR /app
USER tester
ENTRYPOINT ["/app/docker/e2e-entrypoint.sh"]
CMD ["/opt/kwin-mcp-venv/bin/python", "-m", "pytest", "tests/e2e", "-v"]
