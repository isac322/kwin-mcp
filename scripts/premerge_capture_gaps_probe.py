#!/usr/bin/env python3
"""Capture real ScreenShot2 evidence for the mixed-height output gap.

The probe deliberately starts KWin through ``tests/e2e/visual_harness.py`` and
uses the installed ``kwin-mcp-cli`` entry point.  It does not import the source
checkout as the server under test: ``KWIN_MCP_PROBE_WORKTREE`` is used only to
locate the existing visual fixture and its GUI probe application.

Run once on the unfixed wheel and once on the candidate wheel, for example:

    KWIN_MCP_PROBE_COMMIT=427798d8166e1e051055787b07fab531f17f5dac \
    KWIN_MCP_PROBE_WORKTREE=/worktree \
    KWIN_MCP_CAPTURE_GAPS_EVIDENCE=/artifacts/capture-gaps/baseline \
    python3 /groups/capture-gaps/probe_native_visual.py --expect opaque

    KWIN_MCP_PROBE_COMMIT=c6fd2d53da85008df7f2c4532832ae0d4760a056 \
    KWIN_MCP_PROBE_WORKTREE=/worktree \
    KWIN_MCP_CAPTURE_GAPS_EVIDENCE=/artifacts/capture-gaps/candidate \
    python3 /groups/capture-gaps/probe_native_visual.py --expect transparent

The fixture owns the D-Bus/KWin process group, Wayland socket, temporary HOME,
and all logs under the artifact directory.  Its context manager is the only
cleanup path for those resources.
The parent must verify the subject SHA at wheel build time. This is distinct
from the fixture checkout SHA: the runtime image does not need git. Installed
package paths, source SHA-256 values, and wheel RECORD agreement are retained.
Run with the installed venv interpreter and an existing writable artifact root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image


@dataclass(frozen=True)
class Topology:
    outputs: tuple[dict[str, Any], ...]
    virtual: tuple[int, int, int, int]
    raw: dict[str, Any]


@dataclass(frozen=True)
class Window:
    window_id: str
    frame: tuple[int, int, int, int]


class InstalledCli:
    """Bounded binary pipe driver; run the real installed CLI, never a project fake."""

    def __init__(self, *, env: dict[str, str], stderr_path: Path) -> None:
        executable = shutil.which("kwin-mcp-cli", path=env.get("PATH"))
        if executable is None:
            raise RuntimeError("kwin-mcp-cli is not installed on PATH")
        self._stderr_path = stderr_path
        self._stdin_path = stderr_path.with_name("kwin-mcp-cli.stdin")
        self._stdout_path = stderr_path.with_name("kwin-mcp-cli.stdout.log")
        self._history_path = stderr_path.with_name("kwin-mcp-cli.commands.jsonl")
        self._stderr_file = stderr_path.open("wb")
        self._selector = selectors.DefaultSelector()
        try:
            self._process = subprocess.Popen(
                [executable],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr_file,
                env={**env, "PYTHONUNBUFFERED": "1"},
                cwd=stderr_path.parent,
                bufsize=0,
            )
        except BaseException:
            self._selector.close()
            self._stderr_file.close()
            raise
        assert self._process.stdin is not None and self._process.stdout is not None
        os.set_blocking(self._process.stdin.fileno(), False)
        os.set_blocking(self._process.stdout.fileno(), False)
        self._selector.register(self._process.stdout, selectors.EVENT_READ)
        self._buffer = b""
        self._marker = 0

    def run(self, command: str, *, timeout_s: float = 120.0) -> str:
        process = self._process
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("kwin-mcp-cli pipes are unavailable")
        self._marker += 1
        marker = f"capture_gaps_marker_{self._marker}_{os.getpid()}"
        # cli.py:KwinMcpShell.default prints this exact line for an unknown command.
        expected = f"Unknown command: {marker}. Type 'help' for available commands."
        payload = f"{command}\n{marker}\n".encode()
        started = time.monotonic()
        deadline = started + timeout_s
        stderr_start = self._stderr_path.stat().st_size
        lines: list[str] = []
        status = "failed"
        error_text = ""
        try:
            with self._stdin_path.open("ab") as stream:
                stream.write(payload)
            # Bound writes too: a stalled previous command must not fill stdin forever.
            with selectors.DefaultSelector() as writer:
                writer.register(process.stdin, selectors.EVENT_WRITE)
                pending = memoryview(payload)
                while pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not writer.select(remaining):
                        raise TimeoutError(f"kwin-mcp-cli stdin timed out during {command!r}")
                    try:
                        pending = pending[os.write(process.stdin.fileno(), pending):]
                    except BlockingIOError:
                        continue
            while True:
                while b"\n" in self._buffer:
                    raw, self._buffer = self._buffer.split(b"\n", 1)
                    line = raw.decode("utf-8", errors="replace").rstrip("\r")
                    if line == expected:
                        with self._stderr_path.open("rb") as stream:
                            stream.seek(stderr_start)
                            stderr = stream.read().decode("utf-8", errors="replace")
                        if any(
                            line.startswith(("Error: ", "Traceback (most recent call last):"))
                            for line in stderr.splitlines()
                        ):
                            raise RuntimeError(
                                f"kwin-mcp-cli command failed: {command!r}\n{stderr[-4000:]}"
                            )
                        status = "completed"
                        return "\n".join(lines)
                    lines.append(line)
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._selector.select(remaining):
                    raise TimeoutError(f"kwin-mcp-cli timed out after {timeout_s}s: {command!r}")
                try:
                    chunk = os.read(process.stdout.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    raise RuntimeError(
                        f"kwin-mcp-cli stdout EOF before marker for {command!r}; "
                        f"status={process.poll()}, partial={self._buffer!r}; "
                        f"stderr={self._stderr_path}"
                    )
                with self._stdout_path.open("ab") as stream:
                    stream.write(chunk)
                self._buffer += chunk
        except BaseException as error:
            error_text = f"{type(error).__name__}: {error}"
            raise
        finally:
            with self._history_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "command": command,
                    "marker": expected,
                    "status": status,
                    "error": error_text,
                    "elapsed_seconds": time.monotonic() - started,
                    "stdout": "\n".join(lines),
                    "stderr_path": str(self._stderr_path),
                }) + "\n")

    def close(self) -> None:
        process = self._process
        try:
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        finally:
            if process.stdout is not None:
                process.stdout.close()
            self._selector.close()
            self._stderr_file.close()


def _command(name: str, **arguments: object) -> str:
    # cli.py:_parse_args consumes this outer JSON directly, without shell quoting.
    return f"{name} {json.dumps(arguments, separators=(',', ':'))}"


def _fixture_environment(artifact_dir: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["KWIN_MCP_ARTIFACT_DIR"] = str(artifact_dir)
    environment["KWIN_MCP_X11_SCREENSHOT"] = "0"
    environment.pop("DISPLAY", None)
    # The server and geometry helper must come from the installed wheel, never
    # from a source checkout accidentally exposed through PYTHONPATH.
    environment.pop("PYTHONPATH", None)
    return environment


def _topology(
    dbus_address: str, *, environment: dict[str, str], timeout_s: float = 20.0
) -> Topology:
    helper_env = environment.copy()
    helper_env["DBUS_SESSION_BUS_ADDRESS"] = dbus_address
    result = subprocess.run(
        [sys.executable, "-I", "-m", "kwin_mcp.geometry"],
        input=json.dumps({"op": "outputs"}),
        text=True,
        capture_output=True,
        check=False,
        env=helper_env,
        cwd=environment["KWIN_MCP_ARTIFACT_DIR"],
        timeout=timeout_s,
    )
    with (Path(environment["KWIN_MCP_ARTIFACT_DIR"]) / "topology.jsonl").open(
        "a", encoding="utf-8"
    ) as stream:
        stream.write(json.dumps({
            "monotonic": time.monotonic(),
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }) + "\n")
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"installed geometry helper failed (exit {result.returncode}): "
            f"stdout={result.stdout!r}, stderr={result.stderr[-2000:]!r}"
        ) from error
    if result.returncode != 0 or not isinstance(response, dict) or response.get("ok") is not True:
        raise RuntimeError(f"installed geometry helper returned an error: {response!r}")
    payload = response.get("result")
    if not isinstance(payload, dict) or not isinstance(payload.get("screens"), list):
        raise RuntimeError(f"installed geometry helper returned invalid outputs: {payload!r}")
    screens = tuple(payload["screens"])
    if len(screens) != 2:
        raise AssertionError(f"expected exactly two real outputs, got {screens!r}")
    names: set[str] = set()
    for screen in screens:
        if not isinstance(screen, dict) or not isinstance(screen.get("name"), str):
            raise RuntimeError(f"malformed output: {screen!r}")
        if not screen["name"] or screen["name"] in names:
            raise RuntimeError(f"output names are empty or duplicated: {screens!r}")
        names.add(screen["name"])
        _integral_rect(screen.get("geometry"), label=f"output {screen['name']}")
        if screen.get("scale") != 1:
            raise AssertionError(f"native pixel oracle requires scale 1, got {screen!r}")
    # geometry.outputs reports KWin's exact virtual rectangle; a missing value
    # is not permission to invent a union and claim it is CaptureWorkspace's canvas.
    virtual = _integral_rect(payload.get("virtual"), label="KWin virtual geometry")
    return Topology(outputs=screens, virtual=virtual, raw=response)


def _integral_rect(value: object, *, label: str) -> tuple[int, int, int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(isinstance(item, bool) or not isinstance(item, int | float) for item in value)
        or any(not float(item).is_integer() for item in value)
        or value[2] <= 0
        or value[3] <= 0
    ):
        raise RuntimeError(f"{label} must be an observed integral positive-size rect: {value!r}")
    x, y, width, height = value
    return int(x), int(y), int(width), int(height)


def _kscreen_environment(visual: Any, base: dict[str, str]) -> dict[str, str]:
    environment = base.copy()
    environment.update(
        {
            "DBUS_SESSION_BUS_ADDRESS": visual.dbus_address,
            "WAYLAND_DISPLAY": visual.wayland_display,
            "QT_QPA_PLATFORM": "wayland",
            "HOME": str(visual.artifact_dir / "home"),
            "XDG_CONFIG_HOME": str(visual.artifact_dir / "home" / ".config"),
            "XDG_DATA_HOME": str(visual.artifact_dir / "home" / ".local" / "share"),
            "XDG_STATE_HOME": str(visual.artifact_dir / "home" / ".local" / "state"),
        }
    )
    environment.pop("DISPLAY", None)
    return environment


def _rotate_one_output(visual: Any, topology: Topology, *, base_env: dict[str, str], log_path: Path) -> str:
    doctor = shutil.which("kscreen-doctor")
    if doctor is None:
        raise RuntimeError("kscreen-doctor is not installed on PATH")
    name = str(topology.outputs[0]["name"])
    command = f"output.{name}.rotation.left"
    try:
        result = subprocess.run(
            [doctor, command],
            env=_kscreen_environment(visual, base_env),
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except subprocess.TimeoutExpired as error:
        log_path.write_text(
            f"$ kscreen-doctor {command}\ntimeout after {error.timeout}s\n"
            f"stdout={error.stdout!r}\nstderr={error.stderr!r}\n",
            encoding="utf-8",
        )
        raise
    log_path.write_text(
        f"$ kscreen-doctor {command}\nexit {result.returncode}\n"
        f"{result.stdout}{result.stderr}",
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"kscreen-doctor failed: {result.stderr.strip()}")
    return command


def _wait_for_mixed_topology(
    dbus_address: str,
    before: Topology,
    *,
    environment: dict[str, str],
) -> Topology:
    deadline = time.monotonic() + 20.0
    selected = before.outputs[0]
    before_by_name = {screen["name"]: screen for screen in before.outputs}
    current = before
    while time.monotonic() < deadline:
        current = _topology(
            dbus_address,
            environment=environment,
            timeout_s=max(0.1, deadline - time.monotonic()),
        )
        current_by_name = {screen["name"]: screen for screen in current.outputs}
        heights = {screen["geometry"][3] for screen in current.outputs}
        if current_by_name.keys() != before_by_name.keys():
            raise AssertionError(f"rotation changed the actual output set: {current!r}")
        rotated = current_by_name[selected["name"]]
        if len(heights) > 1 and rotated["geometry"][2:] == selected["geometry"][2:][::-1]:
            for name, screen in before_by_name.items():
                if name != selected["name"] and current_by_name[name] != screen:
                    raise AssertionError(f"unselected output changed during rotation: {current!r}")
            return current
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
    raise RuntimeError(
        f"rotation did not swap the selected output's size and produce mixed heights; "
        f"before={before!r}; after={current!r}"
    )


def _windows(text: str, caption: str) -> list[Window]:
    blocks = text.split("\n- ")
    found: list[Window] = []
    for block in blocks:
        if f'"{caption}"' not in block:
            continue
        identifier = re.search(r"\n\s+id:\s+(\S+)", block)
        frame = re.search(r"\n\s+frame:\s+\((-?\d+),\s*(-?\d+),\s*(\d+)x(\d+)\)", block)
        if identifier is None or frame is None:
            continue
        found.append(
            Window(
                window_id=identifier.group(1),
                frame=tuple(int(value) for value in frame.groups()),
            )
        )
    return found


def _wait_for_window(cli: InstalledCli, caption: str, *, timeout_s: float = 20.0) -> list[Window]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found = _windows(
            cli.run("window_list", timeout_s=max(0.1, deadline - time.monotonic())), caption
        )
        if found:
            return found
        time.sleep(0.2)
    raise RuntimeError(f"window {caption!r} did not appear")


def _place(cli: InstalledCli, window: Window, rect: dict[str, Any], *, width_limit: int, height_limit: int) -> Window:
    x, y, width, height = (int(rect[key]) for key in ("x", "y", "width", "height"))
    frame = {
        "x": x + 20,
        "y": y + 20,
        "width": min(width_limit, max(120, width - 40)),
        "height": min(height_limit, max(120, height - 40)),
    }
    cli.run(_command("window_place", window={"id": window.window_id}, frame=frame))
    # The fixed-size GUI probe may reject a resize: report observed compositor
    # geometry, never substitute the requested rectangle as a successful result.
    observed = _windows(
        cli.run(_command("window_list", filter={"id": window.window_id})), "GUI Probe"
    )
    if len(observed) != 1 or observed[0].window_id != window.window_id:
        raise RuntimeError(f"placed GUI probe is no longer observable: {observed!r}")
    return observed[0]


def _capture(cli: InstalledCli, destination: Path) -> tuple[Path, str, str]:
    rendered = cli.run(_command("screen_capture", max_edge=0), timeout_s=180)
    match = re.search(r"^Screenshot saved: (.+?) \([0-9.]+ KB\)$", rendered, re.MULTILINE)
    if match is None:
        raise RuntimeError(f"screen_capture did not return a PNG path: {rendered}")
    source = Path(match.group(1))
    if not source.is_file():
        raise RuntimeError(f"screen_capture path does not exist: {source}")
    shutil.copy2(source, destination)
    return destination, rendered, source.as_posix()


def _gap_point(topology: Topology) -> tuple[int, int]:
    vx, vy, width, height = topology.virtual
    for y in range(vy, vy + height):
        for x in range(vx, vx + width):
            if not any(
                float(screen["geometry"][0]) <= x < float(screen["geometry"][0]) + float(screen["geometry"][2])
                and float(screen["geometry"][1]) <= y < float(screen["geometry"][1]) + float(screen["geometry"][3])
                for screen in topology.outputs
            ):
                return x, y
    raise RuntimeError(f"topology has no uncovered pixel: {topology}")


def _pixel(path: Path, logical: tuple[int, int], topology: Topology) -> tuple[int, int, int, int]:
    vx, vy, _, _ = topology.virtual
    with Image.open(path) as image:
        image.load()
        return tuple(image.convert("RGBA").getpixel((logical[0] - vx, logical[1] - vy)))


def _content_pixel(path: Path, topology: Topology) -> tuple[tuple[int, int], tuple[int, int, int, int]]:
    vx, vy, _, _ = topology.virtual
    with Image.open(path) as image:
        image.load()
        rgba = image.convert("RGBA")
        for screen in topology.outputs:
            x, y, width, height = (round(float(value)) for value in screen["geometry"])
            for py in range(max(y, vy), min(y + height, vy + rgba.height)):
                for px in range(max(x, vx), min(x + width, vx + rgba.width)):
                    value = tuple(rgba.getpixel((px - vx, py - vy)))
                    if value[3] == 255 and max(value[:3]) > 0:
                        return (px, py), value
    raise AssertionError("no non-black opaque in-output pixel was observed")


def _assert_capture(
    path: Path,
    rendered: str,
    topology: Topology,
    *,
    expected: str,
    observation: dict[str, Any],
) -> dict[str, Any]:
    with Image.open(path) as image:
        image.load()
        mode = image.mode
        size = image.size
    observation.update({
        "path": str(path), "mode": mode, "size": size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "rendered": rendered,
    })
    _assert_mapping(path, rendered, topology, image_size=size)
    expected_mode = "RGB" if expected == "opaque" else "RGBA"
    if mode != expected_mode:
        raise AssertionError(f"{expected} capture must be {expected_mode}, got {mode}")
    gap = _gap_point(topology)
    gap_pixel = _pixel(path, gap, topology)
    content_point, content_pixel = _content_pixel(path, topology)
    observation.update({
        "gap_logical": gap, "gap_pixel_rgba": gap_pixel,
        "content_logical": content_point, "content_pixel_rgba": content_pixel,
    })
    if expected == "opaque" and gap_pixel != (0, 0, 0, 255):
        raise AssertionError(f"baseline gap must be opaque black, got {gap_pixel} at {gap}")
    if expected == "transparent" and gap_pixel != (0, 0, 0, 0):
        raise AssertionError(f"candidate gap must be transparent black, got {gap_pixel} at {gap}")
    if content_pixel[3] != 255 or max(content_pixel[:3]) == 0:
        raise AssertionError(f"healthy content pixel is not opaque/non-black: {content_pixel}")
    if "coverage full" not in rendered:
        raise AssertionError(f"ScreenShot2 mapping lost full coverage: {rendered}")
    return observation


def _assert_mapping(
    path: Path, rendered: str, topology: Topology, *, image_size: tuple[int, int] | None = None
) -> None:
    if image_size is None:
        with Image.open(path) as image:
            image.load()
            image_size = image.size
    x, y, width, height = topology.virtual
    prefix = (
        f"Coordinate space: logical; origin ({x}, {y}); "
        f"size {width}x{height}; backend screenshot2"
    )
    coordinate_lines = [line for line in rendered.splitlines() if line.startswith("Coordinate space:")]
    if len(coordinate_lines) != 1:
        raise AssertionError(f"missing or ambiguous ScreenShot2 mapping: {rendered}")
    line = coordinate_lines[0]
    if (
        not line.startswith(prefix)
        or re.search(r"backend screenshot2(?: \([^)]*\))?; coverage full(?:;|$)", line) is None
        or "topology observed stable before/after capture" not in line
        or image_size != (width, height)
    ):
        raise AssertionError(
            f"native ScreenShot2 full-resolution mapping mismatch: "
            f"expected={prefix!r}, PNG={image_size!r}, actual={line!r}"
        )


def _version() -> str:
    kwin = shutil.which("kwin_wayland")
    if kwin is None:
        raise RuntimeError("kwin_wayland is absent; native visual proof cannot run")
    result = subprocess.run(
        [kwin, "--version"], capture_output=True, text=True, check=False, timeout=10,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    text = f"{result.stdout}{result.stderr}".strip()
    if result.returncode != 0 or not text:
        raise RuntimeError(f"could not identify installed KWin: {text!r}")
    return text


def _subject_commit() -> str:
    commit = os.environ.get("KWIN_MCP_PROBE_COMMIT", "")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise RuntimeError(
            "KWIN_MCP_PROBE_COMMIT must contain the full subject wheel build-input SHA "
            "verified by the parent workflow; the /app fixture checkout is not wheel provenance"
        )
    return commit


def _installed_provenance(environment: dict[str, str], evidence_dir: Path) -> dict[str, Any]:
    # Query the same interpreter as the console script with isolation enabled.
    # Do not import kwin_mcp from the fixture checkout, or invoke absent runtime git.
    code = r"""
import base64
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import sys

dist = importlib.metadata.distribution("kwin-mcp")
spec = importlib.util.find_spec("kwin_mcp")
if spec is None or spec.origin is None:
    raise RuntimeError("installed kwin_mcp has no module origin")
origin = Path(spec.origin).resolve()
direct = json.loads(dist.read_text("direct_url.json") or "{}")
if "site-packages" not in origin.parts or direct.get("dir_info", {}).get("editable"):
    raise RuntimeError(f"expected a non-editable installed wheel, got {origin}")
if origin != Path(dist.locate_file("kwin_mcp/__init__.py")).resolve():
    raise RuntimeError("import origin differs from installed distribution")
files = {}
for member in dist.files or []:
    relative = str(member)
    if not relative.startswith("kwin_mcp/") or not relative.endswith(".py"):
        continue
    path = Path(dist.locate_file(member)).resolve()
    digest = hashlib.sha256(path.read_bytes()).digest()
    encoded = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    if member.hash is None or member.hash.mode != "sha256" or member.hash.value != encoded:
        raise RuntimeError(f"installed source does not match wheel RECORD: {path}")
    files[relative] = {"path": str(path), "sha256": digest.hex(), "record_verified": True}
required = {"kwin_mcp/" + name + ".py" for name in ("cli", "core", "models", "geometry", "screenshot")}
if not required.issubset(files):
    raise RuntimeError(f"missing installed source provenance: {required - files.keys()}")
tree = {name: info["sha256"] for name, info in sorted(files.items())}
print(json.dumps({
    "python": sys.executable,
    "version": dist.version,
    "package_path": str(origin),
    "package_init_sha256": hashlib.sha256(origin.read_bytes()).hexdigest(),
    "installed_source_tree_sha256": hashlib.sha256(json.dumps(tree, sort_keys=True).encode()).hexdigest(),
    "source_files": files,
    "direct_url": direct,
}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        cwd=evidence_dir,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"installed wheel provenance query failed: stdout={result.stdout!r}; "
            f"stderr={result.stderr!r}"
        )
    info = json.loads(result.stdout)
    executable = shutil.which("kwin-mcp-cli", path=environment.get("PATH"))
    if executable is None:
        raise RuntimeError("installed kwin-mcp-cli executable is absent")
    cli_path = Path(executable).resolve()
    script = cli_path.read_bytes()
    shebang = script.splitlines()[0].decode()
    if not shebang.startswith("#!") or Path(shebang[2:]).resolve() != Path(sys.executable).resolve():
        raise RuntimeError(f"CLI and probe interpreter differ: {cli_path} {shebang!r}")
    info["cli"] = {
        "path": str(cli_path), "sha256": hashlib.sha256(script).hexdigest(), "shebang": shebang,
    }
    return info


def _topology_record(topology: Topology) -> dict[str, Any]:
    return {"virtual": topology.virtual, "outputs": topology.outputs, "raw": topology.raw}


def _run_capture(record: dict[str, Any], evidence_dir: Path, expected: str) -> None:
    worktree = Path(os.environ.get("KWIN_MCP_PROBE_WORKTREE", os.getcwd())).resolve()
    e2e_dir = worktree / "tests" / "e2e"
    fixture_path = e2e_dir / "visual_harness.py"
    probe_path = e2e_dir / "gui_probe.py"
    record["stage"] = "provenance"
    record["commit"] = _subject_commit()
    record["commit_provenance"] = (
        "KWIN_MCP_PROBE_COMMIT: external parent-verified wheel build-input SHA; "
        "not derived from installed Python files or the fixture checkout"
    )
    base_env = _fixture_environment(evidence_dir)
    record["installed_package"] = _installed_provenance(base_env, evidence_dir)
    record["kwin_version"] = _version()
    for path in (fixture_path, probe_path):
        if not path.is_file():
            raise RuntimeError(f"existing visual fixture file is absent: {path}")
    record["fixture"] = {
        "context_manager": "tests/e2e/visual_harness.py:nested_visual_kwin",
        "source_path": str(fixture_path),
        "source_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
        "gui_probe_sha256": hashlib.sha256(probe_path.read_bytes()).hexdigest(),
        "arguments": {"screen_size": [1280, 800], "output_count": 2, "scale": 1.0},
    }
    sys.path.insert(0, str(e2e_dir))
    from visual_harness import nested_visual_kwin

    # The context manager reads the actual process environment, not base_env.
    os.environ["KWIN_MCP_ARTIFACT_DIR"] = str(evidence_dir)
    os.environ["KWIN_MCP_X11_SCREENSHOT"] = "0"
    os.environ.pop("DISPLAY", None)
    os.environ.pop("PYTHONPATH", None)
    record["stage"] = "native fixture startup"
    with nested_visual_kwin(screen_size=(1280, 800), output_count=2, scale=1.0) as visual:
        record["fixture"].update({
            "artifact_dir": str(visual.artifact_dir),
            "wayland_display": visual.wayland_display,
            "socket_path": str(visual.socket_path),
            "dbus_address_file": str(visual.artifact_dir / "dbus-address.txt"),
            "home_dir": str(visual.artifact_dir / "home"),
            "capture_backend": visual.capture_backend,
            "screenshot_service_available": visual.screenshot_service_available,
            "output_count": visual.output_count,
            "xvfb_stdout_path": str(visual.xvfb_stdout_path),
            "xvfb_stderr_path": str(visual.xvfb_stderr_path),
            "kwin_stdout_path": str(visual.kwin_stdout_path),
            "kwin_stderr_path": str(visual.kwin_stderr_path),
            "cleanup": "nested_visual_kwin context manager on block exit",
        })
        if (
            visual.capture_backend != "screenshot2"
            or visual.screenshot_service_available is not True
            or visual.x_display
            or visual.output_count != 2
        ):
            raise RuntimeError(f"native two-output ScreenShot2 fixture not selected: {visual!r}")
        cli_env = _kscreen_environment(visual, base_env) | visual.capture_env
        cli_env.pop("DISPLAY", None)
        record["stage"] = "installed CLI connection"
        cli = InstalledCli(env=cli_env, stderr_path=evidence_dir / "kwin-mcp-cli.stderr.log")
        try:
            record["session_connect"] = cli.run(_command(
                "session_connect",
                dbus_address=visual.dbus_address,
                wayland_display=visual.wayland_display,
                retention={"artifacts": True},
            ))
            before_topology = _topology(visual.dbus_address, environment=base_env)
            record["before_topology"] = _topology_record(before_topology)
            if len({screen["geometry"][3] for screen in before_topology.outputs}) != 1:
                raise AssertionError(f"initial outputs already have mixed heights: {before_topology!r}")
            record["stage"] = "GUI probe launch"
            probe_command = shlex.join([sys.executable, str(probe_path)])
            launch_output = cli.run(_command(
                "app_launch", command=probe_command,
                env={
                    "GDK_BACKEND": "wayland", "GTK_MODULES": "gail:atk-bridge",
                    "NO_AT_BRIDGE": "0", "XDG_SESSION_TYPE": "wayland",
                },
            ))
            record["app_launch"] = launch_output
            pid_match = re.search(r"^App launched: .+ \(PID=(\d+)\)$", launch_output, re.MULTILINE)
            if pid_match is None:
                raise RuntimeError(f"app_launch did not report its current PID format: {launch_output}")
            record["fixture"]["gui_probe_pid"] = int(pid_match.group(1))
            probe = _wait_for_window(cli, "GUI Probe")[0]
            first = before_topology.outputs[0]
            first_rect = dict(zip(("x", "y", "width", "height"), first["geometry"], strict=True))
            probe = _place(cli, probe, first_rect, width_limit=920, height_limit=700)
            record["before_window"] = {"id": probe.window_id, "observed_frame": probe.frame}
            record["stage"] = "healthy before capture"
            before_path, before_rendered, before_source = _capture(cli, evidence_dir / "before.png")
            record["before"] = {
                "path": str(before_path), "source": before_source, "rendered": before_rendered,
            }
            _assert_mapping(before_path, before_rendered, before_topology)
            record["before"]["content"] = _content_pixel(before_path, before_topology)
            record["stage"] = "real output rotation"
            record["rotation_command"] = _rotate_one_output(
                visual, before_topology, base_env=base_env,
                log_path=evidence_dir / "kscreen-doctor-rotation.log",
            )
            after_topology = _wait_for_mixed_topology(
                visual.dbus_address, before_topology, environment=base_env
            )
            record["after_topology"] = _topology_record(after_topology)
            after_window = _wait_for_window(cli, "GUI Probe")[0]
            after_first = next(
                screen for screen in after_topology.outputs if screen["name"] == first["name"]
            )
            after_rect = dict(zip(("x", "y", "width", "height"), after_first["geometry"], strict=True))
            after_window = _place(cli, after_window, after_rect, width_limit=760, height_limit=700)
            record["after_window"] = {"id": after_window.window_id, "observed_frame": after_window.frame}
            record["stage"] = "native mixed-height gap capture"
            after_path, after_rendered, after_source = _capture(cli, evidence_dir / "after.png")
            record["after"] = {
                "path": str(after_path), "source": after_source, "rendered": after_rendered,
            }
            record["after"].update(_assert_capture(
                after_path, after_rendered, after_topology,
                expected=expected, observation=record["after"],
            ))
            record["healthy_content_assertion"] = (
                "opaque non-black pixel selected by scanning observed output rectangles"
            )
            record["stage"] = "CLI session stop"
            record["session_stop"] = cli.run("session_stop")
        finally:
            cli.close()
    record["stage"] = "complete"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect", choices=("opaque", "transparent"), required=True)
    args = parser.parse_args()
    evidence_dir = Path(os.environ.get(
        "KWIN_MCP_CAPTURE_GAPS_EVIDENCE", "/artifacts/capture-gaps"
    )).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "expected": args.expect,
        "evidence_dir": str(evidence_dir),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "failed",
        "stage": "initialization",
    }
    try:
        _run_capture(record, evidence_dir, args.expect)
        record["status"] = "passed"
    except BaseException as error:
        record["failure"] = {"type": type(error).__name__, "message": str(error)}
        (evidence_dir / "traceback.log").write_text(traceback.format_exc(), encoding="utf-8")
        diagnostics: dict[str, Any] = {
            "stage": record["stage"], "failure": record["failure"], "logs": {},
        }
        for path in sorted(evidence_dir.rglob("*.log")):
            diagnostics["logs"][str(path.relative_to(evidence_dir))] = (
                path.read_text(encoding="utf-8", errors="replace")[-8000:]
            )
        (evidence_dir / "failure-diagnostics.json").write_text(
            json.dumps(diagnostics, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        raise
    finally:
        record["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        record["artifacts"] = [
            {"path": str(path.relative_to(evidence_dir)), "size": path.stat().st_size}
            for path in sorted(evidence_dir.rglob("*"))
            if path.is_file() and path.suffix in {".png", ".log", ".json", ".jsonl", ".stdin"}
        ]
        rendered = json.dumps(record, indent=2, sort_keys=True, default=str)
        (evidence_dir / "evidence.json").write_text(rendered + "\n", encoding="utf-8")
        print(rendered, flush=True)


if __name__ == "__main__":
    main()
