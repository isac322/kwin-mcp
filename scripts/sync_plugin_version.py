"""Sync the kwin-mcp plugin manifests and bundled SKILL.md from canonical sources.

Source of truth:
- pyproject.toml [project].version → marketplace.json + plugin.json + package.json (3 manifests)
- integrations/claude-code/skills/kwin-desktop-automation/SKILL.md
  → integrations/opencode/plugin/skill/.../SKILL.md (byte-identical mirror)

Run without args to write the synced state (idempotent). Run with --check to
fail when any manifest version or bundled SKILL.md is out of sync (used by CI).
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

SKILL_SOURCE = (
    REPO / "integrations" / "claude-code" / "skills" / "kwin-desktop-automation" / "SKILL.md"
)
SKILL_MIRRORS: list[Path] = [
    REPO
    / "integrations"
    / "opencode"
    / "plugin"
    / "skill"
    / "kwin-desktop-automation"
    / "SKILL.md",
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


def _sync_versions(*, check: bool) -> list[str]:
    version = _load_version()
    drift: list[str] = []

    for path, fields in TARGETS:
        if not path.exists():
            drift.append(f"{path.relative_to(REPO)}: file not found")
            continue
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

    return drift


def _sync_skill(*, check: bool) -> list[str]:
    drift: list[str] = []
    if not SKILL_SOURCE.exists():
        drift.append(f"{SKILL_SOURCE.relative_to(REPO)}: source not found")
        return drift
    source_bytes = SKILL_SOURCE.read_bytes()
    rel_src = SKILL_SOURCE.relative_to(REPO)
    for dest in SKILL_MIRRORS:
        rel_dest = dest.relative_to(REPO)
        if not dest.exists():
            if check:
                drift.append(f"{rel_dest}: missing (must mirror {rel_src})")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(source_bytes)
            print(f"created {rel_dest} (mirror of {rel_src})")
            continue
        if dest.read_bytes() != source_bytes:
            if check:
                drift.append(f"{rel_dest}: drift (must be byte-identical to {rel_src})")
            else:
                dest.write_bytes(source_bytes)
                print(f"updated {rel_dest} (mirror of {rel_src})")
    return drift


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any manifest version or SKILL.md is out of sync (no writes).",
    )
    args = parser.parse_args()

    drift = _sync_versions(check=args.check) + _sync_skill(check=args.check)
    if drift:
        for line in drift:
            print(line, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
