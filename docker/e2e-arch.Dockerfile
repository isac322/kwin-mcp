# Arch Linux variant of the kwin-mcp end-to-end test image.
#
# It keeps the contract of docker/e2e.Dockerfile (same stages, venv path, user,
# entrypoint, artifact directory and test command) and swaps only the distro
# layer, so tests/e2e runs unchanged against Arch's KWin, libei, Qt and Mesa.
#
# Build from the repository root:
#   docker build -f docker/e2e-arch.Dockerfile -t kwin-mcp-e2e-arch .
# Run:
#   docker run --rm kwin-mcp-e2e-arch
#
# The image is linux/amd64 only. The official archlinux image is amd64-only,
# and no maintained Arch-family arm64 image tracks current packages: Manjaro
# ARM's arm-stable branch still ships KWin 5.27 and Python 3.11. On arm64 hosts
# build it with `--platform linux/amd64` under emulation.

ARG ARCH_ARCHIVE_DATE=2026/09/20

FROM ghcr.io/astral-sh/uv:0.10.8@sha256:88234bc9e09c2b2f6d176a3daf411419eb0370d450a08129257410de9cfafd2a AS uv-bin

FROM archlinux:base-20260920.0.596911@sha256:f3691b4dde62ba4c4b6f0ae2c1fbf28e8c0c8c4b9a35c7e06dc1f70e21aa29f6 AS arch-base

# Read every package from the Arch Linux Archive snapshot of one day, the
# pacman equivalent of the Debian image's snapshot.debian.org pin. `-Syyuu`
# moves the base packages to exactly that snapshot, downgrading if needed.
# The archive throttles bursts of parallel downloads until pacman's 10-second
# low-speed abort fires, so limit parallelism and disable that timeout, as the
# Debian image raises APT's retries and timeout for snapshot.debian.org.
# pacman's download sandbox restricts syscalls with seccomp, which fails with
# EINVAL under qemu-user emulation (for example amd64 builds on arm64 hosts);
# the filesystem part of the sandbox is already disabled by the base image.
ARG ARCH_ARCHIVE_DATE
RUN printf 'Server = https://archive.archlinux.org/repos/%s/$repo/os/$arch\n' \
        "${ARCH_ARCHIVE_DATE}" > /etc/pacman.d/mirrorlist \
    && sed -i \
        -e 's/^#DisableSandboxSyscalls/DisableSandboxSyscalls/' \
        -e 's/^ParallelDownloads.*/ParallelDownloads = 2/' \
        -e '/^\[options\]/a DisableDownloadTimeout' \
        /etc/pacman.conf \
    && pacman -Syyuu --noconfirm \
    && pacman -Scc --noconfirm

FROM arch-base AS wheel-builder

COPY --from=uv-bin /uv /usr/local/bin/uv
RUN pacman -S --noconfirm --needed python \
    && pacman -Scc --noconfirm

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv build --wheel --out-dir /build/dist

FROM arch-base AS runtime-base

# The recorder in docker/e2e-environment.py reports the installed version of
# every package named here, so this list is the single source for both.
# `kate` ships /usr/bin/kwrite on Arch; there is no separate kwrite package.
ENV KWIN_MCP_SYSTEM_PACKAGES="\
        at-spi2-core \
        breeze \
        ca-certificates \
        dbus \
        gtk3 \
        kate \
        kcalc \
        kwin \
        libcap \
        libei \
        libkscreen \
        mesa \
        mesa-utils \
        noto-fonts \
        noto-fonts-cjk \
        noto-fonts-emoji \
        python \
        python-dbus \
        python-gobject \
        python-numpy \
        python-pillow \
        qt6-declarative \
        qt6-wayland \
        scrot \
        spectacle \
        ttf-dejavu \
        wayland-utils \
        wl-clipboard \
        wtype \
        xdotool \
        xorg-server-xvfb \
        xorg-xauth \
        xorg-xdpyinfo \
        xorg-xwayland \
        xorg-xwininfo"

# kwin_wayland carries cap_sys_nice=ep for realtime scheduling. Docker's
# NoNewPrivileges makes the kernel refuse to exec it for the unprivileged test
# user, and the virtual llvmpipe backend does not need it, so strip it.
# Create the same UID/GID 1000 `tester` account as the Debian image.
RUN pacman -S --noconfirm --needed $KWIN_MCP_SYSTEM_PACKAGES \
    && pacman -Scc --noconfirm \
    && rm -rf /var/cache/pacman/pkg/* \
    && setcap -r /usr/bin/kwin_wayland \
    && test -z "$(getcap /usr/bin/kwin_wayland)" \
    && groupadd --gid 1000 tester \
    && useradd --create-home --uid 1000 --gid 1000 --shell /bin/sh tester \
    && install -d -m 1777 -o root -g root /tmp/.X11-unix \
    && install -d -m 0755 -o tester -g tester /app \
    && install -d -m 0755 -o tester -g tester /artifacts \
    && install -d -m 0700 -o tester -g tester /tmp/xdg-runtime

FROM runtime-base AS venv-builder

COPY --from=uv-bin /uv /usr/local/bin/uv
RUN pacman -S --noconfirm --needed \
        cairo \
        gcc \
        gobject-introspection \
        make \
        pkgconf \
    && pacman -Scc --noconfirm

COPY --from=wheel-builder /build/pyproject.toml /tmp/pyproject.toml
COPY --from=wheel-builder /build/dist/ /tmp/wheels/
RUN --mount=type=cache,target=/root/.cache/uv \
    python -m venv --system-site-packages /opt/kwin-mcp-venv \
    && uv pip install \
        --python /opt/kwin-mcp-venv/bin/python \
        /tmp/wheels/*.whl \
    && uv pip install \
        --python /opt/kwin-mcp-venv/bin/python \
        --group /tmp/pyproject.toml:dev

FROM runtime-base AS runtime
ARG ARCH_ARCHIVE_DATE

LABEL org.opencontainers.image.title="kwin-mcp E2E (Arch Linux)" \
      org.opencontainers.image.description="Reproducible virtual KWin environment for kwin-mcp end-to-end tests on Arch Linux" \
      org.opencontainers.image.source="https://github.com/isac322/kwin-mcp" \
      org.opencontainers.image.base.name="archlinux:base-20260920.0.596911" \
      org.opencontainers.image.base.digest="sha256:f3691b4dde62ba4c4b6f0ae2c1fbf28e8c0c8c4b9a35c7e06dc1f70e21aa29f6" \
      io.github.isac322.kwin-mcp.arch-archive-date="${ARCH_ARCHIVE_DATE}"

ENV PATH="/opt/kwin-mcp-venv/bin:${PATH}" \
    HOME=/home/tester \
    XDG_RUNTIME_DIR=/tmp/xdg-runtime \
    KWIN_MCP_ARTIFACT_DIR=/artifacts \
    KWIN_MCP_BASE_IMAGE="archlinux:base-20260920.0.596911@sha256:f3691b4dde62ba4c4b6f0ae2c1fbf28e8c0c8c4b9a35c7e06dc1f70e21aa29f6" \
    KWIN_MCP_BASE_SNAPSHOT="archlinux-archive:${ARCH_ARCHIVE_DATE}" \
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
