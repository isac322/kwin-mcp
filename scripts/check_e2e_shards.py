#!/usr/bin/env python3
"""Validate one row of E2E shard status files and its current-run jobs.

Usage: python3 scripts/check_e2e_shards.py --status-dir DIR --label LABEL --shards N

The checker deliberately evaluates only the requested row.  A row is valid when
it has exactly the requested shard files, every shard reports ``result=success``,
each collected count is a non-negative integer, exactly one shard records a
positive ``full`` count, and the collected counts sum to that full count.  When
``--jobs-file`` is supplied, the file must contain the paginated jobs response
for this run, and the requested image and shard jobs must each appear exactly
once with ``status=completed`` and ``conclusion=success``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_NON_NEGATIVE = re.compile(r"[0-9]+\Z")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


class StatusError(ValueError):
    """A status directory does not satisfy the shard aggregation contract."""


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _label(value: str) -> str:
    if not _LABEL.fullmatch(value):
        raise argparse.ArgumentTypeError("must contain only letters, digits, '-' or '_'")
    return value


def _parse_status(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise StatusError(f"{path.name} cannot be read: {exc}") from exc

    values: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line or "=" not in line:
            raise StatusError(f"{path.name}:{line_number} is not key=value status data")
        key, value = line.split("=", 1)
        if key not in {"result", "collected", "full"}:
            raise StatusError(f"{path.name}:{line_number} has unknown field {key!r}")
        if key in values:
            raise StatusError(f"{path.name} repeats field {key!r}")
        if not value:
            raise StatusError(f"{path.name}:{line_number} has an empty {key!r}")
        values[key] = value

    if set(values) != {"result", "collected"} and set(values) != {"result", "collected", "full"}:
        raise StatusError(f"{path.name} must contain result and collected fields")
    if values.get("result") != "success":
        raise StatusError(f"{path.name} reports result {values.get('result', '<missing>')!r}")
    for key in ("collected", "full"):
        if key in values and not _NON_NEGATIVE.fullmatch(values[key]):
            raise StatusError(f"{path.name} has non-numeric {key} count {values[key]!r}")
    return values


def _parse_jobs(path: Path, label: str, shards: int) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StatusError(f"jobs file cannot be read as JSON: {exc}") from exc

    if not isinstance(payload, list):
        raise StatusError("jobs file must contain a JSON list of response pages")

    jobs: list[dict[str, object]] = []
    for page_number, page in enumerate(payload, 1):
        if not isinstance(page, dict):
            raise StatusError(f"jobs page {page_number} must be a JSON object")
        page_jobs = page.get("jobs")
        if not isinstance(page_jobs, list):
            raise StatusError(f"jobs page {page_number} must contain a jobs list")
        for job_number, job in enumerate(page_jobs, 1):
            if not isinstance(job, dict):
                raise StatusError(f"jobs page {page_number} entry {job_number} must be an object")
            if any(key not in job for key in ("name", "status", "conclusion")):
                raise StatusError(
                    f"jobs page {page_number} entry {job_number} must contain "
                    "name, status, and conclusion"
                )
            if (
                not isinstance(job["name"], str)
                or not isinstance(job["status"], str)
                or not (isinstance(job["conclusion"], str) or job["conclusion"] is None)
            ):
                raise StatusError(
                    f"jobs page {page_number} entry {job_number} has invalid "
                    "name, status, or conclusion"
                )
            jobs.append(job)

    expected = {
        f"Build E2E image ({label})",
        *(f"E2E shard ({label}, {number}/{shards})" for number in range(1, shards + 1)),
    }
    for expected_name in sorted(expected):
        matching = [job for job in jobs if job["name"] == expected_name]
        if not matching:
            raise StatusError(f"jobs file is missing required job {expected_name!r}")
        if len(matching) != 1:
            raise StatusError(
                f"jobs file has duplicate required job {expected_name!r} (found {len(matching)})"
            )
        job = matching[0]
        if job["status"] != "completed" or job["conclusion"] != "success":
            raise StatusError(
                f"required job {expected_name!r} is not successful "
                f"(status={job['status']!r}, conclusion={job['conclusion']!r})"
            )


def check_row(status_dir: Path, label: str, shards: int) -> tuple[int, int]:
    """Validate one row and return ``(collected, full)`` on success."""
    if not status_dir.is_dir():
        raise StatusError(f"status directory does not exist: {status_dir}")

    expected = {f"{label}-s{number}.txt" for number in range(1, shards + 1)}
    try:
        files = [path for path in status_dir.iterdir() if path.is_file()]
    except OSError as exc:
        raise StatusError(f"cannot list status directory {status_dir}: {exc}") from exc

    row_prefix = f"{label}-s"
    row_files = {path.name for path in files if path.name.startswith(row_prefix)}
    if row_files != expected:
        missing = sorted(expected - row_files)
        unexpected = sorted(row_files - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        raise StatusError(
            f"row {label!r} must have exactly {shards} shard files ({'; '.join(details)})"
        )

    total = 0
    full_values: list[tuple[str, int]] = []
    for number in range(1, shards + 1):
        path = status_dir / f"{label}-s{number}.txt"
        values = _parse_status(path)
        try:
            collected = int(values["collected"])
        except (KeyError, ValueError) as exc:
            raise StatusError(f"{path.name} has no valid collected count") from exc
        total += collected
        if "full" in values:
            try:
                full = int(values["full"])
            except ValueError as exc:
                raise StatusError(f"{path.name} has no valid full count") from exc
            full_values.append((path.name, full))

    if len(full_values) != 1:
        raise StatusError(f"row {label!r} must record full exactly once (found {len(full_values)})")
    full_name, full = full_values[0]
    if full <= 0:
        raise StatusError(f"{full_name} must record a positive full count")
    if total != full:
        raise StatusError(f"row {label!r} collected {total} tests, but full collection is {full}")
    return total, full


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="validate one E2E shard status row")
    parser.add_argument("--status-dir", type=Path, required=True)
    parser.add_argument("--jobs-file", type=Path)
    parser.add_argument("--label", type=_label, required=True)
    parser.add_argument("--shards", type=_positive_int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.jobs_file is not None:
            _parse_jobs(args.jobs_file, args.label, args.shards)
        total, full = check_row(args.status_dir, args.label, args.shards)
    except StatusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"ok: row {args.label} covered {total} tests (full collection {full})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
