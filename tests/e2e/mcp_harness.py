"""Async helpers for exercising the installed kwin-mcp server over stdio.

Every helper drives the real ``kwin-mcp`` entry point as a subprocess; nothing
here imports the server module.

Tool names and protocol eras:
    ``TOOL_NAMES`` / ``REMOVED_TOOL_NAMES``: re-exported from ``tests/tool_surface.py``.
    ``MODELS: dict[str, type[BaseModel]]``: each tool's result model (``render()`` is
    the text the server must send).
    ``LEGACY_PROTOCOL_VERSION`` (``"2025-11-25"``, initialize handshake) and
    ``MODERN_PROTOCOL_VERSION`` (``"2026-07-28"``, no handshake; server requests
    travel as input-required results). ``ERAS = (LEGACY, MODERN)``; parametrize
    era-sensitive tests with ``@pytest.mark.parametrize("era", ERAS)``.

Clients (``McpTestClient`` for the handshake era, ``ModernMcpTestClient`` for
2026-07-28; both share these methods):
    ``call_result(name, arguments=None, *, progress_callback=None) -> CallToolResult``:
    the unmodified MCP result.
    ``call_text(name, arguments=None) -> str``: the joined text; fails on ``isError``.
    ``call_structured(name, arguments=None, *, progress_callback=None) -> (dict, str)``:
    fails on ``isError``; validates ``structuredContent`` against the tool's
    ``outputSchema`` (jsonschema) and asserts ``text == MODELS[name](sc).render()``;
    returns ``(structuredContent, text)``.
    ``call_model(name, arguments=None, *, progress_callback=None) -> BaseModel``:
    ``call_structured`` plus the validated ``MODELS[name]`` instance.
    ``call_tool_error(name, arguments=None, *, lines=2) -> tuple[str, ...]``: asserts
    ``isError``, no ``structuredContent`` and exactly ``lines`` text lines (``1`` for
    the removed-tool text, ``None`` = any); returns the lines.
    ``list_tools() -> list[Tool]``: the full ``tools/list`` (single page asserted).
    ``output_schema(name) -> dict``: the tool's ``outputSchema`` (cached ``tools/list``).
    ``read_resource(uri) -> ReadResourceResult`` and
    ``complete(ref, argument) -> CompleteResult``: thin SDK wrappers.
    ``stderr_text() -> str``: what the server wrote to stderr so far.
    ``McpTestClient.session`` (``ClientSession``) / ``.initialize_result`` and
    ``ModernMcpTestClient.client`` (``Client``) expose the SDK objects.

Module functions:
    ``text_of(result) -> str``: the joined ``TextContent`` of a result.
    ``images(result) -> list[bytes]``: decoded PNG bytes of every ``ImageContent``.
    ``ProgressLog``: a progress callback recording ``(progress, total, message)``;
    ``.values``, ``.events``, ``await .settle(seconds=0.2)``.

Context managers (all ``async with``):
    ``running_mcp_server(*extra_args, env=None, elicitation_callback=None)
    -> McpTestClient``: handshake era; the elicitation capability is declared only
    when ``elicitation_callback`` is given.
    ``running_modern_mcp_client(*extra_args, env=None, elicitation_callback=None)
    -> ModernMcpTestClient``: 2026-07-28 era (forced, never falls back).
    ``running_client(era, *extra_args, env=None, elicitation_callback=None)
    -> McpTestClient | ModernMcpTestClient``: picks one of the two by ``era``.
    ``raw_mcp_client(capabilities, *extra_args, era=LEGACY_PROTOCOL_VERSION,
    env=None) -> RawJsonRpcClient``: newline-delimited JSON-RPC without the SDK.

Elicitation:
    ``Answer = Literal["confirm", "unconfirmed", "decline", "cancel"]``;
    ``ELICIT_ANSWERS: dict[Answer, ElicitResult]``; ``elicit_reply(answer) -> dict``
    (the wire form, for ``RawJsonRpcClient.elicitation_reply``).
    ``ScriptedUser(answer=None)``: an ``elicitation_callback`` recording every
    request in ``.requests``; answers ``.answer`` (one-shot, reset after use) or
    declines when none is scripted, so an unexpected prompt never closes a window.

``RawJsonRpcClient``:
    ``request(method, params=None) -> dict``: the ``result``; asserts no JSON-RPC error.
    ``request_message(method, params=None) -> dict``: the whole response message.
    ``call_tool(name, arguments=None, *, input_responses=None, request_state=None)
    -> dict``: the final ``tools/call`` result; in the 2026-07-28 era it answers
    ``input_required`` rounds itself (elicitations get ``elicitation_reply`` once,
    else a decline). ``input_responses``/``request_state`` seed the first round
    (forge / replay tests).
    ``call_tool_once(name, arguments=None, *, input_responses=None,
    request_state=None) -> dict``: one ``tools/call`` round, unmodified.
    ``call_text(name, arguments=None) -> str``: joined text; fails on ``isError``.
    ``elicitations() -> list[dict]``: every ``elicitation/create`` request received
    (``{"method", "params"}``; modern-era ones also carry ``"key"``).
    ``server_requests``: every server-to-client request; ``elicitation_reply``:
    the scripted reply for the next elicitation.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import anyio
from anyio.streams.buffered import BufferedByteReceiveStream
from jsonschema import validators
from mcp import ClientSession, StdioServerParameters
from mcp.client import Client
from mcp.client.stdio import stdio_client
from mcp.types import ElicitResult, ImageContent, TextContent
from tool_surface import REMOVED_TOOL_NAMES, TOOL_NAMES

from kwin_mcp import models

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping
    from typing import Any, TextIO

    from anyio.abc import ByteSendStream, Process
    from mcp.client import ClientRequestContext
    from mcp.client.session import ElicitationFnT
    from mcp.types import (
        CallToolResult,
        CompleteResult,
        ElicitRequestParams,
        ErrorData,
        InitializeResult,
        PromptReference,
        ReadResourceResult,
        ResourceTemplateReference,
        Tool,
    )
    from pydantic import BaseModel

__all__ = [
    "ELICIT_ANSWERS",
    "ERAS",
    "LEGACY_PROTOCOL_VERSION",
    "MODELS",
    "MODERN_PROTOCOL_VERSION",
    "REMOVED_TOOL_NAMES",
    "TOOL_NAMES",
    "Answer",
    "McpTestClient",
    "ModernMcpTestClient",
    "ProgressLog",
    "RawJsonRpcClient",
    "ScriptedUser",
    "elicit_reply",
    "images",
    "raw_mcp_client",
    "render_structured",
    "running_client",
    "running_mcp_server",
    "running_modern_mcp_client",
    "text_of",
]

# The handshake era: initialize, then server-to-client requests on the stream.
LEGACY_PROTOCOL_VERSION = "2025-11-25"
# The 2026-07-28 protocol era: no initialize handshake, and server-to-client
# requests such as elicitation travel as input-required results (MRTR).
MODERN_PROTOCOL_VERSION = "2026-07-28"
ERAS: tuple[str, str] = (LEGACY_PROTOCOL_VERSION, MODERN_PROTOCOL_VERSION)

MODELS: dict[str, type[BaseModel]] = {
    "session_start": models.SessionInfo,
    "session_connect": models.SessionInfo,
    "session_stop": models.SessionStopResult,
    "session_info": models.SessionInfoResult,
    "app_launch": models.AppLaunchResult,
    "app_log": models.AppLogResult,
    "screen_capture": models.Frame,
    "element_query": models.ElementList,
    "element_act": models.ElementActResult,
    "window_list": models.WindowList,
    "desktop_list": models.DesktopList,
    "window_focus": models.WindowFocusResult,
    "window_place": models.WindowPlaceResult,
    "window_close": models.WindowCloseResult,
    "pointer_click": models.PointerClickResult,
    "pointer_move": models.PointerMoveResult,
    "pointer_scroll": models.PointerScrollResult,
    "pointer_drag": models.PointerDragResult,
    "pointer_hold": models.PointerHoldResult,
    "keyboard_press": models.KeyboardPressResult,
    "keyboard_type": models.KeyboardTypeResult,
    "keyboard_hold": models.KeyboardHoldResult,
    "touch_tap": models.TouchTapResult,
    "touch_swipe": models.TouchSwipeResult,
    "touch_pinch": models.TouchPinchResult,
    "clipboard_read": models.ClipboardText,
    "clipboard_write": models.ClipboardWriteResult,
    "dbus_call": models.DbusReply,
    "wayland_list": models.WaylandGlobals,
}
assert tuple(MODELS) == TOOL_NAMES, set(MODELS) ^ set(TOOL_NAMES)

Answer = Literal["confirm", "unconfirmed", "decline", "cancel"]
ELICIT_ANSWERS: dict[Answer, ElicitResult] = {
    "confirm": ElicitResult(action="accept", content={"confirm": True}),
    "unconfirmed": ElicitResult(action="accept", content={"confirm": False}),
    "decline": ElicitResult(action="decline"),
    "cancel": ElicitResult(action="cancel"),
}

ELICITATION_METHOD = "elicitation/create"
# Upper bound for one JSON-RPC line from the server.
MAX_LINE_BYTES = 16 * 1024 * 1024
RAW_CALL_TIMEOUT_SECONDS = 60.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0
MAX_INPUT_ROUNDS = 10


def elicit_reply(answer: Answer) -> dict[str, Any]:
    """The wire form of a scripted answer, for ``RawJsonRpcClient.elicitation_reply``."""
    return ELICIT_ANSWERS[answer].model_dump(mode="json", by_alias=True, exclude_none=True)


class ScriptedUser:
    """Elicitation callback that records every request and gives one scripted answer.

    With no answer scripted it declines, so an unexpected prompt can never close
    a window; the recorded request count still exposes it.
    """

    def __init__(self, answer: Answer | None = None) -> None:
        self.requests: list[ElicitRequestParams] = []
        self.answer: Answer | None = answer

    async def __call__(
        self, context: ClientRequestContext, params: ElicitRequestParams
    ) -> ElicitResult | ErrorData:
        self.requests.append(params)
        answer, self.answer = self.answer, None
        return ELICIT_ANSWERS[answer if answer is not None else "decline"]


class ProgressLog:
    """Progress callback that records every ``notifications/progress`` of one call."""

    def __init__(self) -> None:
        self.events: list[tuple[float, float | None, str | None]] = []

    async def __call__(self, progress: float, total: float | None, message: str | None) -> None:
        self.events.append((progress, total, message))

    @property
    def values(self) -> list[float]:
        return [progress for progress, _, _ in self.events]

    async def settle(self, seconds: float = 0.2) -> None:
        """Let callbacks the client spawned for already-received notifications run.

        The client dispatches each progress notification to its own task, so the
        last callbacks can still be pending when ``call_tool`` returns.
        """
        await anyio.sleep(seconds)


def text_of(result: CallToolResult) -> str:
    """Every ``TextContent`` of a result, joined with newlines."""
    return "\n".join(content.text for content in result.content if isinstance(content, TextContent))


def images(result: CallToolResult) -> list[bytes]:
    """The decoded bytes of every ``ImageContent`` of a result, in order."""
    return [
        base64.b64decode(content.data, validate=True)
        for content in result.content
        if isinstance(content, ImageContent)
    ]


class _ToolCaller:
    """Assertions over tool results, shared by both protocol eras."""

    def __init__(self, stderr_path: Path) -> None:
        self._stderr_path = stderr_path
        self._tools: dict[str, Tool] | None = None

    def stderr_text(self) -> str:
        """Return stderr emitted by the installed server process so far."""
        try:
            return self._stderr_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    def _with_stderr(self, details: str) -> str:
        stderr = self.stderr_text().strip()
        if stderr:
            return f"{details}\n\nkwin-mcp stderr:\n{stderr[-8000:]}"
        return details

    async def call_result(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        progress_callback: ProgressLog | None = None,
    ) -> CallToolResult:
        """Call a tool over JSON-RPC and return the unmodified MCP result."""
        raise NotImplementedError

    async def list_tools(self) -> list[Tool]:
        """The server's whole ``tools/list``."""
        raise NotImplementedError

    async def read_resource(self, uri: str) -> ReadResourceResult:
        """``resources/read`` for ``uri``."""
        raise NotImplementedError

    async def complete(
        self, ref: ResourceTemplateReference | PromptReference, argument: dict[str, str]
    ) -> CompleteResult:
        """``completion/complete`` for one argument of ``ref``."""
        raise NotImplementedError

    async def call_text(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Call a tool, failing immediately when MCP reports a tool error."""
        result = await self.call_result(name, arguments)
        text = text_of(result)
        if result.is_error:
            details = self._with_stderr(text or repr(result.content))
            raise AssertionError(f"MCP tool {name!r} failed: {details}")
        return text

    async def output_schema(self, name: str) -> dict[str, Any]:
        """The ``outputSchema`` the server published for ``name`` (cached)."""
        if self._tools is None:
            self._tools = {tool.name: tool for tool in await self.list_tools()}
        schema = self._tools[name].output_schema
        assert schema is not None, f"{name} publishes no outputSchema"
        return schema

    async def call_structured(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        progress_callback: ProgressLog | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Call a tool; check its structured content against the schema and the text.

        Fails on ``isError``. ``structuredContent`` must validate against the
        tool's published ``outputSchema``, the first content item must be the only
        ``TextContent``, and its text must equal ``MODELS[name]``'s ``render()`` of
        the structured content.
        """
        result = await self.call_result(name, arguments, progress_callback=progress_callback)
        texts = [content for content in result.content if isinstance(content, TextContent)]
        text = "\n".join(content.text for content in texts)
        if result.is_error:
            details = self._with_stderr(text or repr(result.content))
            raise AssertionError(f"MCP tool {name!r} failed: {details}")
        assert len(texts) == 1, result.content
        assert result.content[0] is texts[0], result.content
        structured = result.structured_content
        assert structured is not None, f"{name} returned no structuredContent: {text}"

        schema = await self.output_schema(name)
        validator_cls = validators.validator_for(schema)
        validator_cls.check_schema(schema)
        errors = sorted(validator_cls(schema).iter_errors(structured), key=str)
        assert not errors, f"{name} structuredContent violates outputSchema: {errors[0]}"

        rendered = render_structured(name, structured)
        assert text == rendered, f"{name} text differs from render():\n{text}\n---\n{rendered}"
        return structured, text

    async def call_model(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        progress_callback: ProgressLog | None = None,
    ) -> BaseModel:
        """``call_structured``, returning the validated ``MODELS[name]`` instance."""
        structured, _ = await self.call_structured(
            name, arguments, progress_callback=progress_callback
        )
        return MODELS[name].model_validate(structured)

    async def call_tool_error(
        self, name: str, arguments: dict[str, Any] | None = None, *, lines: int | None = 2
    ) -> tuple[str, ...]:
        """Call a tool that must fail: ``isError``, no structured content, only text.

        Returns the text lines; there must be exactly ``lines`` of them (two for a
        ``ToolError``: what failed, then the remedy). Pass ``lines=1`` for the
        verbatim single-line texts (removed tools) and ``None`` to skip the count.
        """
        result = await self.call_result(name, arguments)
        text = text_of(result)
        assert result.is_error, f"MCP tool {name!r} unexpectedly succeeded: {text}"
        assert result.structured_content is None, result.structured_content
        assert all(isinstance(content, TextContent) for content in result.content), result
        split = tuple(text.split("\n"))
        assert lines is None or len(split) == lines, f"error text is not {lines} lines: {text!r}"
        return split


def render_structured(name: str, structured: dict[str, Any]) -> str:
    """``MODELS[name]``'s ``render()`` of ``structured``: the text the server must send."""
    model = MODELS[name].model_validate(structured)
    render = getattr(type(model), "render", None)
    assert callable(render), f"{type(model).__name__} has no render()"
    text = render(model)
    assert isinstance(text, str), text
    return text


class McpTestClient(_ToolCaller):
    """Client initialized through the handshake (initialize) protocol era."""

    def __init__(
        self,
        session: ClientSession,
        stderr_path: Path,
        initialize_result: InitializeResult,
    ) -> None:
        super().__init__(stderr_path)
        self.session = session
        self.initialize_result = initialize_result

    async def call_result(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        progress_callback: ProgressLog | None = None,
    ) -> CallToolResult:
        """Call a tool over JSON-RPC and return the unmodified MCP result.

        Passing ``progress_callback`` makes the client send a progressToken, so the
        server's ``notifications/progress`` for this call are recorded in it.
        """
        return await self.session.call_tool(name, arguments, progress_callback=progress_callback)

    async def list_tools(self) -> list[Tool]:
        listing = await self.session.list_tools()
        assert listing.next_cursor is None, listing.next_cursor
        return listing.tools

    async def read_resource(self, uri: str) -> ReadResourceResult:
        return await self.session.read_resource(uri)

    async def complete(
        self, ref: ResourceTemplateReference | PromptReference, argument: dict[str, str]
    ) -> CompleteResult:
        return await self.session.complete(ref, argument)


class ModernMcpTestClient(_ToolCaller):
    """Client speaking the 2026-07-28 protocol era, which has no initialize handshake.

    ``Client.call_tool`` drives input-required rounds itself, answering them
    through the elicitation callback before returning the final result.
    """

    def __init__(self, client: Client, stderr_path: Path) -> None:
        super().__init__(stderr_path)
        self.client = client

    async def call_result(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        progress_callback: ProgressLog | None = None,
    ) -> CallToolResult:
        """Call a tool over JSON-RPC and return the unmodified MCP result.

        Passing ``progress_callback`` makes the client send a progressToken, so the
        server's ``notifications/progress`` for this call are recorded in it.
        """
        return await self.client.call_tool(name, arguments, progress_callback=progress_callback)

    async def list_tools(self) -> list[Tool]:
        listing = await self.client.list_tools()
        assert listing.next_cursor is None, listing.next_cursor
        return listing.tools

    async def read_resource(self, uri: str) -> ReadResourceResult:
        return await self.client.read_resource(uri)

    async def complete(
        self, ref: ResourceTemplateReference | PromptReference, argument: dict[str, str]
    ) -> CompleteResult:
        return await self.client.complete(ref, argument)


def _add_stderr_note(error: BaseException, stderr_path: Path) -> None:
    try:
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return
    if not stderr:
        return

    note = f"kwin-mcp stderr:\n{stderr[-8000:]}"
    if note not in getattr(error, "__notes__", ()):
        error.add_note(note)


def _server_env(env: Mapping[str, str] | None) -> dict[str, str]:
    server_env = os.environ.copy()
    if env is not None:
        server_env.update(env)
    return server_env


@asynccontextmanager
async def _installed_server(
    extra_args: tuple[str, ...], env: Mapping[str, str] | None
) -> AsyncGenerator[tuple[StdioServerParameters, TextIO, Path]]:
    """Describe the installed kwin-mcp entrypoint and own its stderr capture file.

    Failures raised inside the block get the server's stderr attached as a note.
    """
    stderr_file = cast(
        "TextIO",
        tempfile.NamedTemporaryFile(  # noqa: SIM115 - kept open through async yield
            mode="w+",
            encoding="utf-8",
            prefix="kwin-mcp-e2e-stderr-",
            delete=False,
        ),
    )
    stderr_path = Path(stderr_file.name)
    parameters = StdioServerParameters(
        command="kwin-mcp", args=list(extra_args), env=_server_env(env)
    )

    try:
        yield parameters, stderr_file, stderr_path
    except BaseException as error:
        stderr_file.flush()
        _add_stderr_note(error, stderr_path)
        raise
    finally:
        stderr_file.close()
        stderr_path.unlink(missing_ok=True)


@asynccontextmanager
async def running_mcp_server(
    *extra_args: str,
    env: Mapping[str, str] | None = None,
    elicitation_callback: ElicitationFnT | None = None,
) -> AsyncGenerator[McpTestClient]:
    """Start and initialize the installed kwin-mcp entrypoint over real stdio.

    Environment overrides apply only to the spawned server process. The client
    declares the elicitation capability only when ``elicitation_callback`` is set.
    """
    async with (
        _installed_server(extra_args, env) as (parameters, stderr_file, stderr_path),
        stdio_client(parameters, errlog=stderr_file) as (read_stream, write_stream),
        ClientSession(
            read_stream, write_stream, elicitation_callback=elicitation_callback
        ) as session,
    ):
        initialize_result = await session.initialize()
        yield McpTestClient(session, stderr_path, initialize_result)


@asynccontextmanager
async def running_modern_mcp_client(
    *extra_args: str,
    env: Mapping[str, str] | None = None,
    elicitation_callback: ElicitationFnT | None = None,
) -> AsyncGenerator[ModernMcpTestClient]:
    """Start the installed kwin-mcp entrypoint and connect in the 2026-07-28 era.

    The mode is forced, so a server that fell back to the handshake would fail
    to connect instead of silently testing the older era. The client declares
    the elicitation capability (per request) only when ``elicitation_callback``
    is set.
    """
    async with (
        _installed_server(extra_args, env) as (parameters, stderr_file, stderr_path),
        Client(
            stdio_client(parameters, errlog=stderr_file),
            mode=MODERN_PROTOCOL_VERSION,
            elicitation_callback=elicitation_callback,
        ) as client,
    ):
        assert client.protocol_version == MODERN_PROTOCOL_VERSION, client.protocol_version
        yield ModernMcpTestClient(client, stderr_path)


@asynccontextmanager
async def running_client(
    era: str,
    *extra_args: str,
    env: Mapping[str, str] | None = None,
    elicitation_callback: ElicitationFnT | None = None,
) -> AsyncGenerator[McpTestClient | ModernMcpTestClient]:
    """``running_mcp_server`` or ``running_modern_mcp_client``, chosen by ``era``."""
    if era == LEGACY_PROTOCOL_VERSION:
        async with running_mcp_server(
            *extra_args, env=env, elicitation_callback=elicitation_callback
        ) as client:
            negotiated = client.initialize_result.protocol_version
            assert negotiated == LEGACY_PROTOCOL_VERSION, negotiated
            yield client
    elif era == MODERN_PROTOCOL_VERSION:
        async with running_modern_mcp_client(
            *extra_args, env=env, elicitation_callback=elicitation_callback
        ) as client:
            yield client
    else:
        msg = f"unknown protocol era {era!r}; expected one of {ERAS}"
        raise ValueError(msg)


class RawJsonRpcClient:
    """Newline-delimited JSON-RPC over the installed kwin-mcp's stdio, with no SDK.

    Real clients declare elicitation in shapes the SDK clients never send
    (Claude Code: a bare ``{}``; Codex: both modes; url-only clients). This client
    sends exactly the ``capabilities`` it was given. In the handshake era they go
    into ``initialize``; in the 2026-07-28 era every request carries them in
    ``_meta`` with the protocol version.

    Every server-to-client request is recorded. ``elicitation/create`` gets the
    scripted ``elicitation_reply`` once, else a decline, so an unexpected prompt
    cannot close a window; ``ping`` gets an empty result; anything else is
    method-not-found. Callers bound every exchange with a timeout.
    """

    def __init__(
        self,
        stdin: ByteSendStream,
        stdout: BufferedByteReceiveStream,
        *,
        era: str,
        capabilities: dict[str, Any],
    ) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._next_id = 0
        self.era = era
        self.capabilities = capabilities
        self.server_requests: list[dict[str, Any]] = []
        self.elicitation_reply: dict[str, Any] | None = None

    def elicitations(self) -> list[dict[str, Any]]:
        """Every ``elicitation/create`` request the server sent, in order."""
        return [r for r in self.server_requests if r["method"] == ELICITATION_METHOD]

    async def _send(self, message: dict[str, Any]) -> None:
        await self._stdin.send(json.dumps(message).encode() + b"\n")

    async def notify(self, method: str) -> None:
        await self._send({"jsonrpc": "2.0", "method": method})

    def _with_meta(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params = dict(params or {})
        if self.era == MODERN_PROTOCOL_VERSION:
            meta = dict(params.get("_meta") or {})
            meta.setdefault("io.modelcontextprotocol/protocolVersion", MODERN_PROTOCOL_VERSION)
            meta.setdefault("io.modelcontextprotocol/clientCapabilities", self.capabilities)
            meta.setdefault(
                "io.modelcontextprotocol/clientInfo", {"name": "kwin-mcp-e2e-raw", "version": "0"}
            )
            params["_meta"] = meta
        return params

    async def request_message(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send one request and return the whole response message (result or error)."""
        self._next_id += 1
        request_id = self._next_id
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": self._with_meta(params),
            }
        )
        while True:
            message = json.loads(await self._stdout.receive_until(b"\n", MAX_LINE_BYTES))
            if "method" in message:
                if "id" in message:
                    await self._answer(message)
                continue  # notification
            assert message.get("id") == request_id, message
            return message

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one request and return its ``result``; a JSON-RPC error fails."""
        message = await self.request_message(method, params)
        assert "error" not in message, message
        return message["result"]

    def _next_elicitation_reply(self) -> dict[str, Any]:
        reply, self.elicitation_reply = self.elicitation_reply, None
        return reply if reply is not None else {"action": "decline"}

    async def _answer(self, request: dict[str, Any]) -> None:
        self.server_requests.append(request)
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": request["id"]}
        if request["method"] == ELICITATION_METHOD:
            reply["result"] = self._next_elicitation_reply()
        elif request["method"] == "ping":
            reply["result"] = {}
        else:
            reply["error"] = {"code": -32601, "message": "Method not found"}
        await self._send(reply)

    async def call_tool_once(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        input_responses: dict[str, Any] | None = None,
        request_state: str | None = None,
    ) -> dict[str, Any]:
        """One ``tools/call`` round; the result is returned unmodified."""
        params: dict[str, Any] = {"name": name, "arguments": arguments or {}}
        if input_responses is not None:
            params["inputResponses"] = input_responses
        if request_state is not None:
            params["requestState"] = request_state
        return await self.request("tools/call", params)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        input_responses: dict[str, Any] | None = None,
        request_state: str | None = None,
    ) -> dict[str, Any]:
        """The final ``tools/call`` result, answering input-required rounds.

        ``input_responses`` and ``request_state`` seed only the first round.
        """
        result = await self.call_tool_once(
            name, arguments, input_responses=input_responses, request_state=request_state
        )
        for _ in range(MAX_INPUT_ROUNDS):
            if result.get("resultType") != "input_required":
                return result
            responses: dict[str, Any] = {}
            for key, input_request in (result.get("inputRequests") or {}).items():
                self.server_requests.append({**input_request, "key": key})
                if input_request["method"] == ELICITATION_METHOD:
                    responses[key] = self._next_elicitation_reply()
                else:
                    responses[key] = {"code": -32601, "message": "Method not found"}
            result = await self.call_tool_once(
                name,
                arguments,
                input_responses=responses or None,
                request_state=result.get("requestState"),
            )
        msg = f"{name}: still input_required after {MAX_INPUT_ROUNDS} rounds: {result}"
        raise AssertionError(msg)

    async def call_text(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Call a tool and return its joined text; ``isError`` fails."""
        result = await self.call_tool(name, arguments)
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
            await process.wait()
        await process.aclose()


@asynccontextmanager
async def raw_mcp_client(
    capabilities: dict[str, Any],
    *extra_args: str,
    era: str = LEGACY_PROTOCOL_VERSION,
    env: Mapping[str, str] | None = None,
) -> AsyncGenerator[RawJsonRpcClient]:
    """Start the installed kwin-mcp and connect a raw client with exactly ``capabilities``.

    In the handshake era the client sends ``initialize`` (asserting the server
    agrees on ``LEGACY_PROTOCOL_VERSION``) and ``notifications/initialized``; in
    the 2026-07-28 era it sends nothing until the first request.
    """
    if era not in ERAS:
        msg = f"unknown protocol era {era!r}; expected one of {ERAS}"
        raise ValueError(msg)
    with tempfile.TemporaryFile() as stderr_file:
        process = await anyio.open_process(
            ["kwin-mcp", *extra_args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            env=_server_env(env),
        )
        try:
            assert process.stdin is not None
            assert process.stdout is not None
            client = RawJsonRpcClient(
                process.stdin,
                BufferedByteReceiveStream(process.stdout),
                era=era,
                capabilities=capabilities,
            )
            if era == LEGACY_PROTOCOL_VERSION:
                with anyio.fail_after(RAW_CALL_TIMEOUT_SECONDS):
                    result = await client.request(
                        "initialize",
                        {
                            "protocolVersion": LEGACY_PROTOCOL_VERSION,
                            "capabilities": capabilities,
                            "clientInfo": {"name": "kwin-mcp-e2e-raw", "version": "0"},
                        },
                    )
                    assert result["protocolVersion"] == LEGACY_PROTOCOL_VERSION, result
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
