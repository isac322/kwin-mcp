"""End-to-end coverage for native AT-SPI action invocation."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from kwin_mcp.core import AutomationEngine

_DYNAMIC_LABEL = re.compile(
    r'^\s*- \[label] "([^"]*)" \(enabled, sensitive, read-only\) '
    r"@ (?:screen \(-?\d+, -?\d+, \d+x\d+\)|unavailable \([^)]+\))$"
)
POLL_INTERVAL_SECONDS = 0.1
ACTION_TIMEOUT_SECONDS = 3.0


def _binary_value(tree: str) -> str | None:
    lines = tree.splitlines()
    for index, line in enumerate(lines):
        if index == 0 or not re.search(r'\[label] "2" \(enabled, sensitive, read-only\)', line):
            continue
        match = _DYNAMIC_LABEL.match(lines[index - 1])
        assert match is not None, tree[:500]
        return match.group(1).replace(" ", "")
    return None


def _wait_for_binary(engine: AutomationEngine, expected: str) -> None:
    deadline = time.monotonic() + ACTION_TIMEOUT_SECONDS
    actual: str | None = None
    tree = ""
    while time.monotonic() < deadline:
        tree = engine.accessibility_tree(app_name="kcalc")
        actual = _binary_value(tree)
        if actual == expected:
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    assert actual == expected, tree[:500]


def test_invoke_ui_action_works_on_background_window(
    kcalc_session: AutomationEngine,
    wait_for_app: Callable[[str], str],
) -> None:
    kcalc_session.launch_app(command="kwrite /tmp/kwin-mcp-action-background.txt")
    wait_for_app("kwrite")

    # Make the second app definitively foreground; KCalc must remain actionable
    # through its semantic accessibility interface without pointer coordinates.
    focused = kcalc_session.focus_window(app_name="kwrite")
    assert focused.startswith("Focused:"), focused
    assert "kwrite" in kcalc_session.active_window().lower()

    result = kcalc_session.invoke_ui_action(
        query="Seven",
        action_name="Press",
        app_name="kcalc",
    )
    assert "Invoked" in result, result

    _wait_for_binary(kcalc_session, "111")

    # Invoking the background control must not require focusing KCalc first.
    assert "kwrite" in kcalc_session.active_window().lower()


def test_invoke_ui_action_fails_closed_on_ambiguous_match(
    kcalc_session: AutomationEngine,
) -> None:
    result = kcalc_session.invoke_ui_action(
        query="button",
        action_name="Press",
        app_name="kcalc",
    )

    assert result.startswith("Refused ambiguous AT-SPI action:"), result


def test_invoke_ui_action_respects_application_filter(
    kcalc_session: AutomationEngine,
) -> None:
    result = kcalc_session.invoke_ui_action(
        query="Seven",
        action_name="Press",
        app_name="definitely-not-kcalc",
    )

    assert result == ("No element matching 'Seven' exposes AT-SPI action 'Press'.")
