"""Independent AT-SPI oracle: the accessibility tree as the test process reads it.

``element_query`` must never be checked against itself. This module walks the
session's accessibility bus with ``gi.repository.Atspi`` in a child process of
its own (libatspi binds its bus connections once per process), sharing no code
with ``kwin_mcp.accessibility``. Depth 0 is the application node, as in the
1.0 ``element_query`` tree.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_WALKER = r"""
import json
import sys

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

request = json.loads(sys.argv[1])
app_filter = request["app"].lower()
max_depth = request["max_depth"]
out = []


def rect(accessible, coord_type):
    try:
        r = Atspi.Component.get_extents(accessible, coord_type)
    except GLib.Error:
        return None
    if r.width <= 0 and r.height <= 0:
        return None
    return [r.x, r.y, r.width, r.height]


def node(accessible, app_name, pid, path, depth):
    entry = {
        "app": app_name, "pid": pid, "path": path, "depth": depth, "role": None,
        "name": None, "description": None, "states": [], "screen": None, "window": None,
        "text": None, "value": None, "value_max": None, "actions": [], "child_count": 0,
        "error": None,
    }
    try:
        entry["role"] = accessible.get_role_name()
        entry["name"] = accessible.get_name()
        entry["description"] = accessible.get_description()
        entry["states"] = sorted(s.value_nick for s in accessible.get_state_set().get_states())
        entry["child_count"] = accessible.get_child_count()
    except GLib.Error as exc:
        entry["error"] = str(exc)
        return entry
    # PyGObject resolves iface.get_text()/get_action_name() on an Accessible to the
    # deprecated Accessible methods of the same name, so call each interface's
    # function explicitly, and only for interfaces the object implements.
    try:
        interfaces = set(accessible.get_interfaces())
    except GLib.Error as exc:
        entry["error"] = str(exc)
        return entry
    if depth > 0 and "Component" in interfaces:
        entry["screen"] = rect(accessible, Atspi.CoordType.SCREEN)
        entry["window"] = rect(accessible, Atspi.CoordType.WINDOW)
    try:
        if "Text" in interfaces:
            count = Atspi.Text.get_character_count(accessible)
            entry["text"] = Atspi.Text.get_text(accessible, 0, count)
        if "Value" in interfaces:
            entry["value"] = Atspi.Value.get_current_value(accessible)
            entry["value_max"] = Atspi.Value.get_maximum_value(accessible)
        if "Action" in interfaces:
            entry["actions"] = [
                Atspi.Action.get_action_name(accessible, i)
                for i in range(Atspi.Action.get_n_actions(accessible))
            ]
    except GLib.Error as exc:
        entry["error"] = str(exc)
    return entry


def walk(accessible, app_name, pid, path, depth):
    entry = node(accessible, app_name, pid, path, depth)
    out.append(entry)
    if entry["error"] is not None or depth >= max_depth:
        return
    for index in range(entry["child_count"]):
        try:
            child = accessible.get_child_at_index(index)
        except GLib.Error as exc:
            out.append({**entry, "path": [*path, index], "depth": depth + 1, "error": str(exc)})
            continue
        if child is not None:
            walk(child, app_name, pid, [*path, index], depth + 1)


desktop = Atspi.get_desktop(0)
for index in range(desktop.get_child_count()):
    app = desktop.get_child_at_index(index)
    if app is None:
        continue
    try:
        name = app.get_name() or ""
        pid = app.get_process_id()
    except GLib.Error:
        continue
    if app_filter and app_filter not in name.lower():
        continue
    walk(app, name, pid, [index], 0)

sys.stdout.write(json.dumps(out))
"""


class AtspiOracleError(RuntimeError):
    """The AT-SPI walk could not run."""


@dataclass(frozen=True)
class AtspiNode:
    """One accessible object.

    ``path`` is the child-index path from the desktop (``path[0]`` is the
    application's index). ``screen``/``window`` are libatspi's raw extents in
    screen and window coordinates (``None`` for empty or unreadable extents;
    on Wayland, screen coordinates are toolkit-dependent, so combine
    ``window`` with ``kwin_oracle`` client geometry for global positions).
    ``error`` is set when the object could not be read; its children are then
    not walked.
    """

    app: str
    pid: int
    path: tuple[int, ...]
    depth: int
    role: str | None
    name: str | None
    description: str | None
    states: frozenset[str]
    screen: tuple[int, int, int, int] | None
    window: tuple[int, int, int, int] | None
    text: str | None
    value: float | None
    value_max: float | None
    actions: tuple[str, ...]
    child_count: int
    error: str | None


def _box(raw: object) -> tuple[int, int, int, int] | None:
    if not isinstance(raw, list) or len(raw) != 4:
        return None
    x, y, width, height = (int(v) for v in raw)
    return x, y, width, height


def _number(raw: object) -> float | None:
    return float(raw) if isinstance(raw, int | float) else None


def _node(raw: dict[str, object]) -> AtspiNode:
    path = raw["path"]
    states = raw["states"]
    actions = raw["actions"]
    return AtspiNode(
        app=str(raw["app"]),
        pid=int(str(raw["pid"])),
        path=tuple(int(i) for i in path) if isinstance(path, list) else (),
        depth=int(str(raw["depth"])),
        role=None if raw["role"] is None else str(raw["role"]),
        name=None if raw["name"] is None else str(raw["name"]),
        description=None if raw["description"] is None else str(raw["description"]),
        states=frozenset(str(s) for s in states) if isinstance(states, list) else frozenset(),
        screen=_box(raw["screen"]),
        window=_box(raw["window"]),
        text=None if raw["text"] is None else str(raw["text"]),
        value=_number(raw["value"]),
        value_max=_number(raw["value_max"]),
        actions=tuple(str(a) for a in actions) if isinstance(actions, list) else (),
        child_count=int(str(raw["child_count"])),
        error=None if raw["error"] is None else str(raw["error"]),
    )


class AtspiOracle:
    """Walks the AT-SPI tree of the session whose D-Bus session bus is ``dbus_address``."""

    def __init__(self, dbus_address: str, *, timeout_s: float = 60.0) -> None:
        self._dbus_address = dbus_address
        self._timeout_s = timeout_s

    def walk(self, app: str = "", max_depth: int = 64) -> list[AtspiNode]:
        """Pre-order walk of every application whose name contains ``app`` (case-insensitive)."""
        env = {**os.environ, "DBUS_SESSION_BUS_ADDRESS": self._dbus_address}
        # The a11y bus must be the one this session bus announces, not one
        # inherited from the test runner's own environment.
        env.pop("AT_SPI_BUS_ADDRESS", None)
        request = json.dumps({"app": app, "max_depth": max_depth})
        try:
            result = subprocess.run(
                [sys.executable, "-c", _WALKER, request],
                env=env,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            msg = f"AT-SPI oracle walk exceeded {exc.timeout:.0f} s"
            raise AtspiOracleError(msg) from exc
        if result.returncode != 0:
            msg = f"AT-SPI oracle walk failed ({result.returncode}): {result.stderr.strip()}"
            raise AtspiOracleError(msg)
        raw = json.loads(result.stdout)
        if not isinstance(raw, list):
            msg = f"unexpected AT-SPI oracle output: {result.stdout[:200]!r}"
            raise AtspiOracleError(msg)
        return [_node(entry) for entry in raw if isinstance(entry, dict)]

    def applications(self, app: str = "") -> list[AtspiNode]:
        """The application nodes (depth 0) whose name contains ``app``."""
        return self.walk(app, max_depth=0)

    def find(
        self,
        *,
        app: str = "",
        name: str | None = None,
        name_contains: str | None = None,
        role: str | None = None,
        max_depth: int = 64,
    ) -> list[AtspiNode]:
        """Nodes matching every given filter: exact ``name``, substring, exact ``role``."""
        return [
            node
            for node in self.walk(app, max_depth)
            if (name is None or node.name == name)
            and (name_contains is None or (node.name is not None and name_contains in node.name))
            and (role is None or node.role == role)
        ]

    def wait_for(
        self,
        predicate: Callable[[list[AtspiNode]], bool],
        *,
        app: str = "",
        max_depth: int = 64,
        timeout_s: float = 15.0,
        interval_s: float = 0.2,
    ) -> list[AtspiNode]:
        """Poll until satisfied, ending with an observation begun at/after the deadline.

        ``timeout_s`` sets the polling horizon, not a hard wall-clock bound:
        each walk retains its own subprocess timeout.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            walk_started = time.monotonic()
            nodes = self.walk(app, max_depth)
            if predicate(nodes):
                return nodes
            # A walk can return a stale snapshot after consuming the remaining
            # time. Only a walk begun at/after the deadline can establish timeout.
            if walk_started >= deadline:
                names = [(n.role, n.name) for n in nodes[:40]]
                msg = f"AT-SPI tree never satisfied the predicate (first nodes: {names})"
                raise AssertionError(msg)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(interval_s, remaining))
