# docker/manjaro.Dockerfile - Manjaro-based test image (multi-arch, Arch family).
# FROM line uses manjarolinux/base because the official archlinux:base is
# amd64-only on Docker Hub; Manjaro ships archlinux-keyring + manjaro-keyring,
# is pacman-based, and is multi-arch (linux/amd64 + linux/arm64). One Dockerfile
# therefore covers both architectures from the user-facing 'manjaro' slot.
FROM manjarolinux/base:20260322

LABEL org.opencontainers.image.source="https://github.com/isac322/kwin-mcp" \
      org.opencontainers.image.title="kwin-mcp minimal test environment" \
      org.opencontainers.image.description="Prebuilt Manjaro environment for kwin-mcp Docker smoke tests" \
      org.opencontainers.image.licenses="MIT"

ARG UID=1000
ARG GID=1000

RUN pacman-key --init \
 && pacman-key --populate archlinux manjaro \
 && pacman -Syu --noconfirm --needed \
# Package substitutions from T6 spec:
#   - dbus-python-common (Arch package name) -> python-dbus (Manjaro equivalent)
#     [reason: dbus-python-common is not available in Manjaro 20260322 x86_64 repos]
#   - dbus, qt6-declarative kept explicit for safety even though transitive deps
#   - See docker/runtime-contract.md "Package substitutions" section
 && pacman -S --noconfirm --needed \
       kwin spectacle at-spi2-core python-gobject python-dbus dbus mesa wl-clipboard wtype wayland-utils python uv qt6-declarative gcc pkgconf \
 && pacman -Scc --noconfirm \
 && rm -rf /var/cache/pacman/pkg/* /var/lib/pacman/sync/*.db

# kwin_wayland ships with `cap_sys_nice=ep` file capability for realtime
# scheduling. Container runtimes apply NoNewPrivileges by default for non-root
# users, which causes the kernel to refuse exec ("Operation not permitted").
# Virtual mode (--virtual, software rendering) does not need elevated caps,
# so strip them at build time. /usr/bin/kwin_wayland and /usr/sbin/kwin_wayland
# are hardlinks to the same inode; one setcap -r covers both.
RUN setcap -r /usr/bin/kwin_wayland \
 && (getcap /usr/bin/kwin_wayland | tee /tmp/getcap.out; ! grep -q '=' /tmp/getcap.out)

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

RUN existing_group=$(getent group "${GID}" | cut -d: -f1 || true) \
 && if [ -n "$existing_group" ] && [ "$existing_group" != kwinmcp ]; then groupmod -n kwinmcp "$existing_group"; fi \
 && if ! getent group "${GID}" >/dev/null; then groupadd -g "${GID}" kwinmcp; fi \
 && existing_user=$(getent passwd "${UID}" | cut -d: -f1 || true) \
 && if [ -n "$existing_user" ] && [ "$existing_user" != kwinmcp ]; then usermod -l kwinmcp -d /home/kwinmcp -m -s /bin/bash "$existing_user"; fi \
 && if ! getent passwd "${UID}" >/dev/null; then useradd -m -u "${UID}" -g "${GID}" -s /bin/bash kwinmcp; fi

RUN mkdir -p "/run/user/${UID}" \
 && chown "${UID}:${GID}" "/run/user/${UID}" \
 && chmod 0700 "/run/user/${UID}"

ENV XDG_RUNTIME_DIR=/run/user/${UID}

RUN install -d -o "${UID}" -g "${GID}" /opt/kwinmcp-venv \
 && su kwinmcp -c "uv venv --system-site-packages /opt/kwinmcp-venv"

ENV PATH=/opt/kwinmcp-venv/bin:$PATH \
    PYTHONUNBUFFERED=1

RUN install -d -o "${UID}" -g "${GID}" /opt/docker /wheels /evidence

COPY --chown=${UID}:${GID} entrypoint.sh /opt/docker/entrypoint.sh
RUN chmod +x /opt/docker/entrypoint.sh

WORKDIR /home/kwinmcp
USER kwinmcp

ENTRYPOINT ["/opt/docker/entrypoint.sh"]
