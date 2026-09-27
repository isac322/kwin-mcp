"""End-to-end coverage for the window_close confirmation asked through MCP elicitation.

Live sessions (session_connect) close a window only after the user confirms a
form elicitation. Clients that cannot show a form get the plain live-session
refusal and are never asked; virtual sessions close without asking. Every row
runs against the installed kwin-mcp over real stdio, in both protocol eras: the
handshake era, where the server pushes elicitation/create, and 2026-07-28,
where the question travels as an input-required result the client answers by
retrying the call. A raw JSON-RPC client covers the capability shapes real
clients send, which the SDK clients cannot produce.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Protocol

import anyio
import pytest
from anyio.streams.buffered import BufferedByteReceiveStream
from mcp.types import ElicitRequestFormParams, ElicitResult
from mcp_harness import (
    MODERN_PROTOCOL_VERSION,
    running_mcp_server,
    running_modern_mcp_client,
)
from session_harness import live_kwin

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager
    from typing import Any

    from anyio.abc import ByteSendStream, Process
    from mcp.client import ClientRequestContext
    from mcp.types import ElicitRequestParams, ErrorData


class ToolClient(Protocol):
    """Every client flavor the tests drive: SDK sessions of either era and the raw one."""

    async def call_text(self, name: str, arguments: dict[str, Any] | None = None) -> str: ...


HANDSHAKE = "handshake"
ERAS = pytest.mark.parametrize("era", [HANDSHAKE, MODERN_PROTOCOL_VERSION])

APP = "kcalc"
CALL_TIMEOUT_SECONDS = 60.0
SESSION_TIMEOUT_SECONDS = 90.0
WINDOW_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.2
# A window a wrong close request was sent to is gone well within this.
SETTLE_SECONDS = 1.0
# KWin window ids are UUIDs; this one names no window.
UNKNOWN_ID = "{00000000-0000-0000-0000-000000000000}"

LIVE_REFUSAL = (
    "window_close is disabled in live sessions: it could discard unsaved work "
    "on the real desktop. Close the window through the app's own UI instead."
)
CONFIRM = ElicitResult(action="accept", content={"confirm": True})
UNCONFIRMED = ElicitResult(action="accept", content={"confirm": False})
DECLINE = ElicitResult(action="decline")
CANCEL = ElicitResult(action="cancel")

WINDOW_ENTRY = re.compile(r'^- (\S+) ".*"(?: \[active\])?\n    id:\s+(\S+)$', re.MULTILINE)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ScriptedUser:
    """Elicitation callback that records every request and gives one scripted answer.

    With no answer scripted it declines, so an unexpected prompt can never close
    a window; the recorded request count still exposes it.
    """

    def __init__(self) -> None:
        self.requests: list[ElicitRequestParams] = []
        self.answer: ElicitResult | None = None

    async def __call__(
        self, context: ClientRequestContext, params: ElicitRequestParams
    ) -> ElicitResult | ErrorData:
        self.requests.append(params)
        answer, self.answer = self.answer, None
        return answer if answer is not None else DECLINE


@asynccontextmanager
async def _client(
    era: str, user: ScriptedUser | None, env: dict[str, str]
) -> AsyncIterator[ToolClient]:
    if era == HANDSHAKE:
        async with running_mcp_server(env=env, elicitation_callback=user) as client:
            yield client
    else:
        async with running_modern_mcp_client(env=env, elicitation_callback=user) as client:
            yield client


async def _call(client: ToolClient, name: str, arguments: dict[str, Any] | None = None) -> str:
    with anyio.fail_after(CALL_TIMEOUT_SECONDS):
        return await client.call_text(name, arguments)


async def _await_single_window(client: ToolClient) -> tuple[str, str]:
    """Wait until exactly one APP window exists; return its (id, app name)."""
    deadline = time.monotonic() + WINDOW_TIMEOUT_SECONDS
    listing = ""
    while time.monotonic() < deadline:
        listing = await _call(client, "window_geometry", {"app_name": APP})
        entries = WINDOW_ENTRY.findall(listing)
        if len(entries) == 1:
            app_name, window_id = entries[0]
            return window_id, app_name
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"expected one {APP} window:\n{listing}")


async def _window_exists(client: ToolClient, window_id: str) -> bool:
    reply = await _call(client, "window_geometry", {"window_id": window_id})
    if reply == f"No window with id {window_id!r}.":
        return False
    assert [entry[1] for entry in WINDOW_ENTRY.findall(reply)] == [window_id], reply
    return True


async def _await_window_gone(client: ToolClient, window_id: str) -> None:
    deadline = time.monotonic() + WINDOW_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not await _window_exists(client, window_id):
            return
        await anyio.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"window {window_id} still exists after window_close")


async def _assert_window_survives(client: ToolClient, window_id: str) -> None:
    await anyio.sleep(SETTLE_SECONDS)
    assert await _window_exists(client, window_id), window_id


def _assert_form_request(params: ElicitRequestParams, window_id: str, app_name: str) -> None:
    assert isinstance(params, ElicitRequestFormParams), params
    assert params.mode == "form"
    assert window_id in params.message, params.message
    assert app_name in params.message, params.message
    schema = params.requested_schema
    assert schema["properties"]["confirm"]["type"] == "boolean", schema
    assert "confirm" in schema.get("required", []), schema


@asynccontextmanager
async def _live_window[C: ToolClient](
    open_client: Callable[[dict[str, str]], AbstractAsyncContextManager[C]],
) -> AsyncIterator[tuple[C, str, str]]:
    """Connect to a test-owned live KWin, launch APP and yield (client, window id, app name).

    ``open_client`` starts kwin-mcp with the given extra server environment.
    """
    with live_kwin() as live:
        env = {"DBUS_SESSION_BUS_ADDRESS": live.dbus_address}
        async with open_client(env) as client:
            connected = False
            try:
                connect_args: dict[str, Any] = {
                    "dbus_address": live.dbus_address,
                    "wayland_display": live.wayland_display,
                }
                with anyio.fail_after(SESSION_TIMEOUT_SECONDS):
                    output = await client.call_text("session_connect", connect_args)
                assert output.startswith("Connected to live KWin session."), output
                connected = True
                launched = await _call(client, "launch_app", {"command": APP})
                assert f"App launched: {APP}" in launched, launched
                window_id, app_name = await _await_single_window(client)
                yield client, window_id, app_name
            finally:
                if connected:
                    assert await _call(client, "session_stop") == "Disconnected from live session."


@pytest.mark.anyio
@ERAS
async def test_live_window_close_without_elicitation_is_refused(era: str) -> None:
    """A client without the elicitation capability is never asked and gets the refusal."""
    async with _live_window(lambda env: _client(era, None, env)) as (client, window_id, _):
        assert await _call(client, "window_close", {"window_id": window_id}) == LIVE_REFUSAL
        await _assert_window_survives(client, window_id)


@pytest.mark.anyio
@ERAS
async def test_live_window_close_asks_and_honors_each_answer(era: str) -> None:
    user = ScriptedUser()
    async with _live_window(lambda env: _client(era, user, env)) as (client, window_id, app_name):
        not_closed = (
            (
                UNCONFIRMED,
                f"window_close not performed: the user did not confirm closing window {window_id}.",
            ),
            (DECLINE, _declined_text(window_id)),
            (
                CANCEL,
                "window_close not performed: the user dismissed the confirmation "
                f"for window {window_id}.",
            ),
        )
        for answer, expected in not_closed:
            asked = len(user.requests)
            user.answer = answer
            assert await _call(client, "window_close", {"window_id": window_id}) == expected
            assert len(user.requests) == asked + 1, (answer, user.requests)
            _assert_form_request(user.requests[-1], window_id, app_name)
        await _assert_window_survives(client, window_id)

        # Nothing to confirm for an id that names no window.
        asked = len(user.requests)
        reply = await _call(client, "window_close", {"window_id": UNKNOWN_ID})
        assert reply == f"No window with id {UNKNOWN_ID!r}.", reply
        assert len(user.requests) == asked, user.requests

        asked = len(user.requests)
        user.answer = CONFIRM
        closed = await _call(client, "window_close", {"window_id": window_id})
        _assert_close_requested(closed, window_id, app_name)
        assert len(user.requests) == asked + 1, user.requests
        _assert_form_request(user.requests[-1], window_id, app_name)
        await _await_window_gone(client, window_id)


@pytest.mark.anyio
@ERAS
async def test_virtual_window_close_does_not_ask(era: str) -> None:
    user = ScriptedUser()
    async with _client(era, user, {}) as client:
        running = False
        try:
            start_args: dict[str, Any] = {"app_command": APP}
            with anyio.fail_after(SESSION_TIMEOUT_SECONDS):
                output = await client.call_text("session_start", start_args)
            running = True
            assert f"App launched: {APP}" in output, output
            window_id, app_name = await _await_single_window(client)

            closed = await _call(client, "window_close", {"window_id": window_id})
            _assert_close_requested(closed, window_id, app_name)
            assert user.requests == []
            await _await_window_gone(client, window_id)
        finally:
            if running:
                with anyio.fail_after(SESSION_TIMEOUT_SECONDS):
                    assert await client.call_text("session_stop") == "Session stopped."


# ── Raw JSON-RPC clients ──────────────────────────────────────────────────
#
# Real clients declare elicitation in shapes the SDK clients never send: Claude
# Code sends a bare {} (form mode only, per spec 2025-11-25), Codex sends both
# modes, and a url-only client must not get a form.

RAW_PROTOCOL_VERSION = "2025-11-25"
ELICITATION_METHOD = "elicitation/create"
# Upper bound for one JSON-RPC line from the server; these replies are short text.
MAX_LINE_BYTES = 16 * 1024 * 1024
SHUTDOWN_TIMEOUT_SECONDS = 10.0


def _declined_text(window_id: str) -> str:
    return (
        f"window_close not performed: the user declined closing window {window_id}. "
        "Do not retry unless the user asks."
    )


def _assert_close_requested(reply: str, window_id: str, app_name: str) -> None:
    assert reply.startswith(f'Close requested: {app_name} "'), reply
    assert f"({window_id})" in reply, reply


class RawJsonRpcClient:
    """Newline-delimited JSON-RPC over the installed kwin-mcp's stdio, with no SDK.

    Records every server-to-client request. elicitation/create gets the scripted
    ``elicitation_reply`` once, else a decline, so an unexpected prompt cannot
    close a window; ping gets an empty result; anything else is method-not-found.
    Callers bound every exchange with a timeout.
    """

    def __init__(self, stdin: ByteSendStream, stdout: BufferedByteReceiveStream) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._next_id = 0
        self.server_requests: list[dict[str, Any]] = []
        self.elicitation_reply: dict[str, Any] | None = None

    def elicitations(self) -> list[dict[str, Any]]:
        return [r for r in self.server_requests if r["method"] == ELICITATION_METHOD]

    async def _send(self, message: dict[str, Any]) -> None:
        await self._stdin.send(json.dumps(message).encode() + b"\n")

    async def notify(self, method: str) -> None:
        await self._send({"jsonrpc": "2.0", "method": method})

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            message = json.loads(await self._stdout.receive_until(b"\n", MAX_LINE_BYTES))
            if "method" in message:
                if "id" in message:
                    await self._answer(message)
                continue  # notification
            assert message.get("id") == request_id, message
            assert "error" not in message, message
            return message["result"]

    async def _answer(self, request: dict[str, Any]) -> None:
        self.server_requests.append(request)
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": request["id"]}
        if request["method"] == ELICITATION_METHOD:
            reply["result"] = self.elicitation_reply or {"action": "decline"}
            self.elicitation_reply = None
        elif request["method"] == "ping":
            reply["result"] = {}
        else:
            reply["error"] = {"code": -32601, "message": "Method not found"}
        await self._send(reply)

    async def call_text(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        result = await self.request("tools/call", {"name": name, "arguments": arguments or {}})
        text = "\n".join(item["text"] for item in result["content"] if item["type"] == "text")
        assert not result.get("isError"), text or result
        return text


async def _stop_server(process: Process) -> None:
    """Close stdin so the server exits; kill it if it does not within the bound."""
    with anyio.CancelScope(shield=True):
        if process.stdin is not None:
            await process.stdin.aclose()
        with anyio.move_on_after(SHUTDOWN_TIMEOUT_SECONDS):
            await process.wait()
        if process.returncode is None:
            process.kill()
        await process.aclose()


@asynccontextmanager
async def _raw_client(
    capabilities: dict[str, Any], env: dict[str, str]
) -> AsyncIterator[RawJsonRpcClient]:
    """Start the installed kwin-mcp and initialize it with exactly ``capabilities``."""
    with tempfile.TemporaryFile() as stderr_file:
        process = await anyio.open_process(
            ["kwin-mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            env={**os.environ, **env},
        )
        try:
            assert process.stdin is not None
            assert process.stdout is not None
            client = RawJsonRpcClient(process.stdin, BufferedByteReceiveStream(process.stdout))
            with anyio.fail_after(CALL_TIMEOUT_SECONDS):
                result = await client.request(
                    "initialize",
                    {
                        "protocolVersion": RAW_PROTOCOL_VERSION,
                        "capabilities": capabilities,
                        "clientInfo": {"name": "kwin-mcp-e2e-raw", "version": "0"},
                    },
                )
                assert result["protocolVersion"] == RAW_PROTOCOL_VERSION, result
                await client.notify("notifications/initialized")
            yield client
        except BaseException as error:
            stderr_file.seek(0)
            stderr = stderr_file.read().decode(errors="replace").strip()
            if stderr:
                error.add_note(f"kwin-mcp stderr:\n{stderr[-8000:]}")
            raise
        finally:
            await _stop_server(process)


def _assert_raw_form_request(request: dict[str, Any], window_id: str, app_name: str) -> None:
    params = request["params"]
    # Spec 2025-11-25: a request without mode is a form request.
    assert params.get("mode", "form") == "form", params
    assert window_id in params["message"], params
    assert app_name in params["message"], params
    schema = params["requestedSchema"]
    assert schema["properties"]["confirm"]["type"] == "boolean", schema
    assert "confirm" in schema.get("required", []), schema


@pytest.mark.anyio
@pytest.mark.parametrize(
    "capabilities",
    [{}, {"elicitation": {"url": {}}}],
    ids=["no-elicitation", "url-only"],
)
async def test_raw_client_without_form_elicitation_is_refused_unasked(
    capabilities: dict[str, Any],
) -> None:
    async with _live_window(lambda env: _raw_client(capabilities, env)) as (
        client,
        window_id,
        _,
    ):
        assert await _call(client, "window_close", {"window_id": window_id}) == LIVE_REFUSAL
        assert client.elicitations() == []
        await _assert_window_survives(client, window_id)


@pytest.mark.anyio
async def test_raw_bare_elicitation_capability_is_asked_and_decline_keeps_window() -> None:
    """Claude Code's shape: a bare elicitation capability means form mode."""
    capabilities: dict[str, Any] = {"elicitation": {}}
    async with _live_window(lambda env: _raw_client(capabilities, env)) as (
        client,
        window_id,
        app_name,
    ):
        client.elicitation_reply = {"action": "decline"}
        reply = await _call(client, "window_close", {"window_id": window_id})
        assert reply == _declined_text(window_id)
        [request] = client.elicitations()
        _assert_raw_form_request(request, window_id, app_name)
        await _assert_window_survives(client, window_id)


@pytest.mark.anyio
async def test_raw_form_and_url_capability_is_asked_and_confirm_closes_window() -> None:
    """Codex's shape: both modes declared, so the form request is sent."""
    capabilities: dict[str, Any] = {"elicitation": {"form": {}, "url": {}}}
    async with _live_window(lambda env: _raw_client(capabilities, env)) as (
        client,
        window_id,
        app_name,
    ):
        client.elicitation_reply = {"action": "accept", "content": {"confirm": True}}
        closed = await _call(client, "window_close", {"window_id": window_id})
        _assert_close_requested(closed, window_id, app_name)
        [request] = client.elicitations()
        _assert_raw_form_request(request, window_id, app_name)
        await _await_window_gone(client, window_id)
