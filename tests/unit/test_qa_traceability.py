"""QA traceability gate (G10): ``tests/qa_trace.toml`` and the test tree's QA markers agree.

The ledger is generated from the merged QA list by ``scripts/gen_qa_trace.py``. Test modules are
read with ``ast`` and never imported, so the gate needs neither KWin nor the test dependencies.
A marker is ``@pytest.mark.qa("QA-…")`` on a test function, on its ``Test*`` class, or in the
module's ``pytestmark``; ``slow`` is found the same way.
"""

from __future__ import annotations

import ast
import functools
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS = REPO_ROOT / "tests"
LEDGER = TESTS / "qa_trace.toml"
MUTANTS = TESTS / "mutants.toml"

AUTO_KINDS = frozenset({"unit", "e2e", "e2e-live", "e2e-mcp", "ci-gate", "manual"})
E2E_KINDS = frozenset({"e2e", "e2e-live", "e2e-mcp"})
CHECK_KINDS = frozenset({"pytest", "workflow-step", "checklist", "script"})
GATING = frozenset({"P0", "P1"})
QA_ID = re.compile(r"QA-[A-Z0-9]+-\d{3}")
MUTANT_ID = re.compile(r"M\d{2}")
MUTANT_COUNT = 82


@dataclass(frozen=True)
class MarkedTest:
    """One test function (or method) and the markers that apply to it."""

    path: str
    name: str
    line: int
    qa_ids: frozenset[str]
    slow: bool

    @property
    def node(self) -> str:
        return f"{self.path}::{self.name}"


@dataclass(frozen=True)
class Scan:
    tests: tuple[MarkedTest, ...]
    problems: tuple[str, ...]


def _mark_name(node: ast.expr) -> str | None:
    """``qa`` for ``pytest.mark.qa`` / ``mark.qa``; ``None`` for anything else."""
    if not isinstance(node, ast.Attribute):
        return None
    owner = node.value
    if isinstance(owner, ast.Attribute) and owner.attr == "mark":
        return node.attr
    if isinstance(owner, ast.Name) and owner.id == "mark":
        return node.attr
    return None


def _marks(expr: ast.expr, where: str, problems: list[str]) -> tuple[set[str], bool]:
    """The QA ids and the ``slow`` flag that the marker expression ``expr`` applies."""
    qa_ids: set[str] = set()
    slow = False
    called: set[int] = set()
    for node in ast.walk(expr):
        if isinstance(node, ast.Call) and _mark_name(node.func) is not None:
            called.add(id(node.func))
            name = _mark_name(node.func)
            if name == "qa":
                if (
                    len(node.args) != 1
                    or node.keywords
                    or not isinstance(node.args[0], ast.Constant)
                    or not isinstance(node.args[0].value, str)
                ):
                    problems.append(f"{where}: qa marker needs exactly one string literal id")
                    continue
                qa_id = node.args[0].value
                if not QA_ID.fullmatch(qa_id):
                    problems.append(f"{where}: malformed QA id {qa_id!r}")
                qa_ids.add(qa_id)
            elif name == "slow":
                slow = True
    for node in ast.walk(expr):
        if isinstance(node, ast.Attribute) and id(node) not in called:
            name = _mark_name(node)
            if name == "slow":
                slow = True
            elif name == "qa":
                problems.append(f"{where}: bare pytest.mark.qa without an id")
    return qa_ids, slow


def _decorator_marks(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, where: str, problems: list[str]
) -> tuple[set[str], bool]:
    qa_ids: set[str] = set()
    slow = False
    for decorator in node.decorator_list:
        ids, is_slow = _marks(decorator, where, problems)
        qa_ids |= ids
        slow = slow or is_slow
    return qa_ids, slow


def _pytestmark(body: list[ast.stmt], path: str, problems: list[str]) -> tuple[set[str], bool]:
    """Marks applied by a ``pytestmark = …`` assignment in a module or class body."""
    qa_ids: set[str] = set()
    slow = False
    for node in body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [], node.value
            targets.append(node.target)
        if value is not None and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets
        ):
            ids, is_slow = _marks(value, f"{path}:{node.lineno}", problems)
            qa_ids |= ids
            slow = slow or is_slow
    return qa_ids, slow


def _collect(
    body: list[ast.stmt],
    path: str,
    inherited: tuple[frozenset[str], bool],
    out: list[MarkedTest],
    problems: list[str],
) -> None:
    own_ids, own_slow = _pytestmark(body, path, problems)
    inherited_ids = inherited[0] | own_ids
    inherited_slow = inherited[1] or own_slow
    for node in body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            where = f"{path}:{node.lineno}"
            qa_ids, slow = _decorator_marks(node, where, problems)
            if not node.name.startswith("test"):
                if qa_ids or slow:
                    problems.append(f"{where}: marker on non-test function {node.name}")
                continue
            out.append(
                MarkedTest(
                    path=path,
                    name=node.name,
                    line=node.lineno,
                    qa_ids=inherited_ids | qa_ids,
                    slow=inherited_slow or slow,
                )
            )
        elif isinstance(node, ast.ClassDef):
            where = f"{path}:{node.lineno}"
            qa_ids, slow = _decorator_marks(node, where, problems)
            if not node.name.startswith("Test"):
                if qa_ids or slow:
                    problems.append(f"{where}: marker on non-test class {node.name}")
                continue
            marks = (inherited_ids | qa_ids, inherited_slow or slow)
            _collect(node.body, path, marks, out, problems)


@functools.cache
def scan() -> Scan:
    """Every test function in ``tests/**/test_*.py`` with its markers, read with ``ast`` only."""
    tests: list[MarkedTest] = []
    problems: list[str] = []
    for file in sorted(TESTS.rglob("test_*.py")):
        if "__pycache__" in file.parts:
            continue
        path = file.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=path)
        _collect(tree.body, path, (frozenset(), False), tests, problems)
    return Scan(tests=tuple(tests), problems=tuple(problems))


@functools.cache
def ledger() -> dict[str, dict[str, Any]]:
    data = tomllib.loads(LEDGER.read_text(encoding="utf-8"))
    assert data.get("schema") == 1, f"{LEDGER}: unknown schema {data.get('schema')!r}"
    items: dict[str, dict[str, Any]] = data["items"]
    return items


def traced_nodes(item: dict[str, Any]) -> list[str]:
    """The ``file::function`` node ids that must carry the item's marker."""
    nodes = [f"{item['file']}::{item['test']}"] if item["check"] == "pytest" else []
    return nodes + list(item["extra_tests"])


def _tests_by_node() -> dict[str, list[MarkedTest]]:
    index: dict[str, list[MarkedTest]] = {}
    for test in scan().tests:
        index.setdefault(test.node, []).append(test)
    return index


def _served_items() -> dict[str, set[str]]:
    """Node id -> the ledger ids whose traced nodes name it."""
    served: dict[str, set[str]] = {}
    for qa_id, item in ledger().items():
        for node in traced_nodes(item):
            served.setdefault(node, set()).add(qa_id)
    return served


def test_ledger_entries_are_well_formed() -> None:
    problems: list[str] = []
    for qa_id, item in ledger().items():
        if not QA_ID.fullmatch(qa_id):
            problems.append(f"{qa_id}: malformed id")
        if item["priority"] not in {"P0", "P1", "P2"}:
            problems.append(f"{qa_id}: priority {item['priority']!r}")
        if item["auto"] not in AUTO_KINDS:
            problems.append(f"{qa_id}: auto {item['auto']!r}")
        if item["check"] not in CHECK_KINDS:
            problems.append(f"{qa_id}: check {item['check']!r}")
        if (item["auto"] == "manual") != (item.get("evidence") == "release-checklist"):
            problems.append(f"{qa_id}: manual items (only) need evidence = 'release-checklist'")
        if item["check"] == "pytest" and item["auto"] == "manual":
            problems.append(f"{qa_id}: a pytest check cannot be manual")
        expected_slow = item["priority"] == "P2" and item["auto"] in E2E_KINDS
        if item["slow"] != expected_slow:
            problems.append(f"{qa_id}: slow must be set exactly on P2 e2e items")
        for node in traced_nodes(item):
            path, _, name = node.partition("::")
            if not path.startswith("tests/") or not path.endswith(".py"):
                problems.append(f"{qa_id}: {node} is not under tests/")
            if not name.isidentifier() or not name.startswith("test"):
                problems.append(f"{qa_id}: {node} does not name a test function")
    assert problems == [], "\n".join(problems)


def test_qa_markers_are_well_formed() -> None:
    assert list(scan().problems) == [], "\n".join(scan().problems)


@pytest.mark.qa("QA-PKG-011")
def test_every_p0_p1_automated_item_has_a_test() -> None:
    by_node = _tests_by_node()
    missing: list[str] = []
    for qa_id, item in sorted(ledger().items()):
        if item["priority"] not in GATING or item["auto"] == "manual":
            continue
        if item["check"] != "pytest" and not (REPO_ROOT / item["file"]).is_file():
            missing.append(f"{qa_id}: {item['check']} file {item['file']} does not exist")
        for node in traced_nodes(item):
            path = node.partition("::")[0]
            if not (REPO_ROOT / path).is_file():
                missing.append(f"{qa_id}: {path} does not exist")
            elif node not in by_node:
                missing.append(f"{qa_id}: {node} is not defined")
            elif not any(qa_id in test.qa_ids for test in by_node[node]):
                missing.append(f"{qa_id}: {node} lacks @pytest.mark.qa({qa_id!r})")
    assert missing == [], f"{len(missing)} untraced P0/P1 items:\n" + "\n".join(missing)


def test_every_marker_id_is_in_the_ledger() -> None:
    known = ledger().keys()
    unknown = sorted(
        f"{test.path}:{test.line}: {qa_id}"
        for test in scan().tests
        for qa_id in test.qa_ids
        if qa_id not in known
    )
    assert unknown == [], "marker ids missing from tests/qa_trace.toml:\n" + "\n".join(unknown)


def test_slow_marks_exactly_the_p2_e2e_tests() -> None:
    items = ledger()
    served = _served_items()
    wrong: list[str] = []
    for test in scan().tests:
        ids = served.get(test.node, set())
        # A test that also serves a P0/P1 item runs in every PR shard, so it is never slow.
        expected = (
            test.path.startswith("tests/e2e/") and bool(ids) and all(items[i]["slow"] for i in ids)
        )
        if test.slow != expected:
            verb = "needs" if expected else "must not carry"
            wrong.append(f"{test.path}:{test.line}: {test.name} {verb} @pytest.mark.slow")
    assert wrong == [], "\n".join(wrong)


def test_mutant_catalogue_matches_the_ledger() -> None:
    catalogue = tomllib.loads(MUTANTS.read_text(encoding="utf-8"))["mutant"]
    ids = [mutant["id"] for mutant in catalogue]
    expected_ids = [f"M{number:02d}" for number in range(1, MUTANT_COUNT + 1)]
    assert ids == expected_ids, "tests/mutants.toml must list M01..M82 once, in order"
    items = ledger()
    killers: dict[str, set[str]] = {}
    for qa_id, item in items.items():
        for mutant_id in item["mutants"]:
            assert MUTANT_ID.fullmatch(mutant_id), f"{qa_id}: bad mutant id {mutant_id!r}"
            killers.setdefault(mutant_id, set()).add(qa_id)
    problems: list[str] = []
    for mutant in catalogue:
        mutant_id = mutant["id"]
        expected_qa = sorted(killers.get(mutant_id, set()))
        if sorted(mutant["qa"]) != expected_qa:
            problems.append(f"{mutant_id}: qa {sorted(mutant['qa'])} != ledger {expected_qa}")
        traced = {
            node for qa_id in mutant["qa"] if qa_id in items for node in traced_nodes(items[qa_id])
        }
        tier_dir = {"unit": "tests/unit/", "e2e": "tests/e2e/"}.get(mutant["tier"])
        if tier_dir is None:
            problems.append(f"{mutant_id}: tier {mutant['tier']!r}")
            continue
        if not mutant["kill"]:
            problems.append(f"{mutant_id}: no killing tests")
        for node in mutant["kill"]:
            if node not in traced:
                problems.append(f"{mutant_id}: {node} is not a traced test of {mutant['qa']}")
            if not node.startswith(tier_dir):
                problems.append(f"{mutant_id}: {node} is outside the {mutant['tier']} tier")
        if not mutant["patch"]:
            problems.append(f"{mutant_id}: no patch")
    assert problems == [], "\n".join(problems)
