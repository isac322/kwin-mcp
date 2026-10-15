#!/usr/bin/env python3
"""Regenerate the per-file test durations used for E2E sharding.

Usage: python3 scripts/update_e2e_durations.py OUT.json JUNIT.xml [JUNIT.xml ...]

Each JUnit file is summed per test file (seconds over its test cases); the
output keeps, for every file, the maximum across the inputs so the slowest
matrix row decides. A test case's file comes from its classname with the
capitalized (class) segments dropped: `tests.e2e.test_x.TestY` ->
`tests/e2e/test_x.py`. Keys are sorted and values rounded to one decimal.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree


def classname_to_path(classname: str) -> str | None:
    """Map a pytest JUnit classname to the repository-relative test file path."""
    segments = [segment for segment in classname.split(".") if segment]
    while segments and segments[-1][:1].isupper():
        segments.pop()
    if not segments:
        return None
    return "/".join(segments) + ".py"


def file_durations(junit: Path) -> dict[str, float]:
    """Return the summed test case time per test file in one JUnit report."""
    totals: dict[str, float] = defaultdict(float)
    for case in ElementTree.parse(junit).getroot().iter("testcase"):
        path = classname_to_path(case.get("classname", ""))
        if path is None:
            continue
        totals[path] += float(case.get("time", "0") or 0)
    return dict(totals)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: python3 scripts/update_e2e_durations.py OUT.json JUNIT.xml [JUNIT.xml ...]",
            file=sys.stderr,
        )
        return 2
    output = Path(argv[0])
    merged: dict[str, float] = {}
    for arg in argv[1:]:
        for path, seconds in file_durations(Path(arg)).items():
            merged[path] = max(merged.get(path, 0.0), seconds)
    if not merged:
        print("error: the JUnit inputs contain no test cases", file=sys.stderr)
        return 1
    result = {path: round(merged[path], 1) for path in sorted(merged)}
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(result)} files to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
