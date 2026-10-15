"""Release gate: the CHANGELOG 1.0.0 entry documents the whole tool cutover.

The entry must follow Keep a Changelog, map every 0.11 tool name (and the two PR #103
names) to a 1.0 call that uses only 1.0 tool names, map every removed or renamed 0.11
parameter, and state each of the 24 intentional behavior changes of the design. The
expected sets are written out here, not read from the server or the CHANGELOG itself.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from tool_surface import REMOVED_TOOL_NAMES, TOOL_NAMES

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
README = REPO_ROOT / "README.md"

# The 9 names that 0.11 and 1.0 share; each keeps its name with new parameters or outputs.
KEPT_TOOL_NAMES = frozenset(
    {
        "session_start",
        "session_connect",
        "session_stop",
        "keyboard_type",
        "touch_tap",
        "touch_swipe",
        "touch_pinch",
        "window_close",
        "dbus_call",
    }
)
OLD_TOOL_NAMES = KEPT_TOOL_NAMES | frozenset(REMOVED_TOOL_NAMES)
NEW_TOOL_NAMES = frozenset(TOOL_NAMES) - KEPT_TOOL_NAMES

# Every (0.11 tool, parameter) that 1.0 removed or renamed.
REMOVED_PARAMETERS = frozenset(
    {
        ("session_start", "app_command"),
        ("session_start", "env"),
        ("session_start", "keep_screenshots"),
        ("session_start", "isolate_home"),
        ("session_start", "keep_home"),
        ("session_connect", "keep_screenshots"),
        ("screenshot", "region"),
        ("accessibility_tree", "app_name"),
        ("accessibility_tree", "role"),
        ("find_ui_elements", "query"),
        ("find_ui_elements", "app_name"),
        ("find_ui_elements", "states"),
        ("wait_for_element", "query"),
        ("wait_for_element", "app_name"),
        ("wait_for_element", "expected_states"),
        ("wait_for_element", "timeout_ms"),
        ("wait_for_element", "poll_interval_ms"),
        ("invoke_ui_action", "query"),
        ("invoke_ui_action", "app_name"),
        ("invoke_ui_action", "action_name"),
        ("mouse_click", "x"),
        ("mouse_click", "y"),
        ("mouse_click", "double"),
        ("mouse_click", "triple"),
        ("mouse_click", "modifiers"),
        ("mouse_click", "screenshot_after_ms"),
        ("mouse_move", "x"),
        ("mouse_move", "y"),
        ("mouse_move", "screenshot_after_ms"),
        ("mouse_scroll", "x"),
        ("mouse_scroll", "y"),
        ("mouse_scroll", "horizontal"),
        ("mouse_scroll", "discrete"),
        ("mouse_drag", "from_x"),
        ("mouse_drag", "from_y"),
        ("mouse_drag", "to_x"),
        ("mouse_drag", "to_y"),
        ("mouse_drag", "modifiers"),
        ("mouse_drag", "waypoints"),
        ("mouse_drag", "screenshot_after_ms"),
        ("mouse_button_down", "x"),
        ("mouse_button_down", "y"),
        ("mouse_button_up", "x"),
        ("mouse_button_up", "y"),
        ("keyboard_type", "screenshot_after_ms"),
        ("keyboard_type_unicode", "screenshot_after_ms"),
        ("keyboard_key", "key"),
        ("keyboard_key", "screenshot_after_ms"),
        ("keyboard_key_down", "key"),
        ("keyboard_key_up", "key"),
        ("touch_tap", "x"),
        ("touch_tap", "y"),
        ("touch_tap", "screenshot_after_ms"),
        ("touch_swipe", "from_x"),
        ("touch_swipe", "from_y"),
        ("touch_swipe", "to_x"),
        ("touch_swipe", "to_y"),
        ("touch_swipe", "screenshot_after_ms"),
        ("touch_pinch", "center_x"),
        ("touch_pinch", "center_y"),
        ("touch_pinch", "screenshot_after_ms"),
        ("touch_multi_swipe", "from_x"),
        ("touch_multi_swipe", "from_y"),
        ("touch_multi_swipe", "to_x"),
        ("touch_multi_swipe", "to_y"),
        ("touch_multi_swipe", "fingers"),
        ("touch_multi_swipe", "screenshot_after_ms"),
        ("focus_window", "window_id"),
        ("focus_window", "app_name"),
        ("window_geometry", "window_id"),
        ("window_geometry", "app_name"),
        ("window_set_geometry", "window_id"),
        ("window_set_geometry", "x"),
        ("window_set_geometry", "y"),
        ("window_set_geometry", "width"),
        ("window_set_geometry", "height"),
        ("window_close", "window_id"),
        ("window_set_desktop", "window_id"),
        ("window_set_desktop", "desktop_id"),
        ("wayland_info", "filter_protocol"),
    }
)

# The 24 intentional behavior changes of the design (S §5.4), each with the terms its
# CHANGELOG line must contain.
BEHAVIOR_CHANGES: dict[int, tuple[str, ...]] = {
    1: ("37 become 29", "9 kept, 28 removed, 20 new", "Removed in kwin-mcp 1.0; use"),
    2: ("session_start", "app_launch", "session_stop"),
    3: ("retention={artifacts, home}", 'home="isolated"', "session_connect"),
    4: ("window_list", "exactly one", "kind"),
    5: ("element_query(locator, max_depth, wait)", "child_count: 0"),
    6: ("warnings",),
    7: ("capture", "pointer_scroll", "pointer_hold", "keyboard_hold", "element_act"),
    8: ('keyboard_type(text, method="unicode")', 'method="keys"'),
    9: ("count=2|3", 'axis="horizontal"', 'unit="detent"', 'modifiers="ctrl+shift"'),
    10: ("Element refs", 'wait.until="absent"', "session_info", "pointer_drag(dwell_ms)"),
    11: ("app_id", "consent", "refusal", "not_confirmed", "no_consent"),
    12: ("ydotool",),
    13: ('pointer_hold(phase="press")', 'keyboard_hold(phase="press")', "refused"),
    14: ("pointer_hold(phase)", "keyboard_hold(phase)"),
    15: ("# Accessibility Tree (N elements)", "Found N elements matching"),
    16: ("clipboard_read", "{text}", "window_list(filter={active:true})"),
    17: ("lower bound", "max_depth <= 0"),
    18: ("pointer_move", 'pointer_hold(phase="move")', "use pointer_hold(phase='move'"),
    19: ("kind", 'add kind:["normal"] or use id'),
    20: ("window_close", "window_list"),
    21: ("Unknown arguments are rejected", "capture={delays_ms:[...]}"),
    22: ("No session running.", "No input backend (KWin EIS unavailable)"),
    23: ("keyboard_type", "keyboard_hold", "shift"),
    24: ("EIS device replacement", "stays pressed", "session_info", "replacement device"),
}

_CALL = re.compile(r"\b([a-z][a-z_]*)\(")
_CODE = re.compile(r"`([^`]+)`")


def _slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, drop punctuation, spaces become hyphens."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def _sections(text: str, level: int) -> dict[str, str]:
    """Map each heading of ``level`` to its body, up to the next heading of that level or higher."""
    pattern = re.compile(rf"^#{{1,{level}}} (.+)$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        if not match.group(0).startswith("#" * level + " "):
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1).strip()] = text[match.end() : end]
    return sections


def _table_rows(body: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in body.splitlines():
        if not line.startswith("|") or re.fullmatch(r"\|[-| ]+\|", line):
            continue
        rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows[1:] if rows else rows


def _release_entry() -> tuple[str, str]:
    """The heading and body of the first released version in CHANGELOG.md."""
    versions = list(_sections(CHANGELOG.read_text(encoding="utf-8"), 2).items())
    assert versions[0][0] == "[Unreleased]", "the CHANGELOG must start with [Unreleased]"
    return versions[1]


def _entry_subsections() -> dict[str, str]:
    return _sections(_release_entry()[1], 4)


def _old_name(cell: str) -> str:
    names = _CODE.findall(cell)
    assert len(names) == 1, cell
    return names[0]


@pytest.mark.qa("QA-PKG-004")
def test_changelog_1_0_0_migration_table() -> None:
    heading, body = _release_entry()
    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    assert re.fullmatch(rf"\[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}", heading), heading
    assert version == "1.0.0"
    categories = _sections(body, 3)
    assert {"Added", "Changed", "Removed"} <= categories.keys()
    assert set(categories) <= {"Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"}

    # Wording of the cutover counts.
    assert "9 tool names are kept" in body
    assert "28 are removed and 20 are new" in body

    # Removed lists all 28 names; Changed lists the 9 kept names.
    removed_bullet = next(
        line for line in categories["Removed"].splitlines() if line.startswith("- 28 tool names")
    )
    assert set(_CODE.findall(removed_bullet)) == set(REMOVED_TOOL_NAMES)
    changed_kept = {
        _CODE.findall(line)[0]
        for line in categories["Changed"].splitlines()
        if line.startswith("  - `")
    }
    assert changed_kept == KEPT_TOOL_NAMES
    added_bullet = next(
        line for line in categories["Added"].splitlines() if line.startswith("- New tools:")
    )
    assert set(_CODE.findall(added_bullet)) == NEW_TOOL_NAMES
    assert len(NEW_TOOL_NAMES) == 20

    subsections = _entry_subsections()

    # Tool migration table: the old column is exactly the 37 old names, each new call
    # uses only 1.0 names.
    rows = _table_rows(subsections["Tool migration table"])
    old_names = [_old_name(row[0]) for row in rows]
    assert len(old_names) == len(set(old_names)) == 37
    assert set(old_names) == OLD_TOOL_NAMES
    for row in rows:
        calls = _CALL.findall(row[1])
        assert calls, row
        assert set(calls) <= set(TOOL_NAMES), row

    # Parameter migration table: every removed parameter has a row.
    documented = {
        (_old_name(row[0]), parameter)
        for row in _table_rows(subsections["Parameter migration table"])
        for parameter in _CODE.findall(row[1])
    }
    assert documented >= REMOVED_PARAMETERS, sorted(REMOVED_PARAMETERS - documented)

    # Every intentional behavior change has its own numbered line.
    items = {
        int(match.group(1)): match.group(2)
        for match in re.finditer(
            r"^(\d+)\. (.+)$", subsections["Behavior changes"], flags=re.MULTILINE
        )
    }
    assert sorted(items) == list(range(1, 25))
    for number, terms in BEHAVIOR_CHANGES.items():
        for term in terms:
            assert term in items[number], (number, term)

    # In-entry anchors and the comparison links.
    anchors = {_slug(name) for name in subsections}
    for anchor in re.findall(r"\]\(#([^)]+)\)", body):
        assert anchor in anchors, anchor
    text = CHANGELOG.read_text(encoding="utf-8")
    assert "[Unreleased]: https://github.com/isac322/kwin-mcp/compare/v1.0.0...HEAD" in text
    assert "[1.0.0]: https://github.com/isac322/kwin-mcp/compare/v0.11.0...v1.0.0" in text


def test_readme_links_resolve_to_the_release_entry() -> None:
    heading, _ = _release_entry()
    anchor = _slug(heading)
    links = re.findall(r"CHANGELOG\.md#([\w-]+)", README.read_text(encoding="utf-8"))
    assert links, "README must link to the 1.0.0 CHANGELOG entry"
    assert set(links) == {anchor}


def test_slug_matches_github_anchors() -> None:
    """Calibration of the anchor rule against anchors GitHub generates."""
    assert _slug("[1.0.0] - 2026-10-12") == "100---2026-10-12"
    assert _slug("Tool migration table") == "tool-migration-table"
    assert _slug("Migrating from 0.11") == "migrating-from-011"
