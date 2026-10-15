"""End-to-end coverage of window_close's consent step (S §2.14, §3.6).

A live session (session_connect) closes a window only after the user confirms a
form elicitation. Clients that cannot show a form, and servers started with
``--live-consent=refuse``, get the plain live-session refusal and are never
asked; virtual sessions close without asking. Every test drives the installed
kwin-mcp over real stdio, in both protocol eras where the question travels
differently: the handshake era pushes ``elicitation/create`` to the client, the
2026-07-28 era returns an input-required result the client answers by retrying
the call. A raw JSON-RPC client covers the capability shapes real clients send
and the input-required rounds themselves.

Window existence is read from KWin by ``kwin_oracle`` (test-owned scripts), never
from kwin-mcp. Expected texts are written out literally.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from kwin_oracle import KWinOracle
from kwin_rules import WindowRule, install_rules
from mcp_harness import (
    ELICIT_ANSWERS,
    ERAS,
    LEGACY_PROTOCOL_VERSION,
    MODERN_PROTOCOL_VERSION,
    ScriptedUser,
    elicit_reply,
    raw_mcp_client,
    running_client,
)
from session_harness import live_kwin
from window_zoo import APP_ID as ZOO_APP_ID
from window_zoo import MAIN_CAPTION as ZOO_CAPTION
from window_zoo import ZooProcess

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from kwin_oracle import OracleWindow
    from mcp.client import ClientRequestContext
    from mcp.client.session import ElicitationFnT
    from mcp.types import ElicitRequestParams, ElicitResult, ErrorData
    from mcp_harness import McpTestClient, ModernMcpTestClient, RawJsonRpcClient
    from session_harness import LiveKWin

    type ToolClient = McpTestClient | ModernMcpTestClient
    type Caller = Callable[[str, dict[str, Any] | None], Awaitable[tuple[dict[str, Any], str]]]

CALL_TIMEOUT_SECONDS = 60.0
SESSION_TIMEOUT_SECONDS = 90.0
WINDOW_TIMEOUT_SECONDS = 30.0
# A window a wrong close request was sent to is gone well within this.
SETTLE_SECONDS = 1.0
KCALC = "kcalc"
KCALC_APP_ID = "org.kde.kcalc"
# KWin window ids are UUIDs: this one names no window.
UNKNOWN_ID = "{00000000-0000-0000-0000-000000000000}"
FORGED_CONSENT = {"action": "accept", "content": {"confirm": True}}
MISSING_CAPABILITY = "MISSING_REQUIRED_CLIENT_CAPABILITY"
ELICITATION_METHOD = "elicitation/create"
MAX_LINE_BYTES = 16 * 1024 * 1024

LIVE_REFUSAL = (
    "window_close is disabled in live sessions: it could discard unsaved work "
    "on the real desktop. Close the window through the app's own UI instead."
)
CONFIRM_PROPERTIES = {
    "confirm": {
        "type": "boolean",
        "title": "Close this window",
        "description": "Close it even if it has unsaved work.",
    }
}

NO_FORM_CAPABILITIES = pytest.mark.parametrize(
    "capabilities", [{}, {"elicitation": {"url": {}}}], ids=["no-elicitation", "url-only"]
)

# Server exit bound once stdin closes with a consent prompt pending (QA-WIN-044 c):
# the server's exit drain (2 s) + its exit report bound (1 s) + a live session_stop
# that terminates the launched app (3 s TERM grace + 2 s KILL grace).
EXIT_BOUND_SECONDS = 2.0 + 1.0 + 3.0 + 2.0
# QA-WIN-044 (b) and (c): how long the client leaves the prompt unanswered.
UNANSWERED_BEFORE_CANCEL_SECONDS = 10.0
UNANSWERED_BEFORE_EOF_SECONDS = 5.0


@pytest.fixture
def anyio_backend() -> str:
    """The MCP stdio clients run on asyncio."""
    return "asyncio"


# ── expected texts ───────────────────────────────────────────────────────


def _requested_text(app_id: str, caption: str, window_id: str) -> str:
    return (
        f'Close requested: {app_id} "{caption}" ({window_id}). The app may keep the window '
        "open, for example to ask about unsaved changes; check window_list."
    )


def _not_confirmed_text(window_id: str) -> str:
    return f"window_close not performed: the user did not confirm closing window {window_id}."


def _declined_text(window_id: str) -> str:
    return (
        f"window_close not performed: the user declined closing window {window_id}. "
        "Do not retry unless the user asks."
    )


def _cancelled_text(window_id: str) -> str:
    return (
        f"window_close not performed: the confirmation for window {window_id} was dismissed "
        "or never shown (a client with no one to answer cancels it automatically). If the "
        "user asked to close this window, close it through the app's own UI instead."
    )


def _no_window_text(window_id: str) -> str:
    return f"No window with id '{window_id}'."


def _not_closeable_text(app_id: str, caption: str, window_id: str) -> str:
    return f'Window {app_id} "{caption}" ({window_id}) cannot be closed.'


def _prompt_message(window_id: str, app_id: str) -> str:
    return (
        f"kwin-mcp wants to close window {window_id} ({app_id}) on your desktop. "
        "Unsaved work in that window may be lost."
    )


def _assert_form_request(params: dict[str, Any], window_id: str, app_id: str) -> None:
    """A form request naming the window id and app id, with the one-checkbox schema."""
    # Spec 2025-11-25: a request without mode is a form request.
    assert params.get("mode", "form") == "form", params
    assert params["message"] == _prompt_message(window_id, app_id), params
    schema = params["requestedSchema"]
    assert schema.get("type") == "object", schema
    assert schema["properties"] == CONFIRM_PROPERTIES, schema
    assert schema.get("required") == ["confirm"], schema


def _wire(params: ElicitRequestParams) -> dict[str, Any]:
    return params.model_dump(mode="json", by_alias=True, exclude_none=True)


# ── oracle helpers (blocking KWin scripts run off the event loop) ───────


async def _oracle_window(oracle: KWinOracle, window_id: str) -> OracleWindow | None:
    return await anyio.to_thread.run_sync(oracle.window, window_id)


async def _wait_windows(
    oracle: KWinOracle,
    predicate: Callable[[list[OracleWindow]], bool],
    timeout_s: float = WINDOW_TIMEOUT_SECONDS,
) -> list[OracleWindow]:
    return await anyio.to_thread.run_sync(
        lambda: oracle.wait_for_windows(predicate, timeout_s=timeout_s)
    )


def _of_app(windows: list[OracleWindow], app_id: str) -> list[OracleWindow]:
    return [w for w in windows if w.resource_class == app_id]


async def _await_single_window(oracle: KWinOracle, app_id: str) -> OracleWindow:
    """Wait until exactly one ``app_id`` window exists; return it."""
    windows = await _wait_windows(oracle, lambda ws: len(_of_app(ws, app_id)) == 1)
    return _of_app(windows, app_id)[0]


async def _assert_window_survives(oracle: KWinOracle, window_id: str) -> None:
    await anyio.sleep(SETTLE_SECONDS)
    assert await _oracle_window(oracle, window_id) is not None, window_id


async def _await_window_gone(oracle: KWinOracle, window_id: str) -> None:
    await _wait_windows(oracle, lambda ws: all(w.id != window_id for w in ws))


# ── tool calls ───────────────────────────────────────────────────────────


async def _call(
    client: ToolClient, name: str, arguments: dict[str, Any] | None = None
) -> tuple[dict[str, Any], str]:
    """``call_structured`` (schema + render checked) within the call bound."""
    with anyio.fail_after(SESSION_TIMEOUT_SECONDS):
        return await client.call_structured(name, arguments)


def _raw_text(result: dict[str, Any]) -> str:
    return "\n".join(item["text"] for item in result["content"] if item["type"] == "text")


async def _raw_call(
    raw: RawJsonRpcClient, name: str, arguments: dict[str, Any] | None = None
) -> tuple[dict[str, Any], str]:
    """The final structured result of a raw ``tools/call``; ``isError`` fails."""
    with anyio.fail_after(SESSION_TIMEOUT_SECONDS):
        result = await raw.call_tool(name, arguments)
    text = _raw_text(result)
    assert not result.get("isError"), text or result
    assert MISSING_CAPABILITY not in json.dumps(result), result
    return result["structuredContent"], text


async def _raw_once(
    raw: RawJsonRpcClient,
    arguments: dict[str, Any],
    *,
    input_responses: dict[str, Any] | None = None,
    request_state: str | None = None,
) -> dict[str, Any]:
    with anyio.fail_after(CALL_TIMEOUT_SECONDS):
        return await raw.call_tool_once(
            "window_close",
            arguments,
            input_responses=input_responses,
            request_state=request_state,
        )


def _only_prompt(result: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The key and params of the one elicitation an input-required result carries."""
    assert result.get("resultType") == "input_required", result
    requests = result.get("inputRequests") or {}
    assert len(requests) == 1, requests
    [(key, request)] = requests.items()
    assert request["method"] == ELICITATION_METHOD, request
    return key, request["params"]


def _close_args(window_id: str) -> dict[str, Any]:
    return {"window": {"id": window_id}}


async def _launch_kcalc(call: Caller, oracle: KWinOracle) -> tuple[OracleWindow, int]:
    """Launch kcalc through app_launch; return its one window (from KWin) and pid."""
    launched, _ = await call("app_launch", {"command": KCALC})
    window = await _await_single_window(oracle, KCALC_APP_ID)
    return window, int(launched["pid"])


# ── sessions ─────────────────────────────────────────────────────────────


def _connect_args(live: LiveKWin) -> dict[str, Any]:
    return {"dbus_address": live.dbus_address, "wayland_display": live.wayland_display}


def _live_env(live: LiveKWin) -> dict[str, str]:
    return {"DBUS_SESSION_BUS_ADDRESS": live.dbus_address}


@asynccontextmanager
async def _connected(
    era: str,
    live: LiveKWin,
    user: ElicitationFnT | None,
    *flags: str,
) -> AsyncGenerator[ToolClient]:
    """An SDK client of ``era`` attached to ``live`` (session_stop on exit)."""
    async with running_client(
        era, *flags, env=_live_env(live), elicitation_callback=user
    ) as client:
        info, _ = await _call(client, "session_connect", _connect_args(live))
        assert info["kind"] == "live", info
        try:
            yield client
        finally:
            await _call(client, "session_stop")


@asynccontextmanager
async def _live_client(
    era: str,
    user: ElicitationFnT | None,
    *flags: str,
) -> AsyncGenerator[tuple[ToolClient, LiveKWin, KWinOracle]]:
    """A test-owned live KWin and an SDK client of ``era`` attached to it."""
    with live_kwin() as live:
        async with _connected(era, live, user, *flags) as client:
            yield client, live, live.oracle()


@asynccontextmanager
async def _virtual_client(
    era: str,
    user: ElicitationFnT | None,
    *flags: str,
    home: str = "host",
) -> AsyncGenerator[tuple[ToolClient, dict[str, Any], KWinOracle]]:
    """An SDK client of ``era`` with a virtual session; the oracle reads its KWin."""
    async with running_client(era, *flags, elicitation_callback=user) as client:
        info, _ = await _call(
            client, "session_start", {"screen_width": 1280, "screen_height": 800, "home": home}
        )
        assert info["kind"] == "virtual", info
        try:
            yield client, info, KWinOracle(info["dbus_address"])
        finally:
            await _call(client, "session_stop")


@asynccontextmanager
async def _raw_connected(
    capabilities: dict[str, Any], era: str, live: LiveKWin, *flags: str
) -> AsyncGenerator[RawJsonRpcClient]:
    """A raw client declaring exactly ``capabilities``, attached to ``live``."""
    async with raw_mcp_client(capabilities, *flags, era=era, env=_live_env(live)) as raw:
        info, _ = await _raw_call(raw, "session_connect", _connect_args(live))
        assert info["kind"] == "live", info
        try:
            yield raw
        finally:
            await _raw_call(raw, "session_stop")


# ── ported ledger: tests/e2e/test_window_close_elicitation.py (PR #87) ──


@pytest.mark.qa("QA-WIN-038")
@pytest.mark.anyio
@pytest.mark.parametrize("era", ERAS)
async def test_live_window_close_without_elicitation_is_refused(era: str) -> None:
    """A client without the elicitation capability is never asked and gets the refusal."""
    async with _live_client(era, None) as (client, _, oracle):
        window, _ = await _launch_kcalc(partial(_call, client), oracle)
        result, text = await _call(client, "window_close", _close_args(window.id))
        assert text == LIVE_REFUSAL
        assert (result["requested"], result["consent"], result["refusal"]) == (
            False,
            "unsupported",
            "no_consent",
        ), result
        await _assert_window_survives(oracle, window.id)


@pytest.mark.qa("QA-WIN-035")
@pytest.mark.qa("QA-WIN-036")
@pytest.mark.qa("QA-WIN-037")
@pytest.mark.qa("QA-WIN-039")
@pytest.mark.anyio
@pytest.mark.parametrize("era", ERAS)
async def test_live_window_close_asks_and_honors_each_answer(era: str) -> None:
    """Every answer but confirm keeps the window; confirm closes it.

    Also covers 0.11's test_window_close_is_refused_in_live_sessions: no live
    close happens without the user's confirmation.
    """
    user = ScriptedUser()
    async with _live_client(era, user) as (client, _, oracle):
        window, _ = await _launch_kcalc(partial(_call, client), oracle)
        window_id = window.id
        not_closed = (
            ("unconfirmed", _not_confirmed_text(window_id)),
            ("decline", _declined_text(window_id)),
            ("cancel", _cancelled_text(window_id)),
        )
        for answer, expected in not_closed:
            asked = len(user.requests)
            user.answer = answer
            _, reply = await _call(client, "window_close", _close_args(window_id))
            assert reply == expected
            assert len(user.requests) == asked + 1, (answer, user.requests)
            _assert_form_request(_wire(user.requests[-1]), window_id, KCALC_APP_ID)
            await _assert_window_survives(oracle, window_id)

        # Nothing to confirm for an id that names no window.
        asked = len(user.requests)
        _, reply = await _call(client, "window_close", _close_args(UNKNOWN_ID))
        assert reply == _no_window_text(UNKNOWN_ID)
        assert len(user.requests) == asked, user.requests

        user.answer = "confirm"
        _, closed = await _call(client, "window_close", _close_args(window_id))
        assert closed == _requested_text(KCALC_APP_ID, window.caption, window_id)
        assert len(user.requests) == asked + 1, user.requests
        _assert_form_request(_wire(user.requests[-1]), window_id, KCALC_APP_ID)
        await _await_window_gone(oracle, window_id)


@pytest.mark.anyio
@pytest.mark.parametrize("era", ERAS)
async def test_virtual_window_close_does_not_ask(era: str) -> None:
    user = ScriptedUser()
    async with _virtual_client(era, user) as (client, _, oracle):
        window, _ = await _launch_kcalc(partial(_call, client), oracle)
        _, closed = await _call(client, "window_close", _close_args(window.id))
        assert closed == _requested_text(KCALC_APP_ID, window.caption, window.id)
        assert user.requests == []
        await _await_window_gone(oracle, window.id)


# Real clients declare elicitation in shapes the SDK clients never send: Claude
# Code sends a bare {} (form mode only, per spec 2025-11-25), Codex sends both
# modes, and a url-only client must not get a form.


@pytest.mark.qa("QA-WIN-038")
@pytest.mark.anyio
@pytest.mark.parametrize("era", ERAS)
@NO_FORM_CAPABILITIES
async def test_raw_client_without_form_elicitation_is_refused_unasked(
    capabilities: dict[str, Any], era: str
) -> None:
    with live_kwin() as live:
        oracle = live.oracle()
        async with _raw_connected(capabilities, era, live) as raw:
            window, _ = await _launch_kcalc(partial(_raw_call, raw), oracle)
            _, reply = await _raw_call(raw, "window_close", _close_args(window.id))
            assert reply == LIVE_REFUSAL
            assert raw.elicitations() == []
            await _assert_window_survives(oracle, window.id)


@pytest.mark.qa("QA-WIN-038")
@pytest.mark.anyio
@pytest.mark.parametrize("era", ERAS)
async def test_raw_bare_elicitation_capability_is_asked_and_decline_keeps_window(
    era: str,
) -> None:
    """Claude Code's shape: a bare elicitation capability means form mode."""
    with live_kwin() as live:
        oracle = live.oracle()
        async with _raw_connected({"elicitation": {}}, era, live) as raw:
            window, _ = await _launch_kcalc(partial(_raw_call, raw), oracle)
            raw.elicitation_reply = elicit_reply("decline")
            _, reply = await _raw_call(raw, "window_close", _close_args(window.id))
            assert reply == _declined_text(window.id)
            [request] = raw.elicitations()
            _assert_form_request(request["params"], window.id, KCALC_APP_ID)
            await _assert_window_survives(oracle, window.id)


@pytest.mark.qa("QA-WIN-038")
@pytest.mark.anyio
@pytest.mark.parametrize("era", ERAS)
async def test_raw_form_and_url_capability_is_asked_and_confirm_closes_window(
    era: str,
) -> None:
    """Codex's shape: both modes declared, so the form request is sent."""
    capabilities = {"elicitation": {"form": {}, "url": {}}}
    with live_kwin() as live:
        oracle = live.oracle()
        async with _raw_connected(capabilities, era, live) as raw:
            window, _ = await _launch_kcalc(partial(_raw_call, raw), oracle)
            raw.elicitation_reply = elicit_reply("confirm")
            _, closed = await _raw_call(raw, "window_close", _close_args(window.id))
            assert closed == _requested_text(KCALC_APP_ID, window.caption, window.id)
            [request] = raw.elicitations()
            _assert_form_request(request["params"], window.id, KCALC_APP_ID)
            await _await_window_gone(oracle, window.id)


# ── QA list ──────────────────────────────────────────────────────────────


@pytest.mark.qa("QA-GATE-011")
@pytest.mark.anyio
@pytest.mark.parametrize("era", ERAS)
async def test_injected_consent_argument_rejected(era: str) -> None:
    """``consent`` is an unknown key, top level or nested: rejected before any prompt.

    The scripted user would confirm, so a prompt that slipped through would close
    the window.
    """
    injections = (
        {"window": {"id": "<id>"}, "consent": FORGED_CONSENT},
        {"window": {"id": "<id>", "consent": FORGED_CONSENT}},
    )

    async def assert_rejected(client: ToolClient, window_id: str, user: ScriptedUser) -> None:
        top_level, nested = (
            json.loads(json.dumps(args).replace("<id>", window_id)) for args in injections
        )
        with anyio.fail_after(CALL_TIMEOUT_SECONDS):
            lines = await client.call_tool_error("window_close", top_level)
        assert lines == (
            "Error executing tool window_close: Unknown argument 'consent' for window_close.",
            "Valid arguments: window, capture.",
        ), lines
        with anyio.fail_after(CALL_TIMEOUT_SECONDS):
            lines = await client.call_tool_error("window_close", nested, lines=None)
        assert "consent" in "\n".join(lines), lines
        assert user.requests == []

    live_user = ScriptedUser("confirm")
    async with _live_client(era, live_user) as (client, _, oracle):
        window, _ = await _launch_kcalc(partial(_call, client), oracle)
        await assert_rejected(client, window.id, live_user)
        await _assert_window_survives(oracle, window.id)

    virtual_user = ScriptedUser("confirm")
    async with _virtual_client(era, virtual_user) as (client, _, oracle):
        window, _ = await _launch_kcalc(partial(_call, client), oracle)
        await assert_rejected(client, window.id, virtual_user)
        await _assert_window_survives(oracle, window.id)


RULED_CAPTION = "Zoo Refuser"


async def _ruled_not_closeable(
    oracle: KWinOracle, zoo: ZooProcess, config_home: Path, dbus_address: str
) -> str:
    """Open the zoo's refuser window and force it non-closeable with a KWin rule."""
    await anyio.to_thread.run_sync(zoo.command, "refuser", f"window: {RULED_CAPTION}")
    rule = WindowRule(
        description="kwin-mcp QA-WIN-033",
        wmclass=ZOO_APP_ID,
        title=RULED_CAPTION,
        closeable=False,
    )
    await anyio.to_thread.run_sync(install_rules, config_home, dbus_address, [rule])

    def ruled(windows: list[OracleWindow]) -> bool:
        matches = [w for w in _of_app(windows, ZOO_APP_ID) if w.caption == RULED_CAPTION]
        return len(matches) == 1 and not matches[0].closeable

    windows = await _wait_windows(oracle, ruled)
    return next(w.id for w in _of_app(windows, ZOO_APP_ID) if w.caption == RULED_CAPTION)


async def _assert_not_closeable_refusal(
    client: ToolClient, oracle: KWinOracle, window_id: str, user: ScriptedUser
) -> None:
    result, text = await _call(client, "window_close", _close_args(window_id))
    assert text == _not_closeable_text(ZOO_APP_ID, RULED_CAPTION, window_id)
    assert (result["requested"], result["refusal"]) == (False, "not_closeable"), result
    assert user.requests == []
    await _assert_window_survives(oracle, window_id)


@pytest.mark.qa("QA-WIN-033")
@pytest.mark.anyio
async def test_closeable_false_is_non_error_refusal_not_closeable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window KWin reports non-closeable is a non-error refusal; nobody is asked."""
    # Virtual: the isolated home's .config is the virtual KWin's XDG_CONFIG_HOME.
    virtual_user = ScriptedUser("confirm")
    async with _virtual_client(LEGACY_PROTOCOL_VERSION, virtual_user, home="isolated") as (
        client,
        info,
        oracle,
    ):
        with ZooProcess(
            wayland_display=info["wayland_socket"], dbus_address=info["dbus_address"]
        ) as zoo:
            zoo.expect(f"window: {ZOO_CAPTION}")
            config_home = Path(info["home_dir"]) / ".config"
            window_id = await _ruled_not_closeable(oracle, zoo, config_home, info["dbus_address"])
            await _assert_not_closeable_refusal(client, oracle, window_id, virtual_user)

    # Live: live_kwin() copies os.environ, so its KWin reads rules from here.
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    live_user = ScriptedUser("confirm")
    async with _live_client(LEGACY_PROTOCOL_VERSION, live_user) as (client, live, oracle):
        with ZooProcess(
            wayland_display=live.wayland_display, dbus_address=live.dbus_address
        ) as zoo:
            zoo.expect(f"window: {ZOO_CAPTION}")
            window_id = await _ruled_not_closeable(oracle, zoo, config_home, live.dbus_address)
            await _assert_not_closeable_refusal(client, oracle, window_id, live_user)


@pytest.mark.qa("QA-WIN-035")
@pytest.mark.anyio
@pytest.mark.parametrize("era", ERAS)
async def test_live_elicit_accept_decline_cancel_unconfirmed(era: str) -> None:
    """Accept + confirm closes the window; the question is verbatim and stable."""
    with live_kwin() as live:
        oracle = live.oracle()
        async with _raw_connected({"elicitation": {"form": {}}}, era, live) as raw:
            window, _ = await _launch_kcalc(partial(_raw_call, raw), oracle)
            # A name selector: the prompt names the resolved id, never the caption.
            arguments = {"window": {"app_id": "kcalc", "caption": window.caption}}
            if era == LEGACY_PROTOCOL_VERSION:
                raw.elicitation_reply = elicit_reply("confirm")
                result, text = await _raw_call(raw, "window_close", arguments)
                [request] = raw.elicitations()
                _assert_form_request(request["params"], window.id, KCALC_APP_ID)
            else:
                first = await _raw_once(raw, arguments)
                key, params = _only_prompt(first)
                _assert_form_request(params, window.id, KCALC_APP_ID)
                # The same question on a fresh call renders identically (stable digest).
                again = await _raw_once(raw, arguments)
                assert _only_prompt(again) == (key, params), (first, again)
                assert await _oracle_window(oracle, window.id) is not None
                final = await _raw_once(
                    raw,
                    arguments,
                    input_responses={key: elicit_reply("confirm")},
                    request_state=first["requestState"],
                )
                assert final.get("resultType", "complete") == "complete", final
                assert not final.get("isError"), final
                result, text = final["structuredContent"], _raw_text(final)
            assert text == _requested_text(KCALC_APP_ID, window.caption, window.id)
            assert (result["requested"], result["consent"], result["window_id"]) == (
                True,
                "accepted",
                window.id,
            ), result
            await _await_window_gone(oracle, window.id)


@pytest.mark.qa("QA-WIN-036")
@pytest.mark.anyio
@pytest.mark.parametrize("era", ERAS)
async def test_live_accepted_unchecked_not_confirmed(era: str) -> None:
    user = ScriptedUser("unconfirmed")
    async with _live_client(era, user) as (client, _, oracle):
        window, pid = await _launch_kcalc(partial(_call, client), oracle)
        result, text = await _call(client, "window_close", _close_args(window.id))
        assert text == _not_confirmed_text(window.id)
        assert (result["requested"], result["consent"]) == (False, "not_confirmed"), result
        assert len(user.requests) == 1, user.requests
        await _assert_window_survives(oracle, window.id)
        assert Path(f"/proc/{pid}").exists(), pid


@pytest.mark.qa("QA-WIN-037")
@pytest.mark.anyio
@pytest.mark.parametrize("era", ERAS)
async def test_live_decline_and_cancel_texts(era: str) -> None:
    user = ScriptedUser()
    async with _live_client(era, user) as (client, _, oracle):
        window, _ = await _launch_kcalc(partial(_call, client), oracle)
        for answer, consent, expected in (
            ("decline", "declined", _declined_text(window.id)),
            ("cancel", "cancelled", _cancelled_text(window.id)),
        ):
            user.answer = answer
            result, text = await _call(client, "window_close", _close_args(window.id))
            assert text == expected
            assert (result["requested"], result["consent"]) == (False, consent), result
            await _assert_window_survives(oracle, window.id)
        assert "Do not retry" not in _cancelled_text(window.id)
        assert len(user.requests) == 2, user.requests


@pytest.mark.qa("QA-WIN-038")
@pytest.mark.timeout(300)
@pytest.mark.anyio
@pytest.mark.parametrize("era", ERAS)
async def test_live_client_without_form_elicitation_is_refused_unasked(era: str) -> None:
    """Only a form-capable client is asked; the others get the refusal unasked."""
    shapes: tuple[tuple[dict[str, Any], bool], ...] = (
        ({}, False),
        ({"elicitation": {}}, True),
        ({"elicitation": {"url": {}}}, False),
        ({"elicitation": {"form": {}, "url": {}}}, True),
        ({"elicitation": {"form": {}}}, True),
    )
    with (
        live_kwin() as live,
        ZooProcess(wayland_display=live.wayland_display, dbus_address=live.dbus_address) as zoo,
    ):
        zoo.expect(f"window: {ZOO_CAPTION}")
        oracle = live.oracle()
        window = await _await_single_window(oracle, ZOO_APP_ID)
        for capabilities, asks in shapes:
            async with _raw_connected(capabilities, era, live) as raw:
                # A prompt is declined, so the window stays for the next shape.
                raw.elicitation_reply = elicit_reply("decline")
                result, text = await _raw_call(raw, "window_close", _close_args(window.id))
                if asks:
                    [request] = raw.elicitations()
                    _assert_form_request(request["params"], window.id, ZOO_APP_ID)
                    assert result["consent"] == "declined", (capabilities, result)
                else:
                    assert raw.elicitations() == [], capabilities
                    assert text == LIVE_REFUSAL, capabilities
                    assert (result["consent"], result["refusal"]) == (
                        "unsupported",
                        "no_consent",
                    ), (capabilities, result)
                assert result["requested"] is False, (capabilities, result)
            await _assert_window_survives(oracle, window.id)


@pytest.mark.qa("QA-WIN-039")
@pytest.mark.anyio
@pytest.mark.parametrize("era", ERAS)
async def test_live_unknown_id_no_window_without_prompt(era: str) -> None:
    user = ScriptedUser("confirm")
    async with _live_client(era, user) as (client, _, _):
        result, text = await _call(client, "window_close", _close_args(UNKNOWN_ID))
        assert text == _no_window_text(UNKNOWN_ID)
        assert (result["requested"], result["refusal"], result.get("consent")) == (
            False,
            "no_window",
            None,
        ), result
        assert user.requests == []


async def _close_out_of_band_and_reopen(
    oracle: KWinOracle, zoo: ZooProcess, window_id: str
) -> OracleWindow:
    """Close the zoo's main window through KWin, reopen it, return the new window."""

    def swap() -> None:
        start = len(zoo.lines)
        assert oracle.close(window_id)
        zoo.expect(f"closed: {ZOO_CAPTION}", start=start)
        zoo.command("reopen", f"window: {ZOO_CAPTION}")

    await anyio.to_thread.run_sync(swap)
    windows = await _wait_windows(
        oracle,
        lambda ws: (
            [w.id for w in _of_app(ws, ZOO_APP_ID)] not in ([], [window_id])
            and len(_of_app(ws, ZOO_APP_ID)) == 1
        ),
    )
    replacement = _of_app(windows, ZOO_APP_ID)[0]
    assert replacement.id != window_id, (replacement, window_id)
    assert replacement.caption == ZOO_CAPTION, replacement
    return replacement


@pytest.mark.qa("QA-WIN-040")
@pytest.mark.timeout(300)
@pytest.mark.anyio
@pytest.mark.parametrize("era", ERAS)
async def test_toctou_recheck_closes_only_resolved_id(era: str) -> None:
    """A window replaced while the question is open is never closed by that answer."""
    selector = {"window": {"app_id": ZOO_APP_ID, "kind": ["normal"]}}
    with (
        live_kwin() as live,
        ZooProcess(wayland_display=live.wayland_display, dbus_address=live.dbus_address) as zoo,
    ):
        zoo.expect(f"window: {ZOO_CAPTION}")
        oracle = live.oracle()
        first = await _await_single_window(oracle, ZOO_APP_ID)

        if era == LEGACY_PROTOCOL_VERSION:
            replaced: list[OracleWindow] = []
            requests: list[dict[str, Any]] = []

            async def user(
                context: ClientRequestContext, params: ElicitRequestParams
            ) -> ElicitResult | ErrorData:
                requests.append(_wire(params))
                replaced.append(await _close_out_of_band_and_reopen(oracle, zoo, first.id))
                return ELICIT_ANSWERS["confirm"]

            async with _connected(era, live, user) as client:
                result, text = await _call(client, "window_close", selector)
            [request] = requests
            _assert_form_request(request, first.id, ZOO_APP_ID)
            [second] = replaced
            assert text == _no_window_text(first.id)
            assert (result["requested"], result["refusal"]) == (False, "no_window"), result
            await _assert_window_survives(oracle, second.id)
            windows = await anyio.to_thread.run_sync(oracle.windows)
            assert [w.id for w in _of_app(windows, ZOO_APP_ID)] == [second.id], windows
            return

        # 2026-07-28: round 2 re-resolves the selector and asks again about the
        # replacement; the answer given for the first window is discarded.
        async with _raw_connected({"elicitation": {"form": {}}}, era, live) as raw:
            target = first
            for final_answer in ("decline", "confirm"):
                round_one = await _raw_once(raw, selector)
                key, params = _only_prompt(round_one)
                _assert_form_request(params, target.id, ZOO_APP_ID)
                replacement = await _close_out_of_band_and_reopen(oracle, zoo, target.id)
                round_two = await _raw_once(
                    raw,
                    selector,
                    input_responses={key: elicit_reply("confirm")},
                    request_state=round_one["requestState"],
                )
                key_two, params_two = _only_prompt(round_two)
                _assert_form_request(params_two, replacement.id, ZOO_APP_ID)
                assert params_two["message"] != params["message"]
                # Nothing is closed until the new question is answered.
                await _assert_window_survives(oracle, replacement.id)
                final = await _raw_once(
                    raw,
                    selector,
                    input_responses={key_two: elicit_reply(final_answer)},
                    request_state=round_two["requestState"],
                )
                assert final.get("resultType", "complete") == "complete", final
                assert not final.get("isError"), final
                result = final["structuredContent"]
                assert result["window_id"] == replacement.id, result
                if final_answer == "decline":
                    assert _raw_text(final) == _declined_text(replacement.id)
                    assert (result["requested"], result["consent"]) == (False, "declined")
                    await _assert_window_survives(oracle, replacement.id)
                else:
                    assert _raw_text(final) == _requested_text(
                        ZOO_APP_ID, ZOO_CAPTION, replacement.id
                    )
                    assert (result["requested"], result["consent"]) == (True, "accepted")
                    await _await_window_gone(oracle, replacement.id)
                target = replacement


@pytest.mark.qa("QA-WIN-043")
@pytest.mark.anyio
@pytest.mark.parametrize("era", ERAS)
async def test_live_consent_refuse_flag_never_asks(era: str) -> None:
    live_user = ScriptedUser("confirm")
    async with _live_client(era, live_user, "--live-consent=refuse") as (client, _, oracle):
        window, _ = await _launch_kcalc(partial(_call, client), oracle)
        result, text = await _call(client, "window_close", _close_args(window.id))
        assert text == LIVE_REFUSAL
        assert (result["requested"], result["consent"], result["refusal"]) == (
            False,
            "unsupported",
            "no_consent",
        ), result
        assert live_user.requests == []
        await _assert_window_survives(oracle, window.id)

    virtual_user = ScriptedUser("confirm")
    async with _virtual_client(era, virtual_user, "--live-consent=refuse") as (
        client,
        _,
        oracle,
    ):
        window, _ = await _launch_kcalc(partial(_call, client), oracle)
        result, text = await _call(client, "window_close", _close_args(window.id))
        assert text == _requested_text(KCALC_APP_ID, window.caption, window.id)
        assert (result["requested"], result["consent"]) == (True, "not_required"), result
        assert virtual_user.requests == []
        await _await_window_gone(oracle, window.id)


# Raw protocol access for QA-WIN-044: a request whose prompt is never answered.


async def _send_request(raw: RawJsonRpcClient, method: str, params: dict[str, Any]) -> int:
    raw._next_id += 1
    request_id = raw._next_id
    await raw._send(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": raw._with_meta(params)}
    )
    return request_id


async def _receive(raw: RawJsonRpcClient) -> dict[str, Any]:
    return json.loads(await raw._stdout.receive_until(b"\n", MAX_LINE_BYTES))


async def _open_unanswered_prompt(
    raw: RawJsonRpcClient, era: str, window_id: str
) -> tuple[int, list[dict[str, Any]]]:
    """Send window_close and read until its prompt arrives; never answer it.

    Returns the request id and every message read. In the handshake era the
    prompt is a pending ``elicitation/create``; in 2026-07-28 it is the call's own
    input-required result, which the client simply never retries.
    """
    call_id = await _send_request(
        raw, "tools/call", {"name": "window_close", "arguments": _close_args(window_id)}
    )
    seen: list[dict[str, Any]] = []
    with anyio.fail_after(CALL_TIMEOUT_SECONDS):
        while True:
            message = await _receive(raw)
            seen.append(message)
            if era == LEGACY_PROTOCOL_VERSION and message.get("method") == ELICITATION_METHOD:
                _assert_form_request(message["params"], window_id, KCALC_APP_ID)
                return call_id, seen
            if era == MODERN_PROTOCOL_VERSION and message.get("id") == call_id:
                _, params = _only_prompt(message["result"])
                _assert_form_request(params, window_id, KCALC_APP_ID)
                return call_id, seen
            assert message.get("id") != call_id, message


async def _messages_within(raw: RawJsonRpcClient, seconds: float) -> list[dict[str, Any]]:
    """Every message the server sends in the next ``seconds`` (at least that long)."""
    seen: list[dict[str, Any]] = []
    with anyio.move_on_after(seconds):
        while True:
            seen.append(await _receive(raw))
    return seen


def _process_gone(pid: int) -> bool:
    """No such process, or only a zombie no one reaped yet."""
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    state = next(line for line in status.splitlines() if line.startswith("State:"))
    return state.split()[1] in {"Z", "X"}


@pytest.mark.qa("QA-WIN-044")
@pytest.mark.timeout(300)
@pytest.mark.anyio
@pytest.mark.parametrize("era", ERAS)
async def test_no_server_side_consent_timeout(era: str) -> None:
    """(b) A prompt left open is never answered by the server; the client's cancel
    leaves the window and the server serving. (c) stdin EOF with a prompt open
    exits within the bound and leaves no launched app behind. (a) is a unit test."""
    capabilities = {"elicitation": {"form": {}}}
    with live_kwin() as live:
        oracle = live.oracle()

        # (b) unanswered, then cancelled by the client.
        async with _raw_connected(capabilities, era, live) as raw:
            window, _ = await _launch_kcalc(partial(_raw_call, raw), oracle)
            call_id, _ = await _open_unanswered_prompt(raw, era, window.id)
            started = time.monotonic()
            meanwhile = await _messages_within(raw, UNANSWERED_BEFORE_CANCEL_SECONDS)
            assert time.monotonic() - started >= UNANSWERED_BEFORE_CANCEL_SECONDS
            # No server-side timeout: the call is still open, nothing was answered.
            assert [m for m in meanwhile if m.get("id") == call_id] == [], meanwhile
            assert await _oracle_window(oracle, window.id) is not None
            await raw._send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": call_id, "reason": "user walked away"},
                }
            )
            list_id = await _send_request(raw, "tools/call", {"name": "desktop_list"})
            with anyio.fail_after(CALL_TIMEOUT_SECONDS):
                while True:
                    message = await _receive(raw)
                    if message.get("id") == call_id:
                        # A late reply to the cancelled call must not be a close.
                        closed = message.get("result", {}).get("structuredContent") or {}
                        assert closed.get("requested") is not True, message
                    if message.get("id") == list_id:
                        break
            assert "error" not in message, message
            assert not message["result"].get("isError"), message
            assert message["result"]["structuredContent"]["desktops"], message
            await _assert_window_survives(oracle, window.id)

        # (c) unanswered, then stdin EOF.
        async with raw_mcp_client(capabilities, era=era, env=_live_env(live)) as raw:
            info, _ = await _raw_call(raw, "session_connect", _connect_args(live))
            assert info["kind"] == "live", info
            launched, _ = await _raw_call(raw, "app_launch", {"command": KCALC})
            pid = int(launched["pid"])
            windows = await _wait_windows(
                oracle, lambda ws: any(w.pid == pid for w in _of_app(ws, KCALC_APP_ID))
            )
            [window] = [w for w in _of_app(windows, KCALC_APP_ID) if w.pid == pid]
            await _open_unanswered_prompt(raw, era, window.id)
            await anyio.sleep(UNANSWERED_BEFORE_EOF_SECONDS)
            await raw._stdin.aclose()
            eof_at = time.monotonic()
            with anyio.fail_after(EXIT_BOUND_SECONDS):
                try:
                    while True:
                        await _receive(raw)
                except (anyio.EndOfStream, anyio.IncompleteRead):
                    pass
                while not _process_gone(pid):
                    await anyio.sleep(0.1)
            assert time.monotonic() - eof_at <= EXIT_BOUND_SECONDS
            # The compositor is the user's: kwin-mcp leaves it running.
            assert live.process.poll() is None


@pytest.mark.qa("QA-WIN-045")
@pytest.mark.anyio
async def test_pending_prompt_does_not_block_tool_thread() -> None:
    """Reads complete while the user is still looking at the question."""
    events: list[str] = []
    prompted = anyio.Event()
    release = anyio.Event()
    requests: list[dict[str, Any]] = []

    async def user(
        context: ClientRequestContext, params: ElicitRequestParams
    ) -> ElicitResult | ErrorData:
        requests.append(_wire(params))
        events.append("prompt")
        prompted.set()
        await release.wait()
        events.append("answer")
        return ELICIT_ANSWERS["confirm"]

    async with _live_client(LEGACY_PROTOCOL_VERSION, user) as (client, _, oracle):
        window, _ = await _launch_kcalc(partial(_call, client), oracle)
        closed: list[tuple[dict[str, Any], str]] = []

        async def close() -> None:
            closed.append(await _call(client, "window_close", _close_args(window.id)))
            events.append("closed")

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(close)
            with anyio.fail_after(CALL_TIMEOUT_SECONDS):
                await prompted.wait()
            desktops, _ = await _call(client, "desktop_list")
            assert desktops["desktops"], desktops
            events.append("desktop_list")
            listed, _ = await _call(client, "window_list")
            assert window.id in [w["id"] for w in listed["windows"]], listed
            events.append("window_list")
            events.append("release")
            release.set()

        assert events == ["prompt", "desktop_list", "window_list", "release", "answer", "closed"]
        [(result, text)] = closed
        assert text == _requested_text(KCALC_APP_ID, window.caption, window.id)
        assert (result["requested"], result["consent"]) == (True, "accepted"), result
        [request] = requests
        _assert_form_request(request, window.id, KCALC_APP_ID)
        await _await_window_gone(oracle, window.id)


@pytest.mark.qa("QA-WIN-050")
@pytest.mark.anyio
@pytest.mark.parametrize("era", [MODERN_PROTOCOL_VERSION])
async def test_consent_answers_cannot_be_forged_or_replayed(era: str) -> None:
    """Only an answer to the question this call was actually asked closes a window."""
    with (
        live_kwin() as live,
        ZooProcess(wayland_display=live.wayland_display, dbus_address=live.dbus_address) as zoo,
    ):
        zoo.expect(f"window: {ZOO_CAPTION}")
        await anyio.to_thread.run_sync(zoo.command, "same-caption", f"window: {ZOO_CAPTION}")
        oracle = live.oracle()
        windows = await _wait_windows(oracle, lambda ws: len(_of_app(ws, ZOO_APP_ID)) == 2)
        a, b = (w.id for w in _of_app(windows, ZOO_APP_ID))
        accept = elicit_reply("confirm")

        async with _raw_connected({"elicitation": {"form": {}}}, era, live) as raw:
            # (a) Unsolicited answers on a first call: the server asks instead.
            forged = await _raw_once(
                raw, _close_args(a), input_responses={"close_consent": FORGED_CONSENT}
            )
            key, params = _only_prompt(forged)
            _assert_form_request(params, a, ZOO_APP_ID)
            forged_key = await _raw_once(raw, _close_args(a), input_responses={key: accept})
            assert _only_prompt(forged_key) == (key, params), forged_key
            await _assert_window_survives(oracle, a)

            # (b) The round-2 state + accept of a call about A, replayed into a call about B.
            round_one = await _raw_once(raw, _close_args(a))
            key_a, params_a = _only_prompt(round_one)
            _assert_form_request(params_a, a, ZOO_APP_ID)
            captured = ({key_a: accept}, round_one["requestState"])
            closed_a = await _raw_once(
                raw, _close_args(a), input_responses=captured[0], request_state=captured[1]
            )
            assert closed_a["structuredContent"]["requested"] is True, closed_a
            await _await_window_gone(oracle, a)
            with anyio.fail_after(CALL_TIMEOUT_SECONDS):
                replay = await raw.request_message(
                    "tools/call",
                    {
                        "name": "window_close",
                        "arguments": _close_args(b),
                        "inputResponses": captured[0],
                        "requestState": captured[1],
                    },
                )
            # The SDK binds requestState to the call it was issued for, so the replay is
            # either rejected outright or answered with a fresh question about B.
            if "error" in replay:
                assert replay["error"]["data"]["reason"] == "invalid_request_state", replay
            else:
                _, params_b = _only_prompt(replay["result"])
                _assert_form_request(params_b, b, ZOO_APP_ID)
                assert a not in params_b["message"], params_b
            await _assert_window_survives(oracle, b)

            # (c) A requestState with one byte flipped: an error or a re-ask, never a close.
            round_one_b = await _raw_once(raw, _close_args(b))
            key_b, _ = _only_prompt(round_one_b)
            state = round_one_b["requestState"]
            middle = len(state) // 2
            flipped = state[:middle] + ("A" if state[middle] != "A" else "B") + state[middle + 1 :]
            with anyio.fail_after(CALL_TIMEOUT_SECONDS):
                tampered = await raw.request_message(
                    "tools/call",
                    {
                        "name": "window_close",
                        "arguments": _close_args(b),
                        "inputResponses": {key_b: accept},
                        "requestState": flipped,
                    },
                )
            if "error" not in tampered:
                _, params_tampered = _only_prompt(tampered["result"])
                _assert_form_request(params_tampered, b, ZOO_APP_ID)
            await _assert_window_survives(oracle, b)
