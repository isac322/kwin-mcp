"""AT-SPI2 accessibility worker.

Runs as ``python -m kwin_mcp.accessibility --serve --generation <N>`` on the
session's D-Bus: it answers newline-delimited JSON requests on stdin until
stdin closes, one response line per request. The automation engine keeps one
worker per session bus and bumps ``N`` on every (re)spawn.

Operations (``op``):

- ``query``: one filtered pre-order walk (``locator``, ``max_depth``).
- ``wait``: ``query`` polled until the matches are present or absent.
- ``resolve``: live element data for refs minted by ``query``/``wait``.
- ``action``: invoke one AT-SPI action on a ref, or on the single
  (element, action) candidate a strict, fail-closed search finds.

Every element a response returns carries a ref ``e<generation>.<n>`` that
names its ``Atspi.Accessible`` in this worker's LRU ref table.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import gi

from kwin_mcp.models import Locator, MatchMode
from kwin_mcp.spoken_name import SpokenNameKey, normalize_spoken_name, spoken_name_matches

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kwin_mcp.geometry import KWinWindow

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

# Elements such as editors expose their whole document through the Text
# interface; cap it so a tree dump stays readable.
_MAX_TEXT_CHARS = 200


@dataclass
class ElementInfo:
    """Information about a single UI element.

    ``x``/``y``/``width``/``height`` are global screen coordinates when
    ``mapped`` is true. When ``mapped`` is false the rectangle could not be
    placed on the screen with certainty; ``unavailable`` carries the reason
    and the coordinates must not be used for input. ``ref`` names the element
    in the worker's ref table, ``app`` is its application's name and
    ``window_id`` the KWin id of the window its top-level matched (None for
    application nodes and unmapped top-levels).
    """

    role: str
    name: str
    description: str
    states: list[str]
    x: int
    y: int
    width: int
    height: int
    actions: list[str]
    text: str
    value: float | None
    value_max: float | None
    children_count: int
    depth: int
    has_extents: bool
    mapped: bool
    unavailable: str
    ref: str = ""
    app: str = ""
    window_id: str | None = None


@dataclass
class _WindowMapping:
    """How one AT-SPI top-level maps to a KWin window.

    ``window`` is the matched KWin window dict; ``anchor`` is the screen
    position of the KWin rectangle the AT-SPI window-local coordinates start
    from; ``offset`` is the translation from AT-SPI window-local coordinates
    to screen coordinates. ``reason`` is set whenever the mapping failed.
    """

    window: KWinWindow | None
    reason: str
    rect: tuple[int, int, int, int] | None = None
    offset: tuple[int, int] | None = None
    anchor: tuple[int, int] | None = None


# Shared mapping for elements that are not windows at all (the application
# root node, or AT-SPI children that vanished mid-walk).
_UNMAPPED = _WindowMapping(None, "not-a-window")

# Acting on an element found by an incomplete discovery could trigger the
# wrong control, so any unreadable node in the searched scope — even a
# non-candidate — fails the strict search closed instead of being skipped.
# The depth limit exists for the same reason: no match may hide below it.
_ACTION_SEARCH_MAX_DEPTH = 64

# How long a wait for present elements re-polls matches whose window mapping
# failed because KWin's window list changed during the walk.
_WINDOWS_CHANGED_SETTLE_S = 1.0

# Entries of the ref table; the least recently used ref is evicted beyond it.
REF_TABLE_CAPACITY = 4096

_REF_PATTERN = re.compile(r"e(\d+)\.(\d+)")


# ── Locator matching ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Criteria:
    """A Locator prepared for matching.

    ``app``, ``query`` and ``role`` are lowercased (case-insensitive);
    ``states`` are AT-SPI state nicks compared exactly. Empty values match
    everything, so ``Locator()`` selects every element.
    """

    app: str = ""
    query: str = ""
    role: str = ""
    states: tuple[str, ...] = ()
    match: MatchMode = "substring"
    spoken: SpokenNameKey | None = None

    @classmethod
    def from_locator(cls, locator: Locator) -> _Criteria:
        return cls(
            app=(locator.app or "").lower(),
            query=(
                " ".join((locator.query or "").casefold().split())
                if locator.match == "exact"
                else (locator.query or "").lower()
            ),
            role=(locator.role or "").lower(),
            states=tuple(locator.states or ()),
            match=locator.match,
            spoken=normalize_spoken_name(locator.query or "")
            if locator.match == "spoken"
            else None,
        )

    def matches_app(self, app_name: str) -> bool:
        """Application-name substring, case-insensitive."""
        return self.app in app_name.lower()

    def matches_text(self, name: str, role: str, description: str) -> bool:
        """Role is exact; query is substring text or an exact/spoken accessible name."""
        if self.role and role.lower() != self.role:
            return False
        if not self.query:
            return True
        if self.match == "exact":
            return " ".join(name.casefold().split()) == self.query
        if self.match == "spoken" and self.spoken is not None:
            return spoken_name_matches(self.spoken, normalize_spoken_name(name))
        return (
            self.query in name.lower()
            or self.query in role.lower()
            or self.query in description.lower()
        )

    def matches_states(self, states: list[str]) -> bool:
        """Every required state nick is present, compared case-sensitively."""
        return all(state in states for state in self.states)

    def matches(self, info: ElementInfo) -> bool:
        """Role, query and states; the app is checked once per application."""
        return self.matches_text(info.name, info.role, info.description) and self.matches_states(
            info.states
        )


# ── Ref table ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _RefEntry:
    """What a ref names: the element and what is needed to re-read it in place.

    ``role`` is the role at mint time (a changed role makes the ref stale);
    ``application`` is the element's application node and ``toplevel`` its
    depth-1 ancestor (itself at depth 1, None for an application node), from
    which the live window mapping is recomputed.
    """

    accessible: Atspi.Accessible
    role: str
    app: str
    depth: int
    application: Atspi.Accessible
    toplevel: Atspi.Accessible | None


class _StaleRefError(Exception):
    """A ref that cannot be resolved; the message is the caller-facing text."""


class _RefTable:
    """LRU map from ``e<generation>.<n>`` refs to the elements they name.

    ``n`` increases monotonically for the life of the worker, so minting the
    same element again yields a new ref. Resolving a ref makes it the most
    recently used; beyond ``capacity`` the least recently used ref is evicted.
    """

    def __init__(self, generation: int = 1, capacity: int = REF_TABLE_CAPACITY) -> None:
        self.generation = generation
        self.capacity = capacity
        self._entries: OrderedDict[str, _RefEntry] = OrderedDict()
        self._counter = 0

    def __len__(self) -> int:
        return len(self._entries)

    def mint(self, entry: _RefEntry) -> str:
        self._counter += 1
        ref = f"e{self.generation}.{self._counter}"
        self._entries[ref] = entry
        while len(self._entries) > self.capacity:
            self._entries.popitem(last=False)
        return ref

    def resolve(self, ref: str) -> _RefEntry:
        """The entry ``ref`` names, now most recently used; ``_StaleRefError`` otherwise."""
        match = _REF_PATTERN.fullmatch(ref)
        if match is None:
            msg = (
                f"Malformed ref {ref!r}: refs look like e<generation>.<n>, as element_query "
                "returns them.\nRe-run element_query and copy a ref."
            )
            raise _StaleRefError(msg)
        if int(match.group(1)) != self.generation:
            msg = f"Stale ref {ref} (AT-SPI worker restarted); re-run element_query."
            raise _StaleRefError(msg)
        entry = self._entries.get(ref)
        if entry is None:
            msg = (
                f"Stale ref {ref} (evicted from the ref table or never issued); "
                "re-run element_query."
            )
            raise _StaleRefError(msg)
        self._entries.move_to_end(ref)
        return entry


# The worker's ref table; ``_serve`` replaces it with one for its generation.
_REFS = _RefTable()


# ── query / wait: the lenient filtered walk ──────────────────────────────


@dataclass
class _Found:
    """One element a walk selected, with its mapping and the entry a ref will name."""

    info: ElementInfo
    mapping: _WindowMapping
    entry: _RefEntry


def _walk(criteria: _Criteria, max_depth: int) -> list[_Found]:
    """One filtered pre-order walk from the application nodes (depth 0).

    Elements deeper than ``max_depth`` are not visited (``<= 0``: application
    nodes only). Non-matching elements are hidden but their children are
    still searched. A node that cannot be read is skipped with its subtree;
    applications without children are included.
    """
    kwin_before, kwin_error = _kwin_windows()
    found: list[_Found] = []
    mappings_by_app: dict[frozenset[int], list[_WindowMapping]] = {}
    desktop = Atspi.get_desktop(0)
    for app in _children(desktop):
        try:
            app_name = app.get_name() or ""
        except Exception:
            continue
        if not criteria.matches_app(app_name):
            continue
        try:
            app_info = _extract_info(app, 0)
        except Exception:
            continue
        if criteria.matches(app_info):
            entry = _RefEntry(app, app_info.role, app_name, 0, app, None)
            found.append(_Found(app_info, _UNMAPPED, entry))
        if max_depth < 1:
            continue
        try:
            pids, mappings, toplevels = _resolve_app(app, kwin_before, kwin_error)
        except Exception:
            continue
        mappings_by_app.setdefault(pids, []).extend(mappings)
        for toplevel, mapping in zip(toplevels, mappings, strict=True):
            if toplevel is not None:
                scope = _Scope(app, app_name, toplevel, mapping)
                _collect(toplevel, scope, 1, max_depth, criteria, found)

    _apply_stability(mappings_by_app, kwin_before, kwin_error)
    for item in found:
        _finalize(item.info, item.mapping)
    return found


@dataclass(frozen=True)
class _Scope:
    """The application and top-level a subtree belongs to."""

    application: Atspi.Accessible
    app: str
    toplevel: Atspi.Accessible
    mapping: _WindowMapping


def _collect(
    element: Atspi.Accessible,
    scope: _Scope,
    depth: int,
    max_depth: int,
    criteria: _Criteria,
    found: list[_Found],
) -> None:
    """Add ``element`` (when it matches) and its matching descendants to ``found``."""
    if depth > max_depth:
        return
    try:
        info = _extract_info(element, depth)
    except Exception:
        return
    if criteria.matches(info):
        entry = _RefEntry(element, info.role, scope.app, depth, scope.application, scope.toplevel)
        found.append(_Found(info, scope.mapping, entry))
    for child in _children(element, info.children_count):
        _collect(child, scope, depth + 1, max_depth, criteria, found)


def _children(element: Atspi.Accessible, count: int | None = None) -> list[Atspi.Accessible]:
    """The readable children of ``element``; unreadable ones are left out."""
    if count is None:
        try:
            count = element.get_child_count()
        except Exception:
            return []
    children: list[Atspi.Accessible] = []
    for index in range(count):
        try:
            child = element.get_child_at_index(index)
        except Exception:
            continue
        if child is not None:
            children.append(child)
    return children


def _mint(item: _Found) -> dict[str, Any]:
    """Give a found element a new ref and serialize it."""
    item.info.ref = _REFS.mint(item.entry)
    item.info.app = item.entry.app
    return asdict(item.info)


def query_elements(locator: Locator, max_depth: int = 15) -> list[dict[str, Any]]:
    """Every element matching ``locator`` down to ``max_depth``, in pre-order, with refs."""
    return [_mint(item) for item in _walk(_Criteria.from_locator(locator), max_depth)]


def wait_elements(
    locator: Locator,
    max_depth: int = 15,
    until: str = "present",
    timeout_ms: int = 5000,
    poll_interval_ms: int = 200,
) -> dict[str, Any]:
    """Poll the ``locator`` walk until its matches are present (``>= 1``) or absent (none).

    ``timeout_ms <= 0`` polls exactly once; negative values of both
    ``timeout_ms`` and ``poll_interval_ms`` count as 0. Present matches whose
    window mapping failed only because KWin's window list changed during the
    walk are re-polled for up to ``_WINDOWS_CHANGED_SETTLE_S`` (bounded by the
    timeout) and then returned as they are. A timeout is data:
    ``satisfied=False`` with the matches of the last poll.
    """
    criteria = _Criteria.from_locator(locator)
    interval = max(poll_interval_ms, 0) / 1000.0
    start = time.monotonic()
    deadline = start + max(timeout_ms, 0) / 1000.0
    settle_deadline: float | None = None
    while True:
        found = _walk(criteria, max_depth)
        now = time.monotonic()
        if until == "absent":
            satisfied = not found
        else:
            satisfied = bool(found)
            if satisfied and any(item.info.unavailable == "windows-changed" for item in found):
                # KWin's window list changed during the walk (typically a
                # window still opening): re-poll briefly so the caller gets
                # coordinates, then return what is there.
                if settle_deadline is None:
                    settle_deadline = min(deadline, now + _WINDOWS_CHANGED_SETTLE_S)
                if now < settle_deadline:
                    time.sleep(min(interval, settle_deadline - now))
                    continue
        if satisfied or now >= deadline:
            return {
                "elements": [_mint(item) for item in found],
                "satisfied": satisfied,
                "elapsed_ms": round((now - start) * 1000),
            }
        time.sleep(min(interval, deadline - now))


# ── resolve: live data for minted refs ───────────────────────────────────


@dataclass
class _Resolved:
    """A ref resolved against the live tree: its entry and info, or the error text."""

    ref: str
    entry: _RefEntry | None = None
    info: ElementInfo | None = None
    error: str = ""


def _resolve_refs(refs: list[str]) -> list[_Resolved]:
    """Re-read every ref's element with its live rect and window mapping.

    A ref is stale when it is malformed, of another generation, unknown or
    evicted, when its element is defunct or unreadable, or when its role
    changed (a changed name is tolerated). Every application involved is
    mapped once against one KWin window snapshot, with the same stability
    check as a walk.
    """
    kwin_before, kwin_error = _kwin_windows()
    apps: list[tuple[Atspi.Accessible, list[Atspi.Accessible | None], list[_WindowMapping]]] = []
    mappings_by_app: dict[frozenset[int], list[_WindowMapping]] = {}
    results: list[_Resolved] = []
    pending: list[tuple[ElementInfo, _WindowMapping]] = []

    def toplevel_mapping(entry: _RefEntry) -> _WindowMapping:
        if entry.toplevel is None:
            return _UNMAPPED
        record = next((item for item in apps if item[0] is entry.application), None)
        if record is None:
            try:
                pids, mappings, toplevels = _resolve_app(entry.application, kwin_before, kwin_error)
            except Exception:
                pids, mappings, toplevels = frozenset(), [], []
            mappings_by_app.setdefault(pids, []).extend(mappings)
            record = (entry.application, list(toplevels), mappings)
            apps.append(record)
        for toplevel, mapping in zip(record[1], record[2], strict=True):
            if toplevel is not None and toplevel == entry.toplevel:
                return mapping
        # The top-level is no longer one of its application's children.
        return _WindowMapping(None, "window-set-mismatch")

    for ref in refs:
        try:
            entry = _REFS.resolve(ref)
        except _StaleRefError as exc:
            results.append(_Resolved(ref, error=str(exc)))
            continue
        try:
            info = _extract_info(entry.accessible, entry.depth)
        except Exception:
            info = None
        if info is None or "defunct" in info.states:
            error = f"Stale ref {ref} (element no longer exists); re-run element_query."
            results.append(_Resolved(ref, entry, error=error))
            continue
        if info.role != entry.role:
            error = (
                f"Stale ref {ref} (role changed from '{entry.role}' to '{info.role}'); "
                "re-run element_query."
            )
            results.append(_Resolved(ref, entry, error=error))
            continue
        info.ref, info.app = ref, entry.app
        pending.append((info, toplevel_mapping(entry)))
        results.append(_Resolved(ref, entry, info))

    _apply_stability(mappings_by_app, kwin_before, kwin_error)
    for info, mapping in pending:
        _finalize(info, mapping)
    return results


def resolve_refs(refs: list[str]) -> list[dict[str, Any]]:
    """``{ref, ok: true, element}`` or ``{ref, ok: false, error}`` per ref, in order."""
    out: list[dict[str, Any]] = []
    for resolved in _resolve_refs(refs):
        if resolved.info is None:
            out.append({"ref": resolved.ref, "ok": False, "error": resolved.error})
        else:
            out.append({"ref": resolved.ref, "ok": True, "element": asdict(resolved.info)})
    return out


# ── action: invoke by ref or by strict locator ───────────────────────────


class _IncompleteDiscoveryError(Exception):
    """A read needed to evaluate every element in scope failed.

    ``application`` names the application whose subtree could not be read
    completely; it is empty when the failure precedes any application.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.application = ""


class _ActionRefusedError(Exception):
    """No action was invoked; the message is the caller-facing text."""


def _incomplete_discovery_message(exc: _IncompleteDiscoveryError, app_filter: str) -> str:
    """Refusal text naming the incompletely read application and how to get around it."""
    if not exc.application:
        return f"Incomplete discovery: {exc}"
    message = f"Incomplete discovery in application '{exc.application}': {exc}."
    if app_filter:
        # Already scoped: only a ref, which skips the search, gets past it.
        return message + "\nFind the element with element_query, then act on its ref."
    return message + f"\nSet app='{exc.application}' to scope the search."


def _exposed(actions: list[str]) -> str:
    return ", ".join(actions) if actions else "(none)"


@dataclass
class _Candidate:
    """One (element, action) pair a strict search found."""

    entry: _RefEntry
    name: str
    action: str


def _strict_candidates(
    criteria: _Criteria, wanted: str | Sequence[str]
) -> tuple[list[_Candidate], list[str]]:
    """One preferred matching action per element, with fail-closed discovery."""
    priorities = [wanted.lower()] if isinstance(wanted, str) else [a.lower() for a in wanted]
    candidates: list[_Candidate] = []
    exposed: list[str] = []

    def visit(
        element: Atspi.Accessible,
        application: Atspi.Accessible,
        app_name: str,
        toplevel: Atspi.Accessible | None,
        depth: int,
    ) -> None:
        if depth > _ACTION_SEARCH_MAX_DEPTH:
            msg = f"accessibility subtree deeper than {_ACTION_SEARCH_MAX_DEPTH} levels"
            raise _IncompleteDiscoveryError(msg)
        try:
            name = element.get_name() or ""
        except Exception as exc:
            raise _IncompleteDiscoveryError(f"could not read element name: {exc}") from exc
        try:
            role = element.get_role_name() or "unknown"
        except Exception as exc:
            raise _IncompleteDiscoveryError(f"could not read element role: {exc}") from exc
        try:
            description = element.get_description() or ""
        except Exception as exc:
            raise _IncompleteDiscoveryError(f"could not read element description: {exc}") from exc

        matched = criteria.matches_text(name, role, description)
        if matched and criteria.states:
            try:
                states = _state_nicks(element)
            except Exception as exc:
                raise _IncompleteDiscoveryError(f"could not read element states: {exc}") from exc
            matched = criteria.matches_states(states)

        if matched:
            # An element without an Action interface is not a candidate;
            # every other failure to enumerate its actions is a read fault.
            try:
                action_iface = element.get_action_iface()
            except Exception as exc:
                raise _IncompleteDiscoveryError(f"could not read action interface: {exc}") from exc
            if action_iface is not None:
                try:
                    action_count = action_iface.get_n_actions()
                except Exception as exc:
                    raise _IncompleteDiscoveryError(f"could not read action count: {exc}") from exc
                matched_actions: dict[str, str] = {}
                for index in range(action_count):
                    try:
                        available = action_iface.get_action_name(index) or ""
                    except Exception as exc:
                        msg = f"could not read action name: {exc}"
                        raise _IncompleteDiscoveryError(msg) from exc
                    if available and available not in exposed:
                        exposed.append(available)
                    key = available.lower()
                    if key in priorities:
                        matched_actions.setdefault(key, available)
                chosen = next(
                    (matched_actions[a] for a in priorities if a in matched_actions), None
                )
                if chosen is not None:
                    entry = _RefEntry(element, role, app_name, depth, application, toplevel)
                    candidates.append(_Candidate(entry, name, chosen))

        try:
            child_count = element.get_child_count()
        except Exception as exc:
            raise _IncompleteDiscoveryError(f"could not read child count: {exc}") from exc
        for index in range(child_count):
            try:
                child = element.get_child_at_index(index)
            except Exception as exc:
                raise _IncompleteDiscoveryError(f"could not read child: {exc}") from exc
            if child is not None:
                visit(child, application, app_name, child if depth == 0 else toplevel, depth + 1)

    desktop = Atspi.get_desktop(0)
    try:
        app_count = desktop.get_child_count()
    except Exception as exc:
        raise _IncompleteDiscoveryError(f"could not read desktop child count: {exc}") from exc
    for index in range(app_count):
        try:
            app = desktop.get_child_at_index(index)
        except Exception as exc:
            raise _IncompleteDiscoveryError(f"could not read desktop child: {exc}") from exc
        if app is None:
            continue
        # The app filter decides scope, so the name of every application is
        # needed even when a filter would later exclude it.
        try:
            app_name = app.get_name() or ""
        except Exception as exc:
            raise _IncompleteDiscoveryError(f"could not read application name: {exc}") from exc
        if not criteria.matches_app(app_name):
            continue
        try:
            visit(app, app, app_name, None, 0)
        except _IncompleteDiscoveryError as exc:
            # An application that stopped answering (e.g. a stopped process) can
            # report an empty name instead of failing; name it by its process so
            # the remedy can still say which app= to scope to.
            exc.application = app_name or _process_name(app)
            raise
    return candidates, exposed


def _process_name(app: _AccessibleApp) -> str:
    """The process name (``/proc/<pid>/comm``) behind an AT-SPI application, or ""."""
    try:
        pid = int(app.get_process_id())
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _action_by_ref(
    ref: str, action_name: str | Sequence[str]
) -> tuple[_RefEntry, ElementInfo, object, int, str]:
    """The live element and first available action in the caller's priority order."""
    resolved = _resolve_refs([ref])[0]
    if resolved.entry is None or resolved.info is None:
        raise _ActionRefusedError(resolved.error)
    entry, info = resolved.entry, resolved.info
    try:
        action_iface = entry.accessible.get_action_iface()
        names: list[str] = []
        if action_iface is not None:
            names = [
                action_iface.get_action_name(index) or ""
                for index in range(action_iface.get_n_actions())
            ]
    except Exception as exc:
        msg = f"Stale ref {ref} (element no longer exists); re-run element_query."
        raise _ActionRefusedError(msg) from exc
    requested = [action_name] if isinstance(action_name, str) else list(action_name)
    for wanted in requested:
        indexes = [index for index, name in enumerate(names) if name.lower() == wanted.lower()]
        if action_iface is not None and indexes:
            index = next((i for i in indexes if names[i] == wanted), indexes[0])
            return entry, info, action_iface, index, names[index]
    action_desc = (
        f"no AT-SPI action {requested[0]!r}"
        if len(requested) == 1
        else f"none of AT-SPI actions {requested!r}"
    )
    msg = (
        f'Element {ref} [{info.role}] "{info.name}" has {action_desc}; '
        f"it exposes: {_exposed([n for n in names if n])}.\n"
        "Use one of those action names."
    )
    raise _ActionRefusedError(msg)


def _action_by_locator(
    locator: Locator, action_name: str | Sequence[str]
) -> tuple[_RefEntry, ElementInfo, object, int, str]:
    """The single (element, action) candidate of a strict search; refuse anything else."""
    criteria = _Criteria.from_locator(locator)
    try:
        candidates, exposed = _strict_candidates(criteria, action_name)
    except _IncompleteDiscoveryError as exc:
        raise _ActionRefusedError(_incomplete_discovery_message(exc, criteria.app)) from exc
    desc = locator.describe()
    requested = [action_name] if isinstance(action_name, str) else list(action_name)
    if not candidates:
        target_desc = (
            f"AT-SPI action {requested[0]!r}"
            if len(requested) == 1
            else f"any AT-SPI action in {requested!r}"
        )
        msg = (
            f"No element matching {desc} exposes {target_desc}. "
            f"Matching elements expose: {_exposed(exposed)}.\n"
            "Use one of those action names, or check the locator with element_query."
        )
        raise _ActionRefusedError(msg)
    refs = [_REFS.mint(candidate.entry) for candidate in candidates]
    if len(candidates) > 1:
        listed = "; ".join(
            f'[{candidate.entry.role}] "{candidate.name}" in {candidate.entry.app} ({ref})'
            for candidate, ref in zip(candidates, refs, strict=True)
        )
        expose_desc = f"{requested[0]!r}" if len(requested) == 1 else f"an action in {requested!r}"
        msg = (
            f"Refused ambiguous AT-SPI action: {len(candidates)} elements matching {desc} "
            f"expose {expose_desc}: {listed}\n"
            "Narrow the locator (role, states, app) or act on one ref."
        )
        raise _ActionRefusedError(msg)
    return _action_by_ref(refs[0], candidates[0].action)


def invoke_action(address: dict[str, Any], action_name: str | Sequence[str]) -> dict[str, Any]:
    """Invoke one AT-SPI action on ``{ref}`` or on the strict ``{strict: locator}`` match.

    The ref path skips the search: the element must still be alive with its
    role unchanged and expose the action (case-insensitive). The strict path
    counts (element, action) candidates over a fail-closed walk; anything
    but exactly one raises :class:`_ActionRefusedError` and nothing is invoked.

    If the dispatch itself raises, the result carries ``invoked: None`` and
    the exception text in ``error``: the request was sent and the action may
    have run.
    """
    if "ref" in address:
        entry, info, action_iface, index, actual = _action_by_ref(str(address["ref"]), action_name)
    else:
        locator = Locator.model_validate(address.get("strict") or {})
        entry, info, action_iface, index, actual = _action_by_locator(locator, action_name)
    result: dict[str, Any] = {
        "matches": 1,
        "app": entry.app,
        "role": info.role,
        "name": info.name,
        "action": actual,
        "element": asdict(info),
    }
    try:
        result["invoked"] = bool(Atspi.Action.do_action(action_iface, index))
    except Exception as exc:
        # The request was sent but no reply arrived (e.g. a D-Bus timeout
        # because the action opened a modal dialog): it may still have run.
        result["invoked"] = None
        result["error"] = str(exc)
    return result


def _finalize(info: ElementInfo, mapping: _WindowMapping) -> ElementInfo:
    """Turn window-local extents into screen coordinates, or mark unavailable.

    Element-level problems (no extents at all, empty rectangle) win over
    mapping problems: an element without a rectangle has no position to
    translate regardless of whether its window was identified. ``window_id``
    is the matched KWin window either way.
    """
    info.window_id = None if mapping.window is None else mapping.window["id"]
    if not info.has_extents:
        info.mapped = False
        info.unavailable = "no-extents"
    elif info.width <= 0 or info.height <= 0:
        info.mapped = False
        info.unavailable = "empty-extents"
    elif mapping.offset is not None:
        info.x += mapping.offset[0]
        info.y += mapping.offset[1]
        info.mapped = True
        info.unavailable = ""
    else:
        info.mapped = False
        info.unavailable = mapping.reason or "unmapped"
    return info


def _state_nicks(element: Atspi.Accessible) -> list[str]:
    """The element's active AT-SPI state nicks.

    ``get_states()`` returns only the active members, which is both cheaper
    than probing every enum value and portable: GI enum types are not
    iterable in newer PyGObject releases.
    """
    return [state.value_nick for state in element.get_state_set().get_states() if state.value_nick]


# ── AT-SPI top-level → KWin window mapping ───────────────────────────────
#
# AT-SPI reports window-local coordinates (CoordType.WINDOW); a Wayland
# client cannot know where the compositor placed it. KWin does know, so each
# AT-SPI top-level is matched to exactly one KWin window and the whole
# subtree is translated by the origin of the KWin rectangle it matched.
# Any uncertainty — the KWin query failing, no candidate, several
# candidates, a window set that changed mid-walk — fails closed: elements
# report "unavailable" with a reason instead of coordinates that would
# click the wrong place.
#
# AT-SPI's window-local origin is the client's surface origin. For most
# windows that is KWin's client geometry; a client-side decorated window
# (Chromium) draws its shadow inside the surface, so its top-level matches
# KWin's buffer geometry instead.

_PROC = Path("/proc")
# Flatpak runs each app instance in its own systemd scope.
_FLATPAK_SCOPE = re.compile(r"app-flatpak-.+-\d+\.scope")


def _kwin_windows() -> tuple[list[KWinWindow], str]:
    """Snapshot every KWin window. Returns (windows, error)."""
    try:
        from kwin_mcp import geometry

        return geometry.collect_windows(), ""
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def _window_key(window: KWinWindow) -> tuple[object, ...]:
    """Identity + geometry of a KWin window for before/after comparison."""
    return (
        window["id"],
        window["pid"],
        window["caption"],
        *window["client"],
        *window["buffer"],
    )


def _apply_stability(
    mappings_by_app: dict[frozenset[int], list[_WindowMapping]],
    kwin_before: list[KWinWindow],
    kwin_error: str,
) -> None:
    """Unmap every top-level of an app whose KWin window set changed mid-walk.

    The AT-SPI traversal takes ~200ms; a window opening, closing or moving
    during it would otherwise translate elements against stale geometry.
    ``mappings_by_app`` is keyed by the app's window pids; a Flatpak instance
    can gain a window owner mid-walk, so they are resolved again against the
    second snapshot.
    """
    if kwin_error:
        return
    kwin_after, after_error = _kwin_windows()
    if after_error:
        # Cannot prove the snapshot was stable; fail closed for everything.
        for mappings in mappings_by_app.values():
            for mapping in mappings:
                if mapping.offset is not None:
                    mapping.window, mapping.offset, mapping.reason = (
                        None,
                        None,
                        "windows-changed",
                    )
        return
    for pids, mappings in mappings_by_app.items():
        owners = pids.union(*(_window_pids(pid, kwin_after) for pid in pids))
        before = {_window_key(window) for window in kwin_before if window["pid"] in owners}
        after = {_window_key(window) for window in kwin_after if window["pid"] in owners}
        if before != after:
            for mapping in mappings:
                mapping.window, mapping.offset, mapping.reason = None, None, "windows-changed"


def _cgroup(pid: int) -> str | None:
    """The cgroup v2 path of ``pid``, or None when unreadable."""
    try:
        lines = (_PROC / str(pid) / "cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("0::"):
            return line[3:]
    return None


def _window_pids(pid: int, kwin: list[KWinWindow]) -> frozenset[int]:
    """KWin window pids that belong to the AT-SPI application ``pid``.

    Normally the same pid. A Flatpak app reaches the accessibility bus through
    its sandbox's ``xdg-dbus-proxy``, so AT-SPI reports the proxy's pid while
    KWin reports the app's. Both run in the instance's own
    ``app-flatpak-<app-id>-<n>.scope`` cgroup, which identifies the windows of
    that instance and no other.
    """
    if any(window["pid"] == pid for window in kwin):
        return frozenset({pid})
    try:
        comm = (_PROC / str(pid) / "comm").read_text(encoding="utf-8").strip()
    except OSError:
        return frozenset({pid})
    scope = _cgroup(pid)
    if comm != "xdg-dbus-proxy" or scope is None:
        return frozenset({pid})
    if _FLATPAK_SCOPE.fullmatch(scope.rsplit("/", 1)[-1]) is None:
        return frozenset({pid})
    owners = {window["pid"] for window in kwin if _cgroup(window["pid"]) == scope}
    return frozenset({pid} | owners)


class _ExtentsRect(Protocol):
    """The rectangle members the matcher reads off an AT-SPI extents struct."""

    x: int
    y: int
    width: int
    height: int


class _ComponentShape(Protocol):
    """The AT-SPI Component interface surface the matcher calls."""

    def get_extents(self, coord_type: object, /) -> _ExtentsRect: ...


class _AccessibleTopLevel(Protocol):
    """The AT-SPI top-level surface the matcher calls.

    ``gi.repository.Atspi`` cannot be resolved statically, so the matcher is
    typed against the methods it actually uses; a real ``Atspi.Accessible``
    satisfies this protocol structurally, and so can test doubles.
    """

    def get_name(self) -> str | None: ...

    def get_component_iface(self) -> _ComponentShape | None: ...


class _AccessibleApp(Protocol):
    """The AT-SPI application-root surface the matcher calls."""

    def get_process_id(self) -> int: ...

    def get_child_count(self) -> int: ...

    def get_child_at_index(self, index: int) -> _AccessibleTopLevel | None: ...


def _resolve_app(
    app: _AccessibleApp,
    kwin: list[KWinWindow],
    kwin_error: str,
) -> tuple[frozenset[int], list[_WindowMapping], list[_AccessibleTopLevel | None]]:
    """Match every AT-SPI top-level of one application to a KWin window.

    Returns (KWin window pids of the app, mappings aligned with children,
    children). A child that vanished mid-walk, a top-level with several
    candidate windows, two top-levels claiming the same KWin window, or a
    top-level only the caption check tells apart from a same-size sibling
    unmaps the whole application — a partial mapping could silently click
    the wrong window. A top-level that no KWin window matches is unmapped
    alone: it cannot take a window from a sibling, and Chromium keeps
    hidden omnibox popups as AT-SPI top-levels that report themselves
    showing.
    """
    try:
        pid: int | None = int(app.get_process_id())
    except Exception:
        pid = None

    children: list[_AccessibleTopLevel | None] = []
    for j in range(app.get_child_count()):
        children.append(app.get_child_at_index(j))

    def _unmapped(reason: str) -> list[_WindowMapping]:
        return [_WindowMapping(None, reason) for _ in children]

    if pid is None or pid <= 0:
        return frozenset(), _unmapped("pid-unavailable"), children
    if kwin_error:
        return frozenset({pid}), _unmapped("kwin-query-failed"), children
    if any(child is None for child in children):
        return frozenset({pid}), _unmapped("window-set-mismatch"), children

    pids = _window_pids(pid, kwin)
    mappings: list[_WindowMapping] = []
    poisoned = False
    for child in children:
        assert child is not None
        name = child.get_name() or ""
        rect = _window_rect(child)
        if rect is None:
            # No extents at all: nothing to translate, and nothing to match.
            mappings.append(_WindowMapping(None, "no-extents"))
            continue
        window, reason, anchor = _match_toplevel(pids, name, rect, kwin)
        # A caption-only miss does not prove a top-level owns no window:
        # an unnamed sibling of the same size could still claim that window.
        if reason == "ambiguous" or (
            reason == "no-kwin-window"
            and _match_toplevel(pids, "", rect, kwin)[1] != "no-kwin-window"
        ):
            poisoned = True
        mappings.append(_WindowMapping(window, reason, rect, anchor=anchor))

    claimed: dict[str, int] = {}
    for mapping in mappings:
        if mapping.window is not None:
            claimed[mapping.window["id"]] = claimed.get(mapping.window["id"], 0) + 1
    if poisoned or any(count > 1 for count in claimed.values()):
        for mapping in mappings:
            if mapping.window is not None:
                mapping.window, mapping.reason = None, "ambiguous-window-match"
        return pids, mappings, children

    for mapping in mappings:
        if mapping.window is not None and mapping.rect is not None and mapping.anchor:
            mapping.offset = (
                mapping.anchor[0] - mapping.rect[0],
                mapping.anchor[1] - mapping.rect[1],
            )
    return pids, mappings, children


def _match_toplevel(
    pids: frozenset[int],
    name: str,
    rect: tuple[int, int, int, int],
    kwin: list[KWinWindow],
) -> tuple[KWinWindow | None, str, tuple[int, int] | None]:
    """Match one AT-SPI top-level to exactly one KWin window.

    Returns the window, the failure reason, and the screen origin of the KWin
    rectangle the top-level's window-local coordinates start from. No
    singleton shortcut: every candidate must survive the caption and size
    checks even when it is the only window of the process. The size is
    compared with both the client geometry and the buffer geometry, whose
    shadow margin a client-side decorated window includes in its own
    coordinates; exactly one window may match through either. When that
    window matches through both, the client origin is used.
    """
    candidates = [w for w in kwin if w["pid"] in pids and _eligible(w)]
    candidates = [w for w in candidates if _caption_consistent(w["caption"], name)]
    size = (rect[2], rect[3])
    # Client rects first, so a window that matches through both keeps its client origin.
    matches = [(w, w["client"]) for w in candidates] + [(w, w["buffer"]) for w in candidates]
    matches = [(w, r) for w, r in matches if (r[2], r[3]) == size]
    if not matches:
        return None, "no-kwin-window", None
    if len({w["id"] for w, _ in matches}) > 1:
        return None, "ambiguous", None
    window, matched = matches[0]
    return window, "", (matched[0], matched[1])


def _eligible(window: KWinWindow) -> bool:
    """KWin windows that can own an AT-SPI top-level.

    Popups stay eligible: GTK3 menus are separate AT-SPI top-levels backed by
    KWin popup windows, and excluding them would leave a wrong singleton
    match. They still must pass the caption and size checks.
    """
    return bool(
        window["managed"]
        and not window["deleted"]
        and not window["desktop"]
        and not window["dock"]
        and not window["notification"]
    )


def _caption_consistent(caption: str, name: str) -> bool:
    """Caption check applies only when both sides are non-empty.

    Both sides are compared with whitespace runs collapsed to one space, the
    same way KWin simplifies a client title into its caption: KWrite 26.08
    names its AT-SPI frame 'file.txt  — KWrite' (two spaces) while KWin shows
    'file.txt — KWrite'.

    Accepts exact equality or the KDE " — App" suffix KWin shows for KDE apps
    (AT-SPI 'Open File' vs KWin 'Open File — KWrite'). A plain prefix is never
    accepted: 'Document' must not match 'Document 2 — KWrite'.
    """
    n, c = " ".join(name.split()), " ".join(caption.split())
    if not n or not c:
        return True
    return c == n or c.startswith(n + " — ")


def _window_rect(element: _AccessibleTopLevel) -> tuple[int, int, int, int] | None:
    """Window-local extents of an AT-SPI element, or None when unavailable."""
    try:
        component = element.get_component_iface()
        if component is None:
            return None
        rect = component.get_extents(Atspi.CoordType.WINDOW)
        return rect.x, rect.y, rect.width, rect.height
    except Exception:
        return None


def _extract_info(element: Atspi.Accessible, depth: int) -> ElementInfo:
    """Extract information from an AT-SPI accessible element."""
    role = element.get_role_name() or "unknown"
    name = element.get_name() or ""
    description = element.get_description() or ""

    states = _state_nicks(element)

    # Get position and size. WINDOW extents are window-local and reliable for
    # both Qt and GTK; SCREEN extents are (0,0) on GTK4 and already include the
    # output origin for Qt on a second output, so they cannot be used here.
    # _finalize() adds the origin of the matched KWin rectangle.
    x, y, width, height = 0, 0, 0, 0
    has_extents = False
    try:
        component = element.get_component_iface()
        if component is not None:
            rect = component.get_extents(Atspi.CoordType.WINDOW)
            x, y, width, height = rect.x, rect.y, rect.width, rect.height
            has_extents = True
    except Exception:
        pass

    # Get available actions
    actions: list[str] = []
    try:
        action_iface = element.get_action_iface()
        if action_iface is not None:
            for i in range(action_iface.get_n_actions()):
                action_name = action_iface.get_action_name(i)
                if action_name:
                    actions.append(action_name)
    except Exception:
        pass

    # Editors and entries carry their content in the Text interface rather than
    # in the name, so without this their contents are invisible to callers. The
    # cap keeps a whole document out of a tree dump that walks every element.
    text = ""
    try:
        text_iface = element.get_text_iface()
        if text_iface is not None:
            # Call through the interface class: the bound method on the
            # accessible returns an empty string on this PyGObject version.
            count = Atspi.Text.get_character_count(text_iface)
            if count:
                text = Atspi.Text.get_text(text_iface, 0, min(count, _MAX_TEXT_CHARS))
                if count > _MAX_TEXT_CHARS:
                    text += "…"
    except Exception:
        pass

    # Scrollbars and sliders keep their position in the Value interface; without
    # it a caller can see that a scrollbar exists but not where it sits.
    value: float | None = None
    value_max: float | None = None
    try:
        value_iface = element.get_value_iface()
        if value_iface is not None:
            value = float(Atspi.Value.get_current_value(value_iface))
            value_max = float(Atspi.Value.get_maximum_value(value_iface))
    except Exception:
        pass
    return ElementInfo(
        role=role,
        name=name,
        description=description,
        states=states,
        x=x,
        y=y,
        width=width,
        height=height,
        actions=actions,
        text=text,
        value=value,
        value_max=value_max,
        children_count=element.get_child_count(),
        depth=depth,
        has_extents=has_extents,
        mapped=False,
        unavailable="",
    )


# ── Worker entrypoint ────────────────────────────────────────────────────


def _locator(request: dict[str, Any]) -> Locator:
    return Locator.model_validate(request.get("locator") or {})


def _handle_request(request: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one JSON request to its operation.

    Refusals (stale refs, missing actions, ambiguous or incomplete strict
    searches) are answered ``{"ok": false, "error": <text>}``: the worker is
    fine and the text is meant for the caller as it is.
    """
    op = request.get("op", "")

    if op == "query":
        elements = query_elements(_locator(request), max_depth=int(request.get("max_depth", 15)))
        return {"ok": True, "result": elements}

    if op == "wait":
        result = wait_elements(
            _locator(request),
            max_depth=int(request.get("max_depth", 15)),
            until=str(request.get("until", "present")),
            timeout_ms=int(request.get("timeout_ms", 5000)),
            poll_interval_ms=int(request.get("poll_interval_ms", 200)),
        )
        return {"ok": True, "result": result}

    if op == "resolve":
        return {"ok": True, "result": resolve_refs([str(ref) for ref in request.get("refs", [])])}

    if op == "action":
        try:
            result = invoke_action(
                dict(request.get("address") or {}), request.get("action_name", "")
            )
        except _ActionRefusedError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "result": result}

    return {"ok": False, "error": f"Unknown operation: {op}"}


def _serve(generation: int) -> None:
    """Answer newline-delimited JSON requests on stdin until EOF.

    Refs are minted for ``generation``. A request that raises is answered
    with ``{"worker_error": ...}`` so the caller can discard this worker and
    retry on a fresh one.
    """
    import warnings

    global _REFS
    _REFS = _RefTable(generation)

    # The worker's stderr is the server's stderr; silence the one library
    # warning that fires on every tree walk and is not actionable for users.
    warnings.filterwarnings("ignore", message=r"Atspi\.Action\.get_action_name is deprecated")

    # Keep the response stream private: anything a library prints lands on stderr.
    responses = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    for line in sys.stdin:
        try:
            resp = _handle_request(json.loads(line))
        except Exception as exc:
            resp = {"worker_error": f"{type(exc).__name__}: {exc}"}
        responses.write(json.dumps(resp) + "\n")
        responses.flush()


def _main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="python -m kwin_mcp.accessibility")
    parser.add_argument("--serve", action="store_true", required=True)
    parser.add_argument("--generation", type=int, default=1)
    args = parser.parse_args(argv)
    _serve(args.generation)


if __name__ == "__main__":
    _main(sys.argv[1:])
