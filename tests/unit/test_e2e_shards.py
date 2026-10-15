"""Behavioral tests for the row-local E2E shard status checker."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER = REPO_ROOT / "scripts" / "check_e2e_shards.py"


def _status(collected: int, *, result: str = "success", full: int | None = None) -> str:
    lines = [f"result={result}", f"collected={collected}"]
    if full is not None:
        lines.append(f"full={full}")
    return "\n".join(lines) + "\n"


def _run_checker(
    status_dir: Path,
    *,
    label: str = "amd64",
    shards: int = 5,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(env or {})
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--status-dir",
            str(status_dir),
            "--label",
            label,
            "--shards",
            str(shards),
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _write_row(
    status_dir: Path,
    label: str = "amd64",
    *,
    collected: tuple[int, ...] = (2, 2, 2, 2, 2),
    full: int | None = 10,
    result: str = "success",
) -> None:
    status_dir.mkdir(parents=True, exist_ok=True)
    for number, count in enumerate(collected, 1):
        status_dir.joinpath(f"{label}-s{number}.txt").write_text(
            _status(count, result=result, full=full if number == 1 else None), encoding="utf-8"
        )


def _assert_failure(result: subprocess.CompletedProcess[str], diagnostic: str) -> None:
    assert result.returncode == 1, result.stdout + result.stderr
    assert "error:" in result.stderr
    assert diagnostic in result.stderr, result.stderr


@pytest.mark.qa("QA-PKG-015")
def test_checker_accepts_successful_row_and_ignores_other_row_failure(tmp_path: Path) -> None:
    _write_row(tmp_path)
    _write_row(tmp_path, "fedora-amd64", result="failure")
    _write_row(tmp_path, "opensuse-amd64")
    result = _run_checker(tmp_path, env={"SHARD_MATRIX_RESULT": "failure"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok: row amd64 covered 10 tests" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("shard", "state"),
    [(shard, state) for state in ("failure", "cancelled", "skipped") for shard in (1, 3, 5)],
)
@pytest.mark.qa("QA-PKG-015")
def test_checker_rejects_non_success_shard_states(tmp_path: Path, shard: int, state: str) -> None:
    _write_row(tmp_path)
    status_file = tmp_path / f"amd64-s{shard}.txt"
    status_file.write_text(
        _status(2, result=state, full=10 if shard == 1 else None), encoding="utf-8"
    )

    result = _run_checker(tmp_path)

    _assert_failure(result, f"amd64-s{shard}.txt reports result {state!r}")


@pytest.mark.parametrize(
    ("setup", "diagnostic"),
    [
        ("all_missing", "missing"),
        ("missing", "missing"),
        ("wrong_set", "unexpected"),
        ("empty", "must contain result and collected"),
        ("missing_collected", "must contain result and collected"),
        ("corrupt", "unknown field"),
        ("non_numeric", "non-numeric collected count"),
        ("negative", "non-numeric collected count"),
    ],
)
@pytest.mark.qa("QA-PKG-015")
def test_checker_rejects_missing_or_corrupt_status_files(
    tmp_path: Path, setup: str, diagnostic: str
) -> None:
    if setup != "all_missing":
        _write_row(tmp_path)
    if setup == "missing":
        (tmp_path / "amd64-s5.txt").unlink()
    elif setup == "wrong_set":
        (tmp_path / "amd64-s5.txt").unlink()
        (tmp_path / "amd64-s6.txt").write_text(_status(2), encoding="utf-8")
    elif setup == "empty":
        (tmp_path / "amd64-s3.txt").write_text("", encoding="utf-8")
    elif setup == "missing_collected":
        (tmp_path / "amd64-s3.txt").write_text("result=success\n", encoding="utf-8")
    elif setup == "corrupt":
        (tmp_path / "amd64-s3.txt").write_text("result=success\nwat=2\n", encoding="utf-8")
    elif setup == "non_numeric":
        (tmp_path / "amd64-s3.txt").write_text("result=success\ncollected=two\n", encoding="utf-8")
    elif setup == "negative":
        (tmp_path / "amd64-s3.txt").write_text(_status(-1), encoding="utf-8")

    result = _run_checker(tmp_path)

    _assert_failure(result, diagnostic)


@pytest.mark.parametrize(
    ("contents", "diagnostic"),
    [
        ("result=success\ncollected=2\n", "record full exactly once"),
        ("result=success\ncollected=2\nfull=0\n", "positive full count"),
        ("result=success\ncollected=2\nfull=not-a-number\n", "non-numeric full count"),
    ],
)
@pytest.mark.qa("QA-PKG-015")
def test_checker_rejects_invalid_full_collection_metadata(
    tmp_path: Path, contents: str, diagnostic: str
) -> None:
    _write_row(tmp_path)
    (tmp_path / "amd64-s1.txt").write_text(contents, encoding="utf-8")

    result = _run_checker(tmp_path)

    _assert_failure(result, diagnostic)


@pytest.mark.qa("QA-PKG-015")
def test_checker_rejects_duplicate_full_metadata(tmp_path: Path) -> None:
    _write_row(tmp_path)
    (tmp_path / "amd64-s2.txt").write_text(_status(2, full=10), encoding="utf-8")

    result = _run_checker(tmp_path)

    _assert_failure(result, "record full exactly once (found 2)")


@pytest.mark.parametrize(
    ("collected", "full", "diagnostic"),
    [
        ((3, 2, 2, 2, 2), 10, "collected 11 tests"),
        ((0, 0, 0, 0, 0), 1, "collected 0 tests"),
    ],
)
@pytest.mark.qa("QA-PKG-015")
def test_checker_rejects_collection_coverage_gaps(
    tmp_path: Path, collected: tuple[int, ...], full: int, diagnostic: str
) -> None:
    _write_row(tmp_path, collected=collected, full=full)

    result = _run_checker(tmp_path)

    _assert_failure(result, diagnostic)
