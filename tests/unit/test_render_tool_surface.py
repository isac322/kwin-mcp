"""Unit tests for the tool-surface grid renderer."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = REPO_ROOT / "tests" / "unit" / "snapshots" / "tool_surface.json"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "render_tool_surface", REPO_ROOT / "scripts" / "render_tool_surface.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
    return module


@pytest.fixture(scope="module")
def surface_module() -> Any:
    return _load_module()


@pytest.fixture(scope="module")
def baseline() -> list[dict[str, Any]]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def _tool(baseline: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for tool in baseline:
        if tool["name"] == name:
            return copy.deepcopy(tool)
    raise AssertionError(f"baseline tool not found: {name}")


def _tool_ref(tools: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for tool in tools:
        if tool["name"] == name:
            return tool
    raise AssertionError(f"tool not found: {name}")


def _with_tool(baseline: list[dict[str, Any]], tool: dict[str, Any]) -> list[dict[str, Any]]:
    result = copy.deepcopy(baseline)
    result.append(tool)
    return result


@pytest.mark.qa("QA-SURF-023")
def test_baseline_diff_is_unchanged_and_passes_all_gates(
    surface_module: Any, baseline: list[dict[str, Any]]
) -> None:
    report = surface_module.compute_diff(baseline, baseline)

    assert report["has_surface_changes"] is False
    assert report["counts"] == {"added": 0, "removed": 0, "modified": 0, "unchanged": 29}
    assert report["collisions"] == []
    assert report["ad_hoc_violations"] == []
    assert report["gates_passed"] is True
    assert len(report["cells"]) == 48


@pytest.mark.qa("QA-SURF-024")
def test_added_tool_and_cell_collision_detected(
    surface_module: Any, baseline: list[dict[str, Any]]
) -> None:
    read_tool = _tool(baseline, "screen_capture")
    read_tool["name"] = "screen_record"
    read_tool["annotations"]["readOnlyHint"] = True
    collision_report = surface_module.compute_diff(baseline, _with_tool(baseline, read_tool))

    added = next(tool for tool in collision_report["tools"] if tool["name"] == "screen_record")
    assert added["status"] == "added"
    assert collision_report["collisions"]
    assert any("screen" in collision for collision in collision_report["collisions"])
    assert collision_report["gates_passed"] is False

    empty_cell_tool = _tool(baseline, "screen_capture")
    empty_cell_tool["name"] = "screen_record"
    empty_cell_tool["annotations"]["readOnlyHint"] = False
    empty_report = surface_module.compute_diff(baseline, _with_tool(baseline, empty_cell_tool))

    assert next(tool for tool in empty_report["tools"] if tool["name"] == "screen_record")[
        "status"
    ] == ("added")
    assert empty_report["collisions"] == []
    assert empty_report["gates_passed"] is True

    unknown_domain_tool = _tool(baseline, "screen_capture")
    unknown_domain_tool["name"] = "custom_action"
    unknown_report = surface_module.compute_diff(
        baseline, _with_tool(baseline, unknown_domain_tool)
    )
    assert any("unknown domain 'custom'" in item for item in unknown_report["collisions"])
    assert unknown_report["gates_passed"] is False


@pytest.mark.qa("QA-SURF-025")
def test_removed_and_modified_tool_detected(
    surface_module: Any, baseline: list[dict[str, Any]]
) -> None:
    without_wayland = [tool for tool in baseline if tool["name"] != "wayland_list"]
    removed_report = surface_module.compute_diff(baseline, without_wayland)
    removed = next(tool for tool in removed_report["tools"] if tool["name"] == "wayland_list")
    assert removed["status"] == "removed"
    assert removed["delta_tokens"] < 0

    cases = (
        ("description", lambda tool: tool.__setitem__("description", "Changed description.")),
        (
            "input_schema",
            lambda tool: tool["inputSchema"]["properties"].update(
                {"extra": {"type": "string", "description": "Extra value."}}
            ),
        ),
        (
            "annotations",
            lambda tool: tool["annotations"].__setitem__(
                "destructiveHint", not tool["annotations"]["destructiveHint"]
            ),
        ),
    )
    for changed_field, mutate in cases:
        head = copy.deepcopy(baseline)
        pointer_click = _tool_ref(head, "pointer_click")
        mutate(pointer_click)
        report = surface_module.compute_diff(baseline, head)
        modified = next(tool for tool in report["tools"] if tool["name"] == "pointer_click")
        assert modified["status"] == "modified", changed_field
        assert modified["schema_diff"], changed_field
        assert modified[f"{changed_field}_changed"] is True, changed_field

    head_out = copy.deepcopy(baseline)
    pointer_out = _tool_ref(head_out, "pointer_click")
    pointer_out["outputSchema"]["properties"]["extra_out"] = {"type": "string"}
    out_report = surface_module.compute_diff(baseline, head_out)
    out_modified = next(tool for tool in out_report["tools"] if tool["name"] == "pointer_click")
    assert out_modified["status"] == "modified"
    assert out_modified["output_schema_changed"] is True


@pytest.mark.qa("QA-SURF-026")
def test_ad_hoc_parameter_violations_detected(
    surface_module: Any, baseline: list[dict[str, Any]]
) -> None:
    parameters = {
        "x": {"type": "integer"},
        "timeout_ms": {"type": "integer"},
        "screenshot": {"type": "boolean"},
        "window_id": {"type": "string"},
        "keep_screenshots": {"type": "boolean"},
    }
    for parameter, schema in parameters.items():
        head = copy.deepcopy(baseline)
        pointer_click = _tool_ref(head, "pointer_click")
        pointer_click["inputSchema"]["properties"][parameter] = schema
        report = surface_module.compute_diff(baseline, head)
        assert any(parameter in violation for violation in report["ad_hoc_violations"]), parameter
        assert report["gates_passed"] is False


@pytest.mark.qa("QA-SURF-024")
def test_semantic_overlaps_flag_cross_cell_head_tools(
    surface_module: Any, baseline: list[dict[str, Any]]
) -> None:
    report = surface_module.compute_diff(baseline, baseline)
    pointer = _tool_ref(report["tools"], "pointer_click")
    assert "touch_tap" in pointer["semantic_overlaps"]
    assert "pointer_move" not in pointer["semantic_overlaps"]
    assert _tool_ref(report["tools"], "keyboard_press")["semantic_overlaps"] == []
    assert report["gates_passed"] is True

    modified_head = copy.deepcopy(baseline)
    _tool_ref(modified_head, "pointer_click")["description"] += " Revised description."
    modified = surface_module.compute_diff(baseline, modified_head)
    modified_pointer = _tool_ref(modified["tools"], "pointer_click")
    assert modified_pointer["status"] == "modified"
    assert modified_pointer["semantic_overlaps"] == pointer["semantic_overlaps"]

    head = [tool for tool in baseline if tool["name"] != "touch_tap"]
    removed = surface_module.compute_diff(baseline, head)
    assert "touch_tap" not in _tool_ref(removed["tools"], "pointer_click")["semantic_overlaps"]
    assert _tool_ref(removed["tools"], "touch_tap")["semantic_overlaps"] == []


@pytest.mark.qa("QA-SURF-024")
@pytest.mark.parametrize(
    ("left_names", "right_names", "expected"),
    (
        (list("abcdefg"), list("abcdhij"), True),
        (list("abcde"), list("abc") + list("fghijklmnopqrstuv"), True),
        (list("abcde"), list("abfghi"), False),
        ([], [], False),
        (list("abc"), [], False),
    ),
)
def test_semantic_overlap_thresholds(
    surface_module: Any, left_names: list[str], right_names: list[str], expected: bool
) -> None:
    left: dict[str, Any] = {
        "name": "pointer_one",
        "inputSchema": {"properties": {name: {"type": "string"} for name in left_names}},
    }
    right: dict[str, Any] = {
        "name": "keyboard_one",
        "inputSchema": {"properties": {name: {"type": "string"} for name in right_names}},
    }
    report = surface_module.compute_diff([], [left, right])
    assert _tool_ref(report["tools"], "pointer_one")["semantic_overlaps"] == (
        ["keyboard_one"] if expected else []
    )
    assert _tool_ref(report["tools"], "keyboard_one")["semantic_overlaps"] == (
        ["pointer_one"] if expected else []
    )


@pytest.mark.qa("QA-SURF-024")
def test_semantic_overlap_includes_shared_type_concepts(surface_module: Any) -> None:
    left: dict[str, Any] = {
        "name": "pointer_one",
        "inputSchema": {
            "properties": {"first": {}, "second": {}},
            "$defs": {"Capture": {}, "Wait": {}},
        },
    }
    right: dict[str, Any] = {
        "name": "keyboard_one",
        "inputSchema": {
            "properties": {"third": {}, "fourth": {}, "fifth": {}},
            "$defs": {"Capture": {}, "Wait": {}},
        },
    }
    report = surface_module.compute_diff([], [left, right])
    assert _tool_ref(report["tools"], "pointer_one")["semantic_overlaps"] == []
    right["inputSchema"]["properties"] = {"third": {}}
    report = surface_module.compute_diff([], [left, right])
    assert _tool_ref(report["tools"], "pointer_one")["semantic_overlaps"] == ["keyboard_one"]


@pytest.mark.qa("QA-SURF-027")
def test_token_budget_overflow_detected(
    surface_module: Any, baseline: list[dict[str, Any]]
) -> None:
    head = copy.deepcopy(baseline)
    pointer_click = _tool_ref(head, "pointer_click")
    pointer_click["description"] = "token " * (surface_module.V1_BUDGET_LIMIT * 8)
    report = surface_module.compute_diff(baseline, head)

    assert report["head_tokens"] > surface_module.V1_BUDGET_LIMIT
    assert report["budget_exceeded"] is True
    assert report["gates_passed"] is False


@pytest.mark.qa("QA-SURF-028")
def test_render_markdown_and_html_outputs(
    surface_module: Any, baseline: list[dict[str, Any]]
) -> None:
    head = copy.deepcopy(baseline)
    pointer_click = _tool_ref(head, "pointer_click")
    pointer_click["description"] += " Updated for the surface grid."
    report = surface_module.compute_diff(baseline, head)

    markdown = surface_module.render_markdown(
        report, preview_url="https://cv.bhyoo.com/kwin-mcp/pr-107/"
    )
    assert markdown.startswith("<!-- kwin-mcp-tool-grid-summary -->")
    assert "https://cv.bhyoo.com/kwin-mcp/pr-107/" in markdown
    for domain in surface_module.DOMAIN_OBJECTS:
        assert domain in markdown
    assert "<details>" in markdown

    rendered_html = surface_module.render_html(report)
    assert rendered_html.startswith("<!doctype html>")
    assert not re.search(r'<script[^>]+src=["\']https?://', rendered_html)
    assert not re.search(
        r'<link[^>]+rel=["\']stylesheet["\'][^>]+href=["\']https?://', rendered_html
    )
    grid_data = re.search(
        r'<script[^>]+id=["\']grid-data["\'][^>]*>(.*?)</script>', rendered_html, re.DOTALL
    )
    assert grid_data is not None
    assert json.loads(grid_data.group(1))["base_tool_count"] == 29
    assert "<!--" not in grid_data.group(1)
    assert "10.222" not in rendered_html
    for section_id in ("sGate", "sGrid", "sCards", "sDeep", "s4", "s3"):
        assert f'id="{section_id}"' in rendered_html, section_id
    drawer = re.search(r'<details\b[^>]*\bid="sDeep"[^>]*>', rendered_html)
    assert drawer is not None
    assert not re.search(r"\bopen\b", drawer.group(0))
    assert "OLD_012_TOOLS" not in rendered_html
    for removed_id in ("s0", "s1", "s2", "s7", "sSchema", "proposal-bar"):
        assert f'id="{removed_id}"' not in rendered_html
    assert 'const cardState = { filter: "changed", q: "" };' in rendered_html
    assert "No tools changed between Base and Head." in rendered_html
    assert 'class: "diff-block"' in rendered_html
    assert 'text: "Parameters & Annotations"' in rendered_html

    all_added_report = surface_module.compute_diff([], baseline)
    all_added_md = surface_module.render_markdown(all_added_report)
    assert len(all_added_md) <= 60_000


@pytest.mark.qa("QA-SURF-029")
def test_cli_writes_html_markdown_and_json_files(
    surface_module: Any,
    baseline: list[dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_path = tmp_path / "base.json"
    head_path = tmp_path / "head.json"
    html_path = tmp_path / "index.html"
    markdown_path = tmp_path / "summary.md"
    json_path = tmp_path / "report.json"
    base_path.write_text(json.dumps(baseline), encoding="utf-8")
    head_path.write_text(json.dumps(baseline), encoding="utf-8")

    assert (
        surface_module.main(
            [
                "--base",
                str(base_path),
                "--head",
                str(head_path),
                "--html-out",
                str(html_path),
                "--markdown-out",
                str(markdown_path),
                "--json-out",
                str(json_path),
            ]
        )
        == 0
    )
    assert html_path.exists() and "<!doctype html>" in html_path.read_text(encoding="utf-8")
    assert markdown_path.exists() and "kwin-mcp-tool-grid-summary" in markdown_path.read_text(
        encoding="utf-8"
    )
    assert json.loads(json_path.read_text(encoding="utf-8"))["gates_passed"] is True

    collision = _tool(baseline, "screen_capture")
    collision["name"] = "screen_record"
    collision["annotations"]["readOnlyHint"] = True
    head_path.write_text(json.dumps(_with_tool(baseline, collision)), encoding="utf-8")
    assert surface_module.main(["--base", str(base_path), "--head", str(head_path), "--check"]) == 1
    assert surface_module.main(["--check"]) == 0

    def fake_run(
        args: list[str], **_kwargs: Any
    ) -> surface_module.subprocess.CompletedProcess[str]:
        spec = args[-1]
        if spec.startswith("missing-base:"):
            return surface_module.subprocess.CompletedProcess(
                args,
                128,
                "",
                "fatal: path 'tests/unit/snapshots/tool_surface.json' "
                "does not exist in 'missing-base'",
            )
        return surface_module.subprocess.CompletedProcess(args, 0, json.dumps(baseline), "")

    monkeypatch.setattr(surface_module.subprocess, "run", fake_run)
    assert surface_module._load_tools("missing-base") == []
    assert len(surface_module._load_tools("origin/main")) == 29
