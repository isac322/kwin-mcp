"""dbus_service_file must give the argv dbus-daemon activation gives.

Every expected value below was observed from dbus-daemon 1.16 activating a
service file with that Exec= line on a private dbus-run-session bus.
"""

from __future__ import annotations

import pytest

from kwin_mcp.dbus_service_file import ServiceFileError, parse_exec, service_file_argv

_NAME = "org.example.Test"


def _service(exec_line: str) -> str:
    return f"[D-BUS Service]\nName={_NAME}\nExec={exec_line}\n"


def _argv(text: str, name: str = _NAME) -> list[str]:
    return service_file_argv(text.encode(), name)


@pytest.mark.parametrize(
    ("exec_line", "argv"),
    [
        ("/bin/x a b", ["/bin/x", "a", "b"]),
        (
            '"/opt/dir with space/x" --label="two words"',
            ["/opt/dir with space/x", "--label=two words"],
        ),
        ('/bin/x \'x y\' "z\\\\"w" back\\\\ slash', ["/bin/x", "x y", 'z"w', "back slash"]),
        ("/bin/x \"a\\\\$b\\\\`c\\\\\\\\d\\\\qe\" 'n\\\\o'", ["/bin/x", "a$b`c\\d\\qe", "n\\o"]),
        ("/bin/x a\\sb c", ["/bin/x", "a", "b", "c"]),
        ("/bin/x a\\\\\\nb c\\nd", ["/bin/x", "ab", "c", "d"]),
        ("/bin/x \"x\\\\\\ny\" 'p\\nq'", ["/bin/x", "x\ny", "p\nq"]),
        ('/bin/x "\\\\\\\\" "\\\\\\\\\\\\""', ["/bin/x", "\\", '\\"']),
        ("/bin/x\ta\t\tb", ["/bin/x", "a", "b"]),
        ("/bin/x \"a\"b'c'd", ["/bin/x", "abcd"]),
        ('/bin/x "it\'s" \'say "hi"\'', ["/bin/x", "it's", 'say "hi"']),
        ("/bin/x \\\\'a\\\\' \\\\\"b", ["/bin/x", "'a'", '"b']),
        ("/bin/x '' \"\"", ["/bin/x", "", ""]),
        # A comment runs to the next newline, which it swallows; a # inside a
        # word starts one too, unless quoted or escaped.
        ("/bin/x a#b c", ["/bin/x", "a"]),
        ("/bin/x #\\nnext line", ["/bin/x", "next", "line"]),
        ("/bin/x a\\\\#b \"#\" '#'", ["/bin/x", "a#b", "#", "#"]),
        # dbus-daemon always delimits the last token, so trailing blanks and a
        # trailing comment leave a final empty argument.
        ("/bin/x trailing ", ["/bin/x", "trailing", ""]),
        ("/bin/x x #comment here", ["/bin/x", "x", ""]),
    ],
)
@pytest.mark.qa("QA-SES-027")
def test_service_file_argv_matches_dbus_activation(exec_line: str, argv: list[str]) -> None:
    assert _argv(_service(exec_line)) == argv


@pytest.mark.parametrize(
    "exec_line",
    [
        '/bin/x unclosed"',
        "/bin/x end#",
        "/bin/x \\\\",
        # Escapes the desktop-file layer rejects, so dbus-daemon ignores the file.
        '/bin/x \\"',
        "/bin/x \\$",
        "/bin/x \\",
    ],
)
def test_service_file_argv_rejects_what_dbus_rejects(exec_line: str) -> None:
    with pytest.raises(ServiceFileError):
        _argv(_service(exec_line))


def test_only_the_first_exec_of_the_first_service_group_counts() -> None:
    text = (
        "# comment\n\n"
        "[Other]\nExec=/bin/other\n"
        "[D-BUS Service]\nName=org.example.Test\nExec[fr]=/bin/localized\n"
        "Exec = /bin/first\nExec=/bin/second\n"
        "[D-BUS Service]\nExec=/bin/third\n"
    )
    assert _argv(text) == ["/bin/first"]


def test_the_first_service_group_without_exec_is_an_error() -> None:
    text = "[D-BUS Service]\nName=org.example.Test\n[D-BUS Service]\nExec=/bin/x\n"
    with pytest.raises(ServiceFileError):
        _argv(text)


@pytest.mark.parametrize(
    "text",
    [
        "Exec=/bin/x\n",
        "[D-BUS Service]\n  Exec=/bin/x\n",
        "[D-BUS Service\nExec=/bin/x\n",
        "[]\n[D-BUS Service]\nExec=/bin/x\n",
        "[D-BUS Service]\nExec /bin/x\n",
        "[D-BUS Service]\nName=bad\\escape\nExec=/bin/x\n",
        # dbus-daemon activates a service by Name: a file without it, or with
        # another name, does not start the requested one.
        "[D-BUS Service]\nExec=/bin/x\n",
        "[D-BUS Service]\nName=org.example.Other\nExec=/bin/x\n",
        "[D-BUS Service]\nExec=/bin/x\n[D-BUS Service]\nName=org.example.Test\n",
        pytest.param(
            "[D-BUS Service]\nName=org.example.Test\nExec=/bin/x\n# executed\x00\n",
            id="nul-in-comment-after-exec",
        ),
        pytest.param(
            "[D-BUS Service]\nName=org.example.Test\nExec=/bin/x\nDescription[en]=executed\x00\n",
            id="nul-in-localized-entry-after-exec",
        ),
    ],
)
@pytest.mark.qa("QA-SES-027")
def test_malformed_service_files_are_rejected(text: str) -> None:
    with pytest.raises(ServiceFileError):
        _argv(text)


def test_files_dbus_ignores_are_rejected() -> None:
    valid = _service("/bin/x").encode()
    with pytest.raises(ServiceFileError):
        service_file_argv(b"# \xff\n" + valid, _NAME)
    padding = b"#" * (128 * 1024 - len(valid) - 1) + b"\n"
    assert service_file_argv(padding + valid, _NAME) == ["/bin/x"]
    with pytest.raises(ServiceFileError):
        service_file_argv(b"#" + padding + valid, _NAME)


def test_line_endings_follow_dbus() -> None:
    assert _argv("[D-BUS Service]\r\nExec=/bin/x a\rName=n\r\n", "n") == ["/bin/x", "a"]


def test_parse_exec_does_not_expand() -> None:
    assert parse_exec("/bin/x $HOME ~ *.txt `id` $(id)") == [
        "/bin/x",
        "$HOME",
        "~",
        "*.txt",
        "`id`",
        "$(id)",
    ]
