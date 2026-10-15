"""KWin window-rules fixture: force window properties the tools must then report.

Writes ``kwinrulesrc`` into the KWin session's ``XDG_CONFIG_HOME`` and asks KWin
to reload its configuration (``org.kde.KWin /KWin reconfigure``) over a
test-owned ``dbus-send`` call. Typical uses: ``closeable=False`` (QA-WIN-033), a
forced position, or ``minsize == maxsize`` for a non-resizable window. The
expected values a test asserts come from ``kwin_oracle``, never from the tool
under test. ``reconfigure`` returns before KWin acts: KWin reloads the rules
after its reconfigure delay and then re-applies them to existing windows too,
so a test waits for the effect with ``KWinOracle.wait_for_windows``. Forcing a
position also makes the window unmovable (``moveable`` false).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

RULES_FILE: Final = "kwinrulesrc"

# KWin's Rules::StringMatch and Rules::Type (rules.h).
_MATCH: Final = {"exact": 1, "substring": 2, "regex": 3}
_FORCE: Final = 2

type Match = Literal["exact", "substring", "regex"]


@dataclass(frozen=True)
class WindowRule:
    """One KWin rule, matched on resource class (the Wayland app_id) and optionally title.

    Every property given is applied with policy *Force*. ``size`` sets the
    window size and, unless ``minsize``/``maxsize`` are given, pins both to it so
    the window cannot be resized.
    """

    description: str
    wmclass: str
    wmclass_match: Match = "exact"
    title: str | None = None
    title_match: Match = "exact"
    closeable: bool | None = None
    position: tuple[int, int] | None = None
    size: tuple[int, int] | None = None
    minsize: tuple[int, int] | None = None
    maxsize: tuple[int, int] | None = None

    def entries(self) -> list[tuple[str, str]]:
        """``key=value`` pairs of this rule's kwinrulesrc group, in a stable order."""
        out = [
            ("Description", self.description),
            ("wmclass", self.wmclass),
            ("wmclassmatch", str(_MATCH[self.wmclass_match])),
            ("wmclasscomplete", "false"),
        ]
        if self.title is not None:
            out += [("title", self.title), ("titlematch", str(_MATCH[self.title_match]))]
        if self.closeable is not None:
            out += [
                ("closeable", "true" if self.closeable else "false"),
                ("closeablerule", str(_FORCE)),
            ]
        minsize = self.minsize if self.minsize is not None else self.size
        maxsize = self.maxsize if self.maxsize is not None else self.size
        for key, value in (
            ("position", self.position),
            ("size", self.size),
            ("minsize", minsize),
            ("maxsize", maxsize),
        ):
            if value is not None:
                out += [(key, f"{value[0]},{value[1]}"), (f"{key}rule", str(_FORCE))]
        return out


def render_rules(rules: Sequence[WindowRule]) -> str:
    """The complete ``kwinrulesrc`` text for ``rules`` (groups named 1..N)."""
    groups = [str(index) for index in range(1, len(rules) + 1)]
    lines = ["[General]", f"count={len(rules)}", f"rules={','.join(groups)}", ""]
    for group, rule in zip(groups, rules, strict=True):
        lines.append(f"[{group}]")
        lines.extend(f"{key}={_escape(value)}" for key, value in rule.entries())
        lines.append("")
    return "\n".join(lines)


def _escape(value: str) -> str:
    """KConfig value escaping for the characters a rule value can contain."""
    return (
        value.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    )


def reconfigure(dbus_address: str) -> None:
    """Make KWin reload its configuration, rules included."""
    dbus_send = shutil.which("dbus-send")
    if dbus_send is None:
        msg = "dbus-send is required to reconfigure KWin"
        raise RuntimeError(msg)
    result = subprocess.run(
        [
            dbus_send,
            f"--bus={dbus_address}",
            "--print-reply",
            "--reply-timeout=10000",
            "--dest=org.kde.KWin",
            "/KWin",
            "org.kde.KWin.reconfigure",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        msg = f"KWin reconfigure failed: {result.stderr.strip()}"
        raise RuntimeError(msg)


def install_rules(config_home: Path, dbus_address: str, rules: Sequence[WindowRule]) -> Path:
    """Write ``rules`` to ``<config_home>/kwinrulesrc`` and reconfigure KWin; return the file."""
    config_home.mkdir(parents=True, exist_ok=True)
    path = config_home / RULES_FILE
    path.write_text(render_rules(rules), encoding="utf-8")
    reconfigure(dbus_address)
    return path


def clear_rules(config_home: Path, dbus_address: str) -> None:
    """Remove every rule written by ``install_rules`` and reconfigure KWin."""
    (config_home / RULES_FILE).unlink(missing_ok=True)
    reconfigure(dbus_address)
