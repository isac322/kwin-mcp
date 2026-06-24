#!/usr/bin/env bash
# Install kwin-mcp integration test dependencies on Fedora.
set -euo pipefail

dnf -y update

# KWin + session bootstrap
dnf -y install \
    kwin-wayland \
    xdg-desktop-portal-kde \
    xorg-x11-server-Xwayland \
    dbus-daemon \
    glib2 \
    at-spi2-core \
    wayland-devel \
    wayland-utils \
    mesa-dri-drivers \
    mesa-libGL \
    libcap \
    libei

# Clipboard / input helpers
dnf -y install \
    wl-clipboard \
    wtype \
    spectacle

# End-to-end GUI targets (kcalc for arithmetic, kate for keyboard input)
dnf -y install kcalc kate

# Container runtimes can reject binaries with file capabilities.
setcap -r "$(command -v kwin_wayland)" 2>/dev/null || true

# Disable xdg-desktop-portal D-Bus auto-activation. KWin auto-activates the
# portal on startup, but the portal backend cannot reach a working compositor
# in headless containers and crashes; the activation cascade then segfaults
# KWin while it waits for the portal reply. Renaming the .service files
# prevents activation entirely; KWin runs fine without the portal in our
# test scope (no screencast / sandboxed file-chooser usage).
for service in \
    org.freedesktop.portal.Desktop \
    org.freedesktop.portal.Documents \
    org.freedesktop.impl.portal.desktop.kde \
    org.freedesktop.impl.portal.desktop.gtk; do
    file="/usr/share/dbus-1/services/${service}.service"
    if [ -e "$file" ]; then
        mv "$file" "${file}.disabled"
    fi
done

# PyGObject / dbus-python build dependencies
dnf -y install \
    python3 \
    python3-devel \
    python3-pip \
    python3-gobject \
    python3-dbus \
    dbus-devel \
    cairo-devel \
    cairo-gobject-devel \
    gobject-introspection-devel \
    pkgconf \
    gcc \
    gdb \
    git

# uv (Python package manager)
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
