"""In-process fakes for the engine's external backends (no KWin, no D-Bus, no AT-SPI).

Each fake replaces exactly one seam of ``kwin_mcp.core.AutomationEngine`` and
records what the engine asked of it:

- ``FakeEISClient`` replaces ``kwin_mcp.input.EISClient``, so the real
  ``InputBackend`` composites (click timing, drag paths, held-state tracking)
  run unchanged and only the final libei events are captured.
- ``FakeKWinScriptRunner`` replaces ``AutomationEngine._run_kwin_query``.
- ``FakeAtspiWorker`` replaces ``AutomationEngine._run_atspi``.
- ``FakeElicitation`` plays the user answering a consent prompt.

``tests/unit/conftest.py`` wires them into an engine (``fake_engine`` fixture).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.geometry import KWinWindow
    from kwin_mcp.models import Rect, Window

# Addresses that no real process listens on: a fake that leaks into real code
# fails to connect instead of reaching a developer's desktop session.
FAKE_DBUS_ADDRESS = "unix:path=/nonexistent/kwin-mcp-unit-test-bus"
FAKE_WAYLAND_SOCKET = "wayland-kwin-mcp-unit-test"

# libei button/key states as InputBackend sends them.
PRESSED = 1
RELEASED = 0

type EisEventKind = Literal[
    "motion",
    "button",
    "scroll",
    "scroll_discrete",
    "scroll_stop",
    "key",
    "touch_down",
    "touch_move",
    "touch_up",
    "close",
]


@dataclass(frozen=True)
class EisEvent:
    """One libei call: its kind, its arguments and a monotonic timestamp (s)."""

    kind: EisEventKind
    args: tuple[float, ...]
    at: float


class FakeEISClient:
    """Records the libei-level calls ``InputBackend`` makes, in order.

    ``fail_on(kind, exc)`` makes every later call of that kind raise ``exc``
    before it is recorded, which is how a mid-gesture EIS failure is staged.
    ``disconnected = True`` stages a closed EIS connection and
    ``lose_role(role)`` a device replacement, both as ``InputBackend.revalidate``
    sees them through ``poll()`` (counted in ``polls``; it never blocks).
    """

    def __init__(self) -> None:
        self.events: list[EisEvent] = []
        self.closed = False
        self.disconnected = False
        self.polls = 0
        self.syncs = 0
        self._lost_roles: set[str] = set()
        self._failures: dict[EisEventKind, Exception] = {}
        self._next_touch = 1
        self._touches: set[int] = set()

    def fail_on(self, kind: EisEventKind, exc: Exception) -> None:
        self._failures[kind] = exc

    def clear_failures(self) -> None:
        self._failures.clear()

    def poll(self) -> None:
        self.polls += 1

    def lose_role(self, role: Literal["pointer", "keyboard", "touch"]) -> None:
        """Stage KWin removing (or pausing) the device of ``role``."""
        self._lost_roles.add(role)

    def take_lost_roles(self) -> set[str]:
        lost, self._lost_roles = self._lost_roles, set()
        return lost

    def sync(self) -> bool:
        """A ping round trip: counted in ``syncs``; False once disconnected."""
        self.syncs += 1
        return not self.disconnected

    def _record(self, kind: EisEventKind, *args: float) -> None:
        if self.disconnected:
            msg = "KWin closed the EIS connection; input is unavailable"
            raise RuntimeError(msg)
        failure = self._failures.get(kind)
        if failure is not None:
            raise failure
        self.events.append(EisEvent(kind, tuple(args), time.monotonic()))

    def pointer_move_absolute(self, x: float, y: float) -> None:
        self._record("motion", x, y)

    def pointer_button(self, button: int, state: int) -> None:
        self._record("button", button, state)

    def pointer_scroll(self, dx: float, dy: float) -> None:
        self._record("scroll", dx, dy)

    def pointer_scroll_discrete(self, dx: int, dy: int) -> None:
        self._record("scroll_discrete", dx, dy)

    def pointer_scroll_stop(self) -> None:
        self._record("scroll_stop")

    def keyboard_key(self, keycode: int, state: int) -> None:
        self._record("key", keycode, state)

    def touch_down(self, x: float, y: float) -> int:
        touch_id = self._next_touch
        self._record("touch_down", touch_id, x, y)
        self._next_touch += 1
        self._touches.add(touch_id)
        return touch_id

    def touch_move(self, touch_id: int, x: float, y: float) -> None:
        if touch_id not in self._touches:
            msg = f"Unknown touch id {touch_id}"
            raise RuntimeError(msg)
        self._record("touch_move", touch_id, x, y)

    def touch_up(self, touch_id: int) -> None:
        if touch_id not in self._touches:
            msg = f"Unknown touch id {touch_id}"
            raise RuntimeError(msg)
        self._record("touch_up", touch_id)
        self._touches.discard(touch_id)

    def close(self) -> None:
        self.closed = True
        self.events.append(EisEvent("close", (), time.monotonic()))

    def of_kind(self, *kinds: EisEventKind) -> list[EisEvent]:
        """The recorded events whose kind is one of ``kinds``, in order."""
        return [event for event in self.events if event.kind in kinds]

    def _down(self, kind: EisEventKind) -> set[int]:
        down: set[int] = set()
        for event in self.of_kind(kind):
            code, state = int(event.args[0]), int(event.args[1])
            if state == PRESSED:
                down.add(code)
            else:
                down.discard(code)
        return down

    def buttons_down(self) -> set[int]:
        """evdev button codes pressed and not yet released, as the compositor sees them."""
        return self._down("button")

    def keys_down(self) -> set[int]:
        """evdev keycodes pressed and not yet released, as the compositor sees them."""
        return self._down("key")

    def active_touches(self) -> set[int]:
        return set(self._touches)


type KWinScriptResponse = dict[str, object] | Callable[[dict[str, object]], dict[str, object]]


class FakeKWinScriptRunner:
    """Answers KWin scripting queries from a table keyed by the request's ``op``.

    A request without ``op`` is the window geometry query, keyed ``"query"`` as
    in ``kwin_mcp.geometry``. A value is either the reply dict itself or a
    callable that receives the request. An op without an entry answers
    ``{"ok": False, ...}``, the shape a failed script produces.
    """

    def __init__(self, responses: Mapping[str, KWinScriptResponse] | None = None) -> None:
        self.requests: list[dict[str, object]] = []
        self._responses: dict[str, KWinScriptResponse] = dict(responses or {})

    def respond(self, op: str, response: KWinScriptResponse) -> None:
        self._responses[op] = response

    def ops(self) -> list[str]:
        """The ``op`` of every request received, in order."""
        return [str(request.get("op", "query")) for request in self.requests]

    def __call__(self, request: dict[str, object]) -> dict[str, object]:
        self.requests.append(dict(request))
        op = str(request.get("op", "query"))
        response = self._responses.get(op)
        if response is None:
            return {"ok": False, "error": f"fake KWin script runner has no reply for op {op!r}"}
        if isinstance(response, dict):
            return dict(response)
        return response(dict(request))


def kwin_record(window: Window) -> KWinWindow:
    """The raw geometry-helper record that ``window_from_kwin`` maps to ``window``.

    The helper replies with KWin records, not public windows, so a fake ``windows``
    reply is built from the windows a test means and converted back here.
    """

    def box(rect: Rect) -> list[int]:
        return [rect.x, rect.y, rect.width, rect.height]

    kind = window.kind
    return {
        "pid": 100,
        "id": window.id,
        "app": window.app_id,
        "caption": window.caption,
        "normal": kind == "normal",
        "dialog": kind == "dialog",
        "utility": kind == "utility",
        "popup": kind == "popup",
        "managed": True,
        "deleted": False,
        "desktop": kind == "shell",
        "dock": False,
        "notification": False,
        "active": window.active,
        "closeable": window.closeable,
        "moveable": window.moveable,
        "resizeable": window.resizeable,
        "desktops": list(window.desktops),
        "onAllDesktops": window.on_all_desktops,
        "transient": False,
        "transientFor": None,
        "modal": False,
        "frame": box(window.frame),
        "client": box(window.client),
        "buffer": box(window.frame),
    }


def windows_reply(*windows: Window) -> dict[str, object]:
    """A successful ``windows`` reply listing ``windows`` as the helper reports them."""
    return {"ok": True, "result": [kwin_record(window) for window in windows]}


type AtspiResponse = dict[str, object] | Callable[[dict[str, object]], dict[str, object]]


@dataclass(frozen=True)
class AtspiCall:
    """One request to the fake worker: operation, arguments, and whether a heartbeat was given."""

    op: str
    kwargs: dict[str, object]
    heartbeat: bool
    timeout_s: float | None


class FakeAtspiWorker:
    """Answers AT-SPI worker operations (``tree``, ``find``, ``action``, ``wait``, ...).

    Called with the signature of ``AutomationEngine._run_atspi``. A response is
    the worker's reply dict (``{"result": ...}``) or a callable receiving the
    keyword arguments; an exception instance is raised instead. An unknown op
    raises ``RuntimeError`` as a failed worker request does.
    """

    def __init__(self, responses: Mapping[str, AtspiResponse | Exception] | None = None) -> None:
        self.calls: list[AtspiCall] = []
        self._responses: dict[str, AtspiResponse | Exception] = dict(responses or {})

    def respond(self, op: str, response: AtspiResponse | Exception) -> None:
        self._responses[op] = response

    def __call__(
        self,
        op: str,
        *,
        heartbeat: Callable[[], None] | None = None,
        retry: bool = True,
        timeout_s: float | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        del retry  # the fake never retries, so the flag cannot change the outcome
        self.calls.append(AtspiCall(op, dict(kwargs), heartbeat is not None, timeout_s))
        response = self._responses.get(op)
        if response is None:
            msg = f"AT-SPI2 request failed: fake worker has no reply for op {op!r}"
            raise RuntimeError(msg)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, dict):
            return dict(response)
        return response(dict(kwargs))


type ElicitAnswer = Literal["confirm", "unconfirmed", "decline", "cancel"]
type ElicitAction = Literal["accept", "decline", "cancel"]


@dataclass(frozen=True)
class ElicitRequest:
    """What the server asked the (fake) user."""

    message: str
    requested_schema: dict[str, object]


@dataclass
class FakeElicitation:
    """A scripted user answering consent prompts, one queued answer per prompt.

    ``confirm`` accepts with the box checked, ``unconfirmed`` accepts with it
    unchecked, ``decline`` and ``cancel`` map to the MCP actions of the same
    name. A prompt with no queued answer is a test failure, so a test also
    proves how many prompts were shown.
    """

    answers: deque[ElicitAnswer] = field(default_factory=deque)
    requests: list[ElicitRequest] = field(default_factory=list)

    def queue(self, *answers: ElicitAnswer) -> None:
        self.answers.extend(answers)

    def answer(
        self, message: str, requested_schema: dict[str, object]
    ) -> tuple[ElicitAction, dict[str, object] | None]:
        self.requests.append(ElicitRequest(message, dict(requested_schema)))
        if not self.answers:
            msg = f"unexpected elicitation request: {message!r}"
            raise AssertionError(msg)
        answer = self.answers.popleft()
        if answer == "confirm":
            return "accept", {"confirm": True}
        if answer == "unconfirmed":
            return "accept", {"confirm": False}
        return answer, None


@dataclass(frozen=True)
class FakeEngine:
    """An ``AutomationEngine`` attached to a fake live session, with its fakes."""

    engine: AutomationEngine
    eis: FakeEISClient
    kwin: FakeKWinScriptRunner
    atspi: FakeAtspiWorker
