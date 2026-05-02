"""Sync the kwin-mcp version across plugin manifests.

pyproject.toml [project].version is the source of truth. Run without args to
update marketplace.json / plugin.json / package.json. Run with --check to fail
when any manifest is out of sync (used by CI).
"""

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"

FieldPath = tuple[str | int, ...]

TARGETS: list[tuple[Path, list[FieldPath]]] = [
    (
        REPO / ".claude-plugin" / "marketplace.json",
        [("metadata", "version"), ("plugins", 0, "version")],
    ),
    (
        REPO / "integrations" / "claude-code" / ".claude-plugin" / "plugin.json",
        [("version",)],
    ),
    (
        REPO / "integrations" / "opencode" / "plugin" / "package.json",
        [("version",)],
    ),
]


def _load_version() -> str:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["project"]["version"]


def _get_in(obj: Any, path: FieldPath) -> Any:
    for key in path:
        obj = obj[key]
    return obj


def _set_in(obj: Any, path: FieldPath, value: Any) -> None:
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value


def _sync(*, check: bool) -> int:
    version = _load_version()
    drift: list[str] = []

    for path, fields in TARGETS:
        data = json.loads(path.read_text(encoding="utf-8"))
        changed = False
        for field in fields:
            current = _get_in(data, field)
            if current == version:
                continue
            if check:
                rel = path.relative_to(REPO)
                dotted = ".".join(str(k) for k in field)
                drift.append(f"{rel}:{dotted} == {current!r} (expected {version!r})")
            else:
                _set_in(data, field, version)
                changed = True
        if changed:
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            print(f"updated {path.relative_to(REPO)} -> {version}")

    if drift:
        for line in drift:
            print(line, file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any manifest is out of sync (no writes).",
    )
    args = parser.parse_args()
    return _sync(check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
