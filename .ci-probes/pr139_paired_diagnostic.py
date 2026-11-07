#!/usr/bin/env python3
"""Run the exact-image, main-versus-PR #139 diagnostic on a GitHub Linux runner."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path

REPO = "isac322/kwin-mcp"
IMAGE_NAME = "kwin-mcp-e2e:latest"
UV_IMAGE = "ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424"
TEST_FILES = (
    "tests/_suite_policy.py",
    "tests/e2e/test_element_act.py",
    "tests/e2e/test_host_isolation.py",
    "tests/e2e/test_session_start.py",
)


def run(name: str, args: list[str], *, cwd: Path | None = None, check: bool = True) -> int:
    started = time.monotonic()
    result = subprocess.run(args, cwd=cwd, check=False)
    elapsed = time.monotonic() - started
    record = {"name": name, "argv": args, "returncode": result.returncode, "elapsed_seconds": elapsed}
    (OUT / "commands.jsonl").open("a", encoding="utf-8").write(json.dumps(record) + "\n")
    if check and result.returncode:
        raise RuntimeError(f"{name} failed with exit {result.returncode}")
    return result.returncode


def capture(name: str, args: list[str], output: Path, *, check: bool = True) -> int:
    started = time.monotonic()
    with output.open("wb") as stream:
        result = subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT, check=False)
    record = {
        "name": name,
        "argv": args,
        "returncode": result.returncode,
        "elapsed_seconds": time.monotonic() - started,
        "output": str(output.relative_to(OUT)),
    }
    (OUT / "commands.jsonl").open("a", encoding="utf-8").write(json.dumps(record) + "\n")
    if check and result.returncode:
        raise RuntimeError(f"{name} failed with exit {result.returncode}; see {output}")
    return result.returncode


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def docker_image_id(tag: str) -> str:
    return subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag], text=True
    ).strip()


def prepare_udmabuf() -> None:
    if not Path("/dev/udmabuf").is_char_device():
        capture("modprobe-udmabuf", ["sudo", "-n", "modprobe", "udmabuf"], OUT / "modprobe.log", check=False)
        capture("udevadm-settle", ["sudo", "-n", "udevadm", "settle", "--timeout=10"], OUT / "udevadm.log", check=False)
    node = Path("/dev/udmabuf")
    if not node.is_char_device():
        raise RuntimeError("required host character device /dev/udmabuf is unavailable")
    major, minor = os.major(node.stat().st_rdev), os.minor(node.stat().st_rdev)
    registration = Path("/sys/class/misc/udmabuf/dev")
    if not registration.is_file() or registration.read_text().strip() != f"{major}:{minor}":
        raise RuntimeError("/dev/udmabuf does not match /sys/class/misc/udmabuf/dev")
    sysfs = Path(f"/sys/dev/char/{major}:{minor}")
    if not sysfs.exists():
        raise RuntimeError("registered /dev/udmabuf sysfs identity is unavailable")
    write_json(
        OUT / "udmabuf.json",
        {
            "path": str(node),
            "major": major,
            "minor": minor,
            "group": node.stat().st_gid,
            "mode": stat.S_IMODE(node.stat().st_mode),
            "registration": registration.read_text().strip(),
            "sysfs": str(sysfs.resolve()),
        },
    )


def download_image() -> None:
    image_dir = OUT / "image-download"
    image_dir.mkdir()
    staged = Path(".ci-probes/original-opensuse-image-chunks")
    chunks = sorted(staged.glob("image.tar.zst.part-*"))
    if chunks:
        manifest = json.loads((staged / "manifest.json").read_text(encoding="utf-8"))
        archive = image_dir / "image.tar.zst"
        with archive.open("wb") as output:
            for chunk in chunks:
                with chunk.open("rb") as input_stream:
                    shutil.copyfileobj(input_stream, output)
        if sha256(archive) != os.environ["EXPECTED_IMAGE_TAR_SHA"]:
            raise RuntimeError(f"staged image stream SHA256 mismatch: {sha256(archive)}")
        write_json(OUT / "source-artifact.json", manifest)
        write_json(
            OUT / "source-archive.json",
            {
                "path": str(archive),
                "sha256": sha256(archive),
                "bytes": archive.stat().st_size,
                "source": "committed diagnostic chunks",
                "artifact_zip_digest": manifest["source_artifact_zip_digest"],
            },
        )
    else:
        metadata = OUT / "source-artifacts.json"
        capture(
            "artifact-metadata",
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{REPO}/actions/runs/{os.environ['SOURCE_RUN']}/artifacts?per_page=100",
            ],
            metadata,
        )
        artifact_pages = json.loads(metadata.read_text(encoding="utf-8"))
        artifacts = [
            item
            for page in artifact_pages
            for item in page.get("artifacts", [])
        ]
        candidates = [
            item
            for item in artifacts
            if item["id"] == int(os.environ["SOURCE_ARTIFACT_ID"])
            and item["name"] == "e2e-image-opensuse-amd64"
            and not item["expired"]
        ]
        if len(candidates) != 1:
            raise RuntimeError(f"unexpected source artifact list: {artifact_pages}")
        write_json(OUT / "source-artifact.json", candidates[0])
        capture(
            "artifact-download",
            [
                "gh",
                "run",
                "download",
                os.environ["SOURCE_RUN"],
                "--repo",
                REPO,
                "--name",
                "e2e-image-opensuse-amd64",
                "--dir",
                str(image_dir),
            ],
            OUT / "artifact-download.log",
        )
        archive = image_dir / "image.tar.zst"
        if not archive.is_file():
            raise RuntimeError(f"artifact did not contain {archive}")
        write_json(
            OUT / "source-archive.json",
            {"path": str(archive), "sha256": sha256(archive), "bytes": archive.stat().st_size},
        )
    started = time.monotonic()
    decompressor = subprocess.Popen(
        ["zstd", "--decompress", "--stdout", str(archive)], stdout=subprocess.PIPE
    )
    assert decompressor.stdout is not None
    loader = subprocess.run(["docker", "load"], stdin=decompressor.stdout, check=False)
    decompressor.stdout.close()
    decompressor.wait()
    (OUT / "commands.jsonl").open("a", encoding="utf-8").write(
        json.dumps(
            {
                "name": "docker-load",
                "argv": ["zstd", "--decompress", "--stdout", str(archive), "|", "docker", "load"],
                "returncode": loader.returncode or decompressor.returncode,
                "elapsed_seconds": time.monotonic() - started,
            }
        )
        + "\n"
    )
    if loader.returncode or decompressor.returncode:
        raise RuntimeError("docker image load failed")
    if docker_image_id(IMAGE_NAME) != os.environ["EXPECTED_IMAGE_ID"]:
        raise RuntimeError(f"loaded image identity mismatch: {docker_image_id(IMAGE_NAME)}")
    inspect = subprocess.check_output(
        ["docker", "image", "inspect", IMAGE_NAME], text=True
    )
    (OUT / "image-inspect.json").write_text(inspect, encoding="utf-8")


def fetch_sources() -> tuple[Path, Path]:
    sources = OUT / "sources"
    main = sources / "main"
    pr = sources / "pr139"
    main.mkdir(parents=True)
    pr.mkdir(parents=True)
    run("fetch-base", ["git", "fetch", "--no-tags", "origin", os.environ["BASE_SHA"]])
    run("fetch-pr", ["git", "fetch", "--no-tags", "origin", os.environ["PR_SHA"]])
    for name, sha, target in (
        ("main", os.environ["BASE_SHA"], main),
        ("pr139", os.environ["PR_SHA"], pr),
    ):
        with subprocess.Popen(["git", "archive", sha], stdout=subprocess.PIPE) as archive:
            assert archive.stdout is not None
            subprocess.run(["tar", "-x", "-C", str(target)], stdin=archive.stdout, check=True)
        write_json(target / "source.json", {"name": name, "sha": sha})
    return main, pr

def build_wheel(source: Path, name: str) -> Path:
    wheel_dir = OUT / "wheels" / name
    wheel_dir.mkdir(parents=True)
    context = OUT / "wheel-builders" / name
    source_copy = context / "src"
    context.mkdir(parents=True)
    shutil.copytree(source, source_copy)
    (context / "Dockerfile").write_text(
        f"""FROM {UV_IMAGE} AS uv\nFROM {IMAGE_NAME}\nUSER root\nCOPY --from=uv /uv /usr/local/bin/uv\nCOPY src /src\nRUN mkdir /out && cd /src && uv build --wheel --out-dir /out\n""",
        encoding="utf-8",
    )
    builder_tag = f"kwin-mcp-e2e:pr139-wheel-builder-{name}"
    capture(
        f"wheel-{name}",
        ["docker", "build", "--platform", "linux/amd64", "-t", builder_tag, str(context)],
        OUT / f"wheel-{name}.log",
    )
    container = f"kwin-mcp-pr139-wheel-builder-{name}"
    run("wheel-create", ["docker", "create", "--name", container, builder_tag])
    capture(
        "wheel-copy",
        ["docker", "cp", f"{container}:/out/.", str(wheel_dir)],
        OUT / f"wheel-copy-{name}.log",
    )
    subprocess.run(["docker", "rm", container], stdout=subprocess.DEVNULL, check=False)
    subprocess.run(["docker", "rmi", builder_tag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    wheels = sorted(wheel_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one {name} wheel, found {wheels}")
    write_json(
        wheel_dir / "manifest.json",
        {"sha": sha256(wheels[0]), "path": str(wheels[0]), "bytes": wheels[0].stat().st_size},
    )
    return wheels[0]

def build_derivative(wheel: Path, name: str) -> str:
    context = OUT / "derivatives" / name
    context.mkdir(parents=True)
    shutil.copy2(wheel, context / wheel.name)
    (context / "Dockerfile").write_text(
        f"""FROM {UV_IMAGE} AS uv\nFROM {IMAGE_NAME}\nUSER root\nCOPY --from=uv /uv /usr/local/bin/uv\nCOPY {wheel.name} /tmp/kwin-wheel/{wheel.name}\nRUN uv pip install --python /opt/kwin-mcp-venv/bin/python --no-deps --reinstall /tmp/kwin-wheel/{wheel.name}\nUSER tester\n""",
        encoding="utf-8",
    )
    tag = f"kwin-mcp-e2e:pr139-diagnostic-{name}"
    capture(f"image-{name}", ["docker", "build", "--platform", "linux/amd64", "-t", tag, str(context)], OUT / f"image-{name}.log")
    write_json(OUT / "derivatives" / f"{name}.json", {"tag": tag, "id": docker_image_id(tag), "wheel": str(wheel), "wheel_sha256": sha256(wheel)})
    return tag


def mount_args(pr_source: Path) -> list[str]:
    args: list[str] = []
    for relative in TEST_FILES:
        args.extend(["-v", f"{pr_source / relative}:/app/{relative}:ro"])
    return args


def run_case(
    tag: str, name: str, node: str, pr_source: Path, extra_args: list[str] | None = None
) -> int:
    case = OUT / "cases" / name
    case.mkdir(parents=True)
    container = f"kwin-mcp-pr139-diag-{name}"
    group = str((Path("/dev/udmabuf").stat().st_gid))
    args = [
        "docker", "run", "--name", container,
        "--device=/dev/udmabuf:/dev/udmabuf:rw", "--group-add", group,
        "--env", "KWIN_MCP_E2E_SCREENSHOT=1",
        "--env", "KWIN_MCP_E2E_STRICT_SKIPS=1",
        "--env", "KWIN_MCP_E2E_REQUIRE=layer-shell",
        "--volume", f"{case}:/artifacts",
        *mount_args(pr_source),
        tag,
        "/opt/kwin-mcp-venv/bin/python", "-m", "pytest", "-vv", node,
        *(extra_args or []),
        "--junitxml=/artifacts/junit.xml",
    ]
    started = time.monotonic()
    result = subprocess.run(args, stdout=(case / "pytest.log").open("wb"), stderr=subprocess.STDOUT, check=False)
    (case / "exit-code.txt").write_text(f"{result.returncode}\n", encoding="utf-8")
    inspect = subprocess.run(["docker", "inspect", container], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    (case / "docker-inspect.json").write_bytes(inspect.stdout)
    logs = subprocess.run(["docker", "logs", "--timestamps", container], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    (case / "docker.log").write_bytes(logs.stdout)
    subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    write_json(case / "result.json", {"name": name, "node": node, "tag": tag, "returncode": result.returncode, "elapsed_seconds": time.monotonic() - started})
    return result.returncode


def main() -> int:
    global OUT
    OUT = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "pr139-paired-diagnostic"
    OUT.mkdir(parents=True, exist_ok=True)
    overall = 0
    try:
        prepare_udmabuf()
        download_image()
        main_source, pr_source = fetch_sources()
        main_wheel = build_wheel(main_source, "main")
        pr_wheel = build_wheel(pr_source, "pr139")
        main_tag = build_derivative(main_wheel, "main")
        pr_tag = build_derivative(pr_wheel, "pr139")
        cases = [
            (main_tag, "main-kcalc-fallback", "tests/e2e/test_element_act.py::test_element_act_fallback_actions_on_kcalc", pr_source, []),
            (pr_tag, "pr139-kcalc-fallback", "tests/e2e/test_element_act.py::test_element_act_fallback_actions_on_kcalc", pr_source, []),
            (main_tag, "main-shard-2", "tests", pr_source, ["--kwin-shard=2/5"]),
            (pr_tag, "pr139-shard-2", "tests", pr_source, ["--kwin-shard=2/5"]),
            (main_tag, "main-host-isolation", "tests/e2e/test_host_isolation.py", pr_source, []),
            (pr_tag, "pr139-host-isolation", "tests/e2e/test_host_isolation.py", pr_source, []),
            (pr_tag, "pr139-session-start", "tests/e2e/test_session_start.py", pr_source, []),
        ]
        results = []
        for tag, name, node, tests, extra_args in cases:
            code = run_case(tag, name, node, tests, extra_args)
            results.append({"name": name, "returncode": code})
            overall |= int(code != 0)
        write_json(OUT / "summary.json", {"status": "completed", "results": results, "overall_exit": overall})
    except Exception as error:
        overall = 1
        write_json(OUT / "failure.json", {"type": type(error).__name__, "message": str(error)})
    return overall


if __name__ == "__main__":
    raise SystemExit(main())
