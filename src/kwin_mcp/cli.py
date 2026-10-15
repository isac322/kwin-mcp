"""Interactive CLI for the kwin-mcp automation engine.

Supports both interactive REPL and pipe mode (non-TTY stdin).
Uses cmd.Cmd which automatically switches to non-interactive mode
when stdin is not a TTY. The commands are the 29 tools, with the same parameters,
and each prints the same text the MCP server returns (``result.render()``).

Object, list and dict parameters take JSON, validated like the MCP arguments.

Usage:
    # Interactive mode
    $ uv run kwin-mcp-cli
    kwin-mcp> session_start
    kwin-mcp> app_launch command=kcalc
    kwin-mcp> element_query locator='{"app":"kcalc"}' max_depth=3
    kwin-mcp> pointer_click target='{"x":100,"y":200}'
    kwin-mcp> session_stop
    kwin-mcp> quit

    # Pipe mode
    uv run kwin-mcp-cli <<'EOF'
    session_start
    app_launch command=kcalc
    element_query
    session_stop
    EOF
"""

from __future__ import annotations

import argparse
import cmd
import contextlib
import inspect
import json
import keyword
import shlex
import signal
import sys
import traceback
import types
import typing
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

from pydantic import BaseModel, TypeAdapter

from kwin_mcp import models
from kwin_mcp.core import AutomationEngine
from kwin_mcp.session import process_registry

if TYPE_CHECKING:
    from collections.abc import Callable, Generator


def _needs_json(annotation: object) -> bool:
    """Whether a parameter of this type takes JSON: a model, a list, a dict, or a
    union (e.g. ``Target``, ``X | None``) with such an arm."""
    origin = typing.get_origin(annotation)
    if origin is Annotated:
        return _needs_json(typing.get_args(annotation)[0])
    if origin is typing.Union or origin is types.UnionType:
        return any(_needs_json(arm) for arm in typing.get_args(annotation))
    if origin in (list, dict):
        return True
    return inspect.isclass(annotation) and issubclass(annotation, BaseModel)


def _scalar_type(annotation: object) -> object:
    """``annotation`` without ``Annotated`` and without a ``None`` union arm."""
    origin = typing.get_origin(annotation)
    if origin is Annotated:
        return _scalar_type(typing.get_args(annotation)[0])
    if origin is typing.Union or origin is types.UnionType:
        arms = [arm for arm in typing.get_args(annotation) if arm is not type(None)]
        if len(arms) == 1:
            return _scalar_type(arms[0])
    return annotation


# Union aliases of models.py that help shows by name (``Target``, not its arms).
_NAMED_UNIONS: dict[str, object] = {
    name: value
    for name, value in vars(models).items()
    if not name.startswith("_") and isinstance(value, types.UnionType)
}


def _public_name(name: str) -> str:
    """The tool-side name of an engine parameter (``from_`` is the tool's ``from``)."""
    if name.endswith("_") and keyword.iskeyword(name[:-1]):
        return name[:-1]
    return name


def _type_label(annotation: object) -> str:
    """A readable type for ``help``: model and alias names, no module paths."""
    origin = typing.get_origin(annotation)
    if origin is Annotated:
        return _type_label(typing.get_args(annotation)[0])
    for alias, value in _NAMED_UNIONS.items():
        if annotation == value:
            return alias
    if origin is typing.Union or origin is types.UnionType:
        return " | ".join(_type_label(arm) for arm in typing.get_args(annotation))
    if origin is Literal:
        return f"Literal[{', '.join(repr(value) for value in typing.get_args(annotation))}]"
    if origin is not None:
        args = ", ".join(_type_label(arg) for arg in typing.get_args(annotation))
        return f"{getattr(origin, '__name__', str(origin))}[{args}]"
    if annotation is type(None):
        return "None"
    return getattr(annotation, "__name__", str(annotation))


def _default_text(value: object) -> str:
    """A default as ``help`` shows it: models as their JSON, scalars as Python literals."""
    if isinstance(value, BaseModel):
        return json.dumps(value.model_dump(mode="json", exclude_none=True))
    return repr(value)


def _parse_value(value_str: str, annotation: object) -> object:
    """Convert one ``key=value`` value to the parameter's type.

    Models, unions containing one (``Target``, ``ElementAddress``), lists and dicts
    are JSON-decoded and validated with a pydantic ``TypeAdapter``, so the engine
    receives model instances, e.g. ``target='{"x":10,"y":20}'`` becomes ``Point``.
    ``bool``/``int``/``float`` are converted; anything else stays a string. Every
    value is then validated against the annotation (``Literal`` choices, bounds), so
    a typo such as ``phase=movee`` fails before the engine sends any input.
    """
    if annotation is None:
        return value_str
    if _needs_json(annotation):
        return TypeAdapter[object](annotation).validate_json(value_str)
    scalar = _scalar_type(annotation)
    value: object = value_str
    if scalar is bool:
        value = value_str.lower() in ("true", "1", "yes")
    elif scalar is int:
        value = int(value_str)
    elif scalar is float:
        value = float(value_str)
    return TypeAdapter[object](annotation).validate_python(value)


def _parameter_types(method: Callable[..., Any]) -> dict[str, object]:
    """The parameter annotations of an engine method, resolved at runtime."""
    resolved = typing.get_type_hints(method, include_extras=True)
    return {
        name: resolved.get(name) for name in inspect.signature(method).parameters if name != "self"
    }


def _parameter_name(key: str, types_: dict[str, object]) -> str:
    # A Python keyword cannot be a parameter name: the tool's "from" is the engine's
    # "from_", and both spellings are accepted.
    if key not in types_ and f"{key}_" in types_:
        return f"{key}_"
    return key


def _parse_args(method: Callable[..., Any], arg_string: str) -> dict[str, object]:
    """Parse 'key=value' arguments or a JSON object into kwargs for an engine method.

    Supports:
      - key=value key2=value2
      - key="value with spaces"
      - target='{"x": 10, "y": 20}'  (JSON for object, list and dict parameters)
      - {"key": "value"}  (one JSON object holding every argument)

    An unknown key is passed through, so the call fails with the method's own
    "unexpected keyword argument" error.
    """
    arg_string = arg_string.strip()
    if not arg_string:
        return {}
    types_ = _parameter_types(method)
    kwargs: dict[str, object] = {}

    if arg_string.startswith("{"):
        decoded = json.loads(arg_string)
        if not isinstance(decoded, dict):
            msg = "Expected a JSON object of arguments"
            raise ValueError(msg)
        for key, value in cast("dict[str, object]", decoded).items():
            name = _parameter_name(key, types_)
            annotation = types_.get(name)
            if annotation is not None:
                value = TypeAdapter[object](annotation).validate_python(value)
            kwargs[name] = value
        return kwargs

    # Parse key=value pairs using shlex for proper quoting
    for token in shlex.split(arg_string):
        if "=" not in token:
            msg = f"Invalid argument: {token!r} (expected key=value)"
            raise ValueError(msg)
        key, value = token.split("=", 1)
        name = _parameter_name(key, types_)
        kwargs[name] = _parse_value(value, types_.get(name))
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
        self._shipped_docs: dict[str, tuple[str, dict[str, str]]] | None = None

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
        """The public methods of the engine: exactly the 29 tools, by name."""
        commands: dict[str, Callable[..., Any]] = {}
        for name in sorted(dir(self.engine)):
            if name.startswith("_"):
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
            # The same text the MCP server returns for this call.
            print(result.render())
        except (ValueError, RuntimeError, OSError, TypeError) as exc:
            # Tool errors (invalid input, refusals, failed operations, argument typos):
            # the message, like the MCP isError text (its remedy line follows on the
            # next line). Anything else is a programming error and keeps its traceback.
            print(f"Error: {exc}", file=sys.stderr)
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

        command = self._commands.get(arg)
        if command is None:
            print(f"Unknown command: {arg}")
            return

        sig = inspect.signature(command)
        types_ = typing.get_type_hints(command, include_extras=True)
        description, docs = self._tool_docs().get(arg, ("", {}))
        params = []
        for name, param in sig.parameters.items():
            if name == "self":
                continue
            public = _public_name(name)
            label = _type_label(types_.get(name))
            if param.default is inspect.Parameter.empty:
                entry = f"  {public}: {label} (required)"
            else:
                entry = f"  {public}: {label} = {_default_text(param.default)}"
            params.append(entry)
            doc = docs.get(public)
            if doc:
                params.append(f"      {doc}")

        print(f"\n{arg}")
        if params:
            print("Parameters:")
            for p in params:
                print(p)
        if description:
            print(f"\n{description}")

    def _tool_docs(self) -> dict[str, tuple[str, dict[str, str]]]:
        """The shipped MCP descriptions, built once per shell (they never change).

        Live session mode changes only the intro, so the texts are the plain ones.
        """
        if self._shipped_docs is None:
            # Imported here: the server module is only needed for help texts.
            from kwin_mcp.server import tool_docs

            self._shipped_docs = tool_docs()
        return self._shipped_docs

    def do_quit(self, _arg: str) -> bool:
        """Exit the CLI."""
        self._cleanup()
        return True

    do_exit = do_quit
    do_EOF = do_quit  # noqa: N815

    def _cleanup(self) -> None:
        """Clean up the session and sweep any owned processes left outside it on exit."""
        try:
            if self.engine._session is not None and self.engine._session.is_running:
                print("Stopping session...")
                # A signal must not interrupt the teardown (it would skip launched apps).
                with _defer_shutdown_signals():
                    print(self.engine.session_stop().render())
        except Exception:
            traceback.print_exc()
        finally:
            self._sweep_owned_processes()

    def _sweep_owned_processes(self) -> None:
        """Terminate owned groups the session does not know about, then owned temp dirs.

        A signal can interrupt ``launch_app`` between ``process_registry.spawn``
        (which already registered the child's group) and the session recording the
        app's ``AppInfo``; ``session_stop`` then never sees that child, so the
        registry sweeps the residual here. The same applies to a live session, whose
        KWin is never registered and therefore never touched. The sweep runs on every
        exit path under the same deferral as the session teardown, so a later signal
        cannot abort the SIGTERM/SIGKILL escalation mid-flight.
        """
        try:
            with _defer_shutdown_signals():
                process_registry.close()
                process_registry.terminate_all()
        except Exception:
            # The sweep is the last line of defense: report it, never raise through exit.
            traceback.print_exc()

    def postcmd(self, stop: bool, line: str) -> bool:
        """Print a blank line after each command for readability (interactive only)."""
        if sys.stdin.isatty() and not stop:
            print()
        return stop


def _build_parser() -> argparse.ArgumentParser:
    """The ``kwin-mcp-cli`` argument parser (its help is ``kwin-mcp-cli --help``)."""
    parser = argparse.ArgumentParser(
        prog="kwin-mcp-cli",
        description=(
            "kwin-mcp interactive CLI: the 29 kwin-mcp tools as commands, without an MCP "
            "client. Type 'help' at the prompt for the commands."
        ),
    )
    parser.add_argument(
        "--default-live-session",
        action="store_true",
        help="Show the live-session prompt; session_connect (the live desktop) is the "
        "intended session command instead of session_start (an isolated virtual session).",
    )
    return parser


def main() -> None:
    """Entry point for the kwin-mcp-cli command.

    Supports ``--default-live-session`` flag to switch the default session mode
    from virtual to live. In live mode, the prompt changes and session_connect
    becomes the recommended session tool.
    """
    args = _build_parser().parse_args()

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
