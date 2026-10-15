"""Suite policy: sharding, the strict-skip guard and the test-tree layout."""

from __future__ import annotations

import ast
import json
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _suite_policy import (
    FIRST_TEST,
    KNOWN_LIMIT_PREFIXES,
    PLANNED_TEST_FILES,
    assign_shards,
    default_weight,
    is_known_limit,
    parse_shard,
    require_or_skip,
    required_capabilities,
    strict_skips_enabled,
    unit_key,
)
from tool_surface import REMOVED_TOOL_NAMES, TOOL_NAMES

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS = REPO_ROOT / "tests"
DURATIONS = TESTS / "e2e" / "durations.json"
RUN_E2E_DOCKER = REPO_ROOT / "scripts" / "run-e2e-docker.sh"


# Modules that serve as independent oracles must not share code with the product.
ORACLE_MODULES = ("kwin_oracle.py", "atspi_oracle.py", "window_zoo.py", "kwin_rules.py")


def _loads(owner: dict[str, int], count: int, durations: dict[str, float]) -> list[float]:
    fallback = default_weight(durations)
    loads = [0.0] * count
    for unit, shard in owner.items():
        loads[shard - 1] += durations.get(unit, fallback)
    return loads


# ── assign_shards ────────────────────────────────────────────────────────


def test_assign_shards_partitions_every_file_exactly_once() -> None:
    durations = json.loads(DURATIONS.read_text(encoding="utf-8"))
    files = [*sorted(durations), "tests/e2e/test_not_in_the_table.py"]
    for count in (1, 2, 3, 5):
        owner = assign_shards(files, durations, count)
        assert sorted(owner) == sorted(files)
        assert set(owner.values()) <= set(range(1, count + 1))
        # Every shard receives work when there is enough of it.
        assert set(owner.values()) == set(range(1, count + 1))


def test_assign_shards_is_deterministic_and_order_independent() -> None:
    durations = {f"tests/e2e/test_{i}.py": float(i % 7) + 0.5 for i in range(40)}
    files = list(durations)
    expected = assign_shards(files, durations, 3)
    rng = random.Random(1234)
    for _ in range(20):
        shuffled = files[:]
        rng.shuffle(shuffled)
        assert assign_shards(shuffled + shuffled[:3], durations, 3) == expected


def test_assign_shards_balances_within_the_largest_file() -> None:
    durations = json.loads(DURATIONS.read_text(encoding="utf-8"))
    largest = max(durations.values())
    for count in (2, 3, 4):
        loads = _loads(assign_shards(durations, durations, count), count, durations)
        assert max(loads) - min(loads) <= largest, loads


def test_assign_shards_heaviest_first_ties_to_lowest_index() -> None:
    durations = {"a.py": 5.0, "b.py": 5.0, "c.py": 3.0, "d.py": 1.0}
    assert assign_shards(durations, durations, 2) == {"a.py": 1, "b.py": 2, "c.py": 1, "d.py": 2}


def test_assign_shards_unknown_files_weigh_the_median() -> None:
    durations = {"a.py": 1.0, "b.py": 2.0, "c.py": 30.0}
    assert default_weight(durations) == 2.0
    assert default_weight({}) == 1.0
    # The unknown file weighs 2.0, so it joins the lighter shard holding a.py
    # and b.py (3.0) rather than c.py (30.0).
    owner = assign_shards([*durations, "new.py"], durations, 2)
    assert owner["c.py"] != owner["new.py"]


def test_assign_shards_pins_units_to_shard_one() -> None:
    durations = {"big.py": 100.0, "oracles.py": 1.0, "other.py": 1.0}
    owner = assign_shards(durations, durations, 2, pinned=("oracles.py", "absent.py"))
    assert owner["oracles.py"] == 1
    assert owner["big.py"] == 2
    assert "absent.py" not in owner


def test_first_test_file_is_pinned_by_conftest_and_exists() -> None:
    path, _, name = FIRST_TEST.partition("::")
    source = (REPO_ROOT / path).read_text(encoding="utf-8")
    assert re.search(rf"^(?:async )?def {re.escape(name)}\(", source, re.MULTILINE), FIRST_TEST


def test_assign_shards_rejects_a_non_positive_count() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        assign_shards(["a.py"], {}, 0)


def test_unit_key_uses_class_keys_only_when_listed() -> None:
    durations = {"tests/e2e/test_x.py::TestSlow": 50.0}
    assert unit_key("tests/e2e/test_x.py", "TestSlow", durations) == (
        "tests/e2e/test_x.py::TestSlow"
    )
    assert unit_key("tests/e2e/test_x.py", "TestOther", durations) == "tests/e2e/test_x.py"
    assert unit_key("tests/e2e/test_x.py", None, durations) == "tests/e2e/test_x.py"


@pytest.mark.parametrize(
    ("text", "expected"), [("1/1", (1, 1)), ("1/2", (1, 2)), ("2/2", (2, 2)), ("3/3", (3, 3))]
)
def test_parse_shard_accepts_valid_values(text: str, expected: tuple[int, int]) -> None:
    assert parse_shard(text) == expected


@pytest.mark.parametrize("text", ["", "1", "0/2", "3/2", "1/0", "-1/2", "a/b", "1/2/3", " 1/2"])
def test_parse_shard_rejects_invalid_values(text: str) -> None:
    with pytest.raises(ValueError, match="--kwin-shard"):
        parse_shard(text)


# ── strict skips ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("prefix", KNOWN_LIMIT_PREFIXES)
def test_is_known_limit_accepts_each_registered_prefix(prefix: str) -> None:
    assert is_known_limit(prefix)
    assert is_known_limit(f"{prefix}: with details")


@pytest.mark.parametrize("prefix", KNOWN_LIMIT_PREFIXES)
def test_is_known_limit_rejects_near_misses(prefix: str) -> None:
    assert not is_known_limit(f" {prefix}")
    assert not is_known_limit(prefix.upper() if prefix != prefix.upper() else prefix.lower())
    assert not is_known_limit(prefix[:-1] + "#")


@pytest.mark.parametrize(
    "reason",
    ["", "could not import 'gi': No module named 'gi'", "flaky on CI", "Skipped: unknown"],
)
def test_is_known_limit_rejects_unregistered_reasons(reason: str) -> None:
    assert not is_known_limit(reason)


def test_known_limit_prefixes_are_unique_and_not_nested() -> None:
    assert len(set(KNOWN_LIMIT_PREFIXES)) == len(KNOWN_LIMIT_PREFIXES)
    for a in KNOWN_LIMIT_PREFIXES:
        for b in KNOWN_LIMIT_PREFIXES:
            assert a == b or not b.startswith(a), (a, b)


def test_strict_skips_and_required_capabilities_parse_the_environment() -> None:
    assert strict_skips_enabled({"KWIN_MCP_E2E_STRICT_SKIPS": "1"})
    assert not strict_skips_enabled({"KWIN_MCP_E2E_STRICT_SKIPS": "0"})
    assert not strict_skips_enabled({})
    assert required_capabilities({"KWIN_MCP_E2E_REQUIRE": " layer-shell, ,xwayland "}) == {
        "layer-shell",
        "xwayland",
    }
    assert required_capabilities({}) == frozenset()


def test_require_or_skip_skips_fails_or_rejects() -> None:
    reason = "known KWin limit: layer-shell surface unavailable: typelib missing"
    with pytest.raises(pytest.skip.Exception, match="layer-shell surface unavailable"):
        require_or_skip("layer-shell", reason, {})
    with pytest.raises(pytest.fail.Exception, match="required capability 'layer-shell'"):
        require_or_skip("layer-shell", reason, {"KWIN_MCP_E2E_REQUIRE": "layer-shell"})
    with pytest.raises(ValueError, match="not a registered known limit"):
        require_or_skip("layer-shell", "typelib missing", {})


def _scratch_suite(tmp_path: Path, files: dict[str, str], durations: dict[str, float]) -> Path:
    """A throwaway repository holding the real suite hooks and the given test files."""
    root = tmp_path / "repo"
    (root / "tests" / "e2e").mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\naddopts = -p no:cacheprovider\n", encoding="utf-8")
    shutil.copy(TESTS / "conftest.py", root / "tests" / "conftest.py")
    shutil.copy(TESTS / "_suite_policy.py", root / "tests" / "_suite_policy.py")
    (root / "tests" / "e2e" / "durations.json").write_text(json.dumps(durations), encoding="utf-8")
    for name, source in files.items():
        (root / name).write_text(source, encoding="utf-8")
    return root


def _run_pytest(
    root: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environ = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "KWIN_MCP_E2E"))}
    environ.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "pytest", "tests", *args],
        cwd=root,
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


_SKIPPING_TESTS = """\
import pytest

def test_registered():
    pytest.skip("root ignores directory permissions")

def test_unregistered():
    pytest.skip("flaky on this distro")

@pytest.mark.skipif(True, reason="set KWIN_MCP_E2E_SCREENSHOT=1 to run: reason")
def test_registered_mark():
    pass

def test_passes():
    pass
"""

_IMPORTORSKIP_TESTS = """\
import pytest

pytest.importorskip("kwin_mcp_module_that_does_not_exist")

def test_never_collected():
    pass
"""


def test_strict_skips_fail_unregistered_reasons_only(tmp_path: Path) -> None:
    root = _scratch_suite(
        tmp_path,
        {
            "tests/e2e/test_skips.py": _SKIPPING_TESTS,
            "tests/e2e/test_importorskip.py": _IMPORTORSKIP_TESTS,
        },
        {},
    )
    strict = _run_pytest(
        root,
        "-rA",
        "--continue-on-collection-errors",
        env={"KWIN_MCP_E2E_STRICT_SKIPS": "1"},
    )
    assert strict.returncode == 1, strict.stdout
    assert "unregistered skip: flaky on this distro" in strict.stdout, strict.stdout
    assert "unregistered skip: could not import" in strict.stdout, strict.stdout
    assert re.search(r"\b2 skipped\b", strict.stdout), strict.stdout
    assert re.search(r"\b1 passed\b", strict.stdout), strict.stdout

    relaxed = _run_pytest(root, "-rA")
    assert relaxed.returncode == 0, relaxed.stdout
    assert "unregistered skip" not in relaxed.stdout, relaxed.stdout


def test_kwin_shard_partitions_the_collection_and_orders_the_layer_shell_check_first(
    tmp_path: Path,
) -> None:
    files = {
        f"tests/e2e/test_m{i}.py": "".join(f"def test_{j}():\n    pass\n" for j in range(i + 1))
        for i in range(6)
    }
    first_path, _, first_name = FIRST_TEST.partition("::")
    files[first_path] = f"def test_before():\n    pass\n\ndef {first_name}():\n    pass\n"
    durations = {f"tests/e2e/test_m{i}.py": float(10 * (i + 1)) for i in range(6)}
    root = _scratch_suite(tmp_path, files, durations)

    def collected(*args: str) -> list[str]:
        result = _run_pytest(root, "--collect-only", "-q", *args)
        assert result.returncode == 0, result.stdout + result.stderr
        return [line for line in result.stdout.splitlines() if line.startswith("tests/")]

    full = collected()
    shard1 = collected("--kwin-shard=1/2")
    shard2 = collected("--kwin-shard=2/2")
    assert sorted(shard1 + shard2) == sorted(full)
    assert not set(shard1) & set(shard2)
    assert shard1[0] == FIRST_TEST
    assert full[0] == FIRST_TEST
    # A file never straddles two shards.
    files1 = {node.partition("::")[0] for node in shard1}
    files2 = {node.partition("::")[0] for node in shard2}
    assert not files1 & files2

    bad = _run_pytest(root, "--collect-only", "-q", "--kwin-shard=3/2")
    assert bad.returncode == 4, bad.stdout + bad.stderr
    assert "--kwin-shard expects 1 <= I <= N" in bad.stderr


# ── tree layout ──────────────────────────────────────────────────────────


def test_module_basenames_are_unique_across_tests() -> None:
    seen: dict[str, Path] = {}
    for path in sorted(TESTS.rglob("*.py")):
        if path.name in {"conftest.py", "__init__.py"} or "__pycache__" in path.parts:
            continue
        assert path.stem not in seen, f"{path} and {seen[path.stem]} share a module basename"
        seen[path.stem] = path


def test_durations_json_is_a_sorted_flat_map_of_existing_or_planned_files() -> None:
    text = DURATIONS.read_text(encoding="utf-8")
    durations = json.loads(text)
    assert isinstance(durations, dict)
    assert list(durations) == sorted(durations)
    assert text == json.dumps(durations, indent=2) + "\n"
    for key, seconds in durations.items():
        path = key.partition("::")[0]
        assert path.startswith("tests/") and path.endswith(".py"), key
        assert (REPO_ROOT / path).is_file() or path in PLANNED_TEST_FILES, key
        assert isinstance(seconds, int | float) and seconds >= 0, key
        assert round(float(seconds), 1) == seconds, key


def test_planned_files_are_test_modules_under_tests() -> None:
    for path in PLANNED_TEST_FILES:
        assert re.fullmatch(r"tests/(e2e|unit)/test_[a-z0-9_]+\.py", path), path


@pytest.mark.parametrize("module", ORACLE_MODULES)
def test_oracle_modules_share_no_code_with_the_product(module: str) -> None:
    tree = ast.parse((TESTS / "e2e" / module).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
    assert not [name for name in imported if name.split(".")[0] == "kwin_mcp"], imported


def test_tool_surface_name_sets() -> None:
    assert len(TOOL_NAMES) == 29
    assert len(set(TOOL_NAMES)) == 29
    assert len(REMOVED_TOOL_NAMES) == 28
    assert len(set(REMOVED_TOOL_NAMES)) == 28
    assert not set(TOOL_NAMES) & set(REMOVED_TOOL_NAMES)


# ── runner and workflow wiring ───────────────────────────────────────────

_DOCKER_STUB = """\
#!/bin/sh
n=$(ls "$DOCKER_LOG_DIR" | wc -l | tr -d ' ')
printf '%s\\0' "$@" > "$DOCKER_LOG_DIR/$n"
exit 0
"""


def _run_e2e_docker(
    tmp_path: Path, *args: str, env: dict[str, str] | None = None
) -> list[list[str]]:
    """Run scripts/run-e2e-docker.sh against a stub docker; return each docker argv."""
    root = tmp_path / "repo"
    (root / "docker").mkdir(parents=True)
    (root / "docker" / "e2e.Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    (root / "tests" / "e2e").mkdir(parents=True)
    (root / "tests" / "e2e" / "test_x.py").write_text("", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(_DOCKER_STUB, encoding="utf-8")
    stub.chmod(0o755)
    log_dir = tmp_path / "docker-calls"
    log_dir.mkdir()
    environ = {k: v for k, v in os.environ.items() if not k.startswith(("KWIN_MCP_E2E", "DOCKER_"))}
    environ.update(
        {"PATH": f"{bin_dir}{os.pathsep}{environ.get('PATH', '')}", "DOCKER_LOG_DIR": str(log_dir)}
    )
    environ.update(env or {})
    result = subprocess.run(
        ["sh", str(RUN_E2E_DOCKER), *args],
        cwd=root,
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = sorted(log_dir.iterdir(), key=lambda p: int(p.name))
    return [p.read_bytes().decode().split("\0")[:-1] for p in calls]


def _pytest_run(calls: list[list[str]]) -> list[str]:
    runs = [argv for argv in calls if argv[:1] == ["run"] and "--name" in argv]
    assert len(runs) == 1, calls
    return runs[0]


@pytest.mark.qa("QA-PKG-014")
def test_run_e2e_docker_forwards_paths(tmp_path: Path) -> None:
    syntax = subprocess.run(
        ["sh", "-n", str(RUN_E2E_DOCKER)], capture_output=True, text=True, check=False
    )
    assert syntax.returncode == 0, syntax.stderr

    default = _pytest_run(_run_e2e_docker(tmp_path / "default"))
    assert default[-1] == "tests", default
    assert "--env" in default
    assert "KWIN_MCP_E2E_STRICT_SKIPS=1" in default
    assert "KWIN_MCP_E2E_REQUIRE=layer-shell" in default
    assert "KWIN_MCP_E2E_SCREENSHOT" in default

    node = "tests/e2e/test_x.py::test_y"
    named = _pytest_run(_run_e2e_docker(tmp_path / "named", "--", node, "-x"))
    assert named[-2:] == [node, "-x"], named
    assert "tests" not in named, named

    options = _pytest_run(
        _run_e2e_docker(
            tmp_path / "options",
            "--",
            "--deselect",
            node,
            "-k",
            "tests/e2e/test_x.py",
            "--kwin-shard=1/2",
        )
    )
    assert options[-6:] == [
        "tests",
        "--deselect",
        node,
        "-k",
        "tests/e2e/test_x.py",
        "--kwin-shard=1/2",
    ], options

    relaxed = _pytest_run(
        _run_e2e_docker(
            tmp_path / "relaxed",
            env={"KWIN_MCP_E2E_STRICT_SKIPS": "0", "KWIN_MCP_E2E_REQUIRE": "xwayland"},
        )
    )
    assert "KWIN_MCP_E2E_STRICT_SKIPS=0" in relaxed
    assert "KWIN_MCP_E2E_REQUIRE=xwayland" in relaxed
