"""``kwin-mcp`` command-line flags: parsing, usage errors and the ``--help`` text."""

from __future__ import annotations

import re

import pytest

from kwin_mcp import server
from kwin_mcp.server import ServerFlags, _parse_argv

_USAGE_ERROR_ARGV = [
    ["--default"],
    ["foo"],
    ["--screenshot-max-edge", "٣"],
    ["--screenshot-max-edge", "1", "--screenshot-max-edge", "2"],
    ["--screenshot-max-edge=-1"],
    ["--screenshot-max-edge", "0x10"],
    ["--live-consent=bogus"],
    ["--live-consent"],
    ["--live-consent=elicit", "--live-consent=refuse"],
    ["--live-consent", "refuse", "--live-consent", "refuse"],
]


@pytest.mark.qa("QA-MCP-022")
@pytest.mark.parametrize("argv", _USAGE_ERROR_ARGV, ids=" ".join)
def test_live_consent_argv_parsing(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """Every malformed argv is a usage error: exit 2 with usage and the error on stderr."""
    with pytest.raises(SystemExit) as exc_info:
        _parse_argv(argv)
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("usage: kwin-mcp "), captured.err
    assert "kwin-mcp: error: " in captured.err


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], ServerFlags()),
        (["--live-consent=elicit"], ServerFlags(live_consent="elicit")),
        (["--live-consent=refuse"], ServerFlags(live_consent="refuse")),
        (["--live-consent", "refuse"], ServerFlags(live_consent="refuse")),
        (
            ["--default-live-session", "--screenshot-images", "--screenshot-max-edge", "640"],
            ServerFlags(default_live_session=True, screenshot_images=True, screenshot_max_edge=640),
        ),
        (["--screenshot-max-edge=0"], ServerFlags(screenshot_max_edge=0)),
    ],
)
def test_valid_flags_parse(argv: list[str], expected: ServerFlags) -> None:
    """``--live-consent`` defaults to ``elicit``; the other flags keep their rules."""
    assert _parse_argv(argv) == expected


def test_live_consent_default_is_elicit() -> None:
    assert _parse_argv([]).live_consent == "elicit"
    assert server.build_server([]).flags.live_consent == "elicit"


def test_build_server_applies_flags() -> None:
    """The flags reach the server; the screenshot edge reaches the engine it builds."""
    built = server.build_server(["--live-consent=refuse", "--screenshot-images"])
    assert built.flags == ServerFlags(screenshot_images=True, live_consent="refuse")


def test_usage_lists_every_flag() -> None:
    """``_USAGE`` (printed by ``-h``/``--help``) documents the four flags and help itself."""
    usage = server._USAGE
    assert usage.startswith("usage: kwin-mcp ")
    for flag in (
        "-h, --help",
        "--default-live-session",
        "--screenshot-images",
        "--screenshot-max-edge N",
        "--live-consent {elicit,refuse}",
    ):
        assert flag in usage, flag
    assert usage.rstrip().endswith("Documentation: https://github.com/isac322/kwin-mcp#readme")
    # The usage line names every flag the parser accepts.
    usage_line = usage.split("\n\n", 1)[0]
    assert re.findall(r"--[a-z-]+", usage_line) == [
        "--default-live-session",
        "--screenshot-images",
        "--screenshot-max-edge",
        "--live-consent",
    ]
