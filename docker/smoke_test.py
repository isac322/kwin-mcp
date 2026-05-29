#!/usr/bin/env python3
"""In-process smoke test for kwin-mcp inside the container.

Imports AutomationEngine directly. Exercises session start, qml6 app launch,
accessibility discovery, mouse and keyboard input injection, accessibility-
based state-change verification, and screenshot capture (best-effort).

Verification model:
  * a11y tree substring assertions are the primary checks. They prove that
    input injection actually reached the app and changed observable state.
  * Screenshots are auxiliary evidence: captured when the host exposes a DRM
    render node (KWin ScreenShot2 needs one), skipped otherwise. Their SHA
    distinctness is asserted only when all three frames captured successfully.

Exit codes: 0=pass, 1=assertion failed, 10=uncaught exception.
"""

import contextlib
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import sys
import time
from typing import Any

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if SRC_DIR.exists():
    sys.path.insert(0, str(SRC_DIR))

from kwin_mcp.core import AutomationEngine  # noqa: E402

EVIDENCE = pathlib.Path(os.environ.get("EVIDENCE_DIR", ".sisyphus/evidence"))
PAUSE_AT = os.environ.get("SMOKE_PAUSE_AT", "")
PAUSE_STEPS = (
    "launch_app",
    "screenshot_initial",
    "mouse_click_ping",
    "keyboard_type",
    "screenshot_post_typing",
)
if PAUSE_AT and PAUSE_AT not in PAUSE_STEPS:
    valid_steps = ", ".join(PAUSE_STEPS)
    print(f"Invalid SMOKE_PAUSE_AT={PAUSE_AT!r}; valid values: {valid_steps}", file=sys.stderr)
    sys.exit(2)


def sha256(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


FIND_RE = re.compile(
    r'^- \[(?P<role>[^\]]+)\] "(?P<name>[^"]+)" @ '
    r"\((?P<x>\d+), (?P<y>\d+), (?P<w>\d+)x(?P<h>\d+)\)",
    re.MULTILINE,
)


def find_center(find_output: str, name: str) -> tuple[int, int]:
    for match in FIND_RE.finditer(find_output):
        if match.group("name") == name:
            x, y, w, h = (int(match.group(key)) for key in ("x", "y", "w", "h"))
            return x + w // 2, y + h // 2
    raise AssertionError(
        f"element not found by name={name!r}\n--- find_ui_elements output ---\n{find_output}"
    )


SCREENSHOT_RE = re.compile(r"Screenshot saved: (?P<path>\S+\.png)")


def parse_screenshot_path(out: str) -> pathlib.Path:
    match = SCREENSHOT_RE.search(out)
    assert match, f"could not parse screenshot path from: {out!r}"
    return pathlib.Path(match.group("path"))


def copy_to_evidence(src: pathlib.Path, dst_name: str) -> pathlib.Path:
    dst = EVIDENCE / "screenshots" / dst_name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def write_a11y(name: str, content: str) -> None:
    dst = EVIDENCE / "a11y" / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(content)


def add_scenario(summary: dict[str, Any], name: str, result: str, **extra: Any) -> None:
    summary["scenarios"].append({"name": name, "result": result, **extra})


def _pause_after(step_name: str) -> None:
    if step_name != PAUSE_AT:
        return
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    pause_marker = EVIDENCE / f".paused-at-{step_name}"
    continue_marker = EVIDENCE / ".continue"
    pause_marker.write_text(step_name)
    print(
        f"[smoke] paused at {step_name} - touch {continue_marker} to resume",
        flush=True,
    )
    while not continue_marker.exists():
        time.sleep(0.5)
    pause_marker.unlink(missing_ok=True)
    continue_marker.unlink(missing_ok=True)
    print(f"[smoke] resumed from {step_name}", flush=True)


def best_effort_screenshot(
    engine: AutomationEngine,
    summary: dict[str, Any],
    label: str,
    dst_name: str,
) -> tuple[pathlib.Path | None, str | None]:
    """Capture a screenshot, tolerating render-node-less environments.

    Returns (path, sha256) on success, (None, None) when KWin's ScreenShot2
    pipeline cancels (no /dev/dri/renderD*; standard CI failure mode). The
    a11y assertions remain as the primary verification path.

    TODO(screenshot-coverage): GitHub-hosted Azure runners cannot expose a
    DRM render node, so the entire kwin-mcp screenshot stack
    (kwin_mcp.screenshot.capture_screenshot_dbus -> KWin ScreenShot2 D-Bus ->
    Mesa/EGL/GBM) is **silently skipped** in CI. Regressions in that stack
    will not turn the smoke job red; only local runs with `--device
    /dev/dri/renderD*` exercise it. To close the gap, either:
      (a) add a software render-only fallback to src/kwin_mcp/screenshot.py
          (e.g. trigger QML grabToImage via a smoke-only D-Bus channel), or
      (b) add a self-hosted runner job that mounts a real render node.
    Tracking issue / follow-up PR should reference this comment.
    """
    try:
        out = engine.screenshot()
    except Exception as exc:
        add_scenario(summary, f"screenshot_{label}", "skipped", reason=repr(exc))
        return None, None
    src = parse_screenshot_path(out)
    dst = copy_to_evidence(src, dst_name)
    digest = sha256(dst)
    add_scenario(summary, f"screenshot_{label}", f"size={dst.stat().st_size}", sha256=digest)
    return dst, digest


def assert_status_contains(
    engine: AutomationEngine,
    expected_substring: str,
    *,
    timeout_s: float = 3.0,
) -> str:
    deadline = time.monotonic() + timeout_s
    last_tree = ""
    while time.monotonic() < deadline:
        last_tree = engine.accessibility_tree(max_depth=10)
        if expected_substring in last_tree:
            return last_tree
        time.sleep(0.2)
    raise AssertionError(
        f"a11y tree did not contain {expected_substring!r} within {timeout_s}s\n"
        f"--- last tree ---\n{last_tree[:2000]}"
    )


def run_smoke(engine: AutomationEngine, summary: dict[str, Any]) -> None:
    result = engine.session_start(screen_width=1920, screen_height=1080)
    add_scenario(summary, "session_start", str(result)[:200])

    result = engine.launch_app("qml6 /opt/docker/smoke_app.qml")
    add_scenario(summary, "launch_app", str(result)[:200])
    _pause_after("launch_app")

    engine.wait_for_element(query="Ping button", timeout_ms=20000)
    add_scenario(summary, "wait_ping_button", "ok")
    engine.wait_for_element(query="Smoke entry", timeout_ms=5000)
    add_scenario(summary, "wait_smoke_entry", "ok")
    engine.wait_for_element(query="Status text", timeout_ms=5000)
    add_scenario(summary, "wait_status_text", "ok")

    tree_before = engine.accessibility_tree(max_depth=10)
    write_a11y("before.txt", tree_before)
    assert "ready" in tree_before, (
        f"initial Status text 'ready' not visible in a11y tree\n--- tree ---\n{tree_before[:2000]}"
    )

    find_before = engine.find_ui_elements(query="Ping button")
    bx, by = find_center(find_before, "Ping button")
    add_scenario(summary, "find_ping_button", f"center=({bx},{by})")

    find_entry = engine.find_ui_elements(query="Smoke entry")
    ex, ey = find_center(find_entry, "Smoke entry")
    add_scenario(summary, "find_smoke_entry", f"center=({ex},{ey})")

    # The QML ApplicationWindow is FullScreen at the virtual screen's resolution,
    # so its origin is (0, 0) and AT-SPI window-relative coordinates are already
    # absolute. No screenshot-derived offset translation is required.
    _initial, initial_sha = best_effort_screenshot(engine, summary, "initial", "initial.png")
    _pause_after("screenshot_initial")

    engine.mouse_move(x=960, y=540)
    time.sleep(0.3)
    engine.mouse_move(x=bx, y=by)
    time.sleep(0.3)
    engine.mouse_click(x=bx, y=by)
    add_scenario(summary, "mouse_click_ping", f"mouse at ({bx},{by})")
    _pause_after("mouse_click_ping")

    tree_after_click = assert_status_contains(engine, "clicked")
    write_a11y("after_click.txt", tree_after_click)
    add_scenario(summary, "verify_status_clicked", "ok")

    _post_click, post_click_sha = best_effort_screenshot(
        engine, summary, "post_click", "post-click.png"
    )

    engine.mouse_click(x=ex, y=ey)
    add_scenario(summary, "focus_entry_field", f"mouse at ({ex},{ey})")
    time.sleep(0.5)

    engine.keyboard_type("hello")
    add_scenario(summary, "keyboard_type", "typed text")
    _pause_after("keyboard_type")
    time.sleep(0.5)

    # Re-click Ping so the QML onClicked handler copies entry.text into status.
    # This propagates the typed value into the a11y tree as Status text content,
    # giving us a render-independent assertion that keyboard input reached the
    # app AND the app's state machine processed it correctly.
    engine.mouse_click(x=bx, y=by)
    add_scenario(summary, "mouse_click_ping_after_type", f"mouse at ({bx},{by})")
    time.sleep(0.5)

    tree_after_typing = assert_status_contains(engine, "hello")
    write_a11y("after.txt", tree_after_typing)
    add_scenario(summary, "verify_status_typed_value", "ok")

    _post_typing, post_typing_sha = best_effort_screenshot(
        engine, summary, "post_typing", "post-typing.png"
    )
    _pause_after("screenshot_post_typing")

    assert tree_after_typing != tree_before, "accessibility tree text did not change"

    captured_shas = [s for s in (initial_sha, post_click_sha, post_typing_sha) if s is not None]
    summary["screenshot_sha"] = {
        "initial": initial_sha,
        "post_click": post_click_sha,
        "post_typing": post_typing_sha,
    }
    summary["screenshots_captured"] = len(captured_shas)

    if len(captured_shas) == 3:
        assert len(set(captured_shas)) == len(captured_shas), (
            f"screenshots not all distinct: shas={[s[:8] for s in captured_shas]}"
        )


def merge_install_metadata(summary: dict[str, Any]) -> None:
    install_path = EVIDENCE / "install.json"
    if install_path.exists():
        try:
            summary["install"] = json.loads(install_path.read_text())
        except Exception as exc:
            summary["install"] = {"error": f"could not parse install.json: {exc!r}"}
    else:
        summary["install"] = {"error": "install.json missing; entrypoint did not write it"}


def main() -> None:
    summary: dict[str, Any] = {
        "verdict": "error",
        "started_at": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
        "scenarios": [],
    }
    engine = AutomationEngine()
    try:
        run_smoke(engine, summary)
        summary["verdict"] = "pass"
    except AssertionError as exc:
        summary["verdict"] = "fail"
        summary["error"] = str(exc)
        summary["error_type"] = "assertion"
        sys.exit(1)
    except Exception as exc:
        summary["verdict"] = "error"
        summary["error"] = repr(exc)
        summary["error_type"] = type(exc).__name__
        sys.exit(10)
    finally:
        with contextlib.suppress(Exception):
            engine.session_stop()
        merge_install_metadata(summary)
        summary["tasks_passed"] = sum(
            1 for item in summary.get("scenarios", []) if "error" not in item
        )
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        (EVIDENCE / "summary.json").write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
