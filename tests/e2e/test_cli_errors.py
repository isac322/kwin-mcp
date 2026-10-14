"""How ``kwin-mcp-cli`` reports a failed command, without a KWin session.

A tool error (invalid input, a refusal, a failed operation, a mistyped keyword) is
printed as one ``Error: <message>`` line on stderr, the CLI form of the MCP
``isError`` result. Any other exception is a programming error and keeps its
traceback.
"""

from __future__ import annotations

from typing import NoReturn, cast

import pytest

from kwin_mcp.cli import KwinMcpShell
from kwin_mcp.core import AutomationEngine


class _StubEngine:
    """Engine stand-in whose public methods become the shell's commands."""

    def fail_value(self) -> str:
        msg = "screen_width must be a positive integer, got 0"
        raise ValueError(msg)

    def fail_runtime(self) -> str:
        msg = "No session running. Call session_start first."
        raise RuntimeError(msg)

    def fail_os(self) -> str:
        msg = "No such file or directory: '/nonexistent-app'"
        raise FileNotFoundError(2, msg)

    def fail_key(self) -> str:
        raise KeyError("missing")

    def echo(self, text: str = "") -> str:
        return text


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
        ("fail_value", "Error: screen_width must be a positive integer, got 0"),
        ("fail_runtime", "Error: No session running."),
        ("fail_os", "Error: [Errno 2] No such file or directory"),
        # An unknown keyword is a TypeError from the call itself.
        ("echo nope=1", "Error: "),
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
    _shell().default("echo notkeyvalue")

    captured = capsys.readouterr()
    assert captured.out == ""
    _assert_one_error_line(captured.err, "Error: Invalid argument")


def test_programming_errors_keep_their_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    _shell().default("fail_key")

    captured = capsys.readouterr()
    assert "Traceback" in captured.err, captured.err
    assert "KeyError: 'missing'" in captured.err, captured.err
    assert "Error: " not in captured.err.splitlines()[0], captured.err


def test_successful_command_prints_its_result(capsys: pytest.CaptureFixture[str]) -> None:
    _shell().default("echo text=hello")

    captured = capsys.readouterr()
    assert captured.out == "hello\n"
    assert captured.err == ""


def test_invalid_boolean_is_rejected_before_any_session_or_action(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """exact_name=banana never reaches invoke_ui_action as False (substring matching).

    The real engine supplies the real annotations; reaching its session lookup or the
    AT-SPI worker fails the test.
    """
    engine = AutomationEngine()

    def unreachable(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("the invalid boolean reached the session lookup or AT-SPI dispatch")

    monkeypatch.setattr(engine, "_get_session", unreachable)
    monkeypatch.setattr(engine, "_run_atspi", unreachable)

    KwinMcpShell(engine=engine).default(
        "invoke_ui_action query=Save action_name=Press exact_name=banana"
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    _assert_one_error_line(captured.err, "Error: Invalid boolean value: 'banana'")
