#!/usr/bin/env python3
"""Temporary CI-only current-image diagnostics; never changes image or product source."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

SOURCE_RUN = 39280112134
SOURCE_SHA = "804d5bc7a06d0144e5be4938492c0a2ff413ed00"
EXPECTED_IMAGE_ID = "sha256:8204efb3aade495a8a5b36e7dc918c4a82b6be27e0c2c6fa464bb4420db9d636"
REPOSITORY = "isac322/kwin-mcp"
IMAGE = "kwin-mcp-e2e"
PYTHON = "/opt/kwin-mcp-venv/bin/python"
KCALC_NODE = "tests/e2e/test_capture.py::test_capture_on_all_15_carriers[pointer_click]"
KCALC_SHARD = ["tests", "-v", "-m", "not slow", "--kwin-shard=2/5"]
CLI_NODE = "tests/e2e/test_cli_surface.py::test_cli_keyword_params_from_to"
ROOT = Path(os.environ["RUNNER_TEMP"])
OUT = ROOT / "kwin68-native-diagnostics"
SCRATCH = ROOT / "kwin68-native-inputs"
LEDGER = ROOT / "kwin68-native-cleanup.jsonl"
FIX_NAMES = ("visual_harness.py", "interaction_probe.py", "test_cli_surface.py")
FIX_DIR = Path(__file__).resolve().parent / "fixture-fix"
IMAGE_TEST_ROOT = "/app/tests/e2e"
RESULTS = {}


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def record(kind, identifier, delete_command):
    with LEDGER.open("a") as ledger:
        ledger.write(json.dumps({"kind": kind, "identifier": str(identifier),
                                 "delete_command": delete_command,
                                 "created_at": time.time()}) + "\n")


def command(name, argv, timeout=15):
    """Keep bounded command output and status, never the inherited CI secrets."""
    env = {key: value for key, value in os.environ.items()
           if key not in ("GH_TOKEN", "GITHUB_TOKEN", "GH_DEBUG", "ACTIONS_STEP_DEBUG")}
    secrets = [value.encode() for key, value in os.environ.items()
               if value and any(word in key.lower() for word in
                   ("token", "password", "secret", "credential", "authorization"))]
    started = time.monotonic()
    with (OUT / f"{name}.stdout").open("wb") as stdout, (OUT / f"{name}.stderr").open("wb") as stderr:
        try:
            proc = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL,
                                  stdout=stdout, stderr=stderr, timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            code = 124
        finally:
            for path in (OUT / f"{name}.stdout", OUT / f"{name}.stderr"):
                output = path.read_bytes()
                for secret in secrets:
                    output = output.replace(secret, b"<redacted>")
                path.write_bytes(output)
    RESULTS[name] = {"argv": argv, "returncode": code,
                     "elapsed_seconds": time.monotonic() - started}
    save(OUT / "exit-codes.json", RESULTS)
    return code


def require(name, argv, timeout=15):
    if command(name, argv, timeout):
        raise RuntimeError(f"{name} failed; see its logs and exit-codes.json")
    return (OUT / f"{name}.stdout").read_text()


def api(endpoint, target=None, paginate=False):
    argv = ["gh", "api"]
    if paginate:
        argv += ["--paginate", "--slurp"]
    argv += [f"repos/{REPOSITORY}/{endpoint}"]
    # gh receives the token only through its environment. Do not retain HTTP
    # headers, signed redirect URLs, raw error payloads, or authentication logs.
    env = dict(os.environ)
    env.pop("GH_DEBUG", None)
    with target.open("wb") if target else open(os.devnull, "wb") as sink:
        proc = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL,
                              stdout=sink if target else subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=170 if target else 15)
    if proc.returncode:
        raise RuntimeError(f"GitHub API request failed (exit {proc.returncode}); payload withheld")
    return None if target else json.loads(proc.stdout)


def select_source_image(run, pages):
    """Select only the stable current-run image artifact; never fall back."""
    if run["id"] != SOURCE_RUN or run["head_sha"] != SOURCE_SHA:
        raise RuntimeError("Source workflow run or head SHA does not match the approved current image")
    candidates = [artifact for page in pages for artifact in page["artifacts"]
                  if not artifact["expired"] and artifact["name"] == "e2e-image-opensuse-amd64"]
    if len(candidates) != 1:
        raise RuntimeError("Expected exactly one unexpired stable openSUSE AMD64 image artifact")
    artifact = candidates[0]
    if (artifact["workflow_run"]["id"] != SOURCE_RUN
            or artifact["workflow_run"]["head_sha"] != SOURCE_SHA):
        raise RuntimeError("Artifact source run or SHA mismatch")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["digest"].lower()):
        raise RuntimeError("Artifact has no verifiable SHA-256 digest")
    return artifact, None


def ensure_udmabuf_registration(read_registration, load_module, observations):
    """Return registered major/minor; callbacks read sysfs or run one loader."""
    observations.update(before=None, after=None, loader_attempted=False,
                        loader_returncode=None)
    observations["before"] = read_registration()
    observations["after"] = observations["before"]
    if observations["before"] is None:
        observations["loader_attempted"] = True
        try:
            observations["loader_returncode"] = load_module()
        finally:
            observations["after"] = read_registration()
        if observations["loader_returncode"] != 0:
            raise RuntimeError("Stock udmabuf module loader failed; see its logs")
    identity = observations["after"]
    if identity is None:
        raise RuntimeError("No live udmabuf registration exists after the stock module loader")
    if not re.fullmatch(r"[0-9]+:[0-9]+", identity.strip()):
        raise RuntimeError("Invalid udmabuf sysfs device identity")
    major, minor = map(int, identity.strip().split(":"))
    if major == 0:
        raise RuntimeError("Invalid udmabuf sysfs device major")
    os.makedev(major, minor)
    return major, minor


def validate_udmabuf_node(info, major, minor):
    if not stat.S_ISCHR(info.st_mode) or info.st_rdev != os.makedev(major, minor):
        raise RuntimeError("Existing udmabuf node does not match registered sysfs identity")


# Capture test-owned files before the existing fixture deletes them at teardown.
# No product monkeypatches: this plugin only copies evidence and records reports.
EVIDENCE_PLUGIN = r'''import hashlib
import json
from pathlib import Path
import shutil
import os
import stat
from PIL import Image

OUT = Path("/artifacts")
COLLECTED = []
REPORTS = []

def pytest_itemcollected(item):
    COLLECTED.append(item.nodeid)

def pytest_collection_finish(session):
    (OUT / "collection.json").write_text(json.dumps({
        "collected": COLLECTED, "selected": [item.nodeid for item in session.items],
        "collection_errors": session.testsfailed,
    }, indent=2) + "\n")

def pytest_sessionfinish(session, exitstatus):
    (OUT / "session-result.json").write_text(json.dumps({
        "exitstatus": int(exitstatus), "reports": REPORTS,
        "observations_are_test_completion": False,
    }, indent=2) + "\n")


def pytest_sessionstart(session):
    record = {"uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups(),
              "drm_nodes": [str(path) for path in Path("/dev/dri").glob("*")]}
    try:
        node = Path("/dev/udmabuf")
        info = node.stat()
        record["udmabuf"] = {"character_device": stat.S_ISCHR(info.st_mode),
                             "major": os.major(info.st_rdev), "minor": os.minor(info.st_rdev),
                             "gid": info.st_gid, "mode": oct(stat.S_IMODE(info.st_mode))}
        descriptor = os.open(node, os.O_RDWR | os.O_CLOEXEC)
        os.close(descriptor)
        record["udmabuf"]["open_rdwr"] = True
    except OSError as error:
        record["device_error"] = {"errno": error.errno, "message": str(error)}
    (OUT / "device-access.json").write_text(json.dumps(record, indent=2) + "\n")

def pytest_runtest_logreport(report):
    from _suite_policy import is_known_limit
    reason = (str(report.longrepr[2]) if isinstance(report.longrepr, tuple)
              else str(report.longrepr)).removeprefix("Skipped: ")
    record = {"nodeid": report.nodeid, "when": report.when,
              "outcome": report.outcome,
              "allowed_skip": report.skipped and (
                  hasattr(report, "wasxfail") or is_known_limit(reason))}
    REPORTS.append(record)
    with (OUT / "reports.jsonl").open("a") as log:
        log.write(json.dumps(record) + "\n")
    for source in Path("/tmp").glob("kwin-mcp-screenshots-*"):
        if source.is_dir():
            shutil.copytree(source, OUT / "session-files" / source.name, dirs_exist_ok=True)
    images = []
    for path in (OUT / "session-files").rglob("*.png"):
        with Image.open(path) as image:
            image.load()
            images.append({"path": str(path.relative_to(OUT)), "format": image.format,
                           "width": image.width, "height": image.height,
                           "bytes": path.stat().st_size,
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (OUT / "png-evidence.json").write_text(json.dumps(images, indent=2) + "\n")
'''


def fixture_mounts():
    return [part for name in FIX_NAMES for part in (
        "--mount", f"type=bind,src={FIX_DIR / name},dst={IMAGE_TEST_ROOT}/{name},readonly")]


def collection(case):
    return json.loads((OUT / case / "collection.json").read_text())


def result_passed(case, expected, suite=False):
    """Only actual complete JUnit results qualify; observations cannot pass."""
    if RESULTS[case]["returncode"] != 0:
        return False
    directory = OUT / case
    required = ("junit.xml", "collection.json", "session-result.json")
    if not all((directory / name).is_file() for name in required):
        return False
    actual = collection(case)
    session = json.loads((directory / "session-result.json").read_text())
    if (actual != expected or actual["collection_errors"] or session["exitstatus"] != 0
            or not actual["selected"] or len(set(actual["selected"])) != len(actual["selected"])):
        return False
    tests = list(ET.parse(directory / "junit.xml").iter("testcase"))
    if len(tests) != len(actual["selected"]) or any(
            test.find(tag) is not None for test in tests for tag in ("failure", "error")):
        return False
    reports = session["reports"]
    calls = {report["nodeid"] for report in reports if report["when"] == "call"}
    skips = [report for report in reports if report["outcome"] == "skipped"]
    skipped_nodes = {report["nodeid"] for report in skips}
    junit_skips = sum(test.find("skipped") is not None for test in tests)
    if calls | skipped_nodes != set(actual["selected"]) or junit_skips != len(skips):
        return False
    if suite:
        return all(report["allowed_skip"] for report in skips)
    return len(tests) == 1 and not skips


def experiment(case, node, gid, *, timeout, pytest_args=None, fixed=True,
               collect_only=False):
    artifacts = OUT / case
    artifacts.mkdir(mode=0o777)
    artifacts.chmod(0o777)
    name = f"kwin68-{case}-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
    args = list(pytest_args) if pytest_args is not None else [node]
    argv = ["docker", "run", "--pull=never", "--name", name,
            "--workdir", "/app", "--group-add", str(gid),
            "--env", "QT_FORCE_STDERR_LOGGING=1",
            "--env", "PYTHONPATH=/probe:/app/tests:/app/tests/e2e",
            "--env", "KWIN_MCP_E2E_STRICT_SKIPS=1",
            "--env", "KWIN_MCP_E2E_REQUIRE=layer-shell",
            "--env", "KWIN_MCP_ARTIFACT_DIR=/artifacts",
            "--volume", f"{artifacts}:/artifacts",
            "--volume", f"{SCRATCH / 'helpers'}:/probe:ro"]
    if fixed:
        argv += fixture_mounts()
    if not collect_only:
        argv += ["--device", "/dev/udmabuf:/dev/udmabuf:rw",
                 "--env", "KWIN_MCP_E2E_SCREENSHOT=1"]
    plugins = ["-p", "evidence"]
    if not collect_only and pytest_args is None:
        plugins += ["-p", "current_failures"]
    argv += [IMAGE, PYTHON, "/probe/launch.py", str(int(fixed)),
             *plugins, *args, "-v", "-s", "--junitxml=/artifacts/junit.xml"]
    if collect_only:
        argv.append("--collect-only")
    record("container", name, f"docker rm --force {name}")
    try:
        command(case, argv, timeout=timeout)
    finally:
        command(case + "-kill", ["docker", "kill", name], timeout=5)
        command(case + "-inspect", ["docker", "inspect", name], timeout=5)
        command(case + "-logs", ["docker", "logs", "--timestamps", name], timeout=5)
        command(case + "-processes", ["docker", "top", name,
                                      "-eo", "pid,ppid,user,stat,etime,args"], timeout=5)


LAUNCHER = r'''import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

fixed = sys.argv[1] == "1"
expected = json.loads(Path("/probe/fixture-contract.json").read_text())
observed = {}
mounts = Path("/proc/self/mountinfo").read_text().splitlines()
for name, digest in expected.items():
    path = Path("/app/tests/e2e") / name
    if not path.is_file() or path.resolve() != path:
        raise RuntimeError(f"Image test target is missing or indirect: {path}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    spec = importlib.util.find_spec(path.stem)
    if spec is None or Path(spec.origin) != path:
        raise RuntimeError(f"Image test import target is not {path}")
    if fixed:
        entries = [line.split() for line in mounts if line.split()[4] == str(path)]
        if (actual != digest or len(entries) != 1
                or "ro" not in entries[0][5].split(",")):
            raise RuntimeError(f"Fixed test target is not the approved readonly file: {path}")
    observed[name] = {"path": str(path), "sha256": actual, "fixed": fixed}
Path("/artifacts/test-targets.json").write_text(json.dumps(observed, indent=2) + "\n")
os.execv(sys.executable, [sys.executable, "-m", "pytest", *sys.argv[2:]])
'''


def main():
    OUT.mkdir(exist_ok=False)
    record("scratch-directory", OUT, f"rm -rf -- {OUT}")
    print(f"Ephemeral runner cleanup ledger: {LEDGER}", flush=True)
    SCRATCH.mkdir(exist_ok=False)
    record("scratch-directory", SCRATCH, f"rm -rf -- {SCRATCH}")
    save(OUT / "prior-x11-observation.json", {
        "source": "previous local and native-CI observations supplied with this diagnostic task",
        "local": "forced X11 backend failed with the confirmed DRI3 critical",
        "native_ci": "original X11 backend probe exited 1",
        "scope": "not re-tested or claimed repaired by the udmabuf virtual-backend counterfactual",
        "pixel_proof": "compositor survival is not evidence of captured pixels",
    })
    if not (os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
            and os.environ.get("RUNNER_OS") == "Linux"
            and os.environ.get("RUNNER_ARCH") == "X64"
            and platform.machine() == "x86_64"
            and os.environ.get("GITHUB_REPOSITORY") == REPOSITORY):
        raise RuntimeError("This probe may run only on the repository's fresh native AMD64 GitHub-hosted runner")
    if (not FIX_DIR.is_dir() or FIX_DIR.is_symlink()
            or {path.name for path in FIX_DIR.iterdir()} != set(FIX_NAMES)):
        raise RuntimeError("fixture-fix must contain only the three parent-approved final test files")
    if any(not (FIX_DIR / name).is_file() or (FIX_DIR / name).is_symlink()
           for name in FIX_NAMES):
        raise RuntimeError("Fixed test sources must be regular files, not symlinks")
    fixed_hashes = {name: hashlib.sha256((FIX_DIR / name).read_bytes()).hexdigest()
                    for name in FIX_NAMES}
    save(OUT / "fixture-contract.json", {
        "files": {name: {"source": str(FIX_DIR / name),
                         "target": f"{IMAGE_TEST_ROOT}/{name}", "sha256": digest,
                         "readonly": True} for name, digest in fixed_hashes.items()},
        "source_run": SOURCE_RUN, "source_head_sha": SOURCE_SHA,
        "loaded_image_id": EXPECTED_IMAGE_ID,
        "product_source_mounts": [], "parent_final_sha_check_required": True,
    })
    save(OUT / "current-failure-experiments.json", {
        "status": "observations_only", "passed": False,
        "baseline_failure_evidence_run": 39297094218,
        "pr_7x5_validation": "pending; parent-owned",
    })
    save(OUT / "host-environment.json", {
        "platform": platform.platform(), "uid": os.getuid(), "gid": os.getgid(),
        "ci": {key: os.environ.get(key) for key in (
            "RUNNER_ENVIRONMENT", "RUNNER_OS", "RUNNER_ARCH", "GITHUB_REPOSITORY",
            "GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")},
    })
    require("kernel", ["uname", "-a"])
    require("docker-version", ["docker", "version"])
    if require("preexisting-image", ["docker", "image", "ls", "--quiet",
                                   "--filter", f"reference={IMAGE}"]).strip():
        raise RuntimeError("Refusing to replace an existing test image; a fresh CI runner is required")
    run = api(f"actions/runs/{SOURCE_RUN}")
    # A failed-job rerun can advance run_attempt without rebuilding the image.
    # This diagnostic reuses the original fixture, never that rerun's check status.
    pages = api(f"actions/runs/{SOURCE_RUN}/artifacts?per_page=100", paginate=True)
    artifact, image_attempt = select_source_image(run, pages)
    digest = artifact["digest"].lower()
    provenance = {key: artifact[key] for key in (
        "id", "name", "digest", "created_at", "updated_at", "size_in_bytes")}
    provenance.update(source_run=SOURCE_RUN, source_head_sha=SOURCE_SHA,
                      currentSourceRunAttempt=run["run_attempt"],
                      selectedSourceImageAttempt=image_attempt)
    save(OUT / "image-provenance.json", provenance)
    archive = SCRATCH / "image.zip"
    api(f"actions/artifacts/{artifact['id']}/zip", target=archive)
    with archive.open("rb") as stream:
        actual = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
    provenance["downloaded_digest"] = actual
    provenance["digest_verified"] = actual == digest
    save(OUT / "image-provenance.json", provenance)
    if actual != digest:
        raise RuntimeError("Downloaded artifact ZIP failed SHA-256 verification")
    with zipfile.ZipFile(archive) as zipped:
        if zipped.namelist() != ["image.tar.zst"]:
            raise RuntimeError("Unexpected image ZIP contents")
        zipped.extract("image.tar.zst", SCRATCH)
    require("load-image", ["bash", "-o", "pipefail", "-c",
            'zstd --decompress --stdout "$1" | docker load', "load-image",
            str(SCRATCH / "image.tar.zst")], timeout=170)
    record("docker-image", IMAGE, f"docker image rm {IMAGE}")
    image = json.loads(require("image-inspect", ["docker", "image", "inspect", IMAGE]))[0]
    if (image["Architecture"] != "amd64" or image["Config"]["User"] != "tester"
            or image["Id"] != EXPECTED_IMAGE_ID):
        raise RuntimeError("Loaded image architecture, user, or immutable image ID mismatch")
    provenance["loaded_image_id"] = image["Id"]
    provenance["loaded_image_user"] = image["Config"]["User"]
    provenance["loaded_image_architecture"] = image["Architecture"]
    save(OUT / "image-provenance.json", provenance)
    helpers = SCRATCH / "helpers"
    helpers.mkdir()
    (helpers / "evidence.py").write_text(EVIDENCE_PLUGIN)
    (helpers / "current_failures.py").write_text(
        (Path(__file__).resolve().parent / "kwin68_current_failures.py").read_text())
    (helpers / "launch.py").write_text(LAUNCHER)
    save(helpers / "fixture-contract.json", fixed_hashes)
    gid = os.getgid()
    registration = Path("/sys/class/misc/udmabuf/dev")
    module_present = Path("/sys/module/udmabuf").exists()
    registration_observation = {}
    try:
        major, minor = ensure_udmabuf_registration(
            lambda: registration.read_text() if registration.exists() else None,
            lambda: command("load-udmabuf-module", ["sudo", "-n", "modprobe", "udmabuf"]),
            registration_observation)
    finally:
        save(OUT / "udmabuf-registration.json", registration_observation)
        if registration_observation.get("loader_returncode") == 0 and not module_present:
            record("kernel-module", "udmabuf", "sudo modprobe -r udmabuf")
    node = Path("/dev/udmabuf")
    node_created = False
    if not node.exists():
        require("create-udmabuf-node", ["sudo", "-n", "mknod", str(node), "c", str(major), str(minor)])
        node_created = True
        record("device-node", node, f"sudo rm -- {node}  # recorded only; deletion is not run by this probe")
    info = node.stat()
    validate_udmabuf_node(info, major, minor)
    prior_node = {
        "path": str(node), "inode": info.st_ino, "filesystem_device": info.st_dev,
        "dev_t": info.st_rdev, "device_major_minor": f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}",
        "uid": info.st_uid, "gid": info.st_gid, "mode": oct(stat.S_IMODE(info.st_mode)),
        "character_device": stat.S_ISCHR(info.st_mode), "created_by_probe": node_created,
    }
    save(OUT / "udmabuf-node-before.json", prior_node)
    restore = f"sudo chown {info.st_uid}:{info.st_gid} {node} && sudo chmod {oct(stat.S_IMODE(info.st_mode))[2:]} {node}"
    with LEDGER.open("a") as ledger:
        ledger.write(json.dumps({
            "kind": "device-node-metadata", "identifier": str(node), "prior": prior_node,
            "restore_command": restore, "created_at": time.time(),
            "note": "Record only; restore only after verifying the same inode and dev_t",
        }) + "\n")
    if info.st_gid != gid:
        require("udmabuf-group", ["sudo", "-n", "chgrp", str(gid), str(node)])
    if stat.S_IMODE(info.st_mode) != 0o660:
        require("udmabuf-mode", ["sudo", "-n", "chmod", "0660", str(node)])
    info = node.stat()
    save(OUT / "software-device.json", {
        "sysfs_major_minor": f"{major}:{minor}",
        "device_major": os.major(info.st_rdev), "device_minor": os.minor(info.st_rdev),
        "mode": oct(stat.S_IMODE(info.st_mode)), "gid": info.st_gid,
        "container_user": "image default tester", "container_group_add": gid,
        "passthrough": ["/dev/udmabuf:rw"], "drm_passthrough": False,
    })

    # Collection alone is not a baseline failure replay. Derive all expected
    # identities/counts from the immutable image, never from pinned test totals.
    full_args = ["tests", "-m", "not slow"]
    for case, args, fixed in (
            ("image-full-collection", full_args, False),
            ("image-shard2-collection", KCALC_SHARD, False),
            ("fixed-full-collection", full_args, True)):
        experiment(case, None, gid, timeout=60, pytest_args=args,
                   fixed=fixed, collect_only=True)
        if RESULTS[case]["returncode"] != 0:
            raise RuntimeError(f"{case} failed; collection is not established")
    expected_full = collection("image-full-collection")
    expected_shard = collection("image-shard2-collection")
    collection_ok = (
        collection("fixed-full-collection") == expected_full
        and not expected_full["collection_errors"]
        and not expected_shard["collection_errors"]
        and bool(expected_shard["selected"])
        and set(expected_shard["selected"]).issubset(expected_full["selected"])
        and len(expected_full["selected"]) >= len(expected_shard["selected"])
        and all(expected_full["selected"].count(node) == 1 for node in (KCALC_NODE, CLI_NODE))
    )
    # These three executions are mandatory and independent, even after an
    # earlier node fails. Never substitute observer JSON for actual JUnit.
    experiment("fixed-kcalc-node", KCALC_NODE, gid, timeout=155)
    experiment("fixed-cli-s5-051", CLI_NODE, gid, timeout=155)
    experiment("fixed-kcalc-shard2", None, gid, timeout=1500, pytest_args=KCALC_SHARD)
    experiments = {}
    for case, node, expected, suite in (
            ("fixed-kcalc-node", KCALC_NODE,
             {"collected": [KCALC_NODE], "selected": [KCALC_NODE], "collection_errors": 0}, False),
            ("fixed-cli-s5-051", CLI_NODE,
             {"collected": [CLI_NODE], "selected": [CLI_NODE], "collection_errors": 0}, False),
            ("fixed-kcalc-shard2", KCALC_SHARD, expected_shard, True)):
        experiments[case] = {
            "node": node, "artifact_directory": str(OUT / case), "attempted": True,
            "passed": result_passed(case, expected, suite=suite),
        }
    all_passed = collection_ok and all(item["passed"] for item in experiments.values())
    save(OUT / "current-failure-experiments.json", {
        "status": "completed" if all_passed else "failed",
        "passed": all_passed, "source_run": SOURCE_RUN,
        "source_head_sha": SOURCE_SHA, "loaded_image_id": EXPECTED_IMAGE_ID,
        "baseline_failure_evidence_run": 39297094218,
        "baseline_failures_replayed": False,
        "collection_matches_image": collection_ok,
        "full_collected": len(expected_full["collected"]),
        "full_not_slow_selected": len(expected_full["selected"]),
        "shard2_selected": len(expected_shard["selected"]),
        "experiments": experiments,
        "unsupported_node_outcomes": ["failure", "error", "skipped"],
        "shard_skips": "Only original image strict-skip policy known limits/xfails qualify",
        "observations_are_test_completion": False,
        "pr_7x5_validation": "pending; parent-owned",
        "diagnostic_scope": (
            "Only visual_harness.py, interaction_probe.py, test_cli_surface.py are "
            "readonly-overlaid. Image products and other tests remain original. "
            "Boundary observers run on the two nodes only; the shard uses the "
            "evidence plugin without AutomationEngine/CLI wrappers."),
    })
    if not all_passed:
        raise RuntimeError("Fixed KCalc, CLI, full shard 2, and image collection must all pass")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        OUT.mkdir(exist_ok=True)
        save(OUT / "failure.json", {"type": type(error).__name__, "message": str(error)})
        print(f"Probe failed: {error}", file=sys.stderr)
        sys.exit(1)
    finally:
        save(OUT / "exit-codes.json", RESULTS)
        if LEDGER.exists():
            (OUT / "cleanup-ledger.jsonl").write_bytes(LEDGER.read_bytes())
