#!/usr/bin/env bash
# Install kwin-mcp integration test dependencies on Ubuntu (24.04+).
#
# This uses the stock Ubuntu archive. For a Kubuntu-like environment with
# bleeding-edge KDE, layer the kubuntu-ppa/backports PPA before running this
# script.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update

# KWin + session bootstrap
apt-get install -y --no-install-recommends \
    kwin-wayland \
    xdg-desktop-portal-kde \
    xwayland \
    dbus-daemon \
    libglib2.0-bin \
    at-spi2-core \
    libwayland-client0 \
    wayland-utils \
    libgl1-mesa-dri \
    libglvnd0 \
    libcap2-bin \
    libei1 \
    libeis1

# Clipboard / input helpers
apt-get install -y --no-install-recommends \
    wl-clipboard \
    wtype \
    kde-spectacle

# End-to-end GUI targets (kcalc for arithmetic, kate for keyboard input)
apt-get install -y --no-install-recommends kcalc kate

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
apt-get install -y --no-install-recommends \
    python3 \
    python3-dev \
    python3-pip \
    python3-gi \
    gir1.2-atspi-2.0 \
    python3-dbus \
    libcairo2-dev \
    libgirepository-2.0-dev \
    libdbus-1-dev \
    pkg-config \
    gcc \
    gdb \
    git \
    ca-certificates \
    curl

# uv (Python package manager)
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
