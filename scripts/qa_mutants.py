#!/usr/bin/env python3
"""Mutation gate (G14, QA-PKG-012): every mutant in ``tests/mutants.toml`` must be killed.

Each mutant is a search/replace patch of the current source taken from the QA list's mutation
plan (M01-M82). For every selected mutant the script applies the patch to a temporary copy of
the repository, runs the mutant's killing tests and records whether at least one of them fails.

* ``--tier unit`` runs ``pytest`` on the killing ``tests/unit`` node ids with the mutated
  ``src`` first on ``PYTHONPATH``; it needs no KWin and no image.
* ``--tier e2e`` builds the Debian E2E image once (``docker/e2e.Dockerfile``), then runs the
  killing ``tests/e2e`` node ids in a fresh container per mutant with the mutated ``kwin_mcp``
  package mounted over the installed one. A mutant that patches files outside
  ``src/kwin_mcp`` (packaging metadata) gets its own image built from the mutated copy.

Before any mutant runs, every patch must apply (its search text occurs exactly once) and
every patched Python file must still compile; otherwise the script fails loudly with exit 2.
A baseline run on the unmutated copy must pass every killing test; a mutant whose killers
are missing, failing or only skipped without mutation is ``invalid``.

Statuses: ``killed`` (a killing test failed or errored, or the run timed out), ``survived``
(every killing test passed), ``invalid`` (see above). The JSON report and a Markdown summary
(same stem, ``.md``) are written to ``--report``. Exit status: 0 when every selected mutant is
killed, 1 when any mutant survived, 2 for invalid mutants or a broken catalogue.

Examples::

    python3 scripts/qa_mutants.py --check
    python3 scripts/qa_mutants.py --tier unit --only M01,M05
    python3 scripts/qa_mutants.py --tier e2e --image kwin-mcp-e2e:mutants
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = REPO_ROOT / "tests" / "mutants.toml"
DEFAULT_REPORT = REPO_ROOT / "artifacts" / "mutants" / "report.json"
DOCKERFILE = "docker/e2e.Dockerfile"
PACKAGE_DIR = "src/kwin_mcp"
IMAGE_PYTHON = "/opt/kwin-mcp-venv/bin/python"
IMAGE_APP = "/app"
TIERS = ("unit", "e2e")
TIER_DIRS = {"unit": "tests/unit/", "e2e": "tests/e2e/"}
MUTANT_ID = re.compile(r"M\d{2}")
NODE = re.compile(r"(tests/[\w/]+\.py)::(test\w*)")
# Never copied into the scratch repository: VCS data, local environments, outputs, caches.
COPY_IGNORE = shutil.ignore_patterns(
    ".git",
    ".venv",
    "artifacts",
    "dist",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".codegraph",
)


class CatalogueError(Exception):
    """``tests/mutants.toml`` is malformed or a patch does not apply to the current source."""


@dataclass(frozen=True)
class Patch:
    file: str
    search: str
    replace: str


@dataclass(frozen=True)
class Mutant:
    id: str
    module: str
    mutation: str
    qa: tuple[str, ...]
    tier: str
    kill: tuple[str, ...]
    patches: tuple[Patch, ...]

    @property
    def rebuild(self) -> bool:
        """E2E mutants outside the package need an image built from the mutated copy."""
        return any(not patch.file.startswith(f"{PACKAGE_DIR}/") for patch in self.patches)


@dataclass
class Outcome:
    """Per-test results of one pytest run, read from its junit XML."""

    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    exit_code: int | None = None
    timed_out: bool = False
    log_tail: str = ""


# --------------------------------------------------------------------------- catalogue


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CatalogueError(message)


def load_catalogue(path: Path = CATALOGUE) -> list[Mutant]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw = data.get("mutant")
    _require(isinstance(raw, list) and bool(raw), f"{path}: no [[mutant]] entries")
    mutants: list[Mutant] = []
    seen: set[str] = set()
    for entry in raw:
        mutant_id = entry.get("id")
        _require(
            isinstance(mutant_id, str) and bool(MUTANT_ID.fullmatch(mutant_id)),
            f"bad mutant id {mutant_id!r}",
        )
        _require(mutant_id not in seen, f"duplicate mutant id {mutant_id}")
        seen.add(mutant_id)
        tier = entry.get("tier")
        _require(tier in TIERS, f"{mutant_id}: tier must be one of {TIERS}, not {tier!r}")
        kill = entry.get("kill")
        _require(
            isinstance(kill, list) and bool(kill),
            f"{mutant_id}: kill must list test node ids",
        )
        for node in kill:
            _require(
                isinstance(node, str) and bool(NODE.fullmatch(node)),
                f"{mutant_id}: kill node {node!r} is not tests/…/file.py::test_name",
            )
            _require(
                node.startswith(TIER_DIRS[tier]),
                f"{mutant_id}: kill node {node} is outside the {tier} tier",
            )
        qa = entry.get("qa")
        _require(isinstance(qa, list) and bool(qa), f"{mutant_id}: qa must list QA ids")
        patches_raw = entry.get("patch")
        _require(
            isinstance(patches_raw, list) and bool(patches_raw),
            f"{mutant_id}: needs at least one [[mutant.patch]]",
        )
        patches: list[Patch] = []
        for patch in patches_raw:
            file, search, replace = patch.get("file"), patch.get("search"), patch.get("replace")
            _require(
                isinstance(file, str) and isinstance(search, str) and isinstance(replace, str),
                f"{mutant_id}: a patch needs string file/search/replace",
            )
            _require(bool(search), f"{mutant_id}: empty search text in {file}")
            _require(search != replace, f"{mutant_id}: patch for {file} changes nothing")
            patches.append(Patch(file=file, search=search, replace=replace))
        mutants.append(
            Mutant(
                id=mutant_id,
                module=str(entry.get("module", "")),
                mutation=str(entry.get("mutation", "")),
                qa=tuple(qa),
                tier=tier,
                kill=tuple(kill),
                patches=tuple(patches),
            )
        )
    return mutants


def mutated_texts(mutant: Mutant, root: Path) -> dict[str, str]:
    """The patched contents of every file ``mutant`` touches, read from ``root``.

    Raises ``CatalogueError`` unless every search text occurs exactly once (patches to the
    same file apply in order) and every patched Python file still compiles.
    """
    texts: dict[str, str] = {}
    for patch in mutant.patches:
        path = root / patch.file
        _require(path.is_file(), f"{mutant.id}: {patch.file} does not exist")
        text = texts.get(patch.file)
        if text is None:
            text = path.read_text(encoding="utf-8")
        count = text.count(patch.search)
        _require(
            count == 1,
            f"{mutant.id}: search text occurs {count} times in {patch.file} (needs exactly 1):"
            f"\n{patch.search}",
        )
        texts[patch.file] = text.replace(patch.search, patch.replace, 1)
    for file, text in texts.items():
        if file.endswith(".py"):
            try:
                compile(text, file, "exec")
            except SyntaxError as error:
                message = f"{mutant.id}: mutated {file} does not compile: {error}"
                raise CatalogueError(message) from error
    return texts


def check_catalogue(mutants: list[Mutant], root: Path) -> list[str]:
    problems: list[str] = []
    for mutant in mutants:
        try:
            mutated_texts(mutant, root)
        except CatalogueError as error:
            problems.append(str(error))
    return problems


# --------------------------------------------------------------------------- pytest runs


def _junit_outcome(path: Path) -> Outcome:
    outcome = Outcome()
    if not path.is_file():
        return outcome
    for case in ElementTree.parse(path).getroot().iter("testcase"):
        # xunit2 junit has no file attribute: classname is the dotted module path followed
        # by any (capitalized) test class names.
        parts = case.get("classname", "").split(".")
        modules = [part for part in parts if not part[:1].isupper()]
        label = f"{'/'.join(modules)}.py::{case.get('name', '')}"
        tags = {child.tag for child in case}
        if tags & {"failure", "error"}:
            outcome.failed.append(label)
        elif "skipped" in tags:
            outcome.skipped.append(label)
        else:
            outcome.passed.append(label)
    return outcome


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None, timeout: float) -> Outcome:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        tail = output if isinstance(output, str) else output.decode(errors="replace")
        return Outcome(timed_out=True, log_tail=tail[-4000:])
    outcome = Outcome(exit_code=completed.returncode)
    outcome.log_tail = (completed.stdout + completed.stderr)[-4000:]
    return outcome


def _pytest_args(nodes: list[str], junit: str, *, exitfirst: bool) -> list[str]:
    first = ["-x"] if exitfirst else []
    return ["-m", "pytest", "-p", "no:cacheprovider", "-q", *first, f"--junitxml={junit}", *nodes]


def _function_key(node: str) -> str:
    """``tests/x.py::TestA::test_b[p]`` -> ``tests/x.py::test_b``."""
    path, _, rest = node.partition("::")
    return f"{path}::{rest.split('[', 1)[0].split('::')[-1]}"


def _runnable(node: str) -> str:
    """``tests/x.py::TestA::test_b[p]`` -> ``tests/x.py::TestA::test_b`` (all parameters)."""
    return node.split("[", 1)[0]


def _parse_collection(output: str) -> dict[str, set[str]]:
    collected: dict[str, set[str]] = {}
    for line in output.splitlines():
        node = line.strip()
        if node.startswith("tests/") and "::" in node:
            collected.setdefault(_function_key(node), set()).add(_runnable(node))
    return collected


class Runner:
    """Runs pytest for one tier against the scratch copy at ``root``."""

    def __init__(self, tier: str, root: Path, work: Path, args: argparse.Namespace) -> None:
        self.tier = tier
        self.root = root
        self.work = work
        self.args = args
        self.image = args.image
        self.package_path = ""

    # -- unit tier: the local interpreter with the mutated src first on sys.path
    def _unit_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(self.root / "src"), *filter(None, [env.get("PYTHONPATH", "")])]
        )
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    # -- e2e tier: one container per run, mutated package mounted over the installed one
    def prepare(self) -> None:
        if self.tier != "e2e":
            probe = subprocess.run(
                [
                    self.args.python,
                    "-c",
                    "import kwin_mcp, pathlib; print(pathlib.Path(kwin_mcp.__file__).parent)",
                ],
                cwd=self.root,
                env=self._unit_env(),
                capture_output=True,
                text=True,
                check=False,
            )
            expected = (self.root / PACKAGE_DIR).resolve()
            located = probe.stdout.strip()
            if probe.returncode != 0 or not located or Path(located).resolve() != expected:
                raise RuntimeError(
                    f"{self.args.python} does not import kwin_mcp from the scratch copy "
                    f"({probe.stdout.strip() or probe.stderr.strip()})"
                )
            return
        if not self.args.no_build:
            build_image(REPO_ROOT, self.image)
        probe = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                IMAGE_PYTHON,
                self.image,
                "-c",
                "import kwin_mcp, pathlib; print(pathlib.Path(kwin_mcp.__file__).parent)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.package_path = probe.stdout.strip()
        if not self.package_path.startswith("/"):
            raise RuntimeError(f"cannot locate kwin_mcp in {self.image}: {probe.stdout!r}")

    def _docker(
        self, image: str, out_dir: Path, pytest_args: list[str], *, overlay: bool
    ) -> list[str]:
        command = ["docker", "run", "--rm", "--env", "PYTHONDONTWRITEBYTECODE=1"]
        for name in ("KWIN_MCP_E2E_STRICT_SKIPS", "KWIN_MCP_E2E_REQUIRE"):
            command += ["--env", f"{name}={getattr(self.args, name.lower())}"]
        command += ["--volume", f"{out_dir}:/artifacts"]
        command += ["--volume", f"{self.root / 'tests'}:{IMAGE_APP}/tests:ro"]
        if overlay:
            command += ["--volume", f"{self.root / PACKAGE_DIR}:{self.package_path}:ro"]
        return [*command, image, IMAGE_PYTHON, *pytest_args]

    def collect(self) -> dict[str, set[str]]:
        tier_dir = TIER_DIRS[self.tier].rstrip("/")
        args = [
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--collect-only",
            "--continue-on-collection-errors",
            "-q",
            tier_dir,
        ]
        if self.tier == "unit":
            outcome = subprocess.run(
                [self.args.python, *args],
                cwd=self.root,
                env=self._unit_env(),
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            out_dir = self._out_dir("collect")
            command = self._docker(self.image, out_dir, args, overlay=True)
            command[3:3] = ["--env", "KWIN_MCP_ARTIFACT_DIR="]
            outcome = subprocess.run(command, capture_output=True, text=True, check=False)
        collected = _parse_collection(outcome.stdout)
        if outcome.returncode not in (0, 5) and not collected:
            raise RuntimeError(f"collecting {tier_dir} failed:\n{outcome.stdout}{outcome.stderr}")
        return collected

    def _out_dir(self, label: str) -> Path:
        out_dir = self.work / f"{self.tier}-{label}"
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        out_dir.chmod(0o777)  # the image runs as UID 1000
        return out_dir

    def run(
        self, label: str, nodes: list[str], *, exitfirst: bool, image: str | None = None
    ) -> Outcome:
        out_dir = self._out_dir(label)
        junit = out_dir / "junit.xml"
        if self.tier == "unit":
            command = [self.args.python, *_pytest_args(nodes, str(junit), exitfirst=exitfirst)]
            env: dict[str, str] | None = self._unit_env()
        else:
            pytest_args = _pytest_args(nodes, "/artifacts/junit.xml", exitfirst=exitfirst)
            command = self._docker(image or self.image, out_dir, pytest_args, overlay=image is None)
            env = None
        outcome = _run(command, cwd=self.root, env=env, timeout=self.args.timeout)
        parsed = _junit_outcome(junit)
        parsed.exit_code = outcome.exit_code
        parsed.timed_out = outcome.timed_out
        parsed.log_tail = outcome.log_tail
        return parsed


def build_image(context: Path, tag: str) -> None:
    print(f"building {tag} from {context}", file=sys.stderr, flush=True)
    subprocess.run(
        ["docker", "build", "--file", str(context / DOCKERFILE), "--tag", tag, str(context)],
        check=True,
    )


# --------------------------------------------------------------------------- main loop


def _apply(root: Path, texts: dict[str, str]) -> dict[str, str]:
    originals: dict[str, str] = {}
    for file, text in texts.items():
        path = root / file
        originals[file] = path.read_text(encoding="utf-8")
        path.write_text(text, encoding="utf-8")
    return originals


def _restore(root: Path, originals: dict[str, str]) -> None:
    for file, text in originals.items():
        (root / file).write_text(text, encoding="utf-8")


def run_tier(
    tier: str, mutants: list[Mutant], root: Path, work: Path, args: argparse.Namespace
) -> list[dict[str, Any]]:
    runner = Runner(tier, root, work, args)
    runner.prepare()
    collected = runner.collect()
    results: list[dict[str, Any]] = []
    runnable: dict[str, list[str]] = {}
    missing: dict[str, list[str]] = {}
    for mutant in mutants:
        runnable[mutant.id] = sorted({n for key in mutant.kill for n in collected.get(key, set())})
        missing[mutant.id] = [key for key in mutant.kill if key not in collected]

    baseline_nodes = sorted({node for nodes in runnable.values() for node in nodes})
    print(f"[{tier}] baseline: {len(baseline_nodes)} killing tests", file=sys.stderr, flush=True)
    baseline = Outcome(exit_code=5)
    if baseline_nodes:
        baseline = runner.run("baseline", baseline_nodes, exitfirst=False)
        ran = baseline.passed or baseline.failed or baseline.skipped
        if baseline.timed_out or baseline.exit_code not in (0, 1) or not ran:
            raise RuntimeError(
                f"[{tier}] baseline run did not complete (exit {baseline.exit_code}):\n"
                f"{baseline.log_tail}"
            )
    broken_keys = {_function_key(label) for label in baseline.failed}
    passing_keys = {_function_key(label) for label in baseline.passed}

    for mutant in mutants:
        record: dict[str, Any] = {
            "id": mutant.id,
            "module": mutant.module,
            "mutation": mutant.mutation,
            "qa": list(mutant.qa),
            "tier": tier,
            "kill": list(mutant.kill),
        }
        problems: list[str] = []
        if missing[mutant.id]:
            problems.append(f"killing tests not found: {missing[mutant.id]}")
        if broken := sorted(set(mutant.kill) & broken_keys):
            problems.append(f"killing tests fail without the mutation: {broken}")
        if not problems and not set(mutant.kill) & passing_keys:
            problems.append("no killing test passes without the mutation (all skipped)")
        if problems:
            record.update(status="invalid", detail="; ".join(problems))
            results.append(record)
            print(f"[{tier}] {mutant.id}: invalid ({record['detail']})", file=sys.stderr)
            continue
        started = time.monotonic()
        originals = _apply(root, mutated_texts(mutant, root))
        image: str | None = None
        try:
            if tier == "e2e" and mutant.rebuild:
                image = f"{args.image.split(':', 1)[0]}:mutant-{mutant.id.lower()}"
                build_image(root, image)
            outcome = runner.run(mutant.id, runnable[mutant.id], exitfirst=True, image=image)
        finally:
            _restore(root, originals)
            if image is not None:
                subprocess.run(
                    ["docker", "image", "rm", "--force", image], capture_output=True, check=False
                )
        record["duration_s"] = round(time.monotonic() - started, 1)
        record["failed_tests"] = outcome.failed
        if outcome.failed:
            record.update(status="killed", detail=f"failed: {outcome.failed[0]}")
        elif outcome.timed_out:
            record.update(status="killed", detail=f"timed out after {args.timeout} s")
        elif outcome.exit_code in (2, 3):
            # pytest was interrupted or crashed before reporting a test: the mutated package
            # broke collection (an import-time failure counts as detection).
            record.update(status="killed", detail=f"pytest exit {outcome.exit_code} (collection)")
        elif outcome.exit_code == 0 and outcome.passed:
            record.update(status="survived", detail=f"{len(outcome.passed)} killing tests passed")
        else:
            record.update(
                status="invalid",
                detail=f"no verdict (exit {outcome.exit_code}): {outcome.log_tail[-400:]}",
            )
        print(f"[{tier}] {mutant.id}: {record['status']} ({record['detail']})", file=sys.stderr)
        results.append(record)
    return results


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = report["summary"]
    lines = [
        "# QA mutation report",
        "",
        f"Tiers: {', '.join(report['tiers'])}; killed {summary['killed']}, "
        f"survived {summary['survived']}, invalid {summary['invalid']}.",
        "",
        "| id | tier | status | QA ids | detail |",
        "|---|---|---|---|---|",
    ]
    for record in report["mutants"]:
        detail = str(record["detail"]).replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {record['id']} | {record['tier']} | {record['status']} | "
            f"{', '.join(record['qa'])} | {detail[:300]} |"
        )
    path.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tier", choices=(*TIERS, "all"), default="all")
    parser.add_argument("--only", default="", help="comma-separated mutant ids (default: all)")
    parser.add_argument("--catalogue", type=Path, default=CATALOGUE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--check", action="store_true", help="only verify that every patch applies and compiles"
    )
    parser.add_argument("--python", default=sys.executable, help="interpreter for the unit tier")
    parser.add_argument("--image", default="kwin-mcp-e2e:mutants", help="E2E image tag")
    parser.add_argument("--no-build", action="store_true", help="reuse an existing --image")
    parser.add_argument(
        "--timeout", type=float, default=1800.0, help="seconds per pytest run (default 1800)"
    )
    parser.add_argument(
        "--kwin-mcp-e2e-strict-skips",
        default="1",
        help="KWIN_MCP_E2E_STRICT_SKIPS passed to E2E containers",
    )
    parser.add_argument(
        "--kwin-mcp-e2e-require",
        default="layer-shell",
        help="KWIN_MCP_E2E_REQUIRE passed to E2E containers",
    )
    args = parser.parse_args(argv)

    try:
        mutants = load_catalogue(args.catalogue)
    except (CatalogueError, tomllib.TOMLDecodeError) as error:
        print(f"{args.catalogue}: {error}", file=sys.stderr)
        return 2
    problems = check_catalogue(mutants, REPO_ROOT)
    if problems:
        print("mutant catalogue does not apply to the current source:", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        return 2
    if args.check:
        print(f"{len(mutants)} mutants apply cleanly")
        return 0

    only = {item.strip() for item in args.only.split(",") if item.strip()}
    unknown = only - {mutant.id for mutant in mutants}
    if unknown:
        print(f"unknown mutant ids: {sorted(unknown)}", file=sys.stderr)
        return 2
    tiers = TIERS if args.tier == "all" else (args.tier,)
    selected = [m for m in mutants if m.tier in tiers and (not only or m.id in only)]
    if not selected:
        print("no mutants selected", file=sys.stderr)
        return 2

    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="kwin-mcp-mutants-") as scratch:
        work = Path(scratch)
        work.chmod(0o755)  # E2E containers run as UID 1000 and read the copy through mounts
        root = work / "repo"
        shutil.copytree(REPO_ROOT, root, ignore=COPY_IGNORE, symlinks=True)
        try:
            for tier in tiers:
                tier_mutants = [m for m in selected if m.tier == tier]
                if tier_mutants:
                    results += run_tier(tier, tier_mutants, root, work, args)
        except (RuntimeError, subprocess.CalledProcessError) as error:
            print(f"mutation run aborted: {error}", file=sys.stderr)
            return 2

    summary = {
        status: sum(1 for r in results if r["status"] == status)
        for status in ("killed", "survived", "invalid")
    }
    report = {
        "tiers": list(tiers),
        "image": args.image if "e2e" in tiers else None,
        "summary": summary,
        "survivors": [r["id"] for r in results if r["status"] == "survived"],
        "mutants": sorted(results, key=lambda r: r["id"]),
    }
    write_report(args.report, report)
    print(json.dumps({"report": str(args.report), **summary}))
    if summary["survived"]:
        return 1
    return 2 if summary["invalid"] else 0


if __name__ == "__main__":
    sys.exit(main())
