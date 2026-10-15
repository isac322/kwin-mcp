"""window_close consent: resolvers, elicitation outcomes and the tool-thread re-check.

The server runs in-process behind an SDK ``Client``; the scripted user is
``FakeElicitation`` and KWin is the fake script runner of the ``fake_engine`` fixture,
so every text below comes from the real engine and the real result model.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
from mcp.client.client import Client
from mcp.types import CallToolResult, ElicitRequestFormParams, ElicitResult, TextContent
from tool_surface import TOOL_NAMES

from kwin_mcp import server as server_module
from kwin_mcp.core import CloseTarget
from kwin_mcp.models import Rect, Window, WindowCloseResult
from kwin_mcp.server import KwinServer, ServerFlags
from kwin_mcp.session import LiveSession, Session

from ._fakes import windows_reply

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.client.session import ClientRequestContext
    from mcp.types import ElicitRequestParams, ErrorData

    from kwin_mcp.core import AutomationEngine

    from ._fakes import ElicitAnswer, FakeElicitation, FakeEngine

WINDOW_ID = "{k1}"
APP_ID = "org.kde.kate"
CAPTION = "notes.txt - Kate"
PROMPT = (
    f"kwin-mcp wants to close window {WINDOW_ID} ({APP_ID}) on your desktop. "
    "Unsaved work in that window may be lost."
)
REQUESTED = (
    f'Close requested: {APP_ID} "{CAPTION}" ({WINDOW_ID}). The app may keep the window '
    "open, for example to ask about unsaved changes; check window_list."
)
NOT_CONFIRMED = f"window_close not performed: the user did not confirm closing window {WINDOW_ID}."
DECLINED = (
    f"window_close not performed: the user declined closing window {WINDOW_ID}. "
    "Do not retry unless the user asks."
)
CANCELLED = (
    f"window_close not performed: the confirmation for window {WINDOW_ID} was dismissed or "
    "never shown (a client with no one to answer cancels it automatically). If the user "
    "asked to close this window, close it through the app's own UI instead."
)
UNSUPPORTED = (
    "window_close is disabled in live sessions: it could discard unsaved work "
    "on the real desktop. Close the window through the app's own UI instead."
)


def _window(*, closeable: bool = True) -> Window:
    return Window(
        id=WINDOW_ID,
        app_id=APP_ID,
        caption=CAPTION,
        kind="normal",
        active=True,
        frame=Rect(x=100, y=100, width=400, height=300),
        client=Rect(x=100, y=130, width=400, height=270),
        moveable=True,
        resizeable=True,
        closeable=closeable,
        desktops=["d1"],
        on_all_desktops=False,
    )


_CLOSE_REPLY: dict[str, object] = {
    "ok": True,
    "result": {"found": True, "closeable": True, "app": APP_ID, "caption": CAPTION},
}


@pytest.fixture
def kwin_desktop(fake_engine: FakeEngine) -> FakeEngine:
    """The fake live engine showing one closeable Kate window."""
    fake_engine.kwin.respond("windows", windows_reply(_window()))
    fake_engine.kwin.respond("close", _CLOSE_REPLY)
    return fake_engine


def _elicitation_callback(fake: FakeElicitation) -> Any:
    async def callback(
        _context: ClientRequestContext, params: ElicitRequestParams
    ) -> ElicitResult | ErrorData:
        assert isinstance(params, ElicitRequestFormParams)
        action, content = fake.answer(params.message, dict(params.requested_schema))
        return ElicitResult.model_validate({"action": action, "content": content})

    return callback


async def _call(
    server: KwinServer,
    arguments: dict[str, Any],
    user: FakeElicitation | None,
    *,
    name: str = "window_close",
    mode: str = "legacy",
) -> CallToolResult:
    callback = None if user is None else _elicitation_callback(user)
    async with Client(server, elicitation_callback=callback, mode=mode) as client:
        return await client.call_tool(name, arguments)


def _run(
    server: KwinServer,
    arguments: dict[str, Any],
    user: FakeElicitation | None,
    **kwargs: str,
) -> CallToolResult:
    return asyncio.run(_call(server, arguments, user, **kwargs))


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _server(engine: AutomationEngine, **flags: Any) -> KwinServer:
    return KwinServer(engine, ServerFlags(**flags))


@pytest.mark.qa("QA-WIN-048")
@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
@pytest.mark.parametrize(
    ("answer", "text", "consent", "closed"),
    [
        ("confirm", REQUESTED, "accepted", True),
        ("unconfirmed", NOT_CONFIRMED, "not_confirmed", False),
        ("decline", DECLINED, "declined", False),
        ("cancel", CANCELLED, "cancelled", False),
    ],
)
def test_consent_outcome_texts_verbatim(
    kwin_desktop: FakeEngine,
    fake_elicitation: FakeElicitation,
    mode: str,
    answer: ElicitAnswer,
    text: str,
    consent: str,
    closed: bool,
) -> None:
    """One prompt naming the exact window; each answer gives its verbatim non-error text."""
    fake_elicitation.queue(answer)
    result = _run(
        _server(kwin_desktop.engine), {"window": {"app_id": "kate"}}, fake_elicitation, mode=mode
    )
    assert not result.is_error
    assert _text(result) == text
    assert result.structured_content is not None
    assert result.structured_content["consent"] == consent
    assert result.structured_content["requested"] is closed
    assert [request.message for request in fake_elicitation.requests] == [PROMPT]
    schema = fake_elicitation.requests[0].requested_schema
    assert schema["required"] == ["confirm"]
    confirm = schema["properties"]
    assert isinstance(confirm, dict)
    assert confirm["confirm"]["type"] == "boolean"
    assert confirm["confirm"]["title"] == "Close this window"
    assert confirm["confirm"]["description"] == "Close it even if it has unsaved work."
    assert ("close" in kwin_desktop.kwin.ops()) is closed


@pytest.mark.qa("QA-WIN-048")
def test_refusals_that_never_prompt(
    kwin_desktop: FakeEngine, fake_elicitation: FakeElicitation
) -> None:
    """No form support, --live-consent=refuse, an unknown id, a not-closeable window and
    a virtual session all answer without asking the user."""
    engine = kwin_desktop.engine

    # The client cannot show a form: the unsupported refusal.
    result = _run(_server(engine), {"window": {"id": WINDOW_ID}}, None)
    assert (result.is_error, _text(result)) == (False, UNSUPPORTED)
    assert result.structured_content is not None
    assert result.structured_content["consent"] == "unsupported"

    # --live-consent=refuse never elicits, even when the client could.
    result = _run(
        _server(engine, live_consent="refuse"), {"window": {"id": WINDOW_ID}}, fake_elicitation
    )
    assert (result.is_error, _text(result)) == (False, UNSUPPORTED)

    # An id-only selector that matches nothing: the no_window refusal, no prompt.
    result = _run(_server(engine), {"window": {"id": "{gone}"}}, fake_elicitation)
    assert (result.is_error, _text(result)) == (False, "No window with id '{gone}'.")

    # A window that cannot be closed.
    kwin_desktop.kwin.respond("windows", windows_reply(_window(closeable=False)))
    result = _run(_server(engine), {"window": {"id": WINDOW_ID}}, fake_elicitation)
    assert (result.is_error, _text(result)) == (
        False,
        f'Window {APP_ID} "{CAPTION}" ({WINDOW_ID}) cannot be closed.',
    )

    assert fake_elicitation.requests == []
    assert "close" not in kwin_desktop.kwin.ops()


@pytest.mark.qa("QA-WIN-048")
def test_virtual_session_closes_without_prompt(
    monkeypatch: pytest.MonkeyPatch, kwin_desktop: FakeEngine, fake_elicitation: FakeElicitation
) -> None:
    """A virtual session needs no consent: the close is requested with not_required."""
    monkeypatch.setattr(Session, "is_running", property(lambda _self: True))
    kwin_desktop.engine._session = Session()
    result = _run(_server(kwin_desktop.engine), {"window": {"id": WINDOW_ID}}, fake_elicitation)
    assert (result.is_error, _text(result)) == (False, REQUESTED)
    assert result.structured_content is not None
    assert result.structured_content["consent"] == "not_required"
    assert fake_elicitation.requests == []
    assert kwin_desktop.kwin.ops().count("close") == 1


@pytest.mark.qa("QA-WIN-048")
def test_shutdown_is_checked_before_any_prompt(
    kwin_desktop: FakeEngine, fake_elicitation: FakeElicitation
) -> None:
    """Once shutdown began, the target lookup does not run and nobody is asked."""
    server_module._shutdown.set()
    try:
        result = _run(_server(kwin_desktop.engine), {"window": {"id": WINDOW_ID}}, fake_elicitation)
    finally:
        server_module._shutdown.clear()
    assert result.is_error
    assert _text(result) == (
        "Error executing tool window_close: Server is shutting down; tool not executed."
    )
    assert fake_elicitation.requests == []
    assert kwin_desktop.kwin.ops() == []


@pytest.mark.qa("QA-WIN-041")
def test_live_recheck_on_tool_thread(
    monkeypatch: pytest.MonkeyPatch, kwin_desktop: FakeEngine, fake_elicitation: FakeElicitation
) -> None:
    """The body re-checks the session on the tool thread; a change since the resolvers
    ran refuses instead of closing."""
    engine = kwin_desktop.engine
    live = engine._session
    assert isinstance(live, LiveSession)
    monkeypatch.setattr(Session, "is_running", property(lambda _self: True))
    real_close = engine._close

    def swap_then_close(session: Session | LiveSession) -> Callable[..., WindowCloseResult]:
        def close(*args: Any, **kwargs: Any) -> WindowCloseResult:
            engine._session = session
            return real_close(*args, **kwargs)

        return close

    # (a) The resolvers saw a virtual session (no prompt); the body sees a live one.
    engine._session = Session()
    monkeypatch.setattr(engine, "_close", swap_then_close(live))
    result = _run(_server(engine), {"window": {"id": WINDOW_ID}}, fake_elicitation)
    assert (result.is_error, _text(result)) == (False, UNSUPPORTED)
    assert fake_elicitation.requests == []
    assert "close" not in kwin_desktop.kwin.ops()

    # (b) The user confirmed on the live session; the body sees it replaced by a
    # virtual session that has no such window.
    engine._session = live
    fake_elicitation.queue("confirm")

    def replaced(*args: Any, **kwargs: Any) -> WindowCloseResult:
        kwin_desktop.kwin.respond("windows", windows_reply())
        return swap_then_close(Session())(*args, **kwargs)

    monkeypatch.setattr(engine, "_close", replaced)
    result = _run(_server(engine), {"window": {"id": WINDOW_ID}}, fake_elicitation)
    assert (result.is_error, _text(result)) == (False, f"No window with id '{WINDOW_ID}'.")
    assert [request.message for request in fake_elicitation.requests] == [PROMPT]
    assert "close" not in kwin_desktop.kwin.ops()


@pytest.mark.qa("QA-WIN-042")
def test_lookup_failure_is_tool_error(
    monkeypatch: pytest.MonkeyPatch, kwin_desktop: FakeEngine, fake_elicitation: FakeElicitation
) -> None:
    """A failed or ambiguous window lookup is a ToolError, and nobody is asked."""
    engine = kwin_desktop.engine

    # A name selector that matches nothing: the selector error, not the id refusal.
    result = _run(_server(engine), {"window": {"app_id": "nope"}}, fake_elicitation)
    assert result.is_error
    assert _text(result) == (
        "Error executing tool window_close: No window matches {app_id='nope'}.\n"
        "List windows with window_list."
    )

    def failing_lookup(_window: object) -> CloseTarget | None:
        msg = "Window lookup for 'x' failed: boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(engine, "_close_target", failing_lookup)
    result = _run(_server(engine), {"window": {"app_id": "x"}}, fake_elicitation)
    assert result.is_error
    assert result.structured_content is None
    assert _text(result) == (
        "Error executing tool window_close: Window lookup for 'x' failed: boom"
    )
    assert fake_elicitation.requests == []
    assert "close" not in kwin_desktop.kwin.ops()


@pytest.mark.qa("QA-WIN-044")
def test_no_server_side_timeout_fake_clock(kwin_desktop: FakeEngine) -> None:
    """The prompt waits for the user however long it takes: with the event loop's clock
    advanced 70 s while the answer is pending, a late confirmation still closes."""
    answered = asyncio.Event()
    prompts: list[str] = []

    async def slow_user(
        context: ClientRequestContext, params: ElicitRequestParams
    ) -> ElicitResult | ErrorData:
        del context  # the prompt text alone identifies the request
        assert isinstance(params, ElicitRequestFormParams)
        prompts.append(params.message)
        await answered.wait()
        return ElicitResult(action="accept", content={"confirm": True})

    async def scenario() -> CallToolResult:
        loop = asyncio.get_running_loop()
        real_time = loop.time
        async with Client(
            _server(kwin_desktop.engine), elicitation_callback=slow_user, mode="legacy"
        ) as client:
            call = asyncio.create_task(
                client.call_tool("window_close", {"window": {"id": WINDOW_ID}})
            )
            while not prompts:
                await asyncio.sleep(0.01)
            # Jump the loop clock: any server-side deadline would now have expired.
            with mock.patch.object(loop, "time", lambda: real_time() + 70.0):
                for _ in range(5):
                    await asyncio.sleep(0)
                pending = not call.done()
            answered.set()
            result = await call
        assert pending, "the call ended before the user answered"
        return result

    result = asyncio.run(scenario())
    assert (result.is_error, _text(result)) == (False, REQUESTED)
    assert prompts == [PROMPT]


# Minimal valid arguments for every tool.
_MINIMAL_ARGUMENTS: dict[str, dict[str, Any]] = {
    "session_start": {},
    "session_connect": {},
    "session_stop": {},
    "session_info": {},
    "app_launch": {"command": "true"},
    "app_log": {"pid": 1},
    "screen_capture": {},
    "element_query": {},
    "element_act": {"element": {"ref": "e1.1"}, "action": "Press"},
    "window_list": {},
    "desktop_list": {},
    "window_focus": {"window": {"id": WINDOW_ID}},
    "window_place": {"window": {"id": WINDOW_ID}, "frame": {"x": 0}},
    "window_close": {"window": {"app_id": "kate"}},
    "pointer_click": {"target": {"x": 1, "y": 1}},
    "pointer_move": {"target": {"x": 1, "y": 1}},
    "pointer_scroll": {"target": {"x": 1, "y": 1}, "delta": 1},
    "pointer_drag": {"from": {"x": 1, "y": 1}, "to": {"x": 2, "y": 2}},
    "pointer_hold": {"phase": "press", "target": {"x": 1, "y": 1}},
    "keyboard_press": {"keys": "a"},
    "keyboard_type": {"text": "a"},
    "keyboard_hold": {"phase": "press", "keys": "a"},
    "touch_tap": {"target": {"x": 1, "y": 1}},
    "touch_swipe": {"from": {"x": 1, "y": 1}, "to": {"x": 2, "y": 2}},
    "touch_pinch": {"center": {"x": 5, "y": 5}, "start_distance": 10, "end_distance": 20},
    "clipboard_read": {},
    "clipboard_write": {"text": "a"},
    "dbus_call": {"service": "org.kde.KWin", "path": "/KWin", "interface": "a.b", "method": "M"},
    "wayland_list": {},
}


@pytest.mark.qa("QA-MCP-023")
def test_close_target_runs_on_tool_thread(
    monkeypatch: pytest.MonkeyPatch, fake_engine: FakeEngine, fake_elicitation: FakeElicitation
) -> None:
    """Every engine entry point, tool bodies and window_close's resolver alike, runs on
    the single kwin-mcp-tool thread."""
    engine = fake_engine.engine
    engine._clipboard_enabled = True
    threads: dict[str, str] = {}

    def spy(name: str) -> Callable[..., object]:
        def method(*_args: object, **_kwargs: object) -> object:
            threads[name] = threading.current_thread().name
            if name == "_close_target":
                return CloseTarget(
                    window_id=WINDOW_ID, app_id=APP_ID, caption=CAPTION, closeable=True
                )
            if name == "_close":
                return WindowCloseResult(window_id=WINDOW_ID, requested=False, consent="declined")
            msg = f"spy: {name}"
            raise RuntimeError(msg)

        return method

    for name in (*TOOL_NAMES, "_close_target", "_close"):
        monkeypatch.setattr(engine, name, spy(name))
    assert set(_MINIMAL_ARGUMENTS) == set(TOOL_NAMES)

    async def call_all() -> None:
        session_tools = ("session_start", "session_connect")
        fake_elicitation.queue("confirm")
        async with Client(
            _server(engine),
            elicitation_callback=_elicitation_callback(fake_elicitation),
            mode="legacy",
        ) as client:
            for name, arguments in _MINIMAL_ARGUMENTS.items():
                if name not in session_tools:
                    await client.call_tool(name, arguments)
            # The session tools are gated on no running session.
            engine._session = None
            for name in session_tools:
                await client.call_tool(name, _MINIMAL_ARGUMENTS[name])

    asyncio.run(call_all())
    assert set(threads) == {*TOOL_NAMES, "_close_target", "_close"} - {"window_close"}
    assert all(thread.startswith("kwin-mcp-tool") for thread in threads.values()), threads
    assert [request.message for request in fake_elicitation.requests] == [PROMPT]
