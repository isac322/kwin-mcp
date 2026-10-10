"""MCP server for KDE Wayland GUI automation.

Thin wrapper that registers MCP tools with parameter descriptions,
delegating all logic to AutomationEngine in core.py.

Supports ``--default-live-session`` flag to switch the default session mode
from virtual (isolated) to live (real desktop), ``--screenshot-images`` flag
to attach the screenshot PNGs a tool call captured as MCP image content, and
``--screenshot-max-edge N`` to downscale every screenshot and frame so its
longer side is at most N pixels.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import importlib.metadata
import os
import signal
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Annotated, NoReturn

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.context_injection import find_context_parameter
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from kwin_mcp import progress
from kwin_mcp.core import AutomationEngine
from kwin_mcp.results import (
    AccessibilityTreeResult,
    FindUIElementsResult,
    ListWindowsResult,
    WindowGeometryResult,
    format_find,
    format_list_windows,
    format_tree,
    format_window_geometry,
)
from kwin_mcp.session import process_registry

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Future
    from types import FunctionType

_MAX_EDGE_FLAG = "--screenshot-max-edge"


def _max_edge_flag_spans(argv: list[str]) -> list[tuple[int, int, str]]:
    """Locate every ``--screenshot-max-edge N`` or ``--screenshot-max-edge=N`` in ``argv``.

    Returns each argument slice ``(start, stop)`` and its value text, in order.
    """
    spans: list[tuple[int, int, str]] = []
    for index, arg in enumerate(argv):
        if arg == _MAX_EDGE_FLAG:
            value = argv[index + 1] if index + 1 < len(argv) else ""
            spans.append((index, index + 2, value))
        elif arg.startswith(f"{_MAX_EDGE_FLAG}="):
            spans.append((index, index + 1, arg.split("=", 1)[1]))
    return spans


def _screenshot_max_edge(argv: list[str]) -> int:
    spans = _max_edge_flag_spans(argv)
    if not spans:
        return 0
    if len(spans) > 1:
        msg = f"kwin-mcp: {_MAX_EDGE_FLAG} may only be specified once"
        raise SystemExit(msg)
    value = spans[0][2]
    if not value.isdigit():
        msg = f"kwin-mcp: {_MAX_EDGE_FLAG} needs a pixel count (0 = no limit), got {value!r}"
        raise SystemExit(msg)
    return int(value)


mcp = MCPServer("kwin-mcp", version=importlib.metadata.version("kwin-mcp"))

# Detect custom flags early (before MCP framework consumes args)
_live_session_mode = "--default-live-session" in sys.argv
_screenshot_images = "--screenshot-images" in sys.argv
_engine = AutomationEngine(screenshot_max_edge=_screenshot_max_edge(sys.argv))

# Tool descriptions that replace the docstrings when --default-live-session is active.
_LIVE_SESSION_DESCRIPTIONS: dict[str, str] = {
    "session_start": (
        "Start an isolated virtual KWin Wayland session. "
        "Only use when explicitly asked for an isolated/virtual session. "
        "The default session tool is session_connect (live session mode is active)."
    ),
    "session_connect": (
        "Connect to an existing KWin session (e.g. the real desktop or a container). "
        "This is the default session tool. Connects to a KWin compositor that is already "
        "running. Clipboard is always available. Input injection uses KWin EIS when "
        "possible, with ydotool as fallback."
    ),
}

# All tool bodies run on this single worker thread. AutomationEngine state (session,
# EIS/libei input, D-Bus connections) is not thread-safe and has thread affinity, so
# calls stay serialized on one thread, as they were when tools ran inline one at a time.
_tool_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kwin-mcp-tool")

# Set the moment shutdown begins. Tool bodies queued after this point return without
# running, so queued work cannot start new resources once the server is exiting.
_shutdown = threading.Event()
# How long the exit path waits for the final session_stop to START on the tool
# thread (its first statement sets a flag) before concluding the tool thread is
# still busy with an in-flight tool. If the stop starts, the exit path waits for
# it to finish — no forced exit while it runs, and its own waits are bounded. If
# it does not start, the stop is still queued behind the in-flight tool, so it is
# cancelled before it runs and the owned process groups are terminated directly,
# without touching the engine. The value is long enough for a short tool to
# finish and the stop to start (the graceful path) and short enough that a long
# tool (e.g. a 40 s screenshot_after_ms) triggers the registry path quickly.
EXIT_DRAIN_SECONDS = 2.0
# How long the forced exit waits for its stderr report (the cleanup outcome, and the
# traceback on a server failure). The write runs on a daemon thread that os._exit
# abandons, so a client that stopped draining stderr cannot hold the exit.
EXIT_REPORT_SECONDS = 1.0

# Tool annotations (MCP hints) shared by tools with identical behavior profiles.
_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
# Input injection and other actions that change app state and are not safe to repeat.
_DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)
# Destructive but repeating with the same arguments has no further effect.
_DESTRUCTIVE_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)
_NON_DESTRUCTIVE_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
# Attaches to sessions/processes outside the server's closed domain without
# running a caller-chosen command.
_SPAWN = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
# Starts sessions/apps by running an arbitrary caller command (shlex + Popen):
# any action the command can take is possible, so destructive.
_SPAWN_COMMAND = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)
_DBUS_CALL = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)


def _tool[F: FunctionType](
    *, annotations: ToolAnnotations, images: bool = False
) -> Callable[[F], F]:
    """Register a sync body as an MCP tool that runs on the dedicated tool thread.

    ``annotations`` are published in tools/list. ``images`` marks tools that may capture
    screenshot PNGs; with ``--screenshot-images`` the PNGs a call recorded through
    ``progress.record_image`` are appended as ImageContent after the unchanged text.
    The body runs inside a ``progress.call_scope`` that forwards ``progress.report``
    calls to the client as progress notifications (no-op without a progressToken).

    Any exception is re-raised as ``ToolError`` so the client sees
    ``Error executing tool <name>: <message>``. Input and output schemas are derived
    from ``fn`` unchanged, and the body never receives the Context. Returns ``fn``.
    """

    def register(fn: F) -> F:
        attach_images = images and _screenshot_images

        def run_body(
            report_fn: progress.ReportFn, kwargs: dict[str, object]
        ) -> tuple[str | None, list[str], str | None]:
            # The server is exiting: do not start new resources (a queued tool must not
            # run after shutdown began). A tool already running still finishes, then the
            # final session_stop runs on this same thread.
            if _shutdown.is_set():
                return None, [], "Server is shutting down; tool not executed."
            # Reduce a failure to its message here, on the tool thread: the exception's
            # traceback keeps the failed call's D-Bus connections and fds alive, and they
            # must be released before the response goes out, as they were when tools ran
            # inline on the event loop. PNGs are read and encoded here too, off the loop.
            try:
                with progress.call_scope(report_fn, collect_images=attach_images) as scope:
                    text = fn(**kwargs)
                    encoded = [
                        base64.b64encode(path.read_bytes()).decode("ascii") for path in scope.images
                    ]
                return text, encoded, None
            except Exception as exc:
                return None, [], str(exc)

        @functools.wraps(fn)
        async def wrapper(*, ctx: Context, **kwargs: object) -> str | CallToolResult:
            loop = asyncio.get_running_loop()
            sent: list[Future[None]] = []

            def report_fn(value: float, total: float | None, message: str | None) -> None:
                # Called on the tool thread. Do not wait for the send: blocking on the loop
                # would slow every step. report_progress never raises on a closed channel.
                coro = ctx.report_progress(value, total, message)
                sent.append(asyncio.run_coroutine_threadsafe(coro, loop))

            text, encoded, error = await asyncio.wrap_future(
                _tool_executor.submit(run_body, report_fn, kwargs)
            )
            # Flush progress sends before the response; the SDK drops notifications for a
            # request once its response is written. The body is done, so no appends race.
            await asyncio.gather(*map(asyncio.wrap_future, sent), return_exceptions=True)
            if error is not None:
                raise ToolError(error)
            assert text is not None
            if not encoded:
                return text
            return CallToolResult(
                content=[
                    TextContent(text=text),
                    *(ImageContent(data=data, mime_type="image/png") for data in encoded),
                ],
                structured_content={"result": text},
            )

        # Context injection: the SDK finds the Context parameter via get_type_hints(wrapper),
        # while the argument model and schemas follow __wrapped__ to fn. Use a new dict so
        # fn.__annotations__ (introspected by the CLI) is not mutated.
        wrapper.__annotations__ = {**fn.__annotations__, "ctx": Context}

        description = _LIVE_SESSION_DESCRIPTIONS.get(fn.__name__) if _live_session_mode else None
        # The SDK silently skips Context injection when type hints fail to resolve; check it
        # with the same lookup the SDK uses before registering.
        assert find_context_parameter(wrapper) == "ctx", fn.__name__
        mcp.tool(description=description, annotations=annotations)(wrapper)
        return fn

    return register


# ── Session management ──────────────────────────────────────────────────


@_tool(annotations=_SPAWN_COMMAND)
def session_start(
    app_command: Annotated[
        str,
        Field(
            description='Command to launch (e.g. "kcalc" or "/path/to/app --arg"). '
            "Leave empty to start session without an app."
        ),
    ] = "",
    screen_width: Annotated[int, Field(description="Virtual screen width in pixels.")] = 1920,
    screen_height: Annotated[int, Field(description="Virtual screen height in pixels.")] = 1080,
    enable_clipboard: Annotated[
        bool,
        Field(
            description="Enable clipboard tools (wl-copy/wl-paste). Disabled by default "
            "because wl-copy can hang in isolated sessions."
        ),
    ] = False,
    keep_screenshots: Annotated[
        bool,
        Field(
            description="Keep screenshot files after session_stop instead of deleting them. "
            "Useful for debugging. Files must be cleaned up manually when enabled."
        ),
    ] = False,
    isolate_home: Annotated[
        bool,
        Field(
            description="Create a temporary HOME directory with isolated XDG directories "
            "(config, data, cache, state). Prevents apps from reading/writing host user settings."
        ),
    ] = False,
    keep_home: Annotated[
        bool,
        Field(
            description="Keep the isolated home directory after session_stop "
            "instead of deleting it. Only effective when isolate_home=true. "
            "Files must be cleaned up manually when enabled."
        ),
    ] = False,
    env: Annotated[
        dict[str, str] | None,
        Field(description="Extra environment variables to pass to the launched app."),
    ] = None,
) -> str:
    """Start an isolated KWin Wayland session, optionally launching an app.

    This must be called before any other tool. If a session is already running,
    call session_stop first. Returns session status including the Wayland socket
    path, launched app PID (if any), and input backend availability.
    """
    return _engine.session_start(
        app_command=app_command,
        screen_width=screen_width,
        screen_height=screen_height,
        enable_clipboard=enable_clipboard,
        keep_screenshots=keep_screenshots,
        isolate_home=isolate_home,
        keep_home=keep_home,
        env=env,
    )


@_tool(annotations=_SPAWN)
def session_connect(
    dbus_address: Annotated[
        str,
        Field(
            description="D-Bus session bus address. Leave empty to use the current desktop "
            "session ($DBUS_SESSION_BUS_ADDRESS)."
        ),
    ] = "",
    wayland_display: Annotated[
        str,
        Field(
            description="Wayland display socket name. Leave empty to use the current desktop "
            "($WAYLAND_DISPLAY)."
        ),
    ] = "",
    keep_screenshots: Annotated[
        bool,
        Field(description="Keep screenshot files after session_stop instead of deleting them."),
    ] = False,
) -> str:
    """Connect to an existing KWin session (e.g. the real desktop or a container).

    Only use when explicitly asked to interact with a real/existing desktop session.
    For normal GUI automation, use session_start instead (creates an isolated virtual session).
    This connects to a KWin compositor that is already running. Clipboard is always available.
    Input injection uses KWin EIS when possible, with ydotool as fallback.
    """
    return _engine.session_connect(
        dbus_address=dbus_address,
        wayland_display=wayland_display,
        keep_screenshots=keep_screenshots,
    )


@_tool(annotations=_DESTRUCTIVE_IDEMPOTENT)
def session_stop() -> str:
    """Stop the current session and clean up.

    For virtual sessions: terminates KWin, all launched apps, and the D-Bus session.
    For live sessions: disconnects without killing KWin or pre-existing apps.
    Cleans up temporary files and clipboard processes. Safe to call when
    no session is running (returns "No session running.").
    """
    return _engine.session_stop()


# ── Screenshot / Accessibility ───────────────────────────────────────────


@_tool(annotations=_READ_ONLY, images=True)
def screenshot(
    include_cursor: Annotated[
        bool,
        Field(description="If true, render the mouse cursor in the screenshot."),
    ] = False,
    region: Annotated[
        list[int] | None,
        Field(
            description="Crop to [x, y, width, height] in the global logical coordinates "
            "mouse tools take. The part outside the captured workspace is dropped. "
            "Omit for every output.",
            min_length=4,
            max_length=4,
        ),
    ] = None,
    max_edge: Annotated[
        int | None,
        Field(
            description="Downscale so the longer side is at most this many pixels "
            "(0 = full resolution). Omit to use the server's --screenshot-max-edge.",
            ge=0,
        ),
    ] = None,
) -> str:
    """Capture a screenshot of the isolated session.

    Requires an active session. Captures every output, or only ``region``, and
    returns the saved PNG path and size plus a "Coordinate space" line. The
    image is in global logical coordinates: pixel (px, py) shows the point
    (origin_x + px, origin_y + py) that mouse and touch tools take, whatever
    the output scale. The origin can be negative on multi-monitor layouts.
    "coverage partial" lists the regions the capture backend delivered; other
    pixels are transparent. When the image was downscaled, the line also names
    its pixel size and the formula that maps a pixel back to a logical point.
    To read small text on a large desktop, take a downscaled overview, then a
    full-resolution ``region`` around the area of interest. Frames from
    screenshot_after_ms carry the same line and follow --screenshot-max-edge.
    """
    return _engine.screenshot(include_cursor=include_cursor, region=region, max_edge=max_edge)


def _structured(text: str, data: BaseModel) -> CallToolResult:
    """Pair the human-readable text with ``data`` as structured content.

    ``data`` is dumped to a plain dict: a model instance would lose its
    ``null`` fields in the wire dump and then violate the published schema.
    """
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=data.model_dump(mode="json"),
    )


@_tool(annotations=_READ_ONLY)
def accessibility_tree(
    app_name: Annotated[
        str,
        Field(description="Filter to a specific app name (empty string = all apps)."),
    ] = "",
    max_depth: Annotated[int, Field(description="Maximum tree traversal depth.")] = 15,
    role: Annotated[
        str,
        Field(
            description='Filter to elements with this role (e.g. "push button", "text", '
            '"check box"). Empty string = show all roles. Non-matching elements are hidden '
            "but their children are still traversed to find deeper matches."
        ),
    ] = "",
) -> Annotated[CallToolResult, AccessibilityTreeResult]:
    """Get the accessibility tree of apps in the isolated session.

    Returns a formatted text tree with each widget's role, name, states,
    and bounding box in global screen coordinates ("@ screen (x, y, wxh)"),
    the same space mouse_click and touch_tap take. Elements whose window
    cannot be identified with certainty report "@ unavailable (reason)"
    instead of coordinates. Use this to understand UI structure before
    interacting with elements.
    """
    data = _engine.accessibility_tree_data(app_name=app_name, max_depth=max_depth, role=role)
    return _structured(format_tree(data), data)


@_tool(annotations=_READ_ONLY)
def find_ui_elements(
    query: Annotated[
        str,
        Field(
            description="Search text (case-insensitive, matches names/roles/descriptions). "
            "Can be empty string when filtering by states only."
        ),
    ],
    app_name: Annotated[
        str,
        Field(description="Filter to a specific app name (empty string = all apps)."),
    ] = "",
    states: Annotated[
        list[str] | None,
        Field(
            description='Filter by AT-SPI2 states (e.g. ["focused"], ["active", "visible"]). '
            "Only elements matching ALL specified states are returned. "
            "Common states: active, focused, visible, enabled, checked, selected, expanded."
        ),
    ] = None,
) -> Annotated[CallToolResult, FindUIElementsResult]:
    """Find UI elements matching a search query and/or required AT-SPI2 states.

    Returns a list of matching elements with their role, name, bounding box
    in global screen coordinates ("@ screen (x, y, wxh)") — the same space
    mouse_click and touch_tap take — and available actions. Elements whose
    window cannot be identified with certainty report "@ unavailable (reason)"
    instead of coordinates. Use this to locate specific buttons, inputs, or
    labels before clicking or interacting.
    """
    data = _engine.find_ui_elements_data(query=query, app_name=app_name, states=states)
    return _structured(format_find(data), data)


# ── Mouse tools ──────────────────────────────────────────────────────────


@_tool(annotations=_DESTRUCTIVE, images=True)
def mouse_click(
    x: Annotated[
        int,
        Field(description="X coordinate in global logical screen pixels."),
    ],
    y: Annotated[
        int,
        Field(description="Y coordinate in global logical screen pixels."),
    ],
    button: Annotated[
        str, Field(description='Mouse button: "left", "right", or "middle".')
    ] = "left",
    double: Annotated[bool, Field(description="If true, double-click.")] = False,
    triple: Annotated[bool, Field(description="If true, triple-click (overrides double).")] = False,
    modifiers: Annotated[
        list[str] | None,
        Field(description='Modifier keys to hold during click (e.g. ["ctrl"], ["shift", "alt"]).'),
    ] = None,
    hold_ms: Annotated[
        int,
        Field(description="Duration to hold button pressed before release (ms, for long-press)."),
    ] = 0,
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(
            description="Capture screenshots at these delays (ms) after the click. "
            "Example: [0, 50, 200] captures 3 frames showing the click effect."
        ),
    ] = None,
) -> str:
    """Click at coordinates in the isolated session.

    Coordinates are global logical screen pixels, the space window_geometry
    and find_ui_elements report. A screenshot pixel (px, py) is the point
    (origin_x + px, origin_y + py), using the origin from the screenshot's
    "Coordinate space" line; at scale 1 with one output the origin is (0, 0).
    Returns a description of the click performed. Optionally captures
    screenshot frames after the click for visual feedback.
    """
    return _engine.mouse_click(
        x=x,
        y=y,
        button=button,
        double=double,
        triple=triple,
        modifiers=modifiers,
        hold_ms=hold_ms,
        screenshot_after_ms=screenshot_after_ms,
    )


@_tool(annotations=_NON_DESTRUCTIVE_IDEMPOTENT, images=True)
def mouse_move(
    x: Annotated[int, Field(description="X coordinate in pixels.")],
    y: Annotated[int, Field(description="Y coordinate in pixels.")],
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(
            description="Capture screenshots at these delays (ms) after moving. "
            "Useful for observing hover effects and tooltip animations."
        ),
    ] = None,
) -> str:
    """Move the mouse cursor to coordinates without clicking.

    Use this to trigger hover effects, reveal tooltips, or position the
    cursor before a separate button-down action.
    """
    return _engine.mouse_move(x=x, y=y, screenshot_after_ms=screenshot_after_ms)


@_tool(annotations=_DESTRUCTIVE)
def mouse_scroll(
    x: Annotated[int, Field(description="X coordinate in pixels.")],
    y: Annotated[int, Field(description="Y coordinate in pixels.")],
    delta: Annotated[
        int,
        Field(
            description="Scroll amount (positive = down/right, negative = up/left). "
            "Typical values: 1-5 for discrete wheel ticks, 50-200 for smooth pixel scrolling."
        ),
    ],
    horizontal: Annotated[
        bool, Field(description="If true, scroll horizontally instead of vertically.")
    ] = False,
    discrete: Annotated[
        bool,
        Field(
            description="If true, use discrete scroll (wheel ticks) instead of smooth pixels. "
            "Most desktop apps expect discrete scrolling."
        ),
    ] = False,
    steps: Annotated[
        int,
        Field(
            description="Split total delta into this many increments for smooth animation. "
            "Only useful with discrete=false."
        ),
    ] = 1,
) -> str:
    """Scroll at coordinates in the isolated session.

    Moves the cursor to (x, y) and performs a scroll action. Returns a
    description of the scroll performed.
    """
    return _engine.mouse_scroll(
        x=x, y=y, delta=delta, horizontal=horizontal, discrete=discrete, steps=steps
    )


@_tool(annotations=_DESTRUCTIVE, images=True)
def mouse_drag(
    from_x: Annotated[int, Field(description="Starting X coordinate in pixels.")],
    from_y: Annotated[int, Field(description="Starting Y coordinate in pixels.")],
    to_x: Annotated[int, Field(description="Ending X coordinate in pixels.")],
    to_y: Annotated[int, Field(description="Ending Y coordinate in pixels.")],
    button: Annotated[
        str, Field(description='Mouse button: "left", "right", or "middle".')
    ] = "left",
    modifiers: Annotated[
        list[str] | None,
        Field(description='Modifier keys to hold during drag (e.g. ["alt"], ["ctrl"]).'),
    ] = None,
    waypoints: Annotated[
        list[list[int]] | None,
        Field(
            description="Intermediate points as [[x, y, dwell_ms], ...]. "
            "The cursor pauses at each waypoint for dwell_ms milliseconds."
        ),
    ] = None,
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(description="Capture screenshots at these delays (ms) after the drag completes."),
    ] = None,
) -> str:
    """Drag from one point to another in the isolated session.

    Presses the mouse button at (from_x, from_y), moves to (to_x, to_y)
    optionally through waypoints, then releases. Returns a description of
    the drag performed.
    """
    return _engine.mouse_drag(
        from_x=from_x,
        from_y=from_y,
        to_x=to_x,
        to_y=to_y,
        button=button,
        modifiers=modifiers,
        waypoints=waypoints,
        screenshot_after_ms=screenshot_after_ms,
    )


@_tool(annotations=_DESTRUCTIVE_IDEMPOTENT)
def mouse_button_down(
    x: Annotated[int, Field(description="X coordinate in pixels.")],
    y: Annotated[int, Field(description="Y coordinate in pixels.")],
    button: Annotated[
        str, Field(description='Mouse button: "left", "right", or "middle".')
    ] = "left",
) -> str:
    """Press a mouse button at coordinates without releasing.

    Use with mouse_button_up to perform custom drag sequences or
    hold-and-interact patterns. The button stays pressed until
    mouse_button_up is called.
    """
    return _engine.mouse_button_down(x=x, y=y, button=button)


@_tool(annotations=_DESTRUCTIVE_IDEMPOTENT)
def mouse_button_up(
    x: Annotated[int, Field(description="X coordinate in pixels.")],
    y: Annotated[int, Field(description="Y coordinate in pixels.")],
    button: Annotated[
        str, Field(description='Mouse button: "left", "right", or "middle".')
    ] = "left",
) -> str:
    """Release a mouse button at coordinates.

    Pair with mouse_button_down. The release happens at the specified
    coordinates, which may differ from where the button was pressed.
    """
    return _engine.mouse_button_up(x=x, y=y, button=button)


# ── Keyboard tools ───────────────────────────────────────────────────────


@_tool(annotations=_DESTRUCTIVE, images=True)
def keyboard_type(
    text: Annotated[
        str,
        Field(
            description="ASCII text to type. For non-ASCII (Korean, CJK, emoji), "
            "use keyboard_type_unicode instead."
        ),
    ],
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(
            description="Capture screenshots at these delays (ms) after typing. "
            "Useful for observing autocomplete popups and input validation."
        ),
    ] = None,
) -> str:
    """Type ASCII text into the currently focused element.

    Simulates individual key presses for each character. Only supports ASCII
    characters. For non-ASCII text (Korean, CJK, emoji, accented characters),
    use keyboard_type_unicode instead.
    """
    return _engine.keyboard_type(text=text, screenshot_after_ms=screenshot_after_ms)


@_tool(annotations=_DESTRUCTIVE, images=True)
def keyboard_type_unicode(
    text: Annotated[
        str,
        Field(description="Unicode text to type (supports any script: Korean, CJK, emoji, etc.)."),
    ],
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(description="Capture screenshots at these delays (ms) after typing."),
    ] = None,
) -> str:
    """Type arbitrary Unicode text including non-ASCII characters.

    Uses wtype if it succeeds (KWin does not support it), otherwise pastes
    through a temporary clipboard owner: Ctrl+Shift+V in terminals known to
    paste on it (Konsole, GNOME Terminal, kitty, foot, st, ...), Ctrl+V
    elsewhere. The previous clipboard content (all its formats) is restored
    after the paste, and the text is marked as secret so KDE's clipboard
    history skips it. Returns a failure when the clipboard could not be
    taken or nothing requested the text, and without pressing any key when
    the focused window is a recognized terminal with no clipboard paste
    chord (xterm, urxvt) or cannot be identified.
    Use this instead of keyboard_type when the text contains non-ASCII
    characters (e.g. Korean, CJK, emoji, accented characters).
    """
    return _engine.keyboard_type_unicode(text=text, screenshot_after_ms=screenshot_after_ms)


@_tool(annotations=_DESTRUCTIVE, images=True)
def keyboard_key(
    key: Annotated[
        str,
        Field(
            description='Key or combination to press (e.g. "Return", "ctrl+c", '
            '"alt+F4", "Tab", "shift+ctrl+z").'
        ),
    ],
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(
            description="Capture screenshots at these delays (ms) after the key press. "
            "Useful for observing menu openings and dialog transitions."
        ),
    ] = None,
) -> str:
    """Press and release a key or key combination.

    Supports single keys and modifier combinations joined with "+".
    Returns a confirmation of the key pressed.
    """
    return _engine.keyboard_key(key=key, screenshot_after_ms=screenshot_after_ms)


@_tool(annotations=_DESTRUCTIVE_IDEMPOTENT)
def keyboard_key_down(
    key: Annotated[
        str,
        Field(description='Key to press and hold (e.g. "ctrl", "shift", "alt").'),
    ],
) -> str:
    """Press and hold a key without releasing.

    Use with keyboard_key_up to hold modifier keys across multiple actions
    (e.g. hold Ctrl while clicking multiple items). The key stays pressed
    until keyboard_key_up is called with the same key.
    """
    return _engine.keyboard_key_down(key=key)


@_tool(annotations=_DESTRUCTIVE_IDEMPOTENT)
def keyboard_key_up(
    key: Annotated[
        str,
        Field(description='Key to release (e.g. "ctrl", "shift", "alt").'),
    ],
) -> str:
    """Release a previously held key.

    Pair with keyboard_key_down. Must be called to release keys that
    were pressed with keyboard_key_down.
    """
    return _engine.keyboard_key_up(key=key)


# ── Touch tools ──────────────────────────────────────────────────────────


@_tool(annotations=_DESTRUCTIVE, images=True)
def touch_tap(
    x: Annotated[int, Field(description="X coordinate in pixels.")],
    y: Annotated[int, Field(description="Y coordinate in pixels.")],
    hold_ms: Annotated[
        int,
        Field(
            description="Duration to hold before lifting (ms). Use >500 for long-press gestures."
        ),
    ] = 0,
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(description="Capture screenshots at these delays (ms) after the tap."),
    ] = None,
) -> str:
    """Tap at coordinates using touch input.

    Simulates a single finger touch-and-release. Set hold_ms > 0 for
    long-press gestures. Returns a description of the tap performed.
    """
    return _engine.touch_tap(x=x, y=y, hold_ms=hold_ms, screenshot_after_ms=screenshot_after_ms)


@_tool(annotations=_DESTRUCTIVE, images=True)
def touch_swipe(
    from_x: Annotated[int, Field(description="Starting X coordinate in pixels.")],
    from_y: Annotated[int, Field(description="Starting Y coordinate in pixels.")],
    to_x: Annotated[int, Field(description="Ending X coordinate in pixels.")],
    to_y: Annotated[int, Field(description="Ending Y coordinate in pixels.")],
    duration_ms: Annotated[int, Field(description="Duration of the swipe in milliseconds.")] = 300,
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(description="Capture screenshots at these delays (ms) after the swipe."),
    ] = None,
) -> str:
    """Swipe from one point to another using single-finger touch input.

    Returns a description of the swipe performed.
    """
    return _engine.touch_swipe(
        from_x=from_x,
        from_y=from_y,
        to_x=to_x,
        to_y=to_y,
        duration_ms=duration_ms,
        screenshot_after_ms=screenshot_after_ms,
    )


@_tool(annotations=_DESTRUCTIVE, images=True)
def touch_pinch(
    center_x: Annotated[int, Field(description="Center X coordinate of the pinch gesture.")],
    center_y: Annotated[int, Field(description="Center Y coordinate of the pinch gesture.")],
    start_distance: Annotated[
        int, Field(description="Initial distance between two fingers in pixels.")
    ],
    end_distance: Annotated[
        int,
        Field(
            description="Final distance between two fingers in pixels. "
            "Smaller than start_distance = pinch in (zoom out), "
            "larger = pinch out (zoom in)."
        ),
    ],
    duration_ms: Annotated[
        int, Field(description="Duration of the gesture in milliseconds.")
    ] = 500,
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(description="Capture screenshots at these delays (ms) after the pinch."),
    ] = None,
) -> str:
    """Perform a two-finger pinch gesture.

    Simulates two fingers moving symmetrically toward or away from the
    center point. Returns a description of the pinch performed.
    """
    return _engine.touch_pinch(
        center_x=center_x,
        center_y=center_y,
        start_distance=start_distance,
        end_distance=end_distance,
        duration_ms=duration_ms,
        screenshot_after_ms=screenshot_after_ms,
    )


@_tool(annotations=_DESTRUCTIVE, images=True)
def touch_multi_swipe(
    from_x: Annotated[
        int, Field(description="Starting X coordinate (center of finger group) in pixels.")
    ],
    from_y: Annotated[
        int, Field(description="Starting Y coordinate (center of finger group) in pixels.")
    ],
    to_x: Annotated[int, Field(description="Ending X coordinate in pixels.")],
    to_y: Annotated[int, Field(description="Ending Y coordinate in pixels.")],
    fingers: Annotated[int, Field(description="Number of fingers (2-5).")] = 3,
    duration_ms: Annotated[int, Field(description="Duration of the swipe in milliseconds.")] = 300,
    screenshot_after_ms: Annotated[
        list[int] | None,
        Field(description="Capture screenshots at these delays (ms) after the swipe."),
    ] = None,
) -> str:
    """Perform a multi-finger swipe gesture.

    All fingers move in parallel from the start to end coordinates.
    Returns a description of the swipe performed.
    """
    return _engine.touch_multi_swipe(
        from_x=from_x,
        from_y=from_y,
        to_x=to_x,
        to_y=to_y,
        fingers=fingers,
        duration_ms=duration_ms,
        screenshot_after_ms=screenshot_after_ms,
    )


# ── Clipboard tools ──────────────────────────────────────────────────────


@_tool(annotations=_READ_ONLY)
def clipboard_get() -> str:
    """Read the current clipboard content in the isolated session.

    Requires enable_clipboard=true in session_start and wl-clipboard
    installed. Returns the clipboard text or an error message if clipboard
    is not enabled or empty.
    """
    return _engine.clipboard_get()


@_tool(annotations=_NON_DESTRUCTIVE_IDEMPOTENT)
def clipboard_set(
    text: Annotated[str, Field(description="Text to copy to clipboard.")],
) -> str:
    """Set the clipboard content in the isolated session.

    Requires enable_clipboard=true in session_start and wl-clipboard
    installed. The content remains available until replaced by another
    clipboard_set call or the session ends.
    """
    return _engine.clipboard_set(text=text)


# ── Wait-for-UI tools ───────────────────────────────────────────────────


@_tool(annotations=_READ_ONLY)
def wait_for_element(
    query: Annotated[
        str,
        Field(
            description="Search text (case-insensitive, matches names/roles/descriptions). "
            "Can be empty string when waiting for state changes only."
        ),
    ],
    app_name: Annotated[
        str,
        Field(description="Filter to a specific app name (empty string = all apps)."),
    ] = "",
    timeout_ms: Annotated[int, Field(description="Maximum wait time in milliseconds.")] = 5000,
    poll_interval_ms: Annotated[int, Field(description="Polling interval in milliseconds.")] = 200,
    expected_states: Annotated[
        list[str] | None,
        Field(
            description='Wait until elements also have these AT-SPI2 states (e.g. ["active"]). '
            "Useful for waiting until a window becomes active or a checkbox becomes checked. "
            "Common states: active, focused, visible, enabled, checked, selected, expanded."
        ),
    ] = None,
) -> str:
    """Wait for a UI element matching query and/or states to appear.

    Polls repeatedly until a matching element is found or the timeout expires.
    Returns matching elements in the same format as find_ui_elements —
    bounding boxes are global screen coordinates, or "@ unavailable (reason)"
    when the element's window cannot be identified — or a timeout error
    message.
    """
    return _engine.wait_for_element(
        query=query,
        app_name=app_name,
        timeout_ms=timeout_ms,
        poll_interval_ms=poll_interval_ms,
        expected_states=expected_states,
    )


# ── Window management tools ──────────────────────────────────────────────


@_tool(annotations=_SPAWN_COMMAND)
def launch_app(
    command: Annotated[
        str,
        Field(description='Command to launch (e.g. "kcalc" or "/path/to/app --arg").'),
    ],
    env: Annotated[
        dict[str, str] | None,
        Field(description="Extra environment variables to pass to the app."),
    ] = None,
) -> str:
    """Launch an application inside the running isolated session.

    Requires an active session. Returns the app PID (for use with read_app_log)
    and the log file path.
    """
    return _engine.launch_app(command=command, env=env)


@_tool(annotations=_READ_ONLY)
def list_windows() -> Annotated[CallToolResult, ListWindowsResult]:
    """List accessible application windows in the isolated session.

    Uses AT-SPI2 to enumerate top-level applications and their window count.
    Applications that do not support accessibility (AT-SPI2) may not appear.
    Returns a formatted list of app names with window counts.
    """
    data = _engine.list_windows_data()
    return _structured(format_list_windows(data), data)


@_tool(annotations=_NON_DESTRUCTIVE_IDEMPOTENT)
def focus_window(
    app_name: Annotated[
        str,
        Field(description="Application name to focus (case-insensitive substring match)."),
    ],
) -> str:
    """Attempt to focus a window by application name.

    Searches for an application whose name contains the given string
    (case-insensitive) and activates its first focusable window via AT-SPI2.
    """
    return _engine.focus_window(app_name=app_name)


@_tool(annotations=_READ_ONLY)
def window_geometry(
    app_name: Annotated[
        str,
        Field(description="Only report windows whose app name contains this string."),
    ] = "",
    window_id: Annotated[
        str,
        Field(description="Only report the window with exactly this id."),
    ] = "",
) -> Annotated[CallToolResult, WindowGeometryResult]:
    """Report window ids, positions and sizes in global screen coordinates.

    Element rectangles from accessibility_tree and find_ui_elements are
    already translated to this same coordinate space; this tool remains
    useful for locating whole windows and diagnosing placement. Each window
    lists its KWin id (stable while the window exists) and an [active] marker
    on the active window. Pass the id to window_close.
    """
    data = _engine.window_geometry_data(app_name=app_name, window_id=window_id)
    return _structured(format_window_geometry(data, app_name, window_id), data)


@_tool(annotations=_READ_ONLY)
def active_window() -> str:
    """Report the window KWin currently treats as active.

    Returns the same id, frame and client fields as window_geometry for the
    one window that has focus, e.g. after focus_window.
    """
    return _engine.active_window()


@_tool(annotations=_DESTRUCTIVE_IDEMPOTENT)
def window_close(
    window_id: Annotated[
        str,
        Field(description="Id of the window to close, as reported by window_geometry."),
    ],
) -> str:
    """Ask one window to close, like its titlebar close button.

    Only the window with exactly this id is addressed, even when the same app
    has several windows. The app may keep the window open (for example to ask
    about unsaved changes), so confirm with window_geometry. Disabled in live
    sessions (session_connect) to protect unsaved work on the real desktop.
    """
    return _engine.window_close(window_id=window_id)


# ── D-Bus tools ──────────────────────────────────────────────────────────


@_tool(annotations=_DBUS_CALL)
def dbus_call(
    service: Annotated[str, Field(description='D-Bus service name (e.g. "org.kde.KWin").')],
    path: Annotated[str, Field(description='Object path (e.g. "/org/kde/KWin").')],
    interface: Annotated[str, Field(description='Interface name (e.g. "org.kde.KWin.Scripting").')],
    method: Annotated[str, Field(description="Method name to call.")],
    args: Annotated[
        list[str | dict] | None,
        Field(
            description=(
                "Method arguments. Two interchangeable shapes are accepted "
                "and may be mixed in the same list: "
                '(dbus-send) ["string:hello", "int32:42", "boolean:true", '
                '"array:string:a,b", "dict:string:variant:k,string:v,n,int32:3"] OR '
                '(typed JSON) [{"type":"string","value":"hello"}, '
                '{"type":"int32","value":42}, '
                '{"type":"array","element_type":"string","value":["a","b"]}]. '
                "Each argument's type and the count must match one of the method's "
                "declared signatures."
            )
        ),
    ] = None,
) -> str:
    """Call a D-Bus method in the isolated session.

    Executes a D-Bus method call in-process and returns the reply: nothing for
    a void reply, the bare value for a single basic value, JSON otherwise.
    Each entry in ``args`` may use dbus-send notation (``"string:value"``,
    ``"int32:42"``, ``"dict:string:string:KEY,VALUE"``, ...) or the typed-JSON
    shape (``{"type":"string","value":"hello"}``). Both shapes can mix in one
    call. Malformed or mismatched arguments fail without sending anything.
    """
    return _engine.dbus_call(
        service=service, path=path, interface=interface, method=method, args=args
    )


@_tool(annotations=_READ_ONLY)
def read_app_log(
    pid: Annotated[
        int,
        Field(description="PID of the app (returned by launch_app or session_start)."),
    ],
    last_n_lines: Annotated[
        int,
        Field(description="Number of trailing lines to return (0 = all output)."),
    ] = 50,
) -> str:
    """Read stdout/stderr output of a launched app.

    Returns the combined stdout and stderr text captured since the app was
    launched. Use the PID from launch_app or session_start to identify the app.
    """
    return _engine.read_app_log(pid=pid, last_n_lines=last_n_lines)


@_tool(annotations=_READ_ONLY)
def wayland_info(
    filter_protocol: Annotated[
        str,
        Field(
            description="Substring to filter protocol names "
            '(e.g. "plasma_window_management"). Empty = show all.'
        ),
    ] = "",
) -> str:
    """List Wayland protocols available in the isolated session.

    Runs wayland-info to enumerate all exposed Wayland globals. Useful for
    verifying that restricted protocols are accessible. Returns the full
    output or only lines matching the filter.
    """
    return _engine.wayland_info(filter_protocol=filter_protocol)


# Set by the signal handler when the first shutdown signal arrives. The handler
# can only record the signal: while mcp.run() owns the main thread, raising
# through it (or blocking it on the stop) unwinds through anyio task groups that
# wait on the stdio reader's uncancellable stdin readline, so cleanup would not
# start until the client closed stdin. The daemon watcher thread below waits on
# this event and runs the shared exit path off the main thread instead.
_signal_received = threading.Event()
# The first signal's number, written before the event is set so it is stable by
# the time the watcher wakes; a later signal must not overwrite it (the handler
# returns early once shutdown has begun).
_signal_signum = 0
# The exit path can be entered twice — the watcher thread on a signal, the main
# thread when mcp.run() ends — but runs once. The first caller proceeds; a
# second blocks in acquire() until the winner's os._exit ends the process, so a
# signal landing mid-cleanup can never start a second stop.
_finish_lock = threading.Lock()


def _shutdown_handler(signum: int, _frame: object) -> None:
    # Handlers run serialized on the main thread, so the check-and-set is atomic:
    # the first signal wins, and once shutdown has begun (an earlier signal set
    # the event, or the EOF path set _shutdown) a later one is ignored — it must
    # not wake a second exit path or rewrite the exit code.
    global _signal_signum
    if _signal_received.is_set() or _shutdown.is_set():
        return
    _signal_signum = signum
    _signal_received.set()


def _signal_watcher() -> NoReturn:
    """Wait for the first shutdown signal, then exit via the shared finish routine.

    Running on its own thread is what makes the signal path prompt: the cleanup
    does not have to unwind whatever the main thread was doing when the signal
    arrived (an in-flight asyncio task, the loop's own waits).
    """
    _signal_received.wait()
    _finish(128 + _signal_signum)


def _exit_cleanup() -> str:
    """Stop the session (or terminate the owned groups) at server exit; return the outcome."""
    # The engine is not thread-safe and is owned by the single tool thread, so the
    # thread running the exit path must never call into it while the tool thread
    # may be using it. The final session_stop therefore always runs on the tool
    # thread (serialized with any in-flight tool); the exiting thread only decides
    # whether the stop can run at all.
    stop_started = threading.Event()

    def _stop_for_exit() -> None:
        # First statement: lets the exiting thread tell "the stop is running on
        # the tool thread" from "the stop is still queued behind an in-flight
        # tool".
        stop_started.set()
        _engine.session_stop()

    stop = _tool_executor.submit(_stop_for_exit)
    # Give the stop a short chance to start. If the tool thread is free the stop
    # starts immediately and we wait for it to finish (bounded by its own waits;
    # no forced exit while it runs). If an in-flight tool still holds the tool
    # thread the stop stays queued: cancel it before it runs, then terminate the
    # owned process groups and temp dirs directly, without touching the engine.
    if not stop_started.wait(timeout=EXIT_DRAIN_SECONDS) and stop.cancel():
        # The stop is still pending behind the in-flight tool, so it never runs.
        # Clean up the owned groups and dirs directly, without touching the engine.
        process_registry.close()
        process_registry.terminate_all()
        return "owned-process registry path (stop cancelled before it ran; owned groups terminated)"
    # The stop started (or started in the race just after the drain). Wait for it to
    # finish on the tool thread; session_stop's own waits are bounded.
    try:
        stop.result()
    except Exception as exc:  # a failed stop must still exit, with the outcome logged
        return f"serialized session_stop path raised: {type(exc).__name__}: {exc}"
    return "serialized session_stop path (completed on the tool thread)"


def _write_stderr(data: bytes) -> None:
    # Raw writes to fd 2: the sys.stderr buffer lock may be held by a thread blocked
    # on the same pipe, and a broken pipe just ends the report.
    with contextlib.suppress(OSError):
        while data:
            data = data[os.write(2, data) :]


def _report_exit(report: str) -> None:
    """Write ``report`` to stderr, waiting at most ``EXIT_REPORT_SECONDS``."""
    with contextlib.suppress(Exception):
        writer = threading.Thread(
            target=_write_stderr,
            args=(report.encode(errors="replace"),),
            name="kwin-mcp-exit-report",
            daemon=True,
        )
        writer.start()
        writer.join(EXIT_REPORT_SECONDS)


def _finish(exit_code: int, failure: str = "") -> NoReturn:
    """Run the once-only exit cleanup and leave the process with ``exit_code``.

    Whichever path arrives first — the watcher thread after a signal or the main
    thread when ``mcp.run()`` ends — owns the cleanup; a second caller blocks in
    ``_finish_lock.acquire()`` until the process exits, so the stop can never run
    twice. ``failure`` is a traceback reported ahead of the cleanup outcome.
    """
    _finish_lock.acquire()
    # First statement under the lock: tool bodies queued from now on must not run.
    _shutdown.set()
    # Every failure of the cleanup is reported, never raised: the forced exit
    # below is what keeps interpreter shutdown from joining a busy tool thread.
    try:
        outcome = _exit_cleanup()
    except Exception as exc:
        outcome = f"cleanup raised: {type(exc).__name__}: {exc}"
    # On a signal the stdio read worker is still blocked on readline of the stdin
    # pipe (the client does not close it), and interpreter shutdown would hang
    # joining that non-daemon thread (or a still-busy tool thread); exit directly
    # on both the EOF and signal paths once the session is stopped. Nothing on the
    # way out may block on a client pipe: stdout is not flushed (the transport
    # flushes each message itself, and a writer stuck on an undrained pipe holds the
    # buffer lock a flush would wait on), and the report is bounded, so neither a
    # full nor a closed stdout/stderr can delay or skip the exit.
    _report_exit(f"{failure}kwin-mcp: exit cleanup: {outcome}\n")
    os._exit(exit_code)


def main() -> None:
    """Run the MCP server.

    Supports ``--default-live-session`` flag to make session_connect the default
    session tool instead of session_start, ``--screenshot-images`` flag to attach
    captured screenshot PNGs to tool results as image content, and
    ``--screenshot-max-edge N`` to bound every screenshot's longer side.

    All exit paths share the once-only cleanup in ``_finish``: ``mcp.run()``
    returning (stdin EOF) exits 0, an SDK/transport exception exits 1 after a
    traceback, and the first SIGTERM/SIGHUP/SIGINT exits 128+signum (143/129/130)
    via the watcher thread — the signal handler only records the signum and never
    raises, so a signal mid-request cannot stall the cleanup on the loop.
    """
    # Remove our custom flags before MCP framework parses args
    for flag in ("--default-live-session", "--screenshot-images"):
        if flag in sys.argv:
            sys.argv.remove(flag)
    # Later slices first so earlier indexes stay valid. A second occurrence already
    # exited in _screenshot_max_edge above; this drops whichever remains.
    for span in reversed(_max_edge_flag_spans(sys.argv)):
        del sys.argv[span[0] : span[1]]
    # SIGTERM/SIGHUP have no default handler and would kill the process without
    # cleanup; SIGINT is included because installing our own handler keeps
    # asyncio.Runner from installing its KeyboardInterrupt-raising default (it
    # only replaces signal.default_int_handler), which unwound through the loop
    # the same way. signal.signal is only allowed on the main thread, so the
    # handlers are installed here — never inside the watcher thread.
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, _shutdown_handler)
    # Daemon: a still-pending wait must not keep the process alive on the EOF path.
    threading.Thread(target=_signal_watcher, name="kwin-mcp-shutdown", daemon=True).start()
    exit_code = 0
    failure = ""
    try:
        mcp.run()
    except KeyboardInterrupt:
        # Only a KeyboardInterrupt already pending when our SIGINT handler was
        # installed (or one raised directly) reaches here; still exit 130.
        exit_code = 130
    except Exception:
        # Surface a transport/SDK failure and exit non-zero instead of the
        # conventional 0 (the old interpreter-shutdown path did the same). The
        # traceback goes out with the bounded exit report, never on its own.
        exit_code = 1
        with contextlib.suppress(Exception):
            failure = traceback.format_exc()
    finally:
        _finish(exit_code, failure)


if __name__ == "__main__":
    main()
