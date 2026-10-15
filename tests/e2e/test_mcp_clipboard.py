"""Installed-package MCP stdio coverage for clipboard_read, clipboard_write and unicode paste.

Every call goes through the installed ``kwin-mcp`` over stdio. Results are
checked against the selection as the test's own ``wl-paste``/``wl-copy`` see it
on the session socket, and KWrite's document text through the independent
AT-SPI oracle, never only through the tool's own report.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from atspi_oracle import AtspiOracle
from mcp_harness import McpTestClient, running_mcp_server, text_of

from kwin_mcp.core import NO_CLIPBOARD_TEXT

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping
    from pathlib import Path

SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800
POLL_INTERVAL_SECONDS = 0.1
INPUT_TIMEOUT_SECONDS = 5.0
APP_WAIT_TIMEOUT_MS = 15000
UNICODE_MARKER = "비밀-45-한글"
PRIOR_CLIPBOARD = "prior-45-clipboard"
# Longer than the old 100 ms readiness guess, shorter than every product bound
# (the helper's 3 s snapshot deadline and clipboard_write's 5 s readiness wait).
SLOW_BOUNDARY_SECONDS = 1.5
WL_COPY = shutil.which("wl-copy")
WL_PASTE = shutil.which("wl-paste")

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def _session(
    *,
    enable_clipboard: bool = True,
    server_env: Mapping[str, str] | None = None,
) -> AsyncGenerator[tuple[McpTestClient, dict[str, Any]]]:
    """A virtual session over stdio; yields the client and the structured SessionInfo."""
    async with running_mcp_server(env=server_env) as client:
        try:
            info, _text = await client.call_structured(
                "session_start",
                {
                    "screen_width": SCREEN_WIDTH,
                    "screen_height": SCREEN_HEIGHT,
                    "enable_clipboard": enable_clipboard,
                    "home": "isolated",
                },
            )
            assert info["input_backend"] == "eis", info
            assert info["clipboard"] is enable_clipboard, info
            yield client, info
        finally:
            await client.call_result("session_stop")


def _paste_env(socket: str) -> dict[str, str]:
    env = {**os.environ, "WAYLAND_DISPLAY": socket}
    env.pop("DISPLAY", None)
    return env


def _test_paste(socket: str) -> subprocess.CompletedProcess[bytes]:
    assert WL_PASTE is not None
    return subprocess.run(
        [WL_PASTE, "--no-newline"],
        env=_paste_env(socket),
        capture_output=True,
        timeout=5,
        check=False,
    )


@asynccontextmanager
async def _prior_clipboard_owner(socket: str, text: str) -> AsyncGenerator[subprocess.Popen[bytes]]:
    """Own the isolated selection with a real foreground wl-copy the test controls."""
    assert WL_COPY is not None and WL_PASTE is not None
    owner = subprocess.Popen(
        [WL_COPY, "--foreground", "--", text],
        env=_paste_env(socket),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + INPUT_TIMEOUT_SECONDS
        while True:
            pasted = _test_paste(socket)
            if pasted.returncode == 0 and pasted.stdout == text.encode():
                break
            assert time.monotonic() < deadline
            await anyio.sleep(POLL_INTERVAL_SECONDS)
        yield owner
    finally:
        if owner.poll() is None:
            owner.send_signal(signal.SIGCONT)
            owner.terminate()
            owner.wait(timeout=5)


def _wl_copy_shim_env(tmp_path: Path, body: str) -> dict[str, str]:
    """Put a wl-copy wrapper first on the server's PATH."""
    shim_dir = tmp_path / "wl-copy-shim"
    shim_dir.mkdir()
    shim = shim_dir / "wl-copy"
    shim.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    shim.chmod(0o755)
    return {"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"}


def _focused_text(atspi: AtspiOracle) -> str:
    """Text of KWrite's focused document, read by the test's own AT-SPI walk."""
    nodes = [
        n
        for n in atspi.find(app="kwrite", role="text")
        if "focused" in n.states and n.error is None
    ]
    assert len(nodes) == 1, [(n.role, n.name, sorted(n.states)) for n in nodes]
    return nodes[0].text or ""


async def _wait_for_text(atspi: AtspiOracle, expected: str) -> None:
    deadline = time.monotonic() + INPUT_TIMEOUT_SECONDS
    actual = ""
    while time.monotonic() < deadline:
        actual = await anyio.to_thread.run_sync(_focused_text, atspi)
        if actual == expected:
            return
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    assert actual == expected


async def _open_kwrite(
    client: McpTestClient, info: dict[str, Any], tmp_path: Path, name: str
) -> AtspiOracle:
    """Launch KWrite on a fresh document, focus it, and return the AT-SPI oracle."""
    document = tmp_path / f"kwin-mcp-{name}.txt"
    await client.call_structured("app_launch", {"command": f"kwrite {shlex.quote(str(document))}"})
    found, _ = await client.call_structured(
        "element_query",
        {"locator": {"app": "kwrite"}, "wait": {"timeout_ms": APP_WAIT_TIMEOUT_MS}},
    )
    assert found["wait"]["satisfied"] is True and found["elements"], found["wait"]
    await client.call_structured("window_focus", {"window": {"app_id": "org.kde.kwrite"}})
    atspi = AtspiOracle(info["dbus_address"])
    await _wait_for_text(atspi, "")
    return atspi


async def _tool_error(
    client: McpTestClient, name: str, arguments: dict[str, Any] | None = None
) -> str:
    """The engine message of a tool error, with the SDK's ``Error executing tool`` prefix."""
    lines = await client.call_tool_error(name, arguments, lines=None)
    text = "\n".join(lines)
    prefix = f"Error executing tool {name}: "
    assert text.startswith(prefix), text
    return text.removeprefix(prefix)


async def _clipboard_text(client: McpTestClient) -> str:
    structured, _ = await client.call_structured("clipboard_read")
    return structured["text"]


async def test_clipboard_disabled_guards_return_without_hanging() -> None:
    async with _session(enable_clipboard=False) as (client, _info):
        for name, arguments in (("clipboard_read", None), ("clipboard_write", {"text": "x"})):
            with anyio.fail_after(2):
                message = await _tool_error(client, name, arguments)
            assert message == NO_CLIPBOARD_TEXT


async def test_clipboard_roundtrip_and_paste_cross_stdio(tmp_path: Path) -> None:
    async with _session() as (client, info):
        atspi = await _open_kwrite(client, info, tmp_path, "mcp-clipboard")
        payload = "Clipboard MCP QA\n한글\t42!"

        written, text = await client.call_structured("clipboard_write", {"text": payload})
        assert text == f"Clipboard set: {payload!r}"
        assert written == {"length": len(payload), "echo": repr(payload)}
        assert _test_paste(info["wayland_socket"]).stdout == payload.encode()
        assert await _clipboard_text(client) == payload
        await client.call_structured("keyboard_press", {"keys": "ctrl+v"})
        await _wait_for_text(atspi, payload)


@pytest.mark.qa("QA-CLIP-003")
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("".join(chr(0x41 + index % 26) for index in range(256 * 1024)), id="256KiB"),
        pytest.param("a\x00b", id="nul"),
    ],
)
async def test_nul_and_large_over_stdio(payload: str) -> None:
    # argv cannot carry either payload (E2BIG / embedded null byte); stdin can.
    async with _session() as (client, info):
        written, _ = await client.call_structured("clipboard_write", {"text": payload})
        assert written["length"] == len(payload)
        assert _test_paste(info["wayland_socket"]).stdout == payload.encode()
        assert await _clipboard_text(client) == payload


async def test_clipboard_write_echoes_long_text_as_its_length() -> None:
    at_limit = "b" * 200
    long_payload = "L" * 150 + "-long-payload-" + "M" * 150
    async with _session() as (client, _info):
        _, text = await client.call_structured("clipboard_write", {"text": at_limit})
        assert text == f"Clipboard set: {at_limit!r}"
        written, text = await client.call_structured("clipboard_write", {"text": long_payload})
        assert text == f"Clipboard set: <{len(long_payload)} characters>"
        assert written == {"length": len(long_payload), "echo": f"<{len(long_payload)} characters>"}
        assert "-long-payload-" not in text
        assert await _clipboard_text(client) == long_payload


async def test_unicode_paste_restores_prior_clipboard_with_its_own_owner(tmp_path: Path) -> None:
    async with _session() as (client, info):
        socket = info["wayland_socket"]
        atspi = await _open_kwrite(client, info, tmp_path, "unicode-restore")
        async with _prior_clipboard_owner(socket, PRIOR_CLIPBOARD) as owner:
            typed, text = await client.call_structured(
                "keyboard_type", {"text": UNICODE_MARKER, "method": "unicode"}
            )
            assert text.splitlines()[0] == f"Typed unicode: {UNICODE_MARKER!r}"
            assert typed["route"] == "paste:ctrl+v", typed
            await _wait_for_text(atspi, UNICODE_MARKER)
            assert await _clipboard_text(client) == PRIOR_CLIPBOARD
            # The typed text replaced the original owner, so the restored
            # selection must be served by a new owner that stays alive.
            assert (
                await anyio.to_thread.run_sync(lambda: owner.wait(timeout=INPUT_TIMEOUT_SECONDS))
                == 0
            )
            assert await _clipboard_text(client) == PRIOR_CLIPBOARD
            await client.call_structured("keyboard_press", {"keys": "ctrl+v"})
            await _wait_for_text(atspi, UNICODE_MARKER + PRIOR_CLIPBOARD)


async def test_unicode_paste_clears_text_when_clipboard_was_empty(tmp_path: Path) -> None:
    async with _session() as (client, info):
        atspi = await _open_kwrite(client, info, tmp_path, "unicode-empty")
        empty = await _tool_error(client, "clipboard_read")
        assert empty.startswith("Failed to read clipboard: "), empty
        _, text = await client.call_structured(
            "keyboard_type", {"text": UNICODE_MARKER, "method": "unicode"}
        )
        assert text.splitlines()[0] == f"Typed unicode: {UNICODE_MARKER!r}"
        await _wait_for_text(atspi, UNICODE_MARKER)
        empty_after = await _tool_error(client, "clipboard_read")
        assert empty_after.startswith("Failed to read clipboard: "), empty_after


async def test_unicode_paste_waits_for_slow_prior_owner(tmp_path: Path) -> None:
    async with _session() as (client, info):
        socket = info["wayland_socket"]
        atspi = await _open_kwrite(client, info, tmp_path, "unicode-slow-owner")
        async with _prior_clipboard_owner(socket, PRIOR_CLIPBOARD) as owner:
            owner.send_signal(signal.SIGSTOP)

            async def resume_owner() -> None:
                await anyio.sleep(SLOW_BOUNDARY_SECONDS)
                owner.send_signal(signal.SIGCONT)

            async with anyio.create_task_group() as group:
                group.start_soon(resume_owner)
                _, text = await client.call_structured(
                    "keyboard_type", {"text": UNICODE_MARKER, "method": "unicode"}
                )
            assert text.splitlines()[0] == f"Typed unicode: {UNICODE_MARKER!r}"
            await _wait_for_text(atspi, UNICODE_MARKER)
            assert await _clipboard_text(client) == PRIOR_CLIPBOARD


async def test_unicode_paste_fails_without_pasting_when_prior_owner_never_answers(
    tmp_path: Path,
) -> None:
    async with _session() as (client, info):
        socket = info["wayland_socket"]
        atspi = await _open_kwrite(client, info, tmp_path, "unicode-stuck-owner")
        async with _prior_clipboard_owner(socket, PRIOR_CLIPBOARD) as owner:
            owner.send_signal(signal.SIGSTOP)
            try:
                message = await _tool_error(
                    client, "keyboard_type", {"text": UNICODE_MARKER, "method": "unicode"}
                )
                assert message == f"Failed to type unicode: {UNICODE_MARKER!r}"
                # No Ctrl+V was sent, so nothing can arrive late.
                assert _focused_text(atspi) == ""
                assert owner.poll() is None
            finally:
                owner.send_signal(signal.SIGCONT)
            assert await _clipboard_text(client) == PRIOR_CLIPBOARD


async def test_unicode_paste_rejects_oversized_multiline_text_without_side_effects(
    tmp_path: Path,
) -> None:
    # Public limit for the clipboard route is 1 MiB; 2 MiB of two-byte lines
    # exceeds it no matter the implementation constant, and each line would be
    # a protocol command if the payload ever reached the helper's stdin. It is
    # also far past Linux's per-argument limit, so a wtype on PATH fails to
    # launch and falls through.
    oversized = "x\n" * (1024 * 1024)
    async with _session() as (client, info):
        socket = info["wayland_socket"]
        atspi = await _open_kwrite(client, info, tmp_path, "unicode-oversized")
        async with _prior_clipboard_owner(socket, PRIOR_CLIPBOARD) as owner:
            with anyio.fail_after(INPUT_TIMEOUT_SECONDS):
                result = await client.call_result(
                    "keyboard_type", {"text": oversized, "method": "unicode"}
                )
            assert result.is_error is True
            assert text_of(result) == (
                "Error executing tool keyboard_type: "
                f"Failed to type unicode: <{len(oversized)} characters>"
            )
            assert _focused_text(atspi) == ""
            # The original owner was never replaced, so nothing took the selection.
            assert owner.poll() is None
            assert await _clipboard_text(client) == PRIOR_CLIPBOARD


async def test_clipboard_write_waits_for_slow_wl_copy(tmp_path: Path) -> None:
    assert WL_COPY is not None
    server_env = _wl_copy_shim_env(
        tmp_path, f'sleep {SLOW_BOUNDARY_SECONDS}\nexec {shlex.quote(WL_COPY)} "$@"'
    )
    async with _session(server_env=server_env) as (client, info):
        payload = "slow-45-클립보드"
        _, text = await client.call_structured("clipboard_write", {"text": payload})
        assert text == f"Clipboard set: {payload!r}"
        assert _test_paste(info["wayland_socket"]).stdout == payload.encode()
        assert await _clipboard_text(client) == payload


async def test_clipboard_write_reports_slow_wl_copy_failure(tmp_path: Path) -> None:
    server_env = _wl_copy_shim_env(tmp_path, f"sleep {SLOW_BOUNDARY_SECONDS}\nexit 2")
    async with (
        _session(server_env=server_env) as (client, info),
        _prior_clipboard_owner(info["wayland_socket"], PRIOR_CLIPBOARD),
    ):
        message = await _tool_error(client, "clipboard_write", {"text": "never-set-45"})
        assert message == "Failed to set clipboard: wl-copy exited with status 2"
        assert await _clipboard_text(client) == PRIOR_CLIPBOARD
