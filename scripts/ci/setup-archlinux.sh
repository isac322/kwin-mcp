#!/usr/bin/env bash
# Install kwin-mcp integration test dependencies on Arch Linux.
#
# Consumed by both the CI matrix (.github/workflows/integration.yml) and the
# local reproduction helper (scripts/run-integration-local.sh).
set -euo pipefail

pacman -Syu --noconfirm --needed

# KWin + session bootstrap
pacman -S --noconfirm --needed \
    kwin \
    xdg-desktop-portal-kde \
    dbus \
    glib2 \
    at-spi2-core \
    wayland \
    wayland-utils \
    mesa \
    libglvnd \
    libcap \
    libei \
    xorg-xwayland

# Clipboard / input helpers
pacman -S --noconfirm --needed \
    wl-clipboard \
    wtype \
    spectacle

# End-to-end GUI targets (kcalc for arithmetic, kate for keyboard input)
pacman -S --noconfirm --needed kcalc kate

# Container runtimes can reject binaries with file capabilities.
setcap -r "$(command -v kwin_wayland)" 2>/dev/null || true

# PyGObject / dbus-python build dependencies
pacman -S --noconfirm --needed \
    python \
    python-gobject \
    python-dbus \
    cairo \
    gobject-introspection \
    pkgconf \
    gcc \
    gdb \
    git

# uv (Python package manager)
if ! command -v uv >/dev/null 2>&1; then
    pacman -S --noconfirm --needed uv
fi
