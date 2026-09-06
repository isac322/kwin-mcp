"""Tests for per-session XDG_CONFIG_HOME isolation and XKB default stripping.

``kwin_wayland --virtual`` inherits the host's XDG config directory: KWin
compiles the host's ``kxkbrc`` layout list (e.g. ``LayoutList=ru,us``) into
its keymap — or falls back to the ``XKB_DEFAULT_*`` environment variables —
so evdev keycodes injected through the EIS keyboard produce the host layout's
characters (``hello`` → ``руддщ``) instead of ASCII. The kwallet popup
(ksecretd/kwalletd) also steals compositor focus at session start.

``Session`` now creates a throwaway config dir pre-seeded with ``kwalletrc``
(wallet disabled), points ``XDG_CONFIG_HOME`` at it, and strips the host
``XKB_DEFAULT_*`` variables so libxkbcommon resets to the plain ``us`` layout.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kwin_mcp.session import Session, SessionConfig, _write_deterministic_session_config

if TYPE_CHECKING:
    from pathlib import Path

_XKB_VARS = (
    "XKB_DEFAULT_LAYOUT",
    "XKB_DEFAULT_VARIANT",
    "XKB_DEFAULT_OPTIONS",
    "XKB_DEFAULT_RULES",
    "XKB_DEFAULT_MODEL",
)


def test_deterministic_config_disables_kwallet(tmp_path: Path) -> None:
    """The pre-seeded kwalletrc disables the wallet manager popup."""
    config_dir = tmp_path / "config"
    _write_deterministic_session_config(config_dir)
    content = (config_dir / "kwalletrc").read_text()
    assert "[Wallet]" in content
    assert "Enabled=false" in content


def test_deterministic_config_does_not_overwrite_existing(tmp_path: Path) -> None:
    """Explicit pre-seeding wins: an existing kwalletrc is left untouched."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "kwalletrc").write_text("[Wallet]\nEnabled=true\n")

    _write_deterministic_session_config(config_dir)

    assert (config_dir / "kwalletrc").read_text() == "[Wallet]\nEnabled=true\n"


def test_build_env_points_config_home_at_session_dir(tmp_path: Path, monkeypatch) -> None:
    """_build_env overrides XDG_CONFIG_HOME with the per-session config dir."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "/host/config")

    session = Session()
    session._session_config_dir = tmp_path
    env = session._build_env(SessionConfig())

    assert env["XDG_CONFIG_HOME"] == str(tmp_path)


def test_build_env_strips_host_xkb_defaults(tmp_path: Path, monkeypatch) -> None:
    """Host XKB_DEFAULT_* variables must not reach the compositor."""
    for var in _XKB_VARS:
        monkeypatch.setenv(var, "ru,us")

    session = Session()
    session._session_config_dir = tmp_path
    env = session._build_env(SessionConfig())

    for var in _XKB_VARS:
        assert var not in env


def test_build_env_without_session_dir_keeps_host_config(monkeypatch) -> None:
    """Without a session config dir (e.g. before start()) nothing is injected."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "/host/config")

    session = Session()
    env = session._build_env(SessionConfig())

    assert env["XDG_CONFIG_HOME"] == "/host/config"
