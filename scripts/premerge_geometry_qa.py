#!/usr/bin/env python3
"""Run the two approved geometry GUI cases against an installed subject wheel."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

sys.path.insert(0, "/probes")
from premerge_capture_gaps_probe import (
    _installed_provenance,
    _subject_commit,
    _version,
)

TEST_NODES = (
    "tests/e2e/test_element_query.py::"
    "test_closed_menu_keeps_discovery_and_action_without_pointer_coordinates",
    "tests/e2e/test_element_query.py::"
    "test_off_desktop_control_keeps_action_but_old_ref_cannot_move_or_click_pointer",
)


def verify_junit(path: Path) -> list[str]:
    cases = list(ET.parse(path).getroot().iter("testcase"))
    expected = sorted(node.rsplit("::", 1)[1] for node in TEST_NODES)
    actual = sorted(case.get("name", "") for case in cases)
    if actual != expected:
        raise RuntimeError(f"Geometry test identities differ: {actual!r}")
    for case in cases:
        if any(case.find(kind) is not None for kind in ("failure", "error", "skipped")):
            raise RuntimeError(f"Geometry case did not pass: {case.get('name')}")
    return actual


def main() -> int:
    artifacts = Path(os.environ.get("KWIN_MCP_ARTIFACT_DIR", "/artifacts"))
    artifacts.mkdir(parents=True, exist_ok=True)
    log_path = artifacts / "geometry-pytest.log"
    junit_path = artifacts / "geometry-junit.xml"
    record: dict[str, Any] = {
        "status": "failed",
        "test_nodes": list(TEST_NODES),
        "pytest_exit": None,
        "junit_verified": False,
    }
    exit_code = 1
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("DISPLAY", None)
    try:
        record["subject_commit"] = _subject_commit()
        record["installed_provenance"] = _installed_provenance(environment, artifacts)
        provenance = record["installed_provenance"]
        direct_url = provenance.get("direct_url") or {}
        if (direct_url.get("dir_info") or {}).get("editable"):
            raise RuntimeError("Editable installs are not native wheel proof")
        if not str(direct_url.get("url", "")).endswith(".whl"):
            raise RuntimeError("Installed package has no wheel archive origin")
        package = Path(provenance["package_path"]).resolve()
        if not package.is_relative_to("/opt/kwin-mcp-venv"):
            raise RuntimeError(f"Subject package is outside the installed environment: {package}")
        sources = provenance.get("source_files") or {}
        if not sources or any(data.get("record_verified") is not True for data in sources.values()):
            raise RuntimeError("Installed source files do not all match wheel RECORD")
        record["kwin_version"] = _version()
        command = [sys.executable, "-m", "pytest", *TEST_NODES, "-vv", f"--junitxml={junit_path}"]
        record["command"] = command
        with log_path.open("w", encoding="utf-8") as log:
            completed = subprocess.run(
                command,
                cwd="/app",
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=900,
                check=False,
            )
        exit_code = completed.returncode
        record["pytest_exit"] = completed.returncode
        (artifacts / "geometry-pytest.exit").write_text(f"{completed.returncode}\n")
        if completed.returncode == 0:
            exit_code = 1
            record["reported_tests"] = verify_junit(junit_path)
            record["junit_verified"] = True
            record["status"] = "passed"
            exit_code = 0
    except Exception as error:
        record["error"] = f"{type(error).__name__}: {error}"
    finally:
        record["wrapper_exit"] = exit_code
        (artifacts / "geometry-proof.json").write_text(json.dumps(record, indent=2) + "\n")
        if log_path.is_file():
            print(log_path.read_text(encoding="utf-8", errors="replace"), end="")
        print(json.dumps(record), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
