"""How ``kwin-mcp-cli`` reports a failed command, without a KWin session.

A tool error (invalid input, a refusal, a failed operation, a mistyped keyword) is
printed on stderr as ``Error: <message>``: the first line carries the prefix and
any further message lines (a remedy, a validation detail) follow unchanged, the
CLI form of the MCP ``isError`` text. Any other exception is a programming error
and keeps its traceback. Commands are the tool names; each prints ``render()``.
"""

from __future__ import annotations

import re
from typing import cast

import pytest
from mcp_harness import running_mcp_server

from kwin_mcp.cli import KwinMcpShell
from kwin_mcp.core import AutomationEngine

# Imported at runtime: the shell resolves the stub's annotations with get_type_hints.
from kwin_mcp.models import (
    AppLaunchResult,
    ClipboardWriteResult,
    DesktopList,
    SessionInfo,
    WaylandGlobals,
)


class _StubEngine:
    """Engine stand-in whose public methods become the shell's commands."""

    def session_start(self, screen_width: int = 1920) -> SessionInfo:
        msg = f"screen_width must be a positive integer, got {screen_width}"
        raise ValueError(msg)

    def wayland_list(self, filter: str = "") -> WaylandGlobals:  # noqa: A002
        msg = "wayland-info failed: synthetic failure"
        raise RuntimeError(msg)

    def app_launch(self, command: str) -> AppLaunchResult:
        raise FileNotFoundError(2, "No such file or directory", command)

    def desktop_list(self) -> DesktopList:
        raise KeyError("missing")

    def clipboard_write(self, text: str = "") -> ClipboardWriteResult:
        return ClipboardWriteResult(length=len(text), echo=repr(text))


def _shell() -> KwinMcpShell:
    return KwinMcpShell(engine=cast("AutomationEngine", _StubEngine()))


def _assert_one_error_line(stderr: str, prefix: str) -> None:
    assert stderr.startswith(prefix), stderr
    assert stderr.count("\n") == 1, stderr
    assert stderr.endswith("\n"), stderr
    assert "Traceback" not in stderr, stderr


@pytest.mark.parametrize(
    ("line", "prefix"),
    [
        ("session_start screen_width=0", "Error: screen_width must be a positive integer, got 0"),
        ("wayland_list", "Error: wayland-info failed: synthetic failure"),
        (
            "app_launch command=/nonexistent-app",
            "Error: [Errno 2] No such file or directory: '/nonexistent-app'",
        ),
        # An unknown keyword is a TypeError from the call itself.
        ("clipboard_write nope=1", "Error: "),
    ],
    ids=["value-error", "runtime-error", "os-error", "type-error"],
)
def test_tool_errors_print_one_error_line(
    line: str, prefix: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _shell().default(line)

    captured = capsys.readouterr()
    assert captured.out == ""
    _assert_one_error_line(captured.err, prefix)


def test_argument_without_equals_prints_one_error_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _shell().default("clipboard_write notkeyvalue")

    captured = capsys.readouterr()
    assert captured.out == ""
    _assert_one_error_line(captured.err, "Error: Invalid argument")


def test_programming_errors_keep_their_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    _shell().default("desktop_list")

    captured = capsys.readouterr()
    assert "Traceback" in captured.err, captured.err
    assert "KeyError: 'missing'" in captured.err, captured.err
    assert "Error: " not in captured.err.splitlines()[0], captured.err


def test_successful_command_prints_its_result(capsys: pytest.CaptureFixture[str]) -> None:
    _shell().default("clipboard_write text=hello")

    captured = capsys.readouterr()
    assert captured.out == "Clipboard set: 'hello'\n"
    assert captured.err == ""


# Real engine, no session: every line fails before any KWin is needed.
_SINGLE_LINE_ERRORS = [
    ("screen_capture max_edge=abc", "Error: invalid literal for int() with base 10: 'abc'"),
    ("pointer_click target", "Error: Invalid argument: 'target' (expected key=value)"),
    ("keyboard_type text='unterminated", "Error: No closing quotation"),
    ("pointer_click nosuchparam=1", "Error: "),
]
_VALIDATION_ERRORS = [
    "pointer_click target='{bad json'",
    """pointer_click target='{"x":"a","y":1}'""",
]


@pytest.mark.qa("QA-CLI-005")
def test_one_line_errors(capsys: pytest.CaptureFixture[str]) -> None:
    """One ``Error:`` line per failed command, no traceback, nothing on stdout.

    Single-line messages print exactly one line. A pydantic validation message
    is multi-line; per the contract (CLI errors print ``Error: <first line>`` with
    the remaining message lines after) its first line carries the only prefix.
    """
    shell = KwinMcpShell(engine=AutomationEngine())

    for line, prefix in _SINGLE_LINE_ERRORS:
        shell.default(line)
        captured = capsys.readouterr()
        assert captured.out == "", (line, captured.out)
        _assert_one_error_line(captured.err, prefix)
    assert "unexpected keyword argument 'nosuchparam'" in (
        _error_of(shell, "pointer_click nosuchparam=1", capsys)
    )

    for line in _VALIDATION_ERRORS:
        stderr = _error_of(shell, line, capsys)
        lines = stderr.splitlines()
        assert lines[0].startswith("Error: "), (line, stderr)
        assert re.match(r"Error: \d+ validation errors? for ", lines[0]), (line, stderr)
        assert sum(item.startswith("Error:") for item in lines) == 1, (line, stderr)
        assert "Traceback" not in stderr, stderr

    shell.default("nosuchcommand")
    captured = capsys.readouterr()
    assert captured.out == ("Unknown command: nosuchcommand. Type 'help' for available commands.\n")
    assert captured.err == ""


def _error_of(shell: KwinMcpShell, line: str, capsys: pytest.CaptureFixture[str]) -> str:
    shell.default(line)
    captured = capsys.readouterr()
    assert captured.out == "", (line, captured.out)
    return captured.err


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.slow
@pytest.mark.timeout(600)
@pytest.mark.anyio
@pytest.mark.qa("QA-CLI-006")
async def test_cli_help_shows_types(capsys: pytest.CaptureFixture[str]) -> None:
    async with running_mcp_server() as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    shipped = tools["pointer_click"].description
    assert shipped

    KwinMcpShell(engine=AutomationEngine()).do_help("pointer_click")
    help_text = capsys.readouterr().out

    lines = [line.strip() for line in help_text.splitlines()]
    assert "target: Target (required)" in lines, help_text
    assert any(re.fullmatch(r"button: \w[\w\[\]'\", |]* = 'left'", line) for line in lines), (
        help_text
    )
    assert "count: int = 1" in lines, help_text
    assert "capture: Capture | None = None" in lines, help_text
    assert shipped in help_text, (shipped, help_text)
    for noise in ("ForwardRef", "typing.Annotated", "Annotated[", "kwin_mcp.models.", "<class"):
        assert noise not in help_text, (noise, help_text)
