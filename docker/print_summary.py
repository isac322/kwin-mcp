#!/usr/bin/env python3
"""Print a CI-friendly smoke summary from EVIDENCE_DIR/summary.json."""

import json
import os
import pathlib
import re
import sys
from typing import Any, cast

MAX_REASON_LEN = 500
SCREENSHOT_KEY_TO_FILENAME = {
    "initial": "initial.png",
    "post_click": "post-click.png",
    "post_typing": "post-typing.png",
}
TRUNCATION_SUFFIX = "... [truncated, see summary.json]"


def _sanitize(value: object) -> str:
    text = str(value)
    text = re.sub(r"[\n\r]+", " ", text)
    text = "".join(char for char in text if char == " " or ord(char) >= 0x20).strip()
    if len(text) > MAX_REASON_LEN:
        text = f"{text[:MAX_REASON_LEN]}{TRUNCATION_SUFFIX}"
    return text


def _load_summary(path: pathlib.Path) -> tuple[dict[str, object] | None, str | None]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None, "summary.json missing"
    except (json.JSONDecodeError, OSError):
        return None, "summary.json unreadable"
    if not isinstance(data, dict):
        return None, "summary.json unreadable"
    return data, None


def _screenshots(summary: dict[str, object]) -> str:
    raw = summary.get("screenshot_sha")
    if not isinstance(raw, dict):
        return ""
    screenshot_sha = cast("dict[str, Any]", raw)
    filenames = [
        filename for key, filename in SCREENSHOT_KEY_TO_FILENAME.items() if screenshot_sha.get(key)
    ]
    return ", ".join(filenames)


def _tasks_line(summary: dict[str, object]) -> list[str]:
    tasks_passed = summary.get("tasks_passed")
    return [] if tasks_passed is None else [f"==> Tasks passed: {tasks_passed}"]


def _reason(summary: dict[str, object], fallback: str) -> str:
    value = summary.get("error") or summary.get("reason") or fallback
    return _sanitize(value)


def _error_type_line(summary: dict[str, object]) -> list[str]:
    error_type = summary.get("error_type")
    return [] if not error_type else [f"==> Error type: {_sanitize(error_type)}"]


def _render_pass(summary: dict[str, object], evidence_dir: pathlib.Path) -> list[str]:
    lines = ["==> Smoke summary: PASS", f"==> Evidence: {evidence_dir}", *_tasks_line(summary)]
    screenshots = _screenshots(summary)
    if screenshots:
        lines.append(f"==> Screenshots: {screenshots}")
    return lines


def _render_fail(summary: dict[str, object], evidence_dir: pathlib.Path) -> list[str]:
    lines = ["==> Smoke summary: FAIL"]
    lines.extend(_error_type_line(summary))
    lines.extend(
        [
            f"==> Reason: {_reason(summary, 'smoke failed')}",
            f"==> Evidence: {evidence_dir}",
            *_tasks_line(summary),
        ]
    )
    screenshots = _screenshots(summary)
    if screenshots:
        lines.append(f"==> Screenshots: {screenshots}")
    lines.append("==> See: summary.json, stdout.log, stderr.log")
    return lines


def main() -> None:
    evidence_dir = pathlib.Path(os.environ.get("EVIDENCE_DIR", "/evidence"))
    summary, load_reason = _load_summary(evidence_dir / "summary.json")
    if summary is None:
        reason = load_reason or "summary.json unreadable"
        lines = ["==> Smoke summary: ERROR"]
    elif summary.get("verdict") == "pass":
        lines = _render_pass(summary, evidence_dir)
    elif summary.get("verdict") == "fail":
        lines = _render_fail(summary, evidence_dir)
    else:
        reason = _reason(summary, "summary.json unreadable")
        lines = ["==> Smoke summary: ERROR", *_error_type_line(summary)]
    if summary is None or summary.get("verdict") not in {"pass", "fail"}:
        lines.extend(
            [
                f"==> Reason: {_sanitize(reason)}",
                f"==> Evidence: {evidence_dir}",
                "==> See: stdout.log, stderr.log",
            ]
        )
    for line in lines:
        print(line, flush=True)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
