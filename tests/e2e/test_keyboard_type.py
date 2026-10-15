"""End-to-end contracts for keyboard_type: the keys method and the unicode method.

Oracles: the interaction probe's raw event log (PEL) for every key GTK receives;
the probe's ``Keyboard Target`` entry and KWrite's document read through the
independent AT-SPI oracle; the session selection through the test's own
``wl-copy``/``wl-paste``.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from atspi_oracle import AtspiOracle
from probe_session import (
    KEY_KINDS,
    PROBE_APP,
    Probe,
    assert_matches_golden,
    load_golden,
    of_kind,
    start_probe_session,
)
from window_zoo import APP_ID as ZOO_APP_ID
from window_zoo import MAIN_CAPTION, ZooProcess

from kwin_mcp.core import NO_CLIPBOARD_TEXT
from kwin_mcp.input import InputBackend
from kwin_mcp.models import ClipboardText, HeldInput, KeyboardTypeResult, WindowSelector

if TYPE_CHECKING:
    from collections.abc import Callable

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import (
        AppLaunchResult,
        ElementList,
        HomeMode,
        SessionInfo,
        TypeMethod,
    )

E2E_DIR = Path(__file__).resolve().parent
WAIT_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.1
SENTINEL = "SENTINEL-keyboard-type"
HINT_MIME = "x-kde-passwordManagerHint"
MAX_COPY_BYTES = 1024 * 1024  # the 1 MiB unicode cap (F98)


# ── oracles ───────────────────────────────────────────────────────────────


def _wait_until(predicate: Callable[[], bool], description: str) -> None:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out waiting for {description}")
        time.sleep(POLL_INTERVAL_SECONDS)


def _entry_text(probe: Probe) -> str:
    """The probe's Keyboard Target text, read by the test's own AT-SPI walk."""
    nodes = AtspiOracle(probe.dbus_address).find(app=PROBE_APP, name="Keyboard Target")
    assert len(nodes) == 1, nodes
    return nodes[0].text or ""


def _wait_for_entry(probe: Probe, expected: str) -> None:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    actual = _entry_text(probe)
    while actual != expected and time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        actual = _entry_text(probe)
    assert actual == expected


def _focused_kwrite_text(atspi: AtspiOracle) -> str:
    nodes = [
        n
        for n in atspi.find(app="kwrite", role="text")
        if "focused" in n.states and n.error is None
    ]
    assert len(nodes) == 1, [(n.role, n.name, sorted(n.states)) for n in nodes]
    return nodes[0].text or ""


def _socket(engine: AutomationEngine) -> str:
    info = engine.session_info().session
    assert info is not None
    return info.wayland_socket


def _env(socket: str) -> dict[str, str]:
    env = {**os.environ, "WAYLAND_DISPLAY": socket}
    env.pop("DISPLAY", None)
    return env


def _wl_paste(socket: str, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["wl-paste", *args], env=_env(socket), capture_output=True, timeout=30, check=False
    )


def _selection(socket: str) -> bytes:
    return _wl_paste(socket, "--no-newline").stdout


def _offered_types(socket: str) -> set[str]:
    result = _wl_paste(socket, "--list-types")
    return set(result.stdout.decode().split()) if result.returncode == 0 else set()


def _seed(socket: str, text: str) -> None:
    """Own the selection with a test-side wl-copy daemon."""
    subprocess.run(
        ["wl-copy", "--", text],
        env=_env(socket),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=WAIT_TIMEOUT_SECONDS,
        check=True,
    )
    _wait_until(lambda: _selection(socket) == text.encode(), "the seeded selection")


def _type(
    probe: Probe, text: str, method: TypeMethod = "keys"
) -> tuple[KeyboardTypeResult, list[dict[str, object]]]:
    mark = probe.mark()
    result = probe.engine.keyboard_type(text, method=method)
    return result, of_kind(probe.settle(since=mark), *KEY_KINDS)


def _assert_rejected(probe: Probe, text: str, method: TypeMethod, message: str) -> None:
    mark = probe.mark()
    with pytest.raises(ValueError) as excinfo:
        probe.engine.keyboard_type(text, method=method)
    assert str(excinfo.value) == message
    assert probe.settle(since=mark) == [], (text, method)


def _assert_type_failed(probe: Probe, text: str, echo: str) -> list[dict[str, object]]:
    mark = probe.mark()
    with pytest.raises(RuntimeError) as excinfo:
        probe.engine.keyboard_type(text, method="unicode")
    assert str(excinfo.value) == f"Failed to type unicode: {echo}"
    return probe.settle(since=mark)


# ── keys method ───────────────────────────────────────────────────────────


@pytest.mark.qa("QA-INP-055")
@pytest.mark.parametrize(
    ("host_layout", "home"),
    [(None, "host"), ("ru,us", "host"), ("ru,us", "isolated")],
    ids=["host-default", "host-ru-us", "host-ru-us-isolated-home"],
)
def test_keys_method_default_ignores_host_layout(
    engine: AutomationEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    host_layout: str | None,
    home: HomeMode,
) -> None:
    # keyboard_type sends evdev keycodes from a US QWERTY table, and KWin maps
    # them through its own keymap. A host whose first layout is not US, set in
    # kxkbrc or in XKB_DEFAULT_LAYOUT, must not turn "hello" into "руддщ".
    if host_layout is not None:
        host_config = tmp_path / "host-config"
        host_config.mkdir()
        (host_config / "kxkbrc").write_text(f"[Layout]\nLayoutList={host_layout}\nUse=true\n")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(host_config))
        monkeypatch.setenv("XKB_DEFAULT_LAYOUT", host_layout)
    probe = start_probe_session(engine, tmp_path, home=home)

    hello, _ = _type(probe, "hello")
    assert hello.route == "keys"
    _wait_for_entry(probe, "hello")
    # Idle like the golden capture (1.2 s between runs) before the timed call.
    probe.park()

    result, events = _type(probe, "Ab\tc\n")
    assert result == KeyboardTypeResult(
        method="keys", route="keys", chars=5, echo="'Ab\\tc\\n'", held=HeldInput()
    )
    assert result.render() == "Typed: 'Ab\\tc\\n'\nRoute: keys; 5 characters"
    # Shift wraps A, ~10 ms press, ~20 ms between characters, Tab and Return
    # are real keys: the 0.11 keyboard_type("Ab\tc\n") timeline (A15 bounds).
    assert_matches_golden(events, load_golden("type_text"))


@pytest.mark.qa("QA-INP-056")
def test_non_qwerty_rejected_before_send_with_unicode_remedy(
    engine: AutomationEngine, tmp_path: Path
) -> None:
    probe = start_probe_session(engine, tmp_path)
    socket = _socket(engine)
    _seed(socket, SENTINEL)

    remedy = 'Retry with method="unicode".'
    for text, chars in (
        ("héllo", "'é'"),
        ("a\rb", "'\\r'"),
        ("abc\rdef", "'\\r'"),
        ("ab€", "'€'"),
        ("x日本€y日", "'日', '本', '€'"),
    ):
        _assert_rejected(probe, text, "keys", f"keyboard_type cannot type {chars}.\n{remedy}")

    assert _entry_text(probe) == ""
    assert _selection(socket) == SENTINEL.encode()


@pytest.mark.qa("QA-INP-057")
def test_empty_text_no_keystrokes_success(engine: AutomationEngine, tmp_path: Path) -> None:
    probe = start_probe_session(engine, tmp_path)
    socket = _socket(engine)
    _seed(socket, SENTINEL)

    for method in ("keys", "unicode"):
        result, events = _type(probe, "", method)
        assert result.chars == 0
        assert result.echo == "''"
        assert events == [], (method, events)

    assert _entry_text(probe) == ""
    assert _selection(socket) == SENTINEL.encode()


# ── unicode method ────────────────────────────────────────────────────────


@pytest.mark.qa("QA-INP-059")
def test_unicode_wtype_or_paste_route_reported(
    engine: AutomationEngine,
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Without enable_clipboard: the unicode method has its own private helper.
    probe = start_probe_session(engine, tmp_path)

    korean = "안녕하세요"
    result, events = _type(probe, korean, "unicode")
    # KWin has no virtual-keyboard protocol, so wtype fails and the GTK entry pastes.
    assert result.route == "paste:ctrl+v", result.render()
    assert result.chars == 5  # code points
    assert result.render() == f"Typed unicode: {korean!r}\nRoute: paste:ctrl+v; 5 characters"
    assert [(e["kind"], e["key"]) for e in events] == [
        ("key_press", "Control_L"),
        ("key_press", "v"),
        ("key_release", "v"),
        ("key_release", "Control_L"),
    ]
    _wait_for_entry(probe, korean)

    keys = engine.keyboard_type("abc")
    assert (keys.route, keys.chars) == ("keys", 3)
    _wait_for_entry(probe, korean + "abc")

    # The gate is per tool: the unicode method never enabled clipboard_read.
    with pytest.raises(RuntimeError) as excinfo:
        engine.clipboard_read()
    assert str(excinfo.value) == NO_CLIPBOARD_TEXT

    # A wtype that exits 0 is the reported route even though it typed nothing.
    fake_bin = tmp_path / "fake-wtype"
    fake_bin.mkdir()
    (fake_bin / "wtype").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "wtype").chmod(0o755)
    with monkeypatch.context() as patched:
        patched.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
        result, events = _type(probe, "é", "unicode")
    assert result.route == "wtype", result.render()
    assert events == []
    assert _entry_text(probe) == korean + "abc"

    # A terminal is pasted into with Ctrl+Shift+V.
    launch("konsole --separate -e cat")
    wait_for_app("konsole")
    engine.window_focus(WindowSelector(app_id="org.kde.konsole"))
    terminal = engine.keyboard_type(korean, method="unicode")
    assert terminal.route == "paste:ctrl+shift+v", terminal.render()


@pytest.mark.qa("QA-INP-060")
def test_unicode_restores_clipboard_all_formats(engine: AutomationEngine, tmp_path: Path) -> None:
    probe = start_probe_session(engine, tmp_path, enable_clipboard=True)
    socket = _socket(engine)
    offers = {"text/plain": b"keep", "text/html": b"<b>keep</b>"}
    args = [part for mime, data in offers.items() for part in (mime, data.hex())]
    # A real data-control client owns a two-format selection (see that file).
    owner = subprocess.Popen(
        [
            sys.executable,
            str(E2E_DIR / "test_clipboard_helper_protocol.py"),
            "--held-source",
            *args,
        ],
        env=_env(socket),
        cwd=E2E_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert owner.stdout is not None
        assert owner.stdout.readline() == b"OWNED\n"
        prior = {mime: _wl_paste(socket, "--no-newline", "--type", mime).stdout for mime in offers}
        assert prior == offers
        assert _offered_types(socket) == set(offers)

        result, _ = _type(probe, "é", "unicode")
        assert result.route == "paste:ctrl+v", result.render()
        _wait_for_entry(probe, "é")

        assert _offered_types(socket) == set(offers)
        for mime, data in offers.items():
            assert _wl_paste(socket, "--no-newline", "--type", mime).stdout == data, mime
        assert engine.clipboard_read() == ClipboardText(text="keep")
    finally:
        owner.kill()
        owner.wait(timeout=WAIT_TIMEOUT_SECONDS)


@pytest.mark.qa("QA-INP-062")
def test_nul_and_1mib(engine: AutomationEngine, tmp_path: Path) -> None:
    probe = start_probe_session(engine, tmp_path)
    socket = _socket(engine)
    _seed(socket, SENTINEL)

    remedy = 'Retry with method="unicode".'
    _assert_rejected(probe, "a\x00b", "keys", f"keyboard_type cannot type '\\x00'.\n{remedy}")
    _assert_rejected(probe, "a\x00b", "unicode", "text must not contain NUL characters")

    # Over the 1 MiB cap: refused before any event, selection untouched.
    for text in ("é" * (1 << 20), "가" * 350_000):
        events = _assert_type_failed(probe, text, f"<{len(text)} characters>")
        assert events == []
        assert _selection(socket) == SENTINEL.encode()

    # Exactly the cap succeeds.
    boundary = "x" * MAX_COPY_BYTES
    result, events = _type(probe, boundary, "unicode")
    assert result.route == "paste:ctrl+v", result.render()
    assert (result.chars, result.echo) == (MAX_COPY_BYTES, f"<{MAX_COPY_BYTES} characters>")
    assert [e["key"] for e in events] == ["Control_L", "v", "v", "Control_L"]
    _wait_until(lambda: _entry_text(probe).startswith("x" * 1000), "the pasted text")
    _wait_until(lambda: _selection(socket) == SENTINEL.encode(), "the restored selection")


@pytest.mark.qa("QA-INP-063")
def test_unicode_nothing_requested_text(
    engine: AutomationEngine, start_session: Callable[..., SessionInfo]
) -> None:
    # The window zoo's main window holds buttons only: no text input requests a
    # paste. (KCalc does not qualify: its main window pastes numbers on Ctrl+V.)
    info = start_session()
    socket = info.wayland_socket
    with ZooProcess(wayland_display=socket, dbus_address=info.dbus_address) as zoo:
        zoo.expect(f"window: {MAIN_CAPTION}")
        engine.window_focus(WindowSelector(app_id=ZOO_APP_ID, caption=MAIN_CAPTION))
        _seed(socket, SENTINEL)

        started = time.monotonic()
        with pytest.raises(RuntimeError) as excinfo:
            engine.keyboard_type("é", method="unicode")
        elapsed = time.monotonic() - started

    assert str(excinfo.value) == "Failed to type unicode: 'é'"
    # The paste chord went out; the failure is reported after the 4 s transfer bound.
    assert elapsed >= 4.0 * 0.9, elapsed
    _wait_until(lambda: _selection(socket) == SENTINEL.encode(), "the restored selection")


@pytest.mark.qa("QA-INP-064")
@pytest.mark.slow
@pytest.mark.timeout(600)
def test_unicode_password_hint_mime(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # KWrite, not the GTK probe: the paste target is frozen while the watcher
    # looks, and GTK3 reads a new selection's text as soon as it resumes, which
    # the helper would count as the paste (some client read the text while armed).
    info = start_session()
    socket = info.wayland_socket
    kwrite = launch(f"kwrite {shlex.quote(str(tmp_path / 'hint.txt'))}")
    wait_for_app("kwrite")
    engine.window_focus(WindowSelector(app_id="org.kde.kwrite"))
    atspi = AtspiOracle(info.dbus_address)
    _seed(socket, SENTINEL)
    seen: dict[str, object] = {}

    def watch() -> bool:
        types = _offered_types(socket)
        if HINT_MIME not in types:
            return False
        seen["types"] = types
        seen["hint"] = _wl_paste(socket, "--no-newline", "--type", HINT_MIME).stdout
        return True

    # The paste chord is sent in this process; record when it has gone out, so
    # KWrite resumes only once the helper is armed and waits for its paste.
    chord_sent = threading.Event()
    send_key = InputBackend.keyboard_key

    def keyboard_key(backend: InputBackend, key: str) -> None:
        send_key(backend, key)
        chord_sent.set()

    monkeypatch.setattr(InputBackend, "keyboard_key", keyboard_key)

    os.kill(kwrite.pid, signal.SIGSTOP)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            call = pool.submit(engine.keyboard_type, "pässwörd", "unicode")
            _wait_until(watch, "the transient offer")
            assert chord_sent.wait(WAIT_TIMEOUT_SECONDS), "the paste chord was never sent"
            assert not call.done()
            os.kill(kwrite.pid, signal.SIGCONT)
            result = call.result(timeout=60)
    finally:
        os.kill(kwrite.pid, signal.SIGCONT)

    assert seen["hint"] == b"secret"
    assert seen["types"] == {"text/plain;charset=utf-8", "text/plain", HINT_MIME}
    assert result.route == "paste:ctrl+v", result.render()
    _wait_until(lambda: _focused_kwrite_text(atspi) == "pässwörd", "the pasted text")
    _wait_until(lambda: HINT_MIME not in _offered_types(socket), "the hint to go away")
    assert _selection(socket) == SENTINEL.encode()


# ── composition and KWrite ────────────────────────────────────────────────


@pytest.mark.qa("QA-INP-066")
def test_clipboard_write_then_ctrl_v(engine: AutomationEngine, tmp_path: Path) -> None:
    probe = start_probe_session(engine, tmp_path, enable_clipboard=True)

    engine.clipboard_write("x y")
    engine.keyboard_press("ctrl+v")

    _wait_for_entry(probe, "x y")


def test_keyboard_type_method_unicode_reaches_kwrite(
    engine: AutomationEngine,
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
    tmp_path: Path,
) -> None:
    info = start_session(enable_clipboard=True)
    assert info.input_backend == "eis", info
    launch(f"kwrite {shlex.quote(str(tmp_path / 'kwin-mcp-unicode.txt'))}")
    wait_for_app("kwrite")
    engine.window_focus(WindowSelector(app_id="org.kde.kwrite"))
    atspi = AtspiOracle(info.dbus_address)

    result = engine.keyboard_type("한글", method="unicode")
    assert result.render() == "Typed unicode: '한글'\nRoute: paste:ctrl+v; 2 characters"
    _wait_until(lambda: _focused_kwrite_text(atspi) == "한글", "document text '한글'")

    engine.keyboard_press("ctrl+a")
    engine.keyboard_press("ctrl+c")
    _wait_until(
        lambda: engine.clipboard_read() == ClipboardText(text="한글"), "the copied selection"
    )
