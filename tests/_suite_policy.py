"""Suite policy shared by ``tests/conftest.py`` and ``tests/unit/test_suite_policy.py``.

Everything here except ``require_or_skip`` is a pure function of its arguments,
so the unit tests can pin the sharding and skip rules without running pytest
hooks. ``require_or_skip`` lives here too because tests import it by module
name, which a ``conftest.py`` must not be used for.
"""

from __future__ import annotations

import statistics
from typing import TYPE_CHECKING, Final, NoReturn

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

STRICT_SKIPS_ENV: Final = "KWIN_MCP_E2E_STRICT_SKIPS"
REQUIRE_ENV: Final = "KWIN_MCP_E2E_REQUIRE"

# The only skip reasons allowed when KWIN_MCP_E2E_STRICT_SKIPS=1. A skip reason
# must start with one of these verbatim; any other skip (including
# ``pytest.importorskip``) fails the run as ``unregistered skip: <reason>``.
KNOWN_LIMIT_PREFIXES: Final[tuple[str, ...]] = (
    # test_virtual_session_smoke.py: KWin's virtual backend never answers a
    # ScreenShot2 capture; the only expected skip in CI (one per shard set).
    "set KWIN_MCP_E2E_SCREENSHOT=1 to run",
    # test_exit_cleanup*.py: directory-permission cases cannot fail as root.
    "root ignores directory permissions",
    # test_mcp_session_observation.py: only when KWIN_MCP_E2E_SCREENSHOT=1.
    "headless screenshot-error behavior contradicts the explicitly enabled success path",
    # window_zoo could not create a layer-shell surface (GtkLayerShell missing or
    # unsupported). CI sets KWIN_MCP_E2E_REQUIRE=layer-shell, so it fails there.
    "known KWin limit: layer-shell surface unavailable",
    # QA-WIN-051: decided by tests/e2e/test_oracles.py in phase P1.
    "KWin never activates layer-shell",
    # QA-INP-030: only when kwin_oracle shows the button still down afterwards.
    "known KWin limit: release from a second EIS device does not clear another device's press",
    # QA-INP-047: KWin keeps emulated input held after the EIS client vanishes.
    "KWin limit: emulated state survives client disconnect",
    # QA-INP-047: KWin 6.7.5 (Fedora 44, Tumbleweed, Arch) crashes (SIGSEGV) when an
    # EIS client holding a button and a key is SIGKILLed; KWin 6.3.6 (Debian) survives.
    "KWin limit: KWin crashes when a killed EIS client held input",
)

# The test the shard order starts with: a broken GtkLayerShell/LD_PRELOAD shim
# then fails within seconds instead of after a whole shard (amendment A9).
FIRST_TEST: Final = "tests/e2e/test_oracles.py::test_window_zoo_reports_layer_shell_available"

# Test files that the migration plan creates in later phases. Their seeded
# estimates may stay in tests/e2e/durations.json before the files exist; the
# set is emptied when durations.json is regenerated from the new suite (P7).
PLANNED_TEST_FILES: Final[frozenset[str]] = frozenset(
    {
        "tests/e2e/test_a11y_text_formats.py",
        "tests/e2e/test_app_launch_log.py",
        "tests/e2e/test_capture.py",
        "tests/e2e/test_cli_surface.py",
        "tests/e2e/test_clipboard.py",
        "tests/e2e/test_desktop_list.py",
        "tests/e2e/test_eis_held_revalidation.py",
        "tests/e2e/test_element_act.py",
        "tests/e2e/test_element_query.py",
        "tests/e2e/test_element_refs.py",
        "tests/e2e/test_exit_cleanup_live.py",
        "tests/e2e/test_exit_cleanup_virtual.py",
        "tests/e2e/test_held_input.py",
        "tests/e2e/test_input_options_matrix.py",
        "tests/e2e/test_keyboard_hold.py",
        "tests/e2e/test_keyboard_press.py",
        "tests/e2e/test_keyboard_type.py",
        "tests/e2e/test_mcp_annotations.py",
        "tests/e2e/test_mcp_clipboard.py",
        "tests/e2e/test_mcp_errors.py",
        "tests/e2e/test_mcp_images_flag.py",
        "tests/e2e/test_mcp_input_tools.py",
        "tests/e2e/test_mcp_progress.py",
        "tests/e2e/test_mcp_resources.py",
        "tests/e2e/test_mcp_scenarios.py",
        "tests/e2e/test_mcp_schema_wire.py",
        "tests/e2e/test_mcp_unknown_args.py",
        "tests/e2e/test_pointer_click.py",
        "tests/e2e/test_pointer_drag.py",
        "tests/e2e/test_pointer_hold.py",
        "tests/e2e/test_pointer_move.py",
        "tests/e2e/test_pointer_scroll.py",
        "tests/e2e/test_screen_capture.py",
        "tests/e2e/test_screen_capture_visual.py",
        "tests/e2e/test_session_accessibility_switch.py",
        "tests/e2e/test_session_connect.py",
        "tests/e2e/test_session_info.py",
        "tests/e2e/test_session_processes.py",
        "tests/e2e/test_session_retention.py",
        "tests/e2e/test_session_start.py",
        "tests/e2e/test_session_stop.py",
        "tests/e2e/test_tool_surface_cutover.py",
        "tests/e2e/test_touch.py",
        "tests/e2e/test_wayland_list.py",
        "tests/e2e/test_window_close_consent.py",
        "tests/e2e/test_window_focus.py",
        "tests/e2e/test_window_kinds.py",
        "tests/e2e/test_window_list.py",
        "tests/e2e/test_window_place.py",
        "tests/e2e/test_window_place_desktop.py",
        "tests/e2e/test_window_selector_errors.py",
        "tests/unit/test_app_log_validation.py",
        "tests/unit/test_changelog_migration.py",
        "tests/unit/test_cli_parse_value.py",
        "tests/unit/test_element_query_wait_progress.py",
        "tests/unit/test_element_query_wait_settle.py",
        "tests/unit/test_element_refs_table.py",
        "tests/unit/test_held_state_machine.py",
        "tests/unit/test_keyboard_type_progress.py",
        "tests/unit/test_keychord_grammar.py",
        "tests/unit/test_kwin_script_render.py",
        "tests/unit/test_locator_matching.py",
        "tests/unit/test_qa_traceability.py",
        "tests/unit/test_readme_tables.py",
        "tests/unit/test_render.py",
        "tests/unit/test_schema_budget.py",
        "tests/unit/test_schema_equivalence.py",
        "tests/unit/test_schema_forbidden_strings.py",
        "tests/unit/test_schema_rules.py",
        "tests/unit/test_screenshot_reframe.py",
        "tests/unit/test_server_flags.py",
        "tests/unit/test_session_input_validation.py",
        "tests/unit/test_target_resolution.py",
        "tests/unit/test_tool_registration.py",
        "tests/unit/test_tool_surface_snapshot.py",
        "tests/unit/test_unknown_args.py",
        "tests/unit/test_window_close_consent_flow.py",
        "tests/unit/test_window_place_validation.py",
        "tests/unit/test_window_selector_matching.py",
    }
)


def parse_shard(text: str) -> tuple[int, int]:
    """Parse ``--kwin-shard`` ``I/N`` into ``(I, N)``; ``1 <= I <= N`` is required.

    Raises ``ValueError`` with a message naming the expected form.
    """
    index_text, sep, count_text = text.partition("/")
    if not sep or not index_text.isdigit() or not count_text.isdigit():
        msg = f"--kwin-shard expects I/N with positive integers, got {text!r}"
        raise ValueError(msg)
    index, count = int(index_text), int(count_text)
    if not 1 <= index <= count:
        msg = f"--kwin-shard expects 1 <= I <= N, got {text!r}"
        raise ValueError(msg)
    return index, count


def unit_key(path: str, class_name: str | None, durations: Mapping[str, float]) -> str:
    """Return the sharding unit of a test: ``path::Class`` when durations lists it, else the file.

    A class key lets one very long file be split by class (amendment A6); every
    other test is assigned with its whole file, so module-scoped fixtures never
    run in two shards.
    """
    if class_name is not None:
        key = f"{path}::{class_name}"
        if key in durations:
            return key
    return path


def default_weight(durations: Mapping[str, float]) -> float:
    """Weight of a unit missing from ``durations``: the median known value (1.0 s if none)."""
    return statistics.median(durations.values()) if durations else 1.0


def assign_shards(
    units: Iterable[str],
    durations: Mapping[str, float],
    count: int,
    pinned: Iterable[str] = (),
) -> dict[str, int]:
    """Partition sharding units over ``count`` shards by longest-processing-time greedy.

    Units are sorted by (-seconds, name); each goes to the currently lightest
    shard, ties to the lowest index. A unit missing from ``durations`` weighs
    the median of the known values (1.0 s for an empty table). Units in
    ``pinned`` are placed on shard 1 before the greedy pass and count towards
    its load. Returns unit -> shard index in ``1..count``. Pure and
    deterministic: the result depends only on the set of units, not their order.
    """
    if count < 1:
        msg = f"shard count must be at least 1, got {count}"
        raise ValueError(msg)
    unique = sorted(set(units))
    default = default_weight(durations)

    def weight(unit: str) -> float:
        return durations.get(unit, default)

    loads = [0.0] * count
    owner: dict[str, int] = {}
    pinned_set = set(pinned)
    for unit in unique:
        if unit in pinned_set:
            owner[unit] = 1
            loads[0] += weight(unit)
    for unit in sorted((u for u in unique if u not in owner), key=lambda u: (-weight(u), u)):
        lightest = min(range(count), key=lambda i: (loads[i], i))
        owner[unit] = lightest + 1
        loads[lightest] += weight(unit)
    return owner


def is_known_limit(reason: str) -> bool:
    """Whether a skip reason starts with a registered known-limit prefix."""
    return reason.startswith(KNOWN_LIMIT_PREFIXES)


def strict_skips_enabled(environ: Mapping[str, str]) -> bool:
    """Whether unregistered skips fail the run (``KWIN_MCP_E2E_STRICT_SKIPS=1``)."""
    return environ.get(STRICT_SKIPS_ENV, "") == "1"


def required_capabilities(environ: Mapping[str, str]) -> frozenset[str]:
    """Capabilities whose absence fails instead of skipping (``KWIN_MCP_E2E_REQUIRE``)."""
    raw = environ.get(REQUIRE_ENV, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def require_or_skip(capability: str, reason: str, environ: Mapping[str, str]) -> NoReturn:
    """Skip a test whose capability is missing, or fail when the run requires it.

    ``reason`` must be a registered known limit; anything else is a bug in the
    calling test and raises ``ValueError``. Pass ``os.environ`` as ``environ``.
    """
    if not is_known_limit(reason):
        msg = f"require_or_skip reason is not a registered known limit: {reason!r}"
        raise ValueError(msg)
    if capability in required_capabilities(environ):
        pytest.fail(f"required capability {capability!r} is unavailable: {reason}")
    pytest.skip(reason)
