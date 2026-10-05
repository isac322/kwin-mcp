"""Interactive CLI for kwin-mcp automation engine.

Supports both interactive REPL and pipe mode (non-TTY stdin).
Uses cmd.Cmd which automatically switches to non-interactive mode
when stdin is not a TTY.

Usage:
    # Interactive mode
    $ uv run kwin-mcp-cli
    kwin-mcp> session_start app_command=kcalc
    kwin-mcp> accessibility_tree app_name=kcalc max_depth=3
    kwin-mcp> session_stop
    kwin-mcp> quit

    # Pipe mode
    uv run kwin-mcp-cli <<'EOF'
    session_start app_command=kcalc
    accessibility_tree
    session_stop
    EOF
"""

from __future__ import annotations

import cmd
import contextlib
import inspect
import json
import shlex
import signal
import sys
import traceback
from typing import TYPE_CHECKING, Any, cast

from kwin_mcp.core import AutomationEngine

if TYPE_CHECKING:
    from collections.abc import Callable, Generator


def _parse_value(value_str: str, annotation: type | None) -> object:
    """Convert a string value to the appropriate Python type based on annotation."""
    if annotation is None:
        return value_str

    # Unwrap Optional / Union types (e.g. list[int] | None)
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", None)
    if origin is type(int | str) and args is not None:
        # Get non-None args
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            annotation = non_none[0]
            origin = getattr(annotation, "__origin__", None)

    if annotation is bool:
        return value_str.lower() in ("true", "1", "yes")
    if annotation is int:
        return int(value_str)
    if annotation is float:
        return float(value_str)
    if annotation is str:
        return value_str

    # Handle list[...] and dict[...] via JSON
    if origin in (list, dict):
        return json.loads(value_str)

    return value_str


def _parse_args(method: Callable[..., Any], arg_string: str) -> dict[str, object]:
    """Parse 'key=value' arguments or a JSON object into kwargs for a method.

    Supports:
      - key=value key2=value2
      - key="value with spaces"
      - {"key": "value"}  (JSON object)
    """
    arg_string = arg_string.strip()
    if not arg_string:
        return {}

    # Try JSON first
    if arg_string.startswith("{"):
        return json.loads(arg_string)

    # Use get_type_hints() to resolve string annotations from `from __future__ import annotations`
    import typing

    try:
        resolved = typing.get_type_hints(method)
    except Exception:
        resolved = {}

    sig = inspect.signature(method)
    hints: dict[str, type | None] = {}
    for name in sig.parameters:
        if name == "self":
            continue
        hints[name] = resolved.get(name)

    # Parse key=value pairs using shlex for proper quoting
    kwargs: dict[str, object] = {}
    tokens = shlex.split(arg_string)
    for token in tokens:
        if "=" not in token:
            msg = f"Invalid argument: {token!r} (expected key=value)"
            raise ValueError(msg)
        key, value = token.split("=", 1)
        annotation = hints.get(key)
        kwargs[key] = _parse_value(value, annotation)

    return kwargs


# State for deferring shutdown signals while the session is being torn down. A
# process-directed SIGTERM/SIGHUP can be delivered to any thread, so the deferral lives
# at the Python handler level (not a per-thread sigmask): the handler records the signal
# and, once teardown finishes, the context manager re-raises it as KeyboardInterrupt.
_teardown_depth = 0
_pending_shutdown_signal: int | None = None


def _on_shutdown_signal(signum: int, _frame: object) -> None:
    """Handle SIGTERM/SIGHUP/SIGINT.

    While the session is being torn down, record the signal and return so it is delivered
    once teardown finishes; otherwise raise KeyboardInterrupt (the CLI's normal exit path).
    """
    global _pending_shutdown_signal
    if _teardown_depth > 0:
        _pending_shutdown_signal = signum
        return
    raise KeyboardInterrupt


@contextlib.contextmanager
def _defer_shutdown_signals() -> Generator[None]:
    """Defer SIGTERM/SIGHUP/SIGINT while the session is being torn down.

    A signal arriving mid-teardown would otherwise interrupt the app-kill loop
    (``LiveSession.stop`` clears its running flag before terminating the apps), and the
    signal-path cleanup would then see the session as not running and skip the remaining
    apps. The deferral is at the Python handler level, so it holds no matter which thread
    receives a process-directed signal; a recorded signal raises KeyboardInterrupt on exit
    so the CLI still exits through its normal path.
    """
    global _teardown_depth, _pending_shutdown_signal
    _teardown_depth += 1
    try:
        yield
    finally:
        _teardown_depth -= 1
        if _teardown_depth == 0 and _pending_shutdown_signal is not None:
            _pending_shutdown_signal = None
            raise KeyboardInterrupt


class KwinMcpShell(cmd.Cmd):
    """Interactive shell for kwin-mcp automation."""

    intro = "kwin-mcp CLI. Type 'help' for commands, 'quit' to exit."
    prompt = "kwin-mcp> "

    def __init__(
        self,
        engine: AutomationEngine | None = None,
        *,
        live_session_mode: bool = False,
    ) -> None:
        super().__init__()
        self.engine = engine or AutomationEngine()
        self.live_session_mode = live_session_mode
        self._commands: dict[str, Callable[..., Any]] = self._discover_commands()

        if live_session_mode:
            self.intro = (
                "kwin-mcp CLI (live session mode). Type 'help' for commands, 'quit' to exit."
            )
            self.prompt = "kwin-mcp[live]> "

        # Suppress prompt in pipe mode
        if not sys.stdin.isatty():
            self.prompt = ""
            self.intro = ""

    def _discover_commands(self) -> dict[str, Callable[..., Any]]:
        """Discover all public methods on the engine.

        ``*_data`` methods return typed results for the MCP server; their text
        counterparts (same name without the suffix) are the CLI commands.
        """
        commands: dict[str, Callable[..., Any]] = {}
        for name in sorted(dir(self.engine)):
            if name.startswith("_") or name.endswith("_data"):
                continue
            attr = getattr(self.engine, name)
            if callable(attr):
                commands[name] = cast("Callable[..., Any]", attr)
        return commands

    def default(self, line: str) -> None:
        """Handle commands by dispatching to engine methods."""
        parts = line.split(None, 1)
        cmd_name = parts[0]
        arg_string = parts[1] if len(parts) > 1 else ""

        method = self._commands.get(cmd_name)
        if method is None:
            print(f"Unknown command: {cmd_name}. Type 'help' for available commands.")
            return

        try:
            kwargs = _parse_args(method, arg_string)
            if cmd_name == "session_stop":
                # A signal must not interrupt the teardown (it would skip launched apps).
                with _defer_shutdown_signals():
                    result = method(**kwargs)
            else:
                result = method(**kwargs)
            print(result)
        except Exception:
            traceback.print_exc()

    def completenames(self, text: str, *_ignored: object) -> list[str]:
        """Tab-complete command names."""
        matches = [name for name in self._commands if name.startswith(text)]
        # Also include built-in commands
        matches.extend(super().completenames(text, *_ignored))
        return matches

    def do_help(self, arg: str) -> None:
        """Show help for a command, or list all commands."""
        if not arg:
            print("Available commands:\n")
            for name, method in self._commands.items():
                doc = (method.__doc__ or "").split("\n")[0]
                print(f"  {name:30s} {doc}")
            print(f"\n  {'help <command>':30s} Show detailed help for a command")
            print(f"  {'quit':30s} Exit the CLI")
            return

        method = self._commands.get(arg)
        if method is None:
            print(f"Unknown command: {arg}")
            return

        sig = inspect.signature(method)
        params = []
        for name, param in sig.parameters.items():
            if name == "self":
                continue
            ann = param.annotation
            type_name = getattr(ann, "__name__", str(ann)) if ann != inspect.Parameter.empty else ""
            if param.default is not inspect.Parameter.empty:
                params.append(f"  {name}: {type_name} = {param.default!r}")
            else:
                params.append(f"  {name}: {type_name} (required)")

        print(f"\n{arg}")
        if params:
            print("Parameters:")
            for p in params:
                print(p)
        if method.__doc__:
            print(f"\n{method.__doc__}")

    def do_quit(self, _arg: str) -> bool:
        """Exit the CLI."""
        self._cleanup()
        return True

    do_exit = do_quit
    do_EOF = do_quit  # noqa: N815

    def _cleanup(self) -> None:
        """Clean up session on exit."""
        try:
            if self.engine._session is not None and self.engine._session.is_running:
                print("Stopping session...")
                # A signal must not interrupt the teardown (it would skip launched apps).
                with _defer_shutdown_signals():
                    print(self.engine.session_stop())
        except Exception:
            traceback.print_exc()

    def postcmd(self, stop: bool, line: str) -> bool:
        """Print a blank line after each command for readability (interactive only)."""
        if sys.stdin.isatty() and not stop:
            print()
        return stop


def main() -> None:
    """Entry point for the kwin-mcp-cli command.

    Supports ``--default-live-session`` flag to switch the default session mode
    from virtual to live. In live mode, the prompt changes and session_connect
    becomes the recommended session tool.
    """
    import argparse

    parser = argparse.ArgumentParser(description="kwin-mcp interactive CLI")
    parser.add_argument(
        "--default-live-session",
        action="store_true",
        help="Use live (real desktop) session as the default instead of virtual/isolated.",
    )
    args = parser.parse_args()

    shell = KwinMcpShell(live_session_mode=args.default_live_session)

    # SIGTERM, SIGHUP, and SIGINT are routed through _on_shutdown_signal so that a
    # signal during teardown is deferred (not raised through the app-kill loop), and a
    # signal outside teardown raises KeyboardInterrupt (the CLI's normal exit path).
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, _on_shutdown_signal)

    try:
        shell.cmdloop()
    except KeyboardInterrupt:
        # One-way: a later signal must not throw through the cleanup (it would abort
        # the stop half-done). _cleanup only catches Exception, not KeyboardInterrupt.
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(signum, signal.SIG_IGN)
        print("\nInterrupted.")
        shell._cleanup()


if __name__ == "__main__":
    main()
