"""Read a D-Bus service file's ``Exec=`` command the way dbus-daemon does.

dbus-daemon reads the key with its desktop-file parser (``bus/desktop-file.c``),
which unescapes ``\\s``, ``\\t``, ``\\n``, ``\\r`` and ``\\\\``, then splits the
value with ``_dbus_shell_parse_argv`` (``dbus/dbus-shell.c``): shell-like
single and double quotes and backslash escapes, ``#`` comments, and no
expansion of any kind. This module preserves those parsing rules.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

_SERVICE_SECTION = "D-BUS Service"
_NAME_KEY = "Name"
_EXEC_KEY = "Exec"
# dbus-daemon ignores a service file larger than this.
_MAX_SERVICE_FILE_BYTES = 128 * 1024
_DESKTOP_ESCAPES = {"s": " ", "t": "\t", "n": "\n", "r": "\r", "\\": "\\"}
# Characters dbus-daemon accepts in a key name; a "[" after them marks a
# localized key, which it ignores.
_KEY_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-")
_DOUBLE_QUOTE_ESCAPES = frozenset('"\\`$\n')
_A11Y_BUS_NAME = "org.a11y.Bus"
_ATSPI_REGISTRY_BUS_NAME = "org.a11y.atspi.Registry"


class ServiceFileError(ValueError):
    """The service file or its ``Exec=`` command is one dbus-daemon rejects."""


def _read_service_file(path: Path) -> bytes | None:
    """Bound bytes read and reject nonregular descriptors; filesystem I/O may still stall."""
    fd = -1
    try:
        fd = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOCTTY,
        )
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        if info.st_size > _MAX_SERVICE_FILE_BYTES:
            raise ServiceFileError(f"larger than {_MAX_SERVICE_FILE_BYTES} bytes")
        with os.fdopen(fd, "rb", closefd=False) as service_file:
            data = service_file.read(_MAX_SERVICE_FILE_BYTES + 1)
        if len(data) > _MAX_SERVICE_FILE_BYTES:
            raise ServiceFileError(f"larger than {_MAX_SERVICE_FILE_BYTES} bytes")
        return data
    finally:
        if fd != -1:
            os.close(fd)


def _unescape_value(value: str) -> str:
    out: list[str] = []
    chars = iter(value)
    for char in chars:
        if char != "\\":
            out.append(char)
            continue
        escaped = next(chars, None)
        if escaped is None:
            raise ServiceFileError("value ends in a backslash")
        if escaped not in _DESKTOP_ESCAPES:
            raise ServiceFileError(f"invalid escape sequence \\{escaped}")
        out.append(_DESKTOP_ESCAPES[escaped])
    return "".join(out)


def _service_group(text: str) -> dict[str, str]:
    """Return the unescaped keys of a service file's ``[D-BUS Service]`` group.

    Like dbus-daemon, only the first such group is read, and the first
    occurrence of a key wins.
    """
    section: str | None = None
    in_first_service_section = False
    seen_service_section = False
    group: dict[str, str] = {}
    # dbus-daemon ends a line at \n, \r\n or \r.
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith("["):
            section = line[1:-1]
            if (
                len(line) <= 2
                or not line.endswith("]")
                or any(not " " <= char <= "~" or char in "[]" for char in section)
            ):
                raise ServiceFileError(f"invalid group header {line!r}")
            in_first_service_section = section == _SERVICE_SECTION and not seen_service_section
            seen_service_section = seen_service_section or section == _SERVICE_SECTION
            continue
        if not line.strip(" \t\f") or line.startswith("#"):
            continue
        if section is None:
            raise ServiceFileError("key=value before a [group]")
        key_end = 0
        while key_end < len(line) and line[key_end] in _KEY_CHARS:
            key_end += 1
        if key_end == 0:
            raise ServiceFileError(f"empty key name in {line!r}")
        if key_end < len(line) and line[key_end] == "[":
            continue
        rest = line[key_end:].lstrip(" ")
        if not rest.startswith("="):
            raise ServiceFileError(f"no '=' in {line!r}")
        value = _unescape_value(rest[1:].lstrip(" "))
        if in_first_service_section:
            group.setdefault(line[:key_end], value)
    return group


def _tokenize(command: str) -> list[str]:
    tokens: list[str] = []
    token: list[str] = []
    quote = ""
    # True while the run of backslashes ending at the current character has an
    # odd length, which marks an escaped double quote inside double quotes.
    escaped = False
    i = 0
    while i < len(command):
        char = command[i]
        if quote == "\\":
            # A backslash-newline becomes nothing; any other escape is kept
            # for the unquoting pass.
            if char != "\n":
                token += ["\\", char]
            quote = ""
        elif quote == "#":
            while i < len(command) and command[i] != "\n":
                i += 1
            quote = ""
            if i == len(command):
                break
            char = command[i]
        elif quote:
            if char == quote and not (quote == '"' and escaped):
                quote = ""
            token.append(char)
        elif char == "\n":
            tokens.append("".join(token))
            token = []
        elif char in " \t":
            if token:
                tokens.append("".join(token))
                token = []
        elif char in "'\"":
            token.append(char)
            quote = char
        elif char in "#\\":
            quote = char
        else:
            token.append(char)
        escaped = not escaped if char == "\\" else False
        i += 1
    # dbus-daemon delimits the last token unconditionally, so trailing blanks
    # yield a final empty argument.
    tokens.append("".join(token))
    if quote:
        raise ServiceFileError("unclosed quotes in command line")
    return tokens


def _unquote(token: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(token):
        char = token[i]
        if char == "\\":
            i += 1
            if i < len(token):
                if token[i] != "\n":
                    out.append(token[i])
                i += 1
        elif char == "'":
            end = token.find("'", i + 1)
            if end < 0:
                raise ServiceFileError("unclosed quotes in command line")
            out.append(token[i + 1 : end])
            i = end + 1
        elif char == '"':
            i += 1
            while i < len(token) and token[i] != '"':
                if token[i] == "\\" and i + 1 < len(token):
                    i += 1
                    if token[i] not in _DOUBLE_QUOTE_ESCAPES:
                        out.append("\\")
                out.append(token[i])
                i += 1
            if i == len(token):
                raise ServiceFileError("unclosed quotes in command line")
            i += 1
        else:
            out.append(char)
            i += 1
    return "".join(out)


def parse_exec(command: str) -> list[str]:
    """Split an ``Exec=`` value into the argv dbus-daemon would execute."""
    return [_unquote(token) for token in _tokenize(command)]


def service_file_argv(data: bytes, name: str) -> list[str]:
    """Return the argv dbus-daemon would execute to activate bus name ``name`` from this file.

    A file dbus-daemon would ignore, or one that does not provide ``name``,
    raises ``ServiceFileError``.
    """
    if len(data) > _MAX_SERVICE_FILE_BYTES:
        raise ServiceFileError(f"larger than {_MAX_SERVICE_FILE_BYTES} bytes")
    if b"\0" in data:
        raise ServiceFileError("service file contains a nul byte")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ServiceFileError("not valid UTF-8") from exc
    group = _service_group(text)
    if group.get(_NAME_KEY) != name:
        raise ServiceFileError(f"[{_SERVICE_SECTION}] does not have {_NAME_KEY}={name}")
    if _EXEC_KEY not in group:
        raise ServiceFileError(f"no {_EXEC_KEY}= key in [{_SERVICE_SECTION}]")
    return parse_exec(group[_EXEC_KEY])


def _service_argv(subdir: str, name: str, env: Mapping[str, str]) -> list[str] | None:
    """Return the first matching service file found under the XDG data roots."""
    for data_dir in (env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":"):
        if not data_dir:
            continue
        path = Path(data_dir) / "dbus-1" / subdir / f"{name}.service"
        try:
            if not stat.S_ISREG(path.stat().st_mode):
                continue
        except ValueError:
            continue
        except OSError as exc:
            # Preserve is_file()'s missing/invalid-path skips; other failures close direct start.
            if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP):
                continue
            return None
        try:
            data = _read_service_file(path)
        except FileNotFoundError:
            continue
        except (OSError, ServiceFileError):
            return None
        if data is None:
            continue
        try:
            return service_file_argv(data, name)
        except ServiceFileError:
            return None
    return None


def _main(args: list[str]) -> int:
    """Resolve and exec the wrapper's fixed launcher or registry role."""
    if len(args) != 1 or args[0] not in ("launcher", "registry"):
        return 2
    if args[0] == "launcher":
        launcher = _service_argv("services", _A11Y_BUS_NAME, os.environ)
        registry = _service_argv("accessibility-services", _ATSPI_REGISTRY_BUS_NAME, os.environ)
        if not launcher or not launcher[0] or not registry or not registry[0]:
            return 1
        argv = launcher
    else:
        registry = _service_argv("accessibility-services", _ATSPI_REGISTRY_BUS_NAME, os.environ)
        if not registry or not registry[0]:
            return 1
        argv = registry
    try:
        os.execv(argv[0], argv)
    except OSError as exc:
        message = str(exc).replace("\n", "\\n").replace("\r", "\\r")
        print(f"AT-SPI direct start failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
