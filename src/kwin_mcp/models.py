"""Shared input types and result models of the kwin-mcp tools, with their text rendering.

Every tool parameter that is an object is one of the input models below, and
every tool returns one of the result models. The server publishes the models as
``inputSchema``/``outputSchema`` and ``structuredContent``; the human-readable
text that both the MCP server and ``kwin-mcp-cli`` print is ``result.render()``,
so the two surfaces are identical by construction.

Input models forbid unknown keys. Result models carry every value their text
needs, so ``Model.model_validate(structured).render()`` reproduces the text.

Pydantic only: no mcp, dbus or gi imports, so the engine, the CLI, the server
and the AT-SPI worker can all import it.
"""

import json
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Field, JsonValue, field_validator, model_validator

# ── Enumerations ─────────────────────────────────────────────────────────

MouseButtonName = Literal["left", "right", "middle"]
ScrollAxis = Literal["vertical", "horizontal"]
ScrollUnit = Literal["smooth_step", "detent"]
PointerHoldPhase = Literal["press", "move", "release"]
KeyboardHoldPhase = Literal["press", "release"]
TypeMethod = Literal["keys", "unicode"]
HomeMode = Literal["host", "isolated"]
WaitUntil = Literal["present", "absent"]
WindowKind = Literal["normal", "dialog", "utility", "popup", "shell", "other"]
SessionKind = Literal["virtual", "live"]
AccessibilityState = Literal["on", "switched_on", "off", "unavailable"]
CloseConsent = Literal[
    "not_required", "accepted", "not_confirmed", "declined", "cancelled", "unsupported"
]
CloseRefusal = Literal["no_window", "not_closeable", "no_consent"]
Dispatch = Literal["confirmed", "sent_unconfirmed"]

# "+"-joined key names, modifiers first ("ctrl+shift+t", "Return"): the one key
# grammar of keyboard_press.keys, keyboard_hold.keys and the gestures' modifiers.
KeyChord = str

# Caller text up to this length is echoed as its repr; longer text only as its
# length, so a large payload does not come back into the client's context.
ECHO_MAX_CHARS = 200


def text_echo(text: str) -> str:
    """Echo caller text in a result: its repr, or ``<N characters>`` when long."""
    if len(text) <= ECHO_MAX_CHARS:
        return repr(text)
    return f"<{len(text)} characters>"


# ── Input types ──────────────────────────────────────────────────────────


class _Input(BaseModel, extra="forbid", frozen=True):
    """Base of every tool input object: unknown keys are rejected."""


class Point(_Input):
    """Global logical px."""

    x: int
    y: int


class ElementPoint(_Input):
    """Centre of a ref's live rect + offset."""

    ref: str
    dx: int = 0
    dy: int = 0


# A point on screen: either arm forbids extra keys, so {x, y, ref} matches neither.
Target = Point | ElementPoint


class Rect(_Input):
    """Global logical px."""

    x: int
    y: int
    width: Annotated[int, Field(gt=0)]
    height: Annotated[int, Field(gt=0)]


class PartialRect(_Input):
    """Omitted fields keep their value."""

    x: int | None = None
    y: int | None = None
    width: Annotated[int | None, Field(gt=0)] = None
    height: Annotated[int | None, Field(gt=0)] = None

    @field_validator("x", "y", "width", "height", mode="before")
    @classmethod
    def _no_bool(cls, value: object) -> object:
        # Lax int validation turns true/false into 1/0; a window geometry was never
        # meant to be a boolean, so refuse it as window placement always did.
        if isinstance(value, bool):
            msg = f"frame fields must be integers, got {value!r}"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _needs_a_field(self) -> Self:
        if self.x is None and self.y is None and self.width is None and self.height is None:
            msg = "frame needs at least one of x, y, width or height"
            raise ValueError(msg)
        return self


class Waypoint(_Input):
    """Intermediate drag point; the pointer pauses dwell_ms there."""

    x: int
    y: int
    dwell_ms: int = 0


class Capture(_Input):
    """Frames taken delays_ms after the action ends; same options as screen_capture."""

    delays_ms: list[int]
    region: Rect | None = None
    max_edge: Annotated[int | None, Field(ge=0)] = None
    include_cursor: bool = False


class Wait(_Input):
    """Poll until true; timeout returns satisfied=false."""

    until: WaitUntil = "present"
    timeout_ms: int = 5000
    poll_interval_ms: int = 200


class Retention(_Input):
    """Kept after session end. home needs home='isolated'."""

    artifacts: bool = False
    home: bool = False


# Default of session_start/session_connect.retention: nothing survives the session.
NO_RETENTION = Retention()


class Locator(_Input):
    """AND-ed filters; app/query/role case-insensitive, states = exact AT-SPI state names; {} = all."""  # noqa: E501

    app: str | None = None
    query: str | None = None
    role: str | None = None
    states: list[str] | None = None

    def is_empty(self) -> bool:
        """Whether the locator selects every element (``{}``; empty values count as unset)."""
        return not (self.app or self.query or self.role or self.states)

    def describe(self) -> str:
        """Describe the criteria, e.g. ``query='7', states=['focused']``, or ``(all)``."""
        criteria: list[str] = []
        if self.query:
            criteria.append(f"query='{self.query}'")
        if self.role:
            criteria.append(f"role='{self.role}'")
        if self.states:
            criteria.append(f"states={self.states}")
        if self.app:
            criteria.append(f"app='{self.app}'")
        return ", ".join(criteria) if criteria else "(all)"


# Default of element_query.locator: every element.
ANY_ELEMENT = Locator()


class RefAddr(_Input):
    """A ref minted by element_query."""

    ref: str


class StrictAddr(_Input):
    """Strict: depth 64, fail-closed, exactly one (element, action)."""

    strict: Locator


# One element to act on: {ref} or {strict: Locator}; {ref, strict} matches neither arm.
ElementAddress = RefAddr | StrictAddr


class WindowSelector(_Input):
    """AND-ed filters over all windows; mutators need exactly one match (errors list ids and kinds)."""  # noqa: E501

    id: str | None = None
    app_id: str | None = None
    caption: str | None = None
    active: bool | None = None
    kind: Annotated[list[WindowKind] | None, Field(min_length=1)] = None

    def is_empty(self) -> bool:
        """Whether no field is set (``{}``), which no mutator accepts."""
        return (
            self.id is None
            and self.app_id is None
            and self.caption is None
            and self.active is None
            and self.kind is None
        )


class TypedArg(_Input):
    """Typed D-Bus argument."""

    type: str
    value: JsonValue
    element_type: str | None = None
    key_type: str | None = None
    value_type: str | None = None


# ── Shared result parts ──────────────────────────────────────────────────


def _box(rect: Rect) -> str:
    return f"({rect.x}, {rect.y}, {rect.width}x{rect.height})"


def _point(point: Point | None) -> str:
    return "(unknown)" if point is None else f"({point.x}, {point.y})"


class HeldInput(BaseModel):
    """Mouse buttons and keys kwin-mcp holds pressed."""

    buttons: list[str] = Field(default_factory=list)
    keys: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.buttons and not self.keys

    def render(self) -> str:
        """``left, shift``: buttons first, then keys, each in press order."""
        return ", ".join([*self.buttons, *self.keys])


def _warning_lines(warnings: list[str]) -> list[str]:
    return [f"Warning: {warning}" for warning in warnings]


class ImageMapping(BaseModel):
    """Image px to logical px: logical = origin + image_px / scale."""

    scale: float
    origin: Point


class Frame(BaseModel):
    """One saved PNG of the workspace (or of a region of it)."""

    delay_ms: int | None = None
    path: str
    uri: str
    width: int
    height: int
    size_kb: float
    mapping: ImageMapping | None
    coverage: Literal["full", "partial"]
    coordinate_space: str

    def _image_line(self) -> str:
        scale = "unavailable" if self.mapping is None else f"{self.mapping.scale:g}"
        return f"Image: {self.width}x{self.height} px; scale {scale}; resource {self.uri}"

    def render(self) -> str:
        """The screen_capture text: path with size, coordinate space, image line."""
        return "\n".join(
            [
                f"Screenshot saved: {self.path} ({self.size_kb:.1f} KB)",
                self.coordinate_space,
                self._image_line(),
            ]
        )

    def render_burst_lines(self) -> list[str]:
        """The lines of this frame inside an action's ``Captured N frames:`` block."""
        delay = 0 if self.delay_ms is None else self.delay_ms
        return [
            f"  {delay}ms: {self.path} ({self.size_kb:.1f} KB)",
            f"    {self.coordinate_space}",
            f"    {self._image_line()}",
        ]


def _frame_lines(frames: list[Frame]) -> list[str]:
    if not frames:
        return []
    lines = [f"Captured {len(frames)} frames:"]
    for frame in frames:
        lines.extend(frame.render_burst_lines())
    return lines


# ── Elements ─────────────────────────────────────────────────────────────


class UIElement(BaseModel):
    """One AT-SPI element; rect is global logical px, or null with the reason."""

    ref: str
    role: str
    name: str
    description: str
    states: list[str]
    depth: int
    app: str
    window_id: str | None
    child_count: int
    rect: Rect | None
    unavailable: str | None
    actions: list[str]
    text: str
    value: float | None
    value_max: float | None

    @classmethod
    def from_worker(cls, el: dict[str, Any]) -> Self:
        """Build from one element of the AT-SPI worker's response payload."""
        mapped = bool(el.get("mapped"))
        return cls(
            ref=el["ref"],
            role=el["role"],
            name=el["name"],
            description=el["description"],
            states=el["states"],
            depth=el["depth"],
            app=el["app"],
            window_id=el.get("window_id"),
            child_count=el["children_count"],
            rect=(
                Rect(x=el["x"], y=el["y"], width=el["width"], height=el["height"])
                if mapped
                else None
            ),
            unavailable=None if mapped else (el.get("unavailable") or "unmapped"),
            actions=el["actions"],
            text=el["text"],
            value=el["value"],
            value_max=el["value_max"],
        )

    def _display_name(self) -> str:
        if self.name:
            return self.name
        if self.depth <= 0:
            return "(unnamed)"
        if self.depth == 1:
            return "(untitled)"
        return ""

    def _position(self) -> str:
        """Screen rect, or the reason it is unavailable, so callers never click a guess."""
        if self.rect is not None:
            return f"@ screen {_box(self.rect)}"
        return f"@ unavailable ({self.unavailable})"

    def _suffix(self) -> str:
        text_str = f" text={self.text!r}" if self.text else ""
        value_str = ""
        if self.value is not None and self.value_max is not None:
            value_str = f" value={self.value:g}/{self.value_max:g}"
        actions_str = f" [actions: {', '.join(self.actions)}]" if self.actions else ""
        return f"{text_str}{value_str}{actions_str}"

    def _body(self) -> str:
        states = f" ({', '.join(self.states)})" if self.states else ""
        head = f'- [{self.role}] "{self._display_name()}"{states}'
        return f"{head} {self._position()}{self._suffix()}"

    def _tags(self, *, show_app: bool, show_window: bool) -> str:
        tags = [f"ref={self.ref}"]
        if self.depth <= 0:
            tags.append(f"children={self.child_count}")
        if show_app:
            tags.append(f"app={self.app!r}")
        if show_window:
            tags.append("window=none" if self.window_id is None else f"window={self.window_id}")
        return f" [{', '.join(tags)}]"

    def render(self) -> str:
        """One element on its own, with its app and window."""
        show_app = self.depth > 0 or self.app != self.name
        return self._body() + self._tags(show_app=show_app, show_window=self.window_id is not None)


def _element_lines(elements: list[UIElement]) -> list[str]:
    """Pre-order lines, indented two spaces per depth.

    ``app`` and ``window`` are printed only where they change from the previous
    line; an application line (depth 0) resets both, and its own name is its app.
    """
    lines: list[str] = []
    last_app: str | None = None
    last_window: str | None = None
    for element in elements:
        if element.depth <= 0:
            show_app = element.app != element.name
            show_window = element.window_id is not None
        else:
            show_app = element.app != last_app
            show_window = element.window_id != last_window
        last_app, last_window = element.app, element.window_id
        tags = element._tags(show_app=show_app, show_window=show_window)
        lines.append("  " * max(element.depth, 0) + element._body() + tags)
    return lines


class ElementWaitResult(BaseModel):
    """Outcome of an element_query wait."""

    until: WaitUntil
    timeout_ms: int
    satisfied: bool
    elapsed_ms: int

    def render(self) -> str:
        if self.satisfied:
            return f"Wait satisfied (until={self.until}) after {self.elapsed_ms} ms"
        return (
            f"NOT SATISFIED: Timeout after {self.timeout_ms} ms "
            f"(until={self.until}, elapsed {self.elapsed_ms} ms)"
        )


class ElementList(BaseModel):
    """Elements matching the locator, in pre-order."""

    locator: Locator = Field(default_factory=Locator)
    elements: list[UIElement]
    wait: ElementWaitResult | None = None

    def _listing(self) -> str:
        if self.locator.is_empty():
            if not self.elements:
                return "(no accessible applications found)"
            header = f"# Accessibility Tree ({len(self.elements)} elements)\n"
        else:
            desc = self.locator.describe()
            if not self.elements:
                return f"No elements found matching {desc}"
            header = f"Found {len(self.elements)} elements matching {desc}:\n"
        return "\n".join([header, *_element_lines(self.elements)])

    def render(self) -> str:
        """Tree header for an empty locator, ``Found N`` header otherwise.

        A wait that timed out puts its ``NOT SATISFIED:`` line first; a
        satisfied wait reports its elapsed time last.
        """
        parts = [self._listing()]
        if self.wait is not None:
            if self.wait.satisfied:
                parts.append(self.wait.render())
            else:
                parts.insert(0, self.wait.render())
        return "\n".join(parts)


# ── Windows and desktops ─────────────────────────────────────────────────


class Window(BaseModel):
    """One KWin window."""

    id: str
    app_id: str
    caption: str
    kind: WindowKind
    active: bool
    frame: Rect
    client: Rect
    moveable: bool
    resizeable: bool
    closeable: bool
    desktops: list[str]
    on_all_desktops: bool

    def render(self) -> str:
        marker = " [active]" if self.active else ""
        all_desktops = " (all desktops)" if self.on_all_desktops else ""
        return (
            f'- {self.app_id} "{self.caption}"{marker}\n'
            f"    id:     {self.id}\n"
            f"    frame:  {_box(self.frame)}\n"
            f"    client: {_box(self.client)}\n"
            f"    kind:   {self.kind}\n"
            f"    moveable: {self.moveable}, resizeable: {self.resizeable}, "
            f"closeable: {self.closeable}\n"
            f"    desktops: {self.desktops}{all_desktops}"
        )


def _is_active_only(selector: WindowSelector) -> bool:
    """Whether the filter is exactly ``{active: true}`` (the active-window query)."""
    return selector.active is True and selector.model_copy(update={"active": None}).is_empty()


class WindowList(BaseModel):
    """Windows matching the filter, in KWin's window list order."""

    filter: WindowSelector | None = None
    windows: list[Window] = Field(default_factory=list)

    def render(self) -> str:
        selector = self.filter
        only_active = selector is not None and _is_active_only(selector)
        if not self.windows:
            if selector is not None and selector.id is not None:
                return f"No window with id {selector.id!r}."
            if selector is not None and selector.app_id:
                return f"No windows found for '{selector.app_id}'."
            if only_active:
                return "No active window."
            return "No windows found."
        if only_active and len(self.windows) == 1:
            return f"Active window:\n{self.windows[0].render()}"
        return "\n".join([f"Windows ({len(self.windows)}):", *(w.render() for w in self.windows)])


class Desktop(BaseModel):
    """One KWin virtual desktop; id is stable while the desktop exists."""

    id: str
    name: str
    current: bool


class DesktopList(BaseModel):
    """Virtual desktops in pager order."""

    desktops: list[Desktop] = Field(default_factory=list)
    current_desktop_id: str | None

    def render(self) -> str:
        if not self.desktops:
            return "No virtual desktops found."
        lines = [f"Virtual desktops ({len(self.desktops)}):"]
        for desktop in self.desktops:
            marker = " [current]" if desktop.current else ""
            lines.append(f'- "{desktop.name}"{marker}\n    id: {desktop.id}')
        return "\n".join(lines)


def _vanished_line(observation_error: str | None) -> str:
    if observation_error is not None:
        return f"observation failed: {observation_error} (the window may already have moved)"
    return "observed: window no longer exists (it closed or its client exited)"


class WindowFocusResult(BaseModel):
    """Result of window_focus; window is observed after the activation."""

    window_id: str
    window: Window | None
    observation_error: str | None = None
    frames: list[Frame] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def render(self) -> str:
        window = self.window
        if window is not None:
            lines = [f"Focused: {window.app_id} {window.caption}", window.render()]
        else:
            lines = [f"Focused: {self.window_id}", _vanished_line(self.observation_error)]
        return "\n".join([*lines, *_frame_lines(self.frames), *_warning_lines(self.warnings)])


class PlaceBefore(BaseModel):
    """The window's state when window_place was requested."""

    app_id: str
    caption: str
    frame: Rect
    desktops: list[str]
    on_all_desktops: bool
    moveable: bool
    resizeable: bool


class PlaceApplied(BaseModel):
    """Per requested part: true = observed applied; null = not requested."""

    frame: bool | None = None
    desktop: bool | None = None


class WindowPlaceResult(BaseModel):
    """Result of window_place, read back after KWin applied it."""

    window_id: str
    requested_frame: PartialRect | None = None
    requested_desktop: str | None = None
    window: Window | None
    observation_error: str | None = None
    before: PlaceBefore
    applied: PlaceApplied
    pending: list[str] = Field(default_factory=list)
    current_desktop_before: str | None = None
    current_desktop_after: str | None = None
    frames: list[Frame] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def _desktop_lines(self, desktop_id: str) -> list[str]:
        before = self.before
        status = "set" if self.applied.desktop else "not confirmed"
        all_desktops = " (all desktops)" if before.on_all_desktops else ""
        lines = [
            f"Window desktop {status}: {self.window_id} -> {desktop_id}",
            f"before:   {before.desktops}{all_desktops}",
        ]
        if self.window is not None:
            lines.append(f"observed: {self.window.desktops}")
        lines.append(
            f"current desktop: {self.current_desktop_before} -> {self.current_desktop_after}"
        )
        return lines

    def _frame_lines(self, frame: PartialRect) -> list[str]:
        before = self.before
        requested = f"x={frame.x!r}, y={frame.y!r}, width={frame.width!r}, height={frame.height!r}"
        lines = [
            f'Geometry update requested: {before.app_id} "{before.caption}" ({self.window_id})',
            f"before:    {_box(before.frame)}",
            f"requested: {requested}",
        ]
        if self.window is not None:
            lines.append(f"observed:  {_box(self.window.frame)}")
            if self.pending:
                lines.append(
                    f"not yet applied: {', '.join(self.pending)} "
                    "(the client may still be applying the change; verify with window_list)"
                )
        return lines

    def render(self) -> str:
        """Desktop part first, then the frame part (the order KWin applies them)."""
        lines: list[str] = []
        if self.requested_desktop is not None:
            lines.extend(self._desktop_lines(self.requested_desktop))
        if self.requested_frame is not None:
            lines.extend(self._frame_lines(self.requested_frame))
        if self.window is None:
            lines.append(_vanished_line(self.observation_error))
        lines.append(f"moveable: {self.before.moveable}, resizeable: {self.before.resizeable}")
        if self.window is not None:
            lines.append(self.window.render())
        return "\n".join([*lines, *_frame_lines(self.frames), *_warning_lines(self.warnings)])


class WindowCloseResult(BaseModel):
    """Result of window_close; outcomes other than a request are refusals, not errors."""

    window_id: str
    app_id: str | None = None
    caption: str | None = None
    requested: bool
    consent: CloseConsent | None = None
    refusal: CloseRefusal | None = None
    frames: list[Frame] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def _outcome(self) -> str:
        name = f'{self.app_id} "{self.caption}"'
        if self.requested:
            return (
                f"Close requested: {name} ({self.window_id}). The app may keep the window open, "
                "for example to ask about unsaved changes; check window_list."
            )
        if self.refusal == "no_window":
            return f"No window with id {self.window_id!r}."
        if self.refusal == "not_closeable":
            return f"Window {name} ({self.window_id}) cannot be closed."
        if self.consent == "not_confirmed":
            return (
                "window_close not performed: the user did not confirm closing window "
                f"{self.window_id}."
            )
        if self.consent == "declined":
            return (
                f"window_close not performed: the user declined closing window {self.window_id}. "
                "Do not retry unless the user asks."
            )
        if self.consent == "cancelled":
            # A cancel is not a refusal: a headless client (claude -p, for example)
            # cancels every confirmation without showing it to anyone.
            return (
                f"window_close not performed: the confirmation for window {self.window_id} "
                "was dismissed or never shown (a client with no one to answer cancels it "
                "automatically). If the user asked to close this window, close it through "
                "the app's own UI instead."
            )
        return (
            "window_close is disabled in live sessions: it could discard unsaved work "
            "on the real desktop. Close the window through the app's own UI instead."
        )

    def render(self) -> str:
        return "\n".join(
            [self._outcome(), *_frame_lines(self.frames), *_warning_lines(self.warnings)]
        )


# ── Input actions ────────────────────────────────────────────────────────


class TypeRoute(BaseModel):
    """How keyboard_type(method='unicode') delivered the text."""

    kind: Literal["wtype", "paste"]
    chord: KeyChord | None = None

    def render(self) -> str:
        """``wtype`` or ``paste:<chord>``, the value of ``KeyboardTypeResult.route``."""
        return "wtype" if self.kind == "wtype" else f"paste:{self.chord}"


class ActionResult(BaseModel):
    """Result of an input action."""

    tool: str
    at: Point | None = None
    element: UIElement | None = None
    frames: list[Frame] = Field(default_factory=list)
    held: HeldInput = Field(default_factory=HeldInput)
    warnings: list[str] = Field(default_factory=list)

    def summary(self) -> str:
        """The first line: what was done. Each tool's subclass words its own."""
        return f"{self.tool} at {_point(self.at)}"

    def render(self) -> str:
        """Summary, element, frames, warnings, then ``Still held:`` last when anything is held."""
        lines = [self.summary()]
        if self.element is not None:
            lines.append(f"Element: {self.element.render()}")
        lines.extend(_frame_lines(self.frames))
        lines.extend(_warning_lines(self.warnings))
        if not self.held.is_empty():
            lines.append(f"Still held: {self.held.render()}")
        return "\n".join(lines)


class PointerClickResult(ActionResult):
    """Result of pointer_click."""

    tool: str = "pointer_click"
    button: MouseButtonName
    count: int
    modifiers: KeyChord | None = None
    hold_ms: int = 0

    def summary(self) -> str:
        desc = f"Clicked {self.button} at {_point(self.at)}"
        if self.count >= 3:
            desc += " (triple)"
        elif self.count == 2:
            desc += " (double)"
        if self.modifiers:
            desc += f" with {self.modifiers}"
        if self.hold_ms > 0:
            desc += f" held {self.hold_ms}ms"
        return desc


class PointerMoveResult(ActionResult):
    """Result of pointer_move."""

    tool: str = "pointer_move"

    def summary(self) -> str:
        return f"Mouse moved to {_point(self.at)}"


class PointerScrollResult(ActionResult):
    """Result of pointer_scroll."""

    tool: str = "pointer_scroll"
    delta: int
    axis: ScrollAxis
    unit: ScrollUnit
    steps: int

    def summary(self) -> str:
        mode = "discrete" if self.unit == "detent" else "smooth"
        desc = f"Scrolled {self.axis} ({mode}) by {self.delta} at {_point(self.at)}"
        if self.steps > 1:
            desc += f" in {self.steps} steps"
        return desc


class PointerDragResult(ActionResult):
    """Result of pointer_drag; at is the drop point."""

    tool: str = "pointer_drag"
    start: Point
    button: MouseButtonName
    modifiers: KeyChord | None = None
    waypoints: int = 0
    dwell_ms: int = 0

    def summary(self) -> str:
        desc = f"Dragged from {_point(self.start)} to {_point(self.at)}"
        if self.button != "left":
            desc += f" with the {self.button} button"
        if self.modifiers:
            desc += f" with {self.modifiers}"
        if self.waypoints:
            desc += f" via {self.waypoints} waypoints"
        if self.dwell_ms > 0:
            desc += f", dwelling {self.dwell_ms}ms before the release"
        return desc


class PointerHoldResult(ActionResult):
    """Result of pointer_hold; changed = whether the held set changed."""

    tool: str = "pointer_hold"
    phase: PointerHoldPhase
    button: MouseButtonName
    changed: bool

    def summary(self) -> str:
        at = _point(self.at)
        if self.phase == "press":
            if self.changed:
                return f"Button {self.button} pressed at {at}"
            return f"Button {self.button} already held; moved to {at} without a press"
        if self.phase == "move":
            return f"Button {self.button} moved to {at} while held"
        if self.changed:
            return f"Button {self.button} released at {at}"
        return f"Button {self.button} released at {at} (it was not held by kwin-mcp)"


class KeyboardPressResult(ActionResult):
    """Result of keyboard_press."""

    tool: str = "keyboard_press"
    keys: KeyChord

    def summary(self) -> str:
        return f"Pressed: {self.keys}"


class KeyboardTypeResult(ActionResult):
    """Result of keyboard_type."""

    tool: str = "keyboard_type"
    method: TypeMethod
    route: str
    chars: int
    echo: str

    def summary(self) -> str:
        prefix = "Typed unicode" if self.method == "unicode" else "Typed"
        return f"{prefix}: {self.echo}\nRoute: {self.route}; {self.chars} characters"


class KeyboardHoldResult(ActionResult):
    """Result of keyboard_hold; changed = whether the held set changed."""

    tool: str = "keyboard_hold"
    phase: KeyboardHoldPhase
    keys: KeyChord
    changed: bool

    def summary(self) -> str:
        if self.phase == "press":
            if self.changed:
                return f"Key down: {self.keys}"
            return f"Key down: {self.keys} (already held; no event sent)"
        if self.changed:
            return f"Key up: {self.keys}"
        return f"Key up: {self.keys} (it was not held by kwin-mcp)"


class TouchTapResult(ActionResult):
    """Result of touch_tap."""

    tool: str = "touch_tap"
    hold_ms: int = 0

    def summary(self) -> str:
        desc = f"Touch tap at {_point(self.at)}"
        if self.hold_ms > 0:
            desc += f" held {self.hold_ms}ms"
        return desc


class TouchSwipeResult(ActionResult):
    """Result of touch_swipe; at is the end point."""

    tool: str = "touch_swipe"
    start: Point
    fingers: int
    duration_ms: int

    def summary(self) -> str:
        path = f"from {_point(self.start)} to {_point(self.at)} in {self.duration_ms}ms"
        if self.fingers == 1:
            return f"Touch swipe {path}"
        return f"{self.fingers}-finger swipe {path}"


class TouchPinchResult(ActionResult):
    """Result of touch_pinch; at is the centre."""

    tool: str = "touch_pinch"
    start_distance: int
    end_distance: int
    duration_ms: int

    def summary(self) -> str:
        direction = "in" if self.end_distance < self.start_distance else "out"
        return (
            f"Pinch {direction} at {_point(self.at)}: "
            f"{self.start_distance}→{self.end_distance}px in {self.duration_ms}ms"
        )


class ElementActResult(ActionResult):
    """Result of element_act."""

    tool: str = "element_act"
    action: str
    dispatch: Dispatch
    dispatch_error: str | None = None

    def summary(self) -> str:
        element = self.element
        role = "" if element is None else element.role
        name = "" if element is None else element.name
        app = "" if element is None else element.app
        if self.dispatch == "sent_unconfirmed":
            return (
                f"Sent AT-SPI action {self.action!r} to "
                f'[{role}] "{name}" in {app}, '
                f"but the application did not confirm ({self.dispatch_error}); "
                "the action may have run — check the UI before retrying."
            )
        return f'Invoked AT-SPI action: {self.action} on [{role}] "{name}" in {app}'


# ── Sessions and apps ────────────────────────────────────────────────────


class ScreenSize(BaseModel):
    """Workspace size, logical px."""

    width: int
    height: int


# The four accessibility-switch lines of a session, by state.
_ACCESSIBILITY_LINES: dict[AccessibilityState, str] = {
    "on": "Accessibility: on (org.a11y.Status.IsEnabled)",
    "switched_on": (
        "Accessibility: switched on for this connection "
        "(org.a11y.Status.IsEnabled); session_stop switches it back off. "
        "Firefox follows it at once; an app that reads it only at startup "
        "must be restarted."
    ),
    "off": (
        "Accessibility: off (org.a11y.Status.IsEnabled is false), so apps that check it, "
        "such as Firefox, expose no accessibility tree. Reconnect with "
        "enable_accessibility=true to switch it on until session_stop."
    ),
    "unavailable": "Accessibility: org.a11y.Status unavailable",
}


class SessionInfo(BaseModel):
    """The running session."""

    kind: SessionKind
    wayland_socket: str
    dbus_address: str
    screen: ScreenSize | None
    home_dir: str | None = None
    retention: Retention = Field(default_factory=Retention)
    artifacts_dir: str
    input_backend: Literal["eis"] | None
    clipboard: bool
    accessibility: AccessibilityState
    warnings: list[str] = Field(default_factory=list)

    def _detail_lines(self) -> list[str]:
        lines: list[str] = []
        if self.home_dir is not None:
            lines.append(f"Isolated home: {self.home_dir}")
        screen = "unknown" if self.screen is None else f"{self.screen.width}x{self.screen.height}"
        lines.append(f"Screen: {screen}")
        lines.append(f"Artifacts: {self.artifacts_dir}")
        lines.append(
            f"Retention: artifacts={str(self.retention.artifacts).lower()}, "
            f"home={str(self.retention.home).lower()}"
        )
        lines.append(f"Clipboard: {'enabled' if self.clipboard else 'disabled'}")
        lines.append(
            "Input backend: KWin EIS" if self.input_backend == "eis" else "Input backend: none"
        )
        lines.append(_ACCESSIBILITY_LINES[self.accessibility])
        lines.extend(_warning_lines(self.warnings))
        return lines

    def render(self) -> str:
        """The session_start / session_connect text."""
        if self.kind == "live":
            head = [
                f"Connected to live KWin session. D-Bus: {self.dbus_address}, "
                f"Wayland: {self.wayland_socket}"
            ]
        else:
            head = [
                f"Session started. Wayland socket: {self.wayland_socket}",
                f"D-Bus: {self.dbus_address}",
            ]
        return "\n".join([*head, *self._detail_lines()])

    def render_status(self) -> list[str]:
        """The session part of the session_info text."""
        return [
            f"Session: {self.kind}. Wayland socket: {self.wayland_socket}",
            f"D-Bus: {self.dbus_address}",
            *self._detail_lines(),
        ]


class AppEntry(BaseModel):
    """One app launched through kwin-mcp in the current session."""

    pid: int
    command: str
    running: bool
    exit_code: int | None
    log_path: str

    def render(self) -> str:
        if self.running:
            state = "running"
        elif self.exit_code is None:
            state = "exited"
        else:
            state = f"exited {self.exit_code}"
        return f"- PID {self.pid}: {self.command} ({state}); log: {self.log_path}"


class SessionInfoResult(BaseModel):
    """Result of session_info."""

    session: SessionInfo | None
    apps: list[AppEntry] = Field(default_factory=list)
    held: HeldInput = Field(default_factory=HeldInput)
    warnings: list[str] = Field(default_factory=list)

    def render(self) -> str:
        if self.session is None:
            lines = ["No session running."]
        else:
            lines = self.session.render_status()
            if self.apps:
                lines.append(f"Apps ({len(self.apps)}):")
                lines.extend(app.render() for app in self.apps)
            else:
                lines.append("Apps: none")
        lines.extend(_warning_lines(self.warnings))
        if not self.held.is_empty():
            lines.append(f"Still held: {self.held.render()}")
        return "\n".join(lines)


class KeptPaths(BaseModel):
    """Directories that survive the session end."""

    artifacts_dir: str | None = None
    home_dir: str | None = None


class SessionStopResult(BaseModel):
    """Result of session_stop."""

    stopped: bool
    kind: SessionKind | None = None
    kept: KeptPaths = Field(default_factory=KeptPaths)
    released: HeldInput = Field(default_factory=HeldInput)
    warnings: list[str] = Field(default_factory=list)

    def render(self) -> str:
        if not self.stopped:
            lines = ["No session running."]
        elif self.kind == "live":
            lines = ["Disconnected from live session."]
        else:
            lines = ["Session stopped."]
        if self.kept.artifacts_dir is not None:
            lines.append(f"Artifacts kept: {self.kept.artifacts_dir}")
        if self.kept.home_dir is not None:
            lines.append(f"Isolated home kept: {self.kept.home_dir}")
        if not self.released.is_empty():
            lines.append(f"Released held input: {self.released.render()}")
        lines.extend(_warning_lines(self.warnings))
        return "\n".join(lines)


class AppLaunchResult(BaseModel):
    """Result of app_launch."""

    pid: int
    command: str
    log_path: str
    log_uri: str

    def render(self) -> str:
        return (
            f"App launched: {self.command} (PID={self.pid})\n"
            f"App log: {self.log_path}\n"
            f"Log resource: {self.log_uri}"
        )


class AppLogResult(BaseModel):
    """Result of app_log: the tail of an app's captured stdout/stderr."""

    pid: int
    log_path: str
    lines: list[str]
    truncated: bool

    def render(self) -> str:
        header = f"App log of PID {self.pid}: {self.log_path}"
        if self.truncated:
            header += f" (last {len(self.lines)} lines)"
        body = "\n".join(self.lines) if self.lines else "(no log output yet)"
        return f"{header}\n{body}"


# ── Clipboard and escape hatches ─────────────────────────────────────────


class ClipboardText(BaseModel):
    """Result of clipboard_read."""

    text: str

    def render(self) -> str:
        return self.text


class ClipboardWriteResult(BaseModel):
    """Result of clipboard_write."""

    length: int
    echo: str

    def render(self) -> str:
        return f"Clipboard set: {self.echo}"


class DbusReply(BaseModel):
    """Result of dbus_call; signature is the reply's D-Bus signature ('' when void)."""

    reply: JsonValue
    signature: str

    def render(self) -> str:
        """A single primitive bare, containers and multi-value replies as JSON."""
        reply = self.reply
        if reply is None:
            body = "(no reply)"
        elif isinstance(reply, bool):
            body = "true" if reply else "false"
        elif isinstance(reply, int | float | str):
            body = str(reply)
        else:
            body = json.dumps(reply)
        if not self.signature:
            return body
        return f"{body}\n(reply signature '{self.signature}')"


class WaylandGlobal(BaseModel):
    """One global the compositor advertises."""

    interface: str
    version: int
    name: int


class WaylandGlobals(BaseModel):
    """Result of wayland_list: raw wayland-info lines and the parsed globals."""

    filter: str = ""
    lines: list[str]
    globals: list[WaylandGlobal] = Field(default_factory=list)

    def render(self) -> str:
        if self.filter and not self.lines:
            return f"No protocols matching '{self.filter}' found."
        return "\n".join(self.lines)
