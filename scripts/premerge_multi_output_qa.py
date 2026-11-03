#!/usr/bin/env python3
"""Native multi-output proof through the installed public CLI.

This script deliberately keeps KWin geometry independent: ``KWinOracle`` reads
workspace.screens through a test-owned KWin script, while every session and
input operation under test goes through ``kwin-mcp-cli``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from PIL import Image

sys.path.insert(0, "/probes")
sys.path.insert(0, "/app/tests/e2e")
from kwin_oracle import KWinOracle
from premerge_capture_gaps_probe import (
    InstalledCli,
    _command,
    _installed_provenance,
    _subject_commit,
    _version,
)

ARTIFACT_DIR = Path(os.environ.get("KWIN_MCP_ARTIFACT_DIR", "/artifacts"))
PYTHON = "/opt/kwin-mcp-venv/bin/python"
EXPECTED_BLUE = (0x28, 0x5A, 0xA6)


def _parse_session(rendered: str) -> tuple[str, tuple[int, int]]:
    match = re.search(r"^D-Bus: (.+)$", rendered, re.MULTILINE)
    if match is None:
        raise AssertionError(f"session_start did not expose D-Bus address: {rendered}")
    screen = re.search(r"^Screen: (\d+)x(\d+)$", rendered, re.MULTILINE)
    if screen is None:
        raise AssertionError(f"session_start did not expose screen size: {rendered}")
    return match.group(1), (int(screen.group(1)), int(screen.group(2)))


def _screens(oracle: KWinOracle) -> list[dict[str, float | str]]:
    raw = oracle.run(
        "var out = [];\n"
        "var screens = workspace.screens;\n"
        "for (var i = 0; i < screens.length; i++) {\n"
        "  var g = screens[i].geometry;\n"
        "  out.push({name: String(screens[i].name), x: g.x, y: g.y, "
        "width: g.width, height: g.height, scale: screens[i].devicePixelRatio});\n"
        "}\n"
        "report(out);"
    )
    if not isinstance(raw, list) or len(raw) < 1:
        raise AssertionError(f"independent KWin output oracle returned {raw!r}")
    screens: list[dict[str, float | str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise AssertionError(f"malformed KWin output: {item!r}")
        required = ("name", "x", "y", "width", "height", "scale")
        if any(key not in item for key in required):
            raise AssertionError(f"incomplete KWin output: {item!r}")
        if float(item["scale"]) != 1.0:
            raise AssertionError(f"native proof requires scale 1: {item!r}")
        screens.append(item)
    return screens


def _union(screens: list[dict[str, float | str]]) -> tuple[int, int, int, int]:
    left = min(float(screen["x"]) for screen in screens)
    top = min(float(screen["y"]) for screen in screens)
    right = max(float(screen["x"]) + float(screen["width"]) for screen in screens)
    bottom = max(float(screen["y"]) + float(screen["height"]) for screen in screens)
    values = tuple(round(value) for value in (left, top, right - left, bottom - top))
    if any(value <= 0 for value in values[2:]):
        raise AssertionError(f"invalid independent KWin union: {values}")
    return values  # type: ignore[return-value]


def _window(rendered: str, caption: str) -> tuple[str, tuple[int, int, int, int]]:
    blocks = rendered.split("\n- ")
    for block in blocks:
        if f'"{caption}"' not in block:
            continue
        identifier = re.search(r"\n\s+id:\s+(\S+)", block)
        frame = re.search(r"\n\s+frame:\s+\((-?\d+),\s*(-?\d+),\s*(\d+)x(\d+)\)", block)
        if identifier and frame:
            return identifier.group(1), tuple(int(value) for value in frame.groups())  # type: ignore[return-value]
    raise AssertionError(f"window {caption!r} absent from CLI output: {rendered}")


def _element_rect(rendered: str, name: str) -> tuple[int, int, int, int]:
    match = re.search(
        rf'- \[[^]]+\] "{re.escape(name)}"[^@]*@ screen '
        r"\((-?\d+),\s*(-?\d+),\s*(\d+)x(\d+)\)",
        rendered,
    )
    if match is None:
        raise AssertionError(f"element {name!r} has no mapped rect: {rendered}")
    return tuple(int(value) for value in match.groups())  # type: ignore[return-value]


def _capture(
    cli: InstalledCli, output_dir: Path, union: tuple[int, int, int, int]
) -> dict[str, Any]:
    rendered = cli.run(_command("screen_capture", max_edge=0), timeout_s=180)
    match = re.search(r"^Screenshot saved: (.+?) \([0-9.]+ KB\)$", rendered, re.MULTILINE)
    coordinate_lines = [
        line for line in rendered.splitlines() if line.startswith("Coordinate space:")
    ]
    if match is None or len(coordinate_lines) != 1:
        raise AssertionError(f"single native mapping was not reported: {rendered}")
    coordinate_line = coordinate_lines[0]
    if "backend screenshot2" not in coordinate_line or "coverage full" not in coordinate_line:
        raise AssertionError(f"full native ScreenShot2 capture was not reported: {rendered}")
    source = Path(match.group(1))
    if not source.is_file():
        raise AssertionError(f"CLI capture path is not a file: {source}")
    destination = output_dir / "full.png"
    shutil.copy2(source, destination)
    with Image.open(destination) as image:
        image.load()
        rgba = image.convert("RGBA")
        actual_size = image.size
        alpha_content = 0
        nonblack = 0
        for pixel in rgba.getdata():
            if pixel[3] == 255 and max(pixel[:3]) > 0:
                alpha_content += 1
            if pixel[3] > 0 and max(pixel[:3]) > 0:
                nonblack += 1
    expected_size = (union[2], union[3])
    if (
        f"origin ({union[0]}, {union[1]})" not in coordinate_line
        or "topology observed stable before/after capture" not in coordinate_line
    ):
        raise AssertionError(
            "CLI mapping origin/stability does not match independent KWin union "
            f"{union}: {rendered}"
        )
    if actual_size != expected_size:
        raise AssertionError(f"PNG size {actual_size} does not match KWin union {expected_size}")
    if alpha_content < 1000 or nonblack < 1000:
        raise AssertionError(
            f"capture lacks rendered GUI content: opaque={alpha_content}, nonblack={nonblack}"
        )
    return {
        "path": str(destination),
        "source": str(source),
        "rendered": rendered,
        "png_size": actual_size,
        "expected_union_size": expected_size,
        "opaque_nonblack_pixels": alpha_content,
        "nonblack_pixels": nonblack,
    }


def _assert_control_pixels(
    path: Path, rect: tuple[int, int, int, int], union: tuple[int, int, int, int]
) -> dict[str, Any]:
    ux, uy, _, _ = union
    x, y, width, height = rect
    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        crop = rgba.crop((x - ux, y - uy, x - ux + width, y - uy + height))
        blue = 0
        opaque = 0
        for red, green, blue_value, alpha in crop.getdata():
            if alpha == 255 and max(red, green, blue_value) > 0:
                opaque += 1
            if alpha == 255 and all(
                abs(actual - expected) <= 12
                for actual, expected in zip((red, green, blue_value), EXPECTED_BLUE, strict=True)
            ):
                blue += 1
    if opaque == 0 or blue == 0:
        raise AssertionError(
            "output-2 control crop lacks rendered GUI content/blue control: "
            f"opaque={opaque}, blue={blue}"
        )
    return {"rect": rect, "opaque_nonblack_pixels": opaque, "expected_blue_pixels": blue}


def _assert_input(
    event_path: Path,
    oracle: KWinOracle,
    click: tuple[int, int],
    client_origin: tuple[int, int],
) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    events: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        events = [json.loads(line) for line in event_path.read_text().splitlines() if line]
        kinds = [event.get("kind") for event in events]
        if "button_press" in kinds and "button_release" in kinds:
            break
        time.sleep(0.1)
    selected = [
        event for event in events if event.get("kind") in {"button_press", "button_release"}
    ]
    if [event.get("kind") for event in selected[-2:]] != ["button_press", "button_release"]:
        raise AssertionError(f"GUI probe did not receive click: {events!r}")
    local_expected = (click[0] - client_origin[0], click[1] - client_origin[1])
    observed = [(int(event["x"]), int(event["y"])) for event in selected[-2:]]
    if any(point != local_expected for point in observed):
        raise AssertionError(f"GUI probe local click coordinates {observed} != {local_expected}")
    cursor = oracle.cursor_pos()
    if abs(cursor[0] - click[0]) > 1 or abs(cursor[1] - click[1]) > 1:
        raise AssertionError(f"independent KWin cursor {cursor} != public CLI click {click}")
    return {"events": selected[-2:], "local_expected": local_expected, "cursor": cursor}


def _run_count(count: int, root: Path) -> dict[str, Any]:
    output_dir = root / f"output-count-{count}"
    output_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "KWIN_MCP_ARTIFACT_DIR": str(output_dir),
            "KWIN_MCP_X11_SCREENSHOT": "0",
            "KWIN_MCP_E2E_SCREENSHOT": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    env.pop("DISPLAY", None)
    env.pop("PYTHONPATH", None)
    cli = InstalledCli(env=env, stderr_path=output_dir / "kwin-mcp-cli.stderr.log")
    started = False
    try:
        session = cli.run(
            _command(
                "session_start",
                screen_width=1280,
                screen_height=800,
                output_count=count,
                retention={"artifacts": True},
            ),
            timeout_s=180,
        )
        started = True
        dbus_address, info_size = _parse_session(session)
        oracle = KWinOracle(dbus_address)
        screens = _screens(oracle)
        if len(screens) != count:
            raise AssertionError(
                f"independent KWin reports {len(screens)} outputs for count={count}: {screens}"
            )
        union = _union(screens)
        if info_size != (union[2], union[3]):
            raise AssertionError(
                f"public session size {info_size} != independent KWin union {union}"
            )
        cli.run(
            _command(
                "app_launch",
                command="python3 /app/tests/e2e/interaction_probe.py",
                env={
                    "GDK_BACKEND": "wayland",
                    "GTK_MODULES": "gail:atk-bridge",
                    "NO_AT_BRIDGE": "0",
                    "XDG_SESSION_TYPE": "wayland",
                    "KWIN_MCP_PROBE_EVENT_LOG": str(output_dir / "interaction-events.jsonl"),
                },
            )
        )
        deadline = time.monotonic() + 20
        while True:
            try:
                window_id, initial_frame = _window(
                    cli.run(_command("window_list")), "Interaction Probe"
                )
                break
            except AssertionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        target_screen = (
            screens[0] if count == 1 else max(screens, key=lambda screen: float(screen["x"]))
        )
        if count > 1 and float(target_screen["x"]) == min(float(screen["x"]) for screen in screens):
            raise AssertionError(f"KWin did not expose a distinct non-left output: {screens}")
        target_frame = {
            "x": round(float(target_screen["x"])) + 20,
            "y": round(float(target_screen["y"])) + 20,
            "width": 920,
            "height": 700,
        }
        placed = cli.run(_command("window_place", window={"id": window_id}, frame=target_frame))
        observed_id, observed_frame = _window(
            cli.run(_command("window_list", filter={"id": window_id})), "Interaction Probe"
        )
        if observed_id != window_id or observed_frame[:2] != (target_frame["x"], target_frame["y"]):
            raise AssertionError(
                f"window_place was not independently observed: {placed}; {observed_frame}"
            )
        observed_window = oracle.window(window_id)
        if observed_window is None:
            raise AssertionError(f"KWin oracle cannot observe placed window {window_id}")
        client_origin = (round(observed_window.client.x), round(observed_window.client.y))
        element = cli.run(
            _command(
                "element_query",
                locator={
                    "app": "interaction_probe.py",
                    "query": "Click Target",
                    "role": "push button",
                },
                max_depth=15,
            )
        )
        click_rect = _element_rect(element, "Click Target")
        click = (click_rect[0] + click_rect[2] // 2, click_rect[1] + click_rect[3] // 2)
        if not any(
            float(screen["x"]) <= click[0] < float(screen["x"]) + float(screen["width"])
            and float(screen["y"]) <= click[1] < float(screen["y"]) + float(screen["height"])
            for screen in screens
            if float(screen["x"]) == float(target_screen["x"])
        ):
            raise AssertionError(
                f"Click Target is not on independently selected output: {click}, {target_screen}"
            )
        before_capture = _capture(cli, output_dir, union)
        control = _assert_control_pixels(Path(before_capture["path"]), click_rect, union)
        input_proof: dict[str, Any] | None = None
        if count == 2:
            cli.run(_command("pointer_click", target={"x": click[0], "y": click[1]}))
            input_proof = _assert_input(
                output_dir / "interaction-events.jsonl", oracle, click, client_origin
            )
        stop = cli.run("session_stop")
        return {
            "count": count,
            "session": session,
            "screens": screens,
            "union": union,
            "initial_frame": initial_frame,
            "window_frame": observed_frame,
            "click_rect": click_rect,
            "capture": before_capture,
            "control": control,
            "input": input_proof,
            "session_stop": stop,
        }
    finally:
        if started:
            with contextlib.suppress(Exception):
                cli.run("session_stop", timeout_s=30)
        cli.close()


def main() -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {"status": "failed"}
    try:
        record["subject_commit"] = _subject_commit()
        record["kwin_version"] = _version()
        record["installed_provenance"] = _installed_provenance(os.environ.copy(), ARTIFACT_DIR)
        record["cli_cases"] = []
        for count in (1, 2, 3):
            result = _run_count(count, ARTIFACT_DIR)
            record["cli_cases"].append(result)
            (ARTIFACT_DIR / "outputs-proof.json").write_text(
                json.dumps(record, indent=2, default=str) + "\n"
            )
        test_env = os.environ.copy()
        test_env.update(
            {"KWIN_MCP_E2E_SCREENSHOT": "1", "KWIN_MCP_ARTIFACT_DIR": str(ARTIFACT_DIR / "pytest")}
        )
        (ARTIFACT_DIR / "pytest").mkdir(parents=True, exist_ok=True)
        test_env.pop("DISPLAY", None)
        test_env.pop("PYTHONPATH", None)
        pytest_log = ARTIFACT_DIR / "multi-output-pytest.log"
        with pytest_log.open("w", encoding="utf-8") as stream:
            result = subprocess.run(
                [
                    PYTHON,
                    "-m",
                    "pytest",
                    "tests/e2e/test_session_start.py::test_multiple_virtual_outputs_report_workspace_union",
                    "-v",
                    "--junitxml=/artifacts/multi-output-junit.xml",
                ],
                cwd="/app",
                env=test_env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=1200,
                check=False,
            )
        record["pytest_exit"] = result.returncode
        if result.returncode != 0:
            raise AssertionError(f"multi-output E2E test failed; see {pytest_log}")
        record["status"] = "passed"
    except BaseException:
        record["traceback"] = traceback.format_exc()
        (ARTIFACT_DIR / "traceback.log").write_text(record["traceback"])
        raise
    finally:
        (ARTIFACT_DIR / "outputs-proof.json").write_text(
            json.dumps(record, indent=2, default=str) + "\n"
        )


if __name__ == "__main__":
    main()
