# Reproducible KWin Wayland environment for kwin-mcp end-to-end tests.
#
# Build from the repository root:
#   docker build -f docker/e2e.Dockerfile -t kwin-mcp-e2e .
# Run:
#   docker run --rm kwin-mcp-e2e

ARG DEBIAN_SNAPSHOT=20260913T000000Z

FROM ghcr.io/astral-sh/uv:0.10.8@sha256:88234bc9e09c2b2f6d176a3daf411419eb0370d450a08129257410de9cfafd2a AS uv-bin

FROM debian:trixie-slim@sha256:d7e12182ce18b85b93007c1dedf31f2d29e01ccf3182cc4017c709b6259bc132 AS debian-snapshot

ARG DEBIAN_SNAPSHOT
RUN printf '%s\n' \
        'Types: deb' \
        "URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/" \
        'Suites: trixie trixie-updates' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        '' \
        'Types: deb' \
        "URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}/" \
        'Suites: trixie-security' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        > /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
        'Acquire::Check-Valid-Until "false";' \
        'Acquire::Retries "5";' \
        'Acquire::http::Timeout "120";' \
        'Acquire::http::Pipeline-Depth "0";' \
        > /etc/apt/apt.conf.d/99debian-snapshot

FROM debian-snapshot AS wheel-builder

COPY --from=uv-bin /uv /usr/local/bin/uv
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv build --wheel --out-dir /build/dist

FROM debian-snapshot AS runtime-base

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        at-spi2-core \
        breeze-cursor-theme \
        ca-certificates \
        dbus \
        fonts-dejavu-core \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
        fonts-noto-core \
        gir1.2-atspi-2.0 \
        gir1.2-gtk-3.0 \
        kcalc \
        kde-spectacle \
        kwin-common \
        kwin-wayland \
        kwrite \
        libcap2-bin \
        libegl-mesa0 \
        libei1 \
        libgirepository-2.0-0 \
        libgl1-mesa-dri \
        libglx-mesa0 \
        libkscreen-bin \
        mesa-utils \
        python3 \
        python3-dbus \
        python3-gi \
        python3-numpy \
        python3-pil \
        python3-venv \
        qt6-wayland \
        scrot \
        wayland-utils \
        wl-clipboard \
        wtype \
        x11-utils \
        xauth \
        xdotool \
        xvfb \
        xwayland \
    && rm -rf /var/lib/apt/lists/* \
    && setcap -r /usr/bin/kwin_wayland \
    && groupadd --gid 1000 tester \
    && useradd --create-home --uid 1000 --gid 1000 --shell /bin/sh tester \
    && install -d -m 1777 -o root -g root /tmp/.X11-unix \
    && install -d -m 0755 -o tester -g tester /app \
    && install -d -m 0755 -o tester -g tester /artifacts \
    && install -d -m 0700 -o tester -g tester /tmp/xdg-runtime

FROM runtime-base AS venv-builder

COPY --from=uv-bin /uv /usr/local/bin/uv
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        libdbus-1-dev \
        libcairo2-dev \
        libgirepository-2.0-dev \
        pkg-config \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

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
ARG DEBIAN_SNAPSHOT

LABEL org.opencontainers.image.title="kwin-mcp E2E" \
      org.opencontainers.image.description="Reproducible virtual KWin environment for kwin-mcp end-to-end tests" \
      org.opencontainers.image.source="https://github.com/isac322/kwin-mcp" \
      org.opencontainers.image.base.name="debian:trixie-slim" \
      org.opencontainers.image.base.digest="sha256:d7e12182ce18b85b93007c1dedf31f2d29e01ccf3182cc4017c709b6259bc132" \
      io.github.isac322.kwin-mcp.debian-snapshot="${DEBIAN_SNAPSHOT}"

ENV PATH="/opt/kwin-mcp-venv/bin:${PATH}" \
    HOME=/home/tester \
    XDG_RUNTIME_DIR=/tmp/xdg-runtime \
    KWIN_MCP_ARTIFACT_DIR=/artifacts \
    KWIN_MCP_BASE_IMAGE="debian:trixie-slim@sha256:d7e12182ce18b85b93007c1dedf31f2d29e01ccf3182cc4017c709b6259bc132" \
    KWIN_MCP_DEBIAN_SNAPSHOT="${DEBIAN_SNAPSHOT}" \
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
