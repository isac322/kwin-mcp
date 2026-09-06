"""Tests for InputBackend text and key-combo delivery.

Covers two real-world delivery failures:

- KDE splits window-close across two bindings by ACCEL convention: Konsole
  binds Ctrl+Shift+Q (its ACCEL convention is Ctrl+Shift), while most other
  KDE apps (kwrite, kcalc) bind plain Ctrl+Q. Sending only one closed just
  whichever app bound it; the ctrl+q alias now sends both combos.
- ``keyboard_type_unicode`` spawned wtype/wl-copy with an environment built
  from ``os.environ`` only, so inside isolated virtual sessions wtype failed
  with "Wayland connection failed" (no session ``WAYLAND_DISPLAY``) and the
  clipboard fallback was unreachable (it also returned early instead of
  falling back). The paste sequence sends Ctrl+Shift+V before Ctrl+V
  (Konsole binds paste by its Ctrl+Shift ACCEL convention) and terminates
  with two Returns: the unbound Ctrl+V leaves a zsh quoted-insert (^V) state
  that silently consumes the first Return that follows it.
"""

from __future__ import annotations

import shutil
import subprocess
from unittest.mock import MagicMock

from kwin_mcp.input import InputBackend

_SESSION_ENV = {
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/fake-session-bus",
    "WAYLAND_DISPLAY": "wayland-mcp-test-42",
    "XDG_RUNTIME_DIR": "/tmp/fake-runtime",
    "HOME": "/tmp/fake-home",
}


def _backend() -> tuple[InputBackend, MagicMock]:
    """Build an InputBackend whose EIS client is a mock (no EIS connection)."""
    backend = InputBackend.__new__(InputBackend)
    client = MagicMock()
    backend._client = client
    return backend, client


def test_keyboard_key_plain_combo_keeps_keycode_path() -> None:
    """A regular combo is pressed via the keyboard device as before."""
    backend, client = _backend()
    backend.keyboard_key("ctrl+x")
    # ctrl (29) + x (45 = KEY_X) pressed and released via the keyboard device.
    codes = [c.args[0] for c in client.keyboard_key.call_args_list]
    assert codes == [29, 45, 45, 29]  # ctrl down, x down, x up, ctrl up


def test_keyboard_key_ctrl_q_sends_both_bindings() -> None:
    """Ctrl+Q sends BOTH bindings: plain Ctrl+Q (bound in most KDE apps —
    kwrite, kcalc — but unbound in Konsole) and Ctrl+Shift+Q (Konsole's
    close-window binding). Sending only one closed just whichever app bound
    it; on apps that bind the other combo it is an inert no-op shortcut."""
    backend, client = _backend()
    backend.keyboard_key("ctrl+q")
    codes = [c.args[0] for c in client.keyboard_key.call_args_list]
    # First combo: ctrl(29) q(16) down/up. Second combo: ctrl(29) shift(42)
    # q(16) down/up. No recursion, each combo sent exactly once.
    assert codes == [29, 16, 16, 29, 29, 42, 16, 16, 42, 29]


def test_keyboard_key_ctrl_q_alias_variants_send_both_bindings() -> None:
    """All ctrl+q alias spellings go through the dual-binding dispatch."""
    for spelling in ("control+q", "ctrl+quit"):
        backend, client = _backend()
        backend.keyboard_key(spelling)
        codes = [c.args[0] for c in client.keyboard_key.call_args_list]
        assert codes == [29, 16, 16, 29, 29, 42, 16, 16, 42, 29]


def test_keyboard_key_plain_ctrl_shift_q_is_not_duplicated() -> None:
    """An explicit ctrl+shift+q request must not trigger the alias dispatch:
    the keycode path sends exactly one ctrl+shift+q combo."""
    backend, client = _backend()
    backend.keyboard_key("ctrl+shift+q")
    codes = [c.args[0] for c in client.keyboard_key.call_args_list]
    assert codes == [29, 42, 16, 16, 42, 29]


def test_keyboard_key_unknown_key_is_noop() -> None:
    """A key with neither an evdev mapping nor a combo form is a silent no-op."""
    backend, client = _backend()
    backend.keyboard_key("f13")  # not in any mapping table
    client.keyboard_key.assert_not_called()


def test_wtype_receives_session_env(monkeypatch) -> None:
    """wtype is invoked with the caller-provided env, not the host environ."""
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0-host")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")

    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], env: dict[str, str], **kwargs: object) -> MagicMock:
        captured["cmd"] = cmd
        captured["env"] = env
        result = MagicMock()
        result.returncode = 0
        return result

    monkeypatch.setattr(shutil, "which", lambda name: name == "wtype")
    monkeypatch.setattr(subprocess, "run", fake_run)

    backend, _ = _backend()
    ok = backend.keyboard_type_unicode("привет", env=_SESSION_ENV)

    assert ok is True
    run_env = captured["env"]
    assert isinstance(run_env, dict)
    # Session-specific values must win over any host-inherited values.
    assert run_env["WAYLAND_DISPLAY"] == "wayland-mcp-test-42"
    assert run_env["XDG_RUNTIME_DIR"] == "/tmp/fake-runtime"
    assert run_env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/tmp/fake-session-bus"
    assert captured["cmd"] == ["wtype", "--", "привет"]


def test_wtype_failure_falls_back_to_clipboard(monkeypatch) -> None:
    """A wtype failure (e.g. missing virtual-keyboard protocol) is retried via wl-copy."""
    monkeypatch.setattr(shutil, "which", lambda name: name in ("wtype", "wl-copy"))

    def fake_run(cmd: list[str], env: dict[str, str], **kwargs: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 1  # wtype fails ("Wayland connection failed")
        return result

    captured: dict[str, object] = {}

    def fake_popen(cmd: list[str], env: dict[str, str], **kwargs: object) -> MagicMock:
        captured["cmd"] = cmd
        captured["env"] = env
        proc = MagicMock()
        proc.poll.return_value = None  # wl-copy is running
        return proc

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    backend, _ = _backend()
    backend.keyboard_key = MagicMock()  # type: ignore[method-assign]
    ok = backend.keyboard_type_unicode("héllo", env=_SESSION_ENV)

    assert ok is True
    assert captured["cmd"] == ["wl-copy", "--", "héllo"]
    assert captured["env"] == _SESSION_ENV  # fallback reuses the session env
    # Paste keys: Konsole binds paste to Ctrl+Shift+V, most apps Ctrl+V; the
    # trailing Returns commit the paste in shells (the second one satisfies
    # the ^V quoted-insert state the unbound Ctrl+V leaves in zsh).
    sent = [call.args[0] for call in backend.keyboard_key.call_args_list]
    assert sent == ["ctrl+shift+v", "ctrl+v", "return", "return"]


def test_no_tools_returns_false(monkeypatch) -> None:
    """Neither wtype nor wl-copy available → returns False, nothing spawned."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(subprocess, "run", MagicMock())
    monkeypatch.setattr(subprocess, "Popen", MagicMock())

    backend, _ = _backend()
    ok = backend.keyboard_type_unicode("test", env=_SESSION_ENV)

    assert ok is False
    subprocess.run.assert_not_called()  # type: ignore[attr-defined]
