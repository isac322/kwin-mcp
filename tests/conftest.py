"""Suite-wide pytest hooks: ``--kwin-shard`` and the strict-skip guard.

This file defines no fixtures. The policy itself (known-limit prefixes, the
sharding algorithm) lives in ``tests/_suite_policy.py`` so the unit tree can
test it directly.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from _suite_policy import (
    FIRST_TEST,
    assign_shards,
    default_weight,
    is_known_limit,
    parse_shard,
    strict_skips_enabled,
    unit_key,
)

if TYPE_CHECKING:
    from collections.abc import Generator

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DURATIONS = Path(__file__).resolve().parent / "e2e" / "durations.json"
_SKIP_PREFIX = "Skipped: "


@dataclass(frozen=True)
class _ShardSummary:
    """What the shard selection kept, for the post-collection report line."""

    index: int
    count: int
    files: int
    seconds: float


_SUMMARY_KEY = pytest.StashKey[_ShardSummary]()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--kwin-shard",
        default="1/1",
        metavar="I/N",
        help="run only the test files assigned to shard I of N (tests/e2e/durations.json)",
    )


def _shard_option(config: pytest.Config) -> tuple[int, int]:
    try:
        return parse_shard(str(config.getoption("--kwin-shard")))
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from exc


def pytest_configure(config: pytest.Config) -> None:
    # Validate early so a bad value is a usage error even with --collect-only.
    _shard_option(config)


def pytest_report_header(config: pytest.Config) -> list[str]:
    index, count = _shard_option(config)
    strict = "on" if strict_skips_enabled(os.environ) else "off"
    return [f"kwin-shard: {index}/{count}, strict skips: {strict}"]


def _relative_path(item: pytest.Item) -> str:
    path = item.path.resolve()
    try:
        return path.relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _item_unit(item: pytest.Item, durations: dict[str, float]) -> str:
    cls = getattr(item, "cls", None)
    class_name = cls.__name__ if isinstance(cls, type) else None
    return unit_key(_relative_path(item), class_name, durations)


def _load_durations() -> dict[str, float]:
    raw = json.loads(_DURATIONS.read_text(encoding="utf-8"))
    return {str(key): float(value) for key, value in raw.items()}


def _is_first_test(item: pytest.Item) -> bool:
    path, _, name = FIRST_TEST.partition("::")
    return _relative_path(item) == path and item.name == name


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    index, count = _shard_option(config)
    if count > 1:
        durations = _load_durations()
        unit_of = {item.nodeid: _item_unit(item, durations) for item in items}
        first_path = FIRST_TEST.partition("::")[0]
        owner = assign_shards(unit_of.values(), durations, count, pinned=(first_path,))
        keep = [item for item in items if owner[unit_of[item.nodeid]] == index]
        drop = [item for item in items if owner[unit_of[item.nodeid]] != index]
        if drop:
            config.hook.pytest_deselected(items=drop)
        items[:] = keep
        kept_units = {unit_of[item.nodeid] for item in keep}
        fallback = default_weight(durations)
        seconds = sum(durations.get(unit, fallback) for unit in kept_units)
        config.stash[_SUMMARY_KEY] = _ShardSummary(index, count, len(kept_units), seconds)
    # Amendment A9: the layer-shell self-test runs before anything that needs it.
    items.sort(key=lambda item: not _is_first_test(item))


def pytest_report_collectionfinish(config: pytest.Config) -> list[str]:
    summary = config.stash.get(_SUMMARY_KEY, None)
    if summary is None:
        return []
    return [
        f"kwin-shard {summary.index}/{summary.count}: {summary.files} files, "
        f"est. {summary.seconds:.0f} s"
    ]


def _skip_reason(longrepr: object) -> str:
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        message = str(longrepr[2])
    else:
        message = str(longrepr)
    return message.removeprefix(_SKIP_PREFIX)


def _fail_unregistered(report: pytest.TestReport | pytest.CollectReport) -> None:
    if not report.skipped or hasattr(report, "wasxfail"):
        return
    if not strict_skips_enabled(os.environ):
        return
    reason = _skip_reason(report.longrepr)
    if is_known_limit(reason):
        return
    report.outcome = "failed"
    report.longrepr = f"unregistered skip: {reason}"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    _fail_unregistered(report)
    return report


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(
    collector: pytest.Collector,
) -> Generator[None, pytest.CollectReport, pytest.CollectReport]:
    # A module-level pytest.skip()/importorskip() skips at collection time.
    report = yield
    _fail_unregistered(report)
    return report
