# Fedora variant of the kwin-mcp end-to-end test image (see docker/e2e.Dockerfile).
#
# Build from the repository root:
#   docker build -f docker/e2e-fedora.Dockerfile -t kwin-mcp-e2e-fedora .
# Run:
#   docker run --rm kwin-mcp-e2e-fedora
#
# The base image is pinned by digest, but Fedora has no dated package snapshot,
# so packages resolve from the fedora and updates repositories at build time.
# environment.json records the installed versions of KWIN_MCP_SYSTEM_PACKAGES.

ARG FEDORA_IMAGE=fedora:44@sha256:43b29f65a41eb9c35e1cd5323e3bdf3b655c2357a9f4f1ff2f9c2798e5045d80

FROM ghcr.io/astral-sh/uv:0.10.8@sha256:88234bc9e09c2b2f6d176a3daf411419eb0370d450a08129257410de9cfafd2a AS uv-bin

FROM ${FEDORA_IMAGE} AS wheel-builder

COPY --from=uv-bin /uv /usr/local/bin/uv
RUN dnf -y install --setopt=install_weak_deps=False python3 \
    && dnf clean all

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv build --wheel --out-dir /build/dist

FROM ${FEDORA_IMAGE} AS runtime-base

# The recorder in docker/e2e-environment.py reports the installed version of
# every package named here, so this list is the single source for both.
ENV KWIN_MCP_SYSTEM_PACKAGES="\
        at-spi2-core \
        breeze-cursor-theme \
        ca-certificates \
        dbus-daemon \
        dbus-tools \
        dejavu-sans-fonts \
        gobject-introspection \
        google-noto-color-emoji-fonts \
        google-noto-sans-cjk-vf-fonts \
        google-noto-sans-fonts \
        gtk3 \
        kcalc \
        konsole \
        kwin \
        kwrite \
        libcap \
        libei \
        libkscreen \
        mesa-demos \
        mesa-dri-drivers \
        mesa-libEGL \
        mesa-libGL \
        python3 \
        python3-dbus \
        python3-gobject \
        python3-numpy \
        python3-pillow \
        qt6-qtwayland \
        scrot \
        shadow-utils \
        spectacle \
        wayland-utils \
        wl-clipboard \
        wtype \
        xdotool \
        xdpyinfo \
        xorg-x11-server-Xvfb \
        xorg-x11-server-Xwayland \
        xorg-x11-xauth \
        xwininfo"

RUN dnf -y install --setopt=install_weak_deps=False $KWIN_MCP_SYSTEM_PACKAGES \
    && dnf clean all \
    && setcap -r /usr/bin/kwin_wayland \
    && groupadd --gid 1000 tester \
    && useradd --create-home --uid 1000 --gid 1000 --shell /bin/sh tester \
    && install -d -m 1777 -o root -g root /tmp/.X11-unix \
    && install -d -m 0755 -o tester -g tester /app \
    && install -d -m 0755 -o tester -g tester /artifacts \
    && install -d -m 0700 -o tester -g tester /tmp/xdg-runtime

FROM runtime-base AS venv-builder

COPY --from=uv-bin /uv /usr/local/bin/uv
RUN dnf -y install --setopt=install_weak_deps=False \
        cairo-devel \
        cairo-gobject-devel \
        dbus-devel \
        gcc \
        gobject-introspection-devel \
        pkgconf-pkg-config \
        python3-devel \
    && dnf clean all

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
ARG FEDORA_IMAGE

LABEL org.opencontainers.image.title="kwin-mcp E2E (Fedora)" \
      org.opencontainers.image.description="Virtual KWin environment for kwin-mcp end-to-end tests on Fedora" \
      org.opencontainers.image.source="https://github.com/isac322/kwin-mcp" \
      org.opencontainers.image.base.name="fedora:44" \
      org.opencontainers.image.base.digest="sha256:43b29f65a41eb9c35e1cd5323e3bdf3b655c2357a9f4f1ff2f9c2798e5045d80"

ENV PATH="/opt/kwin-mcp-venv/bin:${PATH}" \
    HOME=/home/tester \
    XDG_RUNTIME_DIR=/tmp/xdg-runtime \
    KWIN_MCP_ARTIFACT_DIR=/artifacts \
    KWIN_MCP_BASE_IMAGE="${FEDORA_IMAGE}" \
    KWIN_MCP_BASE_SNAPSHOT="fedora and updates repositories at image build time" \
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
