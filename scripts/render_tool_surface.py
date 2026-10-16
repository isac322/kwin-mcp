"""Render kwin-mcp tool-surface diffs as Markdown and a self-contained HTML grid."""

from __future__ import annotations

import argparse
import difflib
import html
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_DEFAULT_SNAPSHOT = "tests/unit/snapshots/tool_surface.json"
_UNIVERSAL_MODIFIERS = frozenset({"capture", "def:Capture", "retention", "def:Retention"})
V1_BUDGET_LIMIT: int = 9510
DOMAIN_OBJECTS: tuple[str, ...] = (
    "session",
    "app",
    "screen",
    "element",
    "window",
    "desktop",
    "pointer",
    "keyboard",
    "touch",
    "clipboard",
    "dbus",
    "wayland",
)
ACCESS_MODES: tuple[str, ...] = ("Read", "Act", "Lifecycle", "Hold")
SHARED_TYPES: tuple[str, ...] = (
    "Target",
    "ElementAddress",
    "Locator",
    "WindowSelector",
    "Rect",
    "Capture",
    "Wait",
    "Retention",
)

MAX_ALLOWED_PER_CELL: dict[tuple[str, str], int] = {
    (obj, mode): 1 for obj in DOMAIN_OBJECTS for mode in ACCESS_MODES
}
MAX_ALLOWED_PER_CELL.update(
    {
        ("session", "Lifecycle"): 3,
        ("window", "Act"): 2,
        ("pointer", "Act"): 4,
        ("keyboard", "Act"): 2,
        ("touch", "Act"): 3,
    }
)

_STATUS_ICON = {"added": "✨", "modified": "✏️", "removed": "❌", "unchanged": ""}


def classify_tool(tool: dict[str, Any]) -> tuple[str, str]:
    """Classify one tool by its name prefix and operation semantics."""
    name = str(tool.get("name", ""))
    prefix, _, verb = name.partition("_")
    annotations = tool.get("annotations") or {}
    if verb in {"start", "connect", "stop", "launch", "close"}:
        mode = "Lifecycle"
    elif verb == "hold":
        mode = "Hold"
    elif annotations.get("readOnlyHint") is True or verb in {
        "info",
        "log",
        "capture",
        "query",
        "list",
        "read",
    }:
        mode = "Read"
    else:
        mode = "Act"
    return prefix, mode


def _properties(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("inputSchema") or {}
    properties = schema.get("properties")
    return properties if isinstance(properties, dict) else {}


def _is_object_with(schema: Any, names: set[str]) -> bool:
    if not isinstance(schema, dict):
        return False
    props = schema.get("properties")
    if isinstance(props, dict) and names.issubset(props):
        return True
    for key in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(key)
        if isinstance(variants, list) and any(_is_object_with(item, names) for item in variants):
            return True
    return False


def _has_any_property(schema: Any, names: set[str]) -> bool:
    if not isinstance(schema, dict):
        return False
    props = schema.get("properties")
    if isinstance(props, dict) and names.intersection(props):
        return True
    for key in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(key)
        if isinstance(variants, list) and any(_has_any_property(item, names) for item in variants):
            return True
    return False


def _mentions_point(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    ref = str(schema.get("$ref", ""))
    if "Point" in ref or "ElementPoint" in ref:
        return True
    description = str(schema.get("description", ""))
    if "ElementPoint" in description or re.search(r"\bPoint\b", description):
        return True
    props = schema.get("properties")
    if isinstance(props, dict) and {"x", "y"}.issubset(props):
        return True
    for key in ("anyOf", "oneOf", "allOf", "items"):
        variants = schema.get(key)
        if isinstance(variants, list):
            if any(_mentions_point(item) for item in variants):
                return True
        elif isinstance(variants, dict) and _mentions_point(variants):
            return True
    return False


def _resolve_schema(value: Any, defs: dict[str, Any], seen: frozenset[str] = frozenset()) -> Any:
    """Expand local shared definitions for concept detection, including union branches."""
    if isinstance(value, list):
        return [_resolve_schema(item, defs, seen) for item in value]
    if not isinstance(value, dict):
        return value
    ref = value.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        name = ref.removeprefix("#/$defs/")
        if name in defs and name not in seen:
            resolved = _resolve_schema(defs[name], defs, seen | {name})
            if isinstance(resolved, dict):
                return {**resolved, **{key: item for key, item in value.items() if key != "$ref"}}
    return {key: _resolve_schema(item, defs, seen) for key, item in value.items()}


def detect_shared_types_and_violations(
    tool: dict[str, Any],
) -> tuple[dict[str, str], list[str]]:
    """Find shared schema concepts and reject common ad-hoc parameter spellings."""
    schema = tool.get("inputSchema") or {}
    props = _properties(tool)
    defs = schema.get("$defs") if isinstance(schema, dict) else {}
    defs = defs if isinstance(defs, dict) else {}
    props = {name: _resolve_schema(value, defs) for name, value in props.items()}
    shared = {name: "none" for name in SHARED_TYPES}

    if any(
        name in props and _mentions_point(props[name])
        for name in ("target", "from", "to", "center")
    ):
        shared["Target"] = "reused"
    element = props.get("element")
    if _has_any_property(element, {"ref", "strict"}) or (
        isinstance(element, dict) and "ElementAddress" in str(element.get("$ref", ""))
    ):
        shared["ElementAddress"] = "reused"
    if "locator" in props or "Locator" in defs:
        shared["Locator"] = "reused"
    window = props.get("window") or props.get("filter")
    if _is_object_with(window, {"app_id", "caption", "kind"}) or (
        isinstance(window, dict)
        and any(key in window.get("properties", {}) for key in ("app_id", "caption", "kind"))
    ):
        shared["WindowSelector"] = "reused"
    for key in ("region", "frame"):
        if _is_object_with(props.get(key), {"x", "y", "width", "height"}):
            shared["Rect"] = "reused"
    capture = props.get("capture")
    if _is_object_with(capture, {"delays_ms"}) or "Capture" in defs:
        shared["Capture"] = "reused"
    wait = props.get("wait")
    if _is_object_with(wait, {"timeout_ms", "poll_interval_ms"}) or "Wait" in defs:
        shared["Wait"] = "reused"
    retention = props.get("retention")
    if _is_object_with(retention, {"artifacts", "home"}) or "Retention" in defs:
        shared["Retention"] = "reused"

    violations: list[str] = []
    groups = {
        "coordinate": (
            {"x", "y", "center_x", "center_y", "from_x", "from_y", "to_x", "to_y"},
            "Ad-hoc coordinate parameter '{prop}'; use Target or Rect",
        ),
        "wait": (
            {"timeout_ms", "poll_interval_ms", "wait_ms"},
            "Ad-hoc wait parameter '{prop}'; use Wait",
        ),
        "capture": (
            {"screenshot", "screenshot_after_ms", "frame_capture_delays_ms"},
            "Ad-hoc capture parameter '{prop}'; use Capture",
        ),
        "selector": (
            {"window_id", "window_title", "app_name"},
            "Ad-hoc selector parameter '{prop}'; use WindowSelector or Locator",
        ),
        "retention": (
            {"keep_screenshots", "keep_home", "keep_artifacts"},
            "Ad-hoc retention parameter '{prop}'; use Retention",
        ),
    }
    violation_types = {
        "coordinate": ("Target", "Rect"),
        "wait": ("Wait",),
        "capture": ("Capture",),
        "selector": ("WindowSelector", "Locator"),
        "retention": ("Retention",),
    }
    for prop in props:
        for group, (names, message) in groups.items():
            if prop in names:
                violations.append(message.format(prop=prop))
                for shared_name in violation_types[group]:
                    shared[shared_name] = "violation"
                break
    return shared, violations


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _token_count(tool: dict[str, Any]) -> int:
    """Estimate tokens for fields exposed in an MCP tool listing."""
    visible = {
        key: tool.get(key)
        for key in ("name", "title", "description", "inputSchema", "annotations")
        if key in tool
    }
    text = _compact(visible)
    return max(1, (len(text) + 3) // 4)


def _schema_diff(base: Any, head: Any) -> str:
    before = json.dumps(base or {}, ensure_ascii=False, sort_keys=True, indent=2).splitlines()
    after = json.dumps(head or {}, ensure_ascii=False, sort_keys=True, indent=2).splitlines()
    return "\n".join(
        difflib.unified_diff(before, after, fromfile="base", tofile="head", lineterm="")
    )


def _parameter_delta(
    base: dict[str, Any] | None, head: dict[str, Any] | None
) -> dict[str, list[str]]:
    old = _properties(base or {})
    new = _properties(head or {})
    old_names, new_names = set(old), set(new)
    return {
        "added": sorted(new_names - old_names),
        "removed": sorted(old_names - new_names),
        "changed": sorted(name for name in old_names & new_names if old[name] != new[name]),
    }


def _tool_record(
    name: str,
    base: dict[str, Any] | None,
    head: dict[str, Any] | None,
    status: str,
) -> dict[str, Any]:
    source = head or base or {"name": name}
    obj, mode = classify_tool(source)
    shared, violations = detect_shared_types_and_violations(source)
    base_tokens = _token_count(base) if base else 0
    head_tokens = _token_count(head) if head else 0
    return {
        "name": name,
        "object": obj,
        "mode": mode,
        "status": status,
        "base_tokens": base_tokens,
        "head_tokens": head_tokens,
        "delta_tokens": head_tokens - base_tokens,
        "description_changed": (base or {}).get("description") != (head or {}).get("description"),
        "annotations_changed": (base or {}).get("annotations") != (head or {}).get("annotations"),
        "input_schema_changed": (base or {}).get("inputSchema") != (head or {}).get("inputSchema"),
        "output_schema_changed": (
            (base or {}).get("outputSchema") != (head or {}).get("outputSchema")
        ),
        "param_Delta": _parameter_delta(base, head),
        "shared_types": shared,
        "violations": violations,
        "semantic_overlaps": [],
        "base_tool": base,
        "head_tool": head,
        "schema_diff": _schema_diff(base, head),
    }


def _semantic_signature(tool: dict[str, Any]) -> set[str]:
    """Return parameter and canonical shared-type concepts for overlap comparison."""
    signature = set(_properties(tool))
    shared, _ = detect_shared_types_and_violations(tool)
    signature.update(f"def:{name}" for name, state in shared.items() if state == "reused")
    return signature


def _semantic_overlap_names(
    head_tools: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Find materially similar tools that occupy different object/mode cells."""
    signatures = {str(tool.get("name", "")): _semantic_signature(tool) for tool in head_tools}
    locations = {str(tool.get("name", "")): classify_tool(tool) for tool in head_tools}
    overlaps = {name: [] for name in signatures}
    names = sorted(signatures)
    for index, name in enumerate(names):
        left = signatures[name]
        if not left:
            continue
        for other in names[index + 1 :]:
            if locations[name][0] == locations[other][0]:
                continue
            right = signatures[other]
            if not right:
                continue
            shared_concepts = left & right
            if not (shared_concepts - _UNIVERSAL_MODIFIERS):
                continue
            intersection = len(shared_concepts)
            union = len(left | right)
            jaccard = intersection / union if union else 0.0
            containment = intersection / min(len(left), len(right))
            if jaccard >= 0.40 or containment >= 0.60:
                overlaps[name].append(other)
                overlaps[other].append(name)
    return {name: sorted(values) for name, values in overlaps.items()}


def compute_diff(
    base_tools: list[dict[str, Any]],
    head_tools: list[dict[str, Any]],
    *,
    base_label: str = "base",
    head_label: str = "head",
) -> dict[str, Any]:
    """Compute a serializable surface report for two tool lists."""
    base_by_name = {str(tool.get("name", "")): tool for tool in base_tools}
    head_by_name = {str(tool.get("name", "")): tool for tool in head_tools}
    names = set(base_by_name) | set(head_by_name)
    statuses: dict[str, str] = {}
    for name in names:
        if name not in base_by_name:
            statuses[name] = "added"
        elif name not in head_by_name:
            statuses[name] = "removed"
        elif base_by_name[name] != head_by_name[name]:
            statuses[name] = "modified"
        else:
            statuses[name] = "unchanged"
    records = [
        _tool_record(name, base_by_name.get(name), head_by_name.get(name), statuses[name])
        for name in names
    ]
    overlap_names = _semantic_overlap_names(head_tools)
    for record in records:
        record["semantic_overlaps"] = overlap_names.get(record["name"], [])
    order = {"added": 0, "modified": 1, "removed": 2, "unchanged": 3}
    records.sort(key=lambda record: (order[record["status"]], record["name"]))
    counts = {status: sum(value == status for value in statuses.values()) for status in order}
    collisions: list[str] = []
    for tool in head_tools:
        obj, mode = classify_tool(tool)
        if obj not in DOMAIN_OBJECTS:
            collisions.append(f"{tool.get('name', '<unnamed>')}: unknown domain '{obj}'")
    cells: list[dict[str, Any]] = []
    for obj in DOMAIN_OBJECTS:
        for mode in ACCESS_MODES:
            cell_tools = [
                record for record in records if record["object"] == obj and record["mode"] == mode
            ]
            head_cell_tools = [record for record in cell_tools if record["head_tool"] is not None]
            max_allowed = MAX_ALLOWED_PER_CELL[(obj, mode)]
            has_collision = len(head_cell_tools) > max_allowed
            if has_collision:
                collisions.append(
                    f"{obj}/{mode}: {len(head_cell_tools)} tools exceeds max {max_allowed}"
                )
            cell_status = "collision" if has_collision else "unchanged"
            if any(record["status"] == "added" for record in cell_tools):
                cell_status = "added"
            elif any(record["status"] == "modified" for record in cell_tools):
                cell_status = "modified"
            elif any(record["status"] == "removed" for record in cell_tools):
                cell_status = "removed"
            cells.append(
                {
                    "object": obj,
                    "mode": mode,
                    "max_allowed": max_allowed,
                    "status": cell_status,
                    "has_collision": has_collision,
                    "tools": [
                        {
                            "name": record["name"],
                            "status": record["status"],
                            "delta_tokens": record["delta_tokens"],
                            "head_tokens": record["head_tokens"],
                            "base_tokens": record["base_tokens"],
                        }
                        for record in cell_tools
                    ],
                }
            )
    ad_hoc: list[str] = []
    for record in records:
        for violation in record["violations"]:
            if record["head_tool"] is not None:
                ad_hoc.append(f"{record['name']}: {violation}")
    base_tokens = sum(_token_count(tool) for tool in base_tools)
    head_tokens = sum(_token_count(tool) for tool in head_tools)
    return {
        "base_label": base_label,
        "head_label": head_label,
        "base_tool_count": len(base_tools),
        "head_tool_count": len(head_tools),
        "delta_tool_count": len(head_tools) - len(base_tools),
        "base_tokens": base_tokens,
        "head_tokens": head_tokens,
        "delta_tokens": head_tokens - base_tokens,
        "v1_budget_limit": V1_BUDGET_LIMIT,
        "budget_exceeded": head_tokens > V1_BUDGET_LIMIT,
        "has_surface_changes": any(counts[key] > 0 for key in ("added", "removed", "modified")),
        "counts": {
            "added": counts["added"],
            "removed": counts["removed"],
            "modified": counts["modified"],
            "unchanged": counts["unchanged"],
        },
        "collisions": sorted(set(collisions)),
        "ad_hoc_violations": sorted(set(ad_hoc)),
        "gates_passed": head_tokens <= V1_BUDGET_LIMIT and not collisions and not ad_hoc,
        "cells": cells,
        "tools": records,
    }


def render_markdown(
    report: dict[str, Any],
    *,
    preview_url: str | None = None,
    artifact_url: str | None = None,
) -> str:
    """Render a compact PR comment and workflow summary."""
    counts = report["counts"]
    gate = "PASS" if report["gates_passed"] else "VIOLATION"
    lines = [
        "<!-- kwin-mcp-tool-grid-summary -->",
        f"## Tool Surface Grid: `{report['base_label']}` → `{report['head_label']}`",
        "",
        f"**Gate: {gate}**",
        "",
        "| KPI | Base | Head | Delta |",
        "| --- | ---: | ---: | ---: |",
        f"| Tools | {report['base_tool_count']} | {report['head_tool_count']} | "
        f"{report['delta_tool_count']:+d} |",
        f"| Model-visible tokens | {report['base_tokens']:,} | {report['head_tokens']:,} | "
        f"{report['delta_tokens']:+,} / {V1_BUDGET_LIMIT:,} |",
        f"| Changed tools | — | {counts['added'] + counts['modified'] + counts['removed']} | — |",
        f"| Collisions / ad-hoc violations | — | "
        f"{len(report['collisions'])} / {len(report['ad_hoc_violations'])} | — |",
    ]
    if preview_url:
        lines.extend(["", f"[Open interactive preview]({preview_url})"])
    if artifact_url:
        lines.append(f"[Download workflow artifact]({artifact_url})")
    lines.extend(["", "### Object x Mode Orthogonality Grid", ""])
    lines.append("| Object | " + " | ".join(ACCESS_MODES) + " |")
    lines.append("| --- | " + " | ".join("---" for _ in ACCESS_MODES) + " |")
    for obj in DOMAIN_OBJECTS:
        row = [obj]
        for mode in ACCESS_MODES:
            cell = next(c for c in report["cells"] if c["object"] == obj and c["mode"] == mode)
            names = (
                ", ".join(
                    f"{_STATUS_ICON[t['status']]} `{t['name']}`".strip() for t in cell["tools"]
                )
                or "—"
            )
            if cell["has_collision"]:
                names = "⚠️ " + names
            row.append(names)
        lines.append("| " + " | ".join(row) + " |")
    lines.extend(["", "### Shared-Type Reuse", ""])
    for shared in SHARED_TYPES:
        reused = [
            tool["name"] for tool in report["tools"] if tool["shared_types"][shared] == "reused"
        ]
        lines.append(f"- **{shared}:** {', '.join(f'`{name}`' for name in reused) or 'none'}")
    changed = [tool for tool in report["tools"] if tool["status"] != "unchanged"]
    for index, tool in enumerate(changed):
        diff_lines = (tool["schema_diff"] or "(no schema diff)").splitlines()
        if len(diff_lines) > 40:
            omitted = len(diff_lines) - 40
            diff_text = (
                "\n".join(diff_lines[:40])
                + f"\n... ({omitted} more diff lines; see interactive preview)"
            )
        else:
            diff_text = "\n".join(diff_lines)
        block = [
            "",
            f"<details><summary>{_STATUS_ICON[tool['status']]} `{tool['name']}` "
            f"({tool['status']})</summary>",
            "",
            f"- Slot: `{tool['object']} / {tool['mode']}`",
            f"- Token delta: `{tool['delta_tokens']:+d}`",
            "",
            "```diff",
            diff_text,
            "```",
            "",
            "</details>",
        ]
        candidate = "\n".join([*lines, *block])
        if len(candidate) > 58_000:
            remaining = len(changed) - index
            lines.extend(
                [
                    "",
                    f"_({remaining} more changed tools omitted to stay within GitHub comment "
                    "limits; see interactive preview.)_",
                ]
            )
            break
        lines.extend(block)
    return "\n".join(lines) + "\n"


_HTML_STYLES = (
    ":root { --bg:#fafaf8; --fg:#202124; --muted:#697078; --line:#d7d9dc; --panel:#fff; "
    "--band:#f1f3f5; --accent:#1769aa; --good:#1b7f4b; --bad:#b3261e; --warn:#9a5b00; "
    "font-family:system-ui,sans-serif; }\n"
    "@media (prefers-color-scheme:dark) { :root { --bg:#17191c; --fg:#edf0f2; "
    "--muted:#aab1b8; --line:#3a3f45; --panel:#20242a; --band:#2a3036; --accent:#7ab8ef; "
    "--good:#6ed49d; --bad:#ff8a80; --warn:#f0b35a; } }\n"
    "* { box-sizing:border-box; }\n"
    "body { margin:0; background:var(--bg); color:var(--fg); line-height:1.45; "
    "font-size:14px; font-variant-numeric:tabular-nums; }\n"
    "::selection { background:var(--accent); color:#fff; }\n"
    ":focus-visible { outline:2px solid var(--accent); outline-offset:2px; }\n"
    ".page-head,main,.page-foot { max-width:1440px; margin:auto; padding:0 20px; }\n"
    ".page-head { padding-top:20px; } h1 { font-size:22px; margin:0 0 6px; }\n"
    "h2 { font-size:18px; margin:0 0 8px; } nav { display:flex; flex-wrap:wrap; gap:8px "
    "14px; margin:8px 0; } nav a { color:var(--accent); text-decoration:none; }\n"
    "section,.deep-drawer { background:var(--panel); border:1px solid var(--line); "
    "border-radius:8px; padding:16px 18px; margin:16px 0; }\n"
    ".lede,.hint,.note { color:var(--muted); } .hint,.note { font-size:12px; }\n"
    ".kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); "
    "gap:8px; } .kpi { border:1px solid var(--line); border-radius:6px; padding:8px; }\n"
    ".kpi strong { display:block; font-size:19px; } .gate { display:inline-block; "
    "color:#fff; border-radius:4px; padding:3px 8px; font-weight:700; }\n"
    ".gate.pass { background:var(--good); } .gate.fail { background:var(--bad); }\n"
    ".budget { height:10px; background:var(--band); border:1px solid var(--line); "
    "border-radius:5px; overflow:hidden; position:relative; }\n"
    ".budget i { display:block; height:100%; background:var(--accent); }\n"
    ".budget b { position:absolute; top:-2px; bottom:-2px; width:1px; background:var(--bad); "
    "}\n"
    ".alerts { display:grid; gap:5px; margin-top:10px; } .alert { border:1px solid "
    "var(--warn); background:color-mix(in srgb,var(--warn) 10%,var(--panel)); "
    "border-radius:5px; padding:6px 8px; }\n"
    ".controls { display:flex; flex-wrap:wrap; align-items:center; gap:6px; margin:8px 0; }\n"
    "button,input { font:inherit; } button.seg { border:1px solid var(--line); "
    "background:var(--panel); color:var(--fg); padding:4px 9px; cursor:pointer; }\n"
    "button.seg[aria-pressed=true] { background:var(--accent); color:#fff; }\n"
    "input[type=search] { min-width:230px; border:1px solid var(--line); border-radius:4px; "
    "padding:4px 8px; background:var(--bg); color:var(--fg); }\n"
    "table { border-collapse:collapse; width:100%; } table.zgrid { border-spacing:4px; "
    "border-collapse:separate; font-size:12px; }\n"
    "table.zgrid th { background:var(--band); padding:5px 6px; text-align:left; }\n"
    "table.zgrid td { border:1px solid var(--line); border-radius:5px; padding:5px; "
    "vertical-align:top; min-width:150px; transition:opacity .16s; }\n"
    "table.zgrid td.empty { border-style:dashed; background:transparent; }\n"
    ".chip { display:inline-flex; gap:4px; align-items:center; border:1px solid var(--line); "
    "border-radius:12px; padding:2px 7px; margin:2px; cursor:pointer; white-space:nowrap; }\n"
    ".schip { border-color:var(--accent); background:color-mix(in srgb,var(--accent) "
    "12%,var(--panel)); font-weight:600; } .status { font-size:10px; border:1px solid "
    "var(--line); border-radius:3px; padding:1px 4px; font-weight:700; }\n"
    ".status.added { color:var(--good); } .status.modified { color:var(--accent); }\n"
    ".status.removed,.status.collision { color:var(--bad); } .dim { opacity:.38; }\n"
    "body.brushing [data-tool]:not(.hl) { opacity:.18; } .hl { font-weight:700; outline:1px "
    "solid var(--accent); }\n"
    ".cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(390px,1fr)); "
    "gap:12px; }\n"
    ".scard { border:1px solid var(--line); border-radius:6px; padding:10px; "
    "background:var(--bg);\n"
    "}\n"
    ".scard header { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }\n"
    ".scard p { margin:6px 0; }\n"
    ".meta { margin:5px 0; } .feat { display:inline-block; border:1px solid var(--line); "
    "border-radius:3px; padding:1px 4px; margin:1px; font-size:11px; }\n"
    ".warn { color:var(--warn); }\n"
    ".diff-block { overflow:auto; max-height:360px; border:1px solid var(--line); "
    "border-radius:4px; padding:8px; background:var(--panel); }\n"
    ".ptable th,.ptable td { border-bottom:1px solid var(--line); padding:4px 6px; "
    "text-align:left; vertical-align:top; font-size:12px; }\n"
    ".req { color:var(--bad); } .deep-drawer summary { cursor:pointer; font-weight:700; }\n"
    ".scroll-x { overflow-x:auto; } svg { display:block; max-width:100%; height:auto; }\n"
    "svg text { fill:var(--fg); font-size:10px; }\n"
    "#tooltip { position:fixed; z-index:10; max-width:420px; background:var(--panel); "
    "border:1px solid var(--line); border-radius:5px; padding:8px; pointer-events:none; }\n"
    ".page-foot { color:var(--muted); font-size:11px; padding-bottom:26px; }\n"
    "\n"
    ".section-head { display:flex; align-items:center; justify-content:space-between; "
    "flex-wrap:wrap; gap:8px; }\n"
    ".section-head .controls { margin:0; }\n"
    ".lede { margin:4px 0; font-size:12px; }\n"
    ".ref-badge { border:1px solid var(--line); border-radius:4px; padding:2px 6px; }\n"
    ".empty-state { grid-column:1 / -1; border:1px solid var(--line); padding:8px; "
    "border-radius:5px; }\n"
    "pre { font-size:12px; } h3 { font-size:14px; margin:16px 0 8px; }\n"
    "svg { max-width:none; } .ptable td { overflow-wrap:anywhere; }\n"
    ".kpi strong { font-size:17px; }\n"
    "body.baseline .page-head { padding-top:12px; }\n"
    "body.baseline section { padding:10px 12px; margin:10px 0; }\n"
    "body.baseline table.zgrid { border-spacing:3px; }\n"
    "body.baseline table.zgrid td { padding:2px 4px; }\n"
    "body.baseline .chip { padding:1px 5px; margin:0; font-size:11px; }\n"
    "body.baseline .deep-drawer { padding:10px 12px; margin:10px 0; }\n"
    "body.baseline .hint { margin:4px 0; }\n"
    "@media(max-width:640px) {\n"
    "  .page-head,main,.page-foot { padding-left:10px; padding-right:10px; }\n"
    "  .cards { grid-template-columns:minmax(0,1fr); } .scard { min-width:0; }\n"
    "  section,.deep-drawer { padding:12px; } .kpis { grid-template-columns:repeat(2,1fr); }\n"
    "}\n"
    "@media(prefers-reduced-motion:reduce) { * { scroll-behavior:auto!important; "
    "transition:none!important; } }\n"
)

_HTML_SCRIPT = (
    '"use strict";\n'
    "(function () {\n"
    "  const $ = (s) => document.querySelector(s);\n"
    '  const REPORT = JSON.parse($("#grid-data").textContent);\n'
    "  const OBJECTS = __OBJECTS__, MODES = __MODES__, SHARED = __SHARED__;\n"
    "  const tools = REPORT.tools, head = tools.filter((t) => t.head_tool);\n"
    '  const changed = tools.filter((t) => t.status !== "unchanged");\n'
    '  const cardState = { filter: "changed", q: "" };\n'
    '  let dimUnchanged = REPORT.has_surface_changes, selected = null, metric = "jaccard";\n'
    "  function el(tag, attrs = {}, children = []) {\n"
    "    const node = document.createElement(tag);\n"
    "    for (const [key, value] of Object.entries(attrs)) {\n"
    '      if (key === "text") node.textContent = value;\n'
    '      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);\n'
    "      else node.setAttribute(key, value);\n"
    "    }\n"
    "    for (const child of [].concat(children)) if (child != null) node.append(child);\n"
    "    return node;\n"
    "  }\n"
    "  function sv(tag, attrs, parent) {\n"
    '    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);\n'
    "    for (const [key, value] of Object.entries(attrs)) {\n"
    '      if (key === "text") node.textContent = value;\n'
    "      else node.setAttribute(key, value);\n"
    "    }\n"
    "    parent?.append(node);\n"
    "    return node;\n"
    "  }\n"
    '  const signed = (n) => `${n >= 0 ? "+" : ""}${n.toLocaleString()}`;\n'
    '  const badge = (s) => el("span", { class: `status ${s}`, text: s.toUpperCase() });\n'
    "  function segment(label, value, current, fn) {\n"
    '    return el("button", { class: "seg", type: "button", text: label,\n'
    '      "aria-pressed": String(value === current), onclick: () => fn(value) });\n'
    "  }\n"
    "  function applyBrush() {\n"
    '    document.body.classList.toggle("brushing", Boolean(selected));\n'
    '    document.querySelectorAll("[data-tool]").forEach((node) => {\n'
    '      node.classList.toggle("hl", node.dataset.tool === selected);\n'
    "    });\n"
    "  }\n"
    "  function inspect(name) {\n"
    "    const tool = tools.find((t) => t.name === name);\n"
    "    if (!tool) return;\n"
    "    selected = name;\n"
    "    if (!matches(tool)) {\n"
    '      cardState.filter = "all"; cardState.q = "";\n'
    "      renderCardControls(); renderCards();\n"
    "    }\n"
    "    applyBrush();\n"
    "    document.getElementById(`scard-${name}`)?.scrollIntoView({\n"
    '      behavior: "smooth", block: "start"\n'
    "    });\n"
    "  }\n"
    "  function chip(tool) {\n"
    '    return el("button", { class: "chip schip", type: "button", "data-tool": tool.name,\n'
    "      onclick: () => inspect(tool.name) }, [\n"
    '      tool.name, tool.status !== "unchanged" ? badge(tool.status) : null,\n'
    '      el("span", { class: "note", text: `${signed(tool.delta_tokens)}t` })\n'
    "    ]);\n"
    "  }\n"
    "  function renderGate() {\n"
    "    const c = REPORT.counts, limit = REPORT.v1_budget_limit;\n"
    "    const scale = Math.max(limit * 1.1, REPORT.head_tokens);\n"
    '    const gauge = el("div", { class: "budget", role: "meter",\n'
    '      "aria-label": "Model-visible token budget", "aria-valuemin": 0,\n'
    '      "aria-valuemax": limit, "aria-valuenow": REPORT.head_tokens }, [\n'
    '      el("i", { style: `width:${REPORT.head_tokens / scale * 100}%` }),\n'
    '      el("b", { style: `left:${limit / scale * 100}%`, title: `${limit} token limit` })\n'
    "    ]);\n"
    '    const kpi = (label, children) => el("div", { class: "kpi" }, [\n'
    '      el("div", { class: "note", text: label }), ...[].concat(children)\n'
    "    ]);\n"
    '    $("#gate-kpis").replaceChildren(\n'
    '      kpi("Gate", el("span", { class: `gate ${REPORT.gates_passed ? "pass" : "fail"}`,\n'
    '        text: REPORT.gates_passed ? "PASS" : "FAIL" })),\n'
    '      kpi("Token Budget", [el("strong", {\n'
    "        text: `${REPORT.head_tokens.toLocaleString()} / ${limit.toLocaleString()}`\n"
    '      }), gauge, el("span", { class: "note",\n'
    "        text: `Delta ${signed(REPORT.delta_tokens)} · ${limit.toLocaleString()} limit` "
    "})]),\n"
    '      kpi("Tool Changes", [el("strong", {\n'
    "        text: `${c.added} / ${c.modified} / ${c.removed} / ${c.unchanged}`\n"
    '      }), el("span", { class: "note", text: "added / modified / removed / unchanged" '
    "})]),\n"
    '      kpi("Cell Collisions", el("strong", { text: REPORT.collisions.length })),\n'
    '      kpi("Ad-hoc Parameter Violations", el("strong", {\n'
    "        text: REPORT.ad_hoc_violations.length\n"
    "      }))\n"
    "    );\n"
    "    const alerts = [...REPORT.collisions, ...REPORT.ad_hoc_violations];\n"
    '    if (REPORT.budget_exceeded) alerts.push("Model-visible token budget exceeded.");\n'
    "    for (const t of changed) {\n"
    "      if (t.semantic_overlaps?.length) {\n"
    '        alerts.push(`${t.name}: semantic overlap with ${t.semantic_overlaps.join(", '
    '")}`);\n'
    "      }\n"
    "    }\n"
    '    $("#gate-alerts").replaceChildren(...alerts.map((text) => el("div", {\n'
    '      class: "alert", role: "alert", text\n'
    "    })));\n"
    '    $("#grid-note").textContent = REPORT.has_surface_changes\n'
    '      ? "Changed and colliding cells stay fully visible. Click a tool to inspect its '
    'diff."\n'
    '      : "0 tools changed in this PR. All cells are shown at full opacity.";\n'
    "  }\n"
    "  function renderGrid() {\n"
    '    const controls = $("#grid-controls"), value = dimUnchanged ? "dim" : "all";\n'
    '    const update = (v) => { dimUnchanged = v === "dim"; renderGrid(); };\n'
    "    controls.replaceChildren(\n"
    '      segment("Dim Unchanged Cells", "dim", value, update),\n'
    '      segment("Show All Equally", "all", value, update)\n'
    "    );\n"
    '    const table = el("table", { class: "zgrid" }, el("thead", {}, el("tr", {}, [\n'
    '      el("th", { text: "Object x Access Mode" }),\n'
    '      ...MODES.map((text) => el("th", { scope: "col", text }))\n'
    "    ])));\n"
    '    const body = el("tbody");\n'
    "    for (const obj of OBJECTS) {\n"
    '      const row = el("tr", {}, el("th", { scope: "row", text: obj }));\n'
    "      for (const mode of MODES) {\n"
    "        const cell = REPORT.cells.find((c) => c.object === obj && c.mode === mode);\n"
    '        const stable = cell.status === "unchanged" && !cell.has_collision;\n'
    "        const dim = dimUnchanged && REPORT.has_surface_changes && stable;\n"
    '        const td = el("td", { class: `${cell.tools.length ? "" : "empty"} ${dim ? "dim" '
    ': ""}` });\n'
    '        if (cell.has_collision) td.append(badge("collision"));\n'
    "        for (const item of cell.tools) {\n"
    "          const record = tools.find((t) => t.name === item.name);\n"
    "          td.append(chip(record));\n"
    "        }\n"
    "        row.append(td);\n"
    "      }\n"
    "      body.append(row);\n"
    "    }\n"
    '    table.append(body); $("#grid").replaceChildren(table); applyBrush();\n'
    "  }\n"
    "  function matches(t) {\n"
    '    if (cardState.filter === "changed" && t.status === "unchanged") return false;\n'
    '    if (!["all", "changed"].includes(cardState.filter)\n'
    "      && cardState.filter !== t.status) return false;\n"
    "    const raw = t.head_tool || t.base_tool || {};\n"
    "    const props = Object.keys(raw.inputSchema?.properties || {});\n"
    '    const search = [t.name, t.object, t.mode, raw.description || "", ...props];\n'
    '    return search.join(" ").toLowerCase().includes(cardState.q.trim().toLowerCase());\n'
    "  }\n"
    "  function renderCardControls() {\n"
    '    const controls = $("#card-controls");\n'
    '    const options = [["changed", `Changed (${changed.length})`],\n'
    '      ["all", `All Tools (${tools.length})`], ["added", "Added"],\n'
    '      ["modified", "Modified"], ["removed", "Removed"]];\n'
    "    controls.replaceChildren(...options.map(([value, label]) => segment(\n"
    "      label, value, cardState.filter, (v) => {\n"
    "        cardState.filter = v; renderCardControls(); renderCards();\n"
    "      }\n"
    '    )), el("input", { type: "search", value: cardState.q,\n'
    '      placeholder: "Search tools or parameters", "aria-label": "Search tools",\n'
    "      oninput: (event) => { cardState.q = event.target.value; renderCards(); }\n"
    "    }));\n"
    "  }\n"
    "  function table(headers, rows) {\n"
    '    return el("table", { class: "ptable" }, [\n'
    '      el("thead", {}, el("tr", {}, headers.map((text) => el("th", { text })))),\n'
    '      el("tbody", {}, rows.map((row) => el("tr", {}, row.map((text) => el("td", { text '
    "})))))\n"
    "    ]);\n"
    "  }\n"
    "  function renderCards() {\n"
    '    const list = tools.filter(matches), holder = $("#cards");\n'
    '    if (!REPORT.has_surface_changes && cardState.filter === "changed" && !cardState.q) '
    "{\n"
    '      holder.replaceChildren(el("div", { class: "empty-state" }, [\n'
    '        "No tools changed between Base and Head. ",\n'
    '        el("button", { class: "seg", type: "button",\n'
    "          text: `Inspect All ${REPORT.head_tool_count} Baseline Tools`, onclick: () => "
    "{\n"
    '            cardState.filter = "all"; renderCardControls(); renderCards();\n'
    "          }\n"
    "        })\n"
    "      ]));\n"
    "      return;\n"
    "    }\n"
    "    if (!list.length) {\n"
    '      holder.replaceChildren(el("div", { class: "empty-state", text: "No matching '
    'tools." }));\n'
    "      return;\n"
    "    }\n"
    "    holder.replaceChildren(...list.map((t) => {\n"
    "      const raw = t.head_tool || t.base_tool || {}, schema = raw.inputSchema || {};\n"
    "      const properties = schema.properties || {}, required = new Set(schema.required || "
    "[]);\n"
    '      const reused = SHARED.filter((s) => t.shared_types[s] === "reused");\n'
    "      const warnings = [...t.violations];\n"
    "      if (t.semantic_overlaps?.length) {\n"
    '        warnings.push(`Semantic overlap: ${t.semantic_overlaps.join(", ")}`);\n'
    "      }\n"
    '      const card = el("article", { class: "scard", id: `scard-${t.name}`,\n'
    '        "data-tool": t.name }, [el("header", {}, [\n'
    '          el("code", { text: t.name }), badge(t.status),\n'
    '          el("span", { class: "note", text: `${t.object} x ${t.mode}` }),\n'
    '          el("span", { class: "note", text: `${t.head_tool ? t.head_tokens : '
    "t.base_tokens}`\n"
    "            + ` tokens (${signed(t.delta_tokens)})` })\n"
    '        ]), el("p", { text: raw.description || "" }),\n'
    '        el("div", { class: "meta" }, ["Shared $defs: ",\n'
    '          ...reused.map((text) => el("span", { class: "feat", text })),\n'
    '          reused.length ? null : "none"\n'
    '        ]), ...warnings.map((text) => el("div", { class: "alert", text }))\n'
    "      ]);\n"
    '      if (t.status !== "unchanged") card.append(el("details", { open: "" }, [\n'
    '        el("summary", { text: "Unified Schema Diff" }),\n'
    '        el("pre", { class: "diff-block", text: t.schema_diff || "(no schema diff)" })\n'
    "      ]));\n"
    "      const rows = Object.entries(properties).map(([name, spec]) => [\n"
    '        `${name}${required.has(name) ? " (required)" : ""}`,\n'
    '        spec.type || spec.$ref || JSON.stringify(spec.anyOf || spec.oneOf || "object"),\n'
    '        required.has(name) ? "" : JSON.stringify(spec.default ?? null), '
    'spec.description || ""\n'
    "      ]);\n"
    '      card.append(el("details", {}, [\n'
    '        el("summary", { text: "Parameters & Annotations" }),\n'
    '        table(["Parameter", "Type", "Default", "Description"], rows),\n'
    '        el("pre", { text: JSON.stringify(raw.annotations || {}, null, 2) })\n'
    "      ]));\n"
    "      return card;\n"
    "    }));\n"
    "    applyBrush();\n"
    "  }\n"
    "  function signature(tool) {\n"
    "    return new Set([...Object.keys(tool.head_tool.inputSchema?.properties || {}),\n"
    '      ...SHARED.filter((s) => tool.shared_types[s] === "reused").map((s) => '
    "`def:${s}`)]);\n"
    "  }\n"
    "  const signatures = head.map(signature), pairs = [];\n"
    "  for (let i = 0; i < head.length; i++) for (let j = i + 1; j < head.length; j++) {\n"
    "    const a = signatures[i], b = signatures[j], shared = [...a].filter((x) => "
    "b.has(x));\n"
    "    const union = a.size + b.size - shared.length, min = Math.min(a.size, b.size);\n"
    "    pairs.push({ i, j, shared, jaccard: union ? shared.length / union : 0,\n"
    "      containment: min ? shared.length / min : 0 });\n"
    "  }\n"
    "  function matrixSvg(rows, columns, title, cell) {\n"
    "    const size = 18, left = 155, top = 140;\n"
    "    const width = left + columns.length * size + 8, height = top + rows.length * size + "
    "8;\n"
    '    const svg = sv("svg", { width, height, viewBox: `0 0 ${width} ${height}`,\n'
    '      role: "img", "aria-label": title });\n'
    '    rows.forEach((name, i) => sv("text", { x: left - 8, y: top + i * size + 13,\n'
    '      "text-anchor": "end", text: name }, svg));\n'
    '    columns.forEach((tool, j) => sv("text", {\n'
    "      transform: `translate(${left + j * size + 12},${top - 6}) rotate(-60)`,\n"
    '      "data-tool": tool.name, text: tool.name\n'
    "    }, svg));\n"
    "    rows.forEach((name, i) => columns.forEach((tool, j) => {\n"
    "      const data = cell(i, j);\n"
    '      const node = sv("rect", { x: left + j * size, y: top + i * size,\n'
    "        width: size - 1, height: size - 1, fill: data.fill,\n"
    '        "data-tool": tool.name, tabindex: "0" }, svg);\n'
    '      sv("title", { text: data.label }, node);\n'
    "    }));\n"
    "    return svg;\n"
    "  }\n"
    "  function renderDefs() {\n"
    '    $("#defs-matrix").replaceChildren(matrixSvg(SHARED, head,\n'
    '      "Shared $defs Contract Matrix", (i, j) => {\n'
    "        const state = head[j].shared_types[SHARED[i]];\n"
    '        const fill = state === "violation" ? "var(--bad)"\n'
    '          : state === "reused" ? "var(--accent)" : "var(--band)";\n'
    "        return { fill, label: `${SHARED[i]} / ${head[j].name}: ${state}` };\n"
    "      }\n"
    "    ));\n"
    "    const details = SHARED.map((name) => {\n"
    "      const variants = new Map();\n"
    "      for (const t of head) {\n"
    "        const definition = t.head_tool.inputSchema?.$defs?.[name];\n"
    "        if (!definition) continue;\n"
    "        const key = JSON.stringify(definition, null, 2);\n"
    "        if (!variants.has(key)) variants.set(key, []);\n"
    "        variants.get(key).push(t.name);\n"
    "      }\n"
    '      const children = [el("summary", { text: `$defs.${name} JSON schemas` })];\n'
    "      for (const [schema, names] of variants) children.push(\n"
    '        el("p", { class: "note", text: names.join(", ") }),\n'
    '        el("pre", { class: "diff-block", text: schema })\n'
    "      );\n"
    '      if (!variants.size) children.push(el("p", {\n'
    '        class: "note", text: "No definition is declared by the Head tools."\n'
    "      }));\n"
    '      return el("details", {}, children);\n'
    "    });\n"
    '    $("#defs-schemas").replaceChildren(...details);\n'
    "  }\n"
    "  function renderDSM() {\n"
    "    const choose = (value) => { metric = value; renderDSM(); };\n"
    '    $("#metric-controls").replaceChildren(\n'
    '      segment("Jaccard", "jaccard", metric, choose),\n'
    '      segment("Containment", "containment", metric, choose)\n'
    "    );\n"
    "    const values = Array.from({ length: head.length }, () => "
    "Array(head.length).fill(0));\n"
    "    for (const pair of pairs) values[pair.i][pair.j] = values[pair.j][pair.i] = "
    "pair[metric];\n"
    '    $("#dsm-heatmap").replaceChildren(matrixSvg(head.map((t) => t.name), head,\n'
    '      "Tool x Tool Coupling DSM Heatmap", (i, j) => {\n'
    "        const value = values[i][j];\n"
    '        return { fill: i === j ? "var(--line)" : value\n'
    "          ? `color-mix(in srgb,var(--accent) ${Math.round(value * 100)}%,var(--band))`\n"
    '          : "var(--band)", label: `${head[i].name} / ${head[j].name}: `\n'
    "            + `${metric} ${value.toFixed(3)}` };\n"
    "      }\n"
    "    ));\n"
    "    const top = pairs.filter((p) => p.jaccard >= .4 || p.containment >= .6)\n"
    "      .sort((a, b) => b[metric] - a[metric] || "
    "head[a.i].name.localeCompare(head[b.i].name));\n"
    '    $("#pairs").replaceChildren(table(\n'
    '      ["Tool A", "Tool B", "Jaccard", "Containment", "Shared Concepts"],\n'
    "      top.slice(0, 12).map((p) => [head[p.i].name, head[p.j].name,\n"
    '        p.jaccard.toFixed(2), p.containment.toFixed(2), p.shared.join(", ")])\n'
    "    ));\n"
    "    applyBrush();\n"
    "  }\n"
    '  const tooltip = $("#tooltip");\n'
    "  function showTooltip(target, event) {\n"
    "    const tool = tools.find((t) => t.name === target.dataset.tool);\n"
    "    if (!tool) return;\n"
    '    tooltip.replaceChildren(el("b", { text: tool.name }), el("div", {\n'
    "      text: `${tool.object} x ${tool.mode} · ${tool.status} · `\n"
    "        + `${tool.head_tool ? tool.head_tokens : tool.base_tokens} tokens `\n"
    "        + `(${signed(tool.delta_tokens)})`\n"
    "    }));\n"
    "    tooltip.hidden = false;\n"
    "    const bounds = target.getBoundingClientRect();\n"
    "    const x = event.clientX ?? bounds.right, y = event.clientY ?? bounds.top;\n"
    "    tooltip.style.left = Math.max(8, Math.min(x + 12,\n"
    '      innerWidth - tooltip.offsetWidth - 8)) + "px";\n'
    "    tooltip.style.top = Math.max(8, Math.min(y + 12,\n"
    '      innerHeight - tooltip.offsetHeight - 8)) + "px";\n'
    "  }\n"
    '  document.addEventListener("click", (event) => {\n'
    '    const target = event.target.closest("svg [data-tool]");\n'
    "    if (target) inspect(target.dataset.tool);\n"
    "  });\n"
    '  document.addEventListener("mouseover", (event) => {\n'
    '    const target = event.target.closest("[data-tool]");\n'
    "    if (target) showTooltip(target, event);\n"
    "  });\n"
    '  document.addEventListener("focusin", (event) => {\n'
    '    const target = event.target.closest("[data-tool]");\n'
    "    if (target) showTooltip(target, event);\n"
    "  });\n"
    '  document.addEventListener("mouseout", () => { tooltip.hidden = true; });\n'
    '  document.addEventListener("focusout", () => { tooltip.hidden = true; });\n'
    '  document.addEventListener("keydown", (event) => {\n'
    '    if (["Enter", " "].includes(event.key)) {\n'
    '      const target = event.target.closest("svg [data-tool]");\n'
    "      if (target) { event.preventDefault(); inspect(target.dataset.tool); }\n"
    "    }\n"
    '    if (event.key === "Escape") { selected = null; tooltip.hidden = true; applyBrush(); '
    "}\n"
    "  });\n"
    "  document.querySelector('nav a[href=\"#sDeep\"]').onclick = () => {\n"
    '    $("#sDeep").open = true;\n'
    "  };\n"
    '  document.body.classList.toggle("baseline", !REPORT.has_surface_changes);\n'
    "  renderGate(); renderGrid(); renderCardControls(); renderCards(); renderDefs(); "
    "renderDSM();\n"
    "})();"
)

_HTML_DOCUMENT = (
    "<!doctype html>\n"
    '<html lang="en">\n'
    "<head>\n"
    '<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    '<meta name="color-scheme" content="light dark">\n'
    "<title>kwin-mcp Tool Surface (__TITLE__)</title>\n"
    "<style>__STYLE__</style>\n"
    "</head>\n"
    "<body>\n"
    '<header class="page-head">\n'
    "<h1>kwin-mcp MCP Tool Surface</h1>\n"
    '<p class="lede"><span class="ref-badge">Base → Head: __TITLE__</span></p>\n'
    '<nav aria-label="Sections"><a href="#sGate">Gate &amp; Budget</a>\n'
    '<a href="#sGrid">Object &times; Mode Grid</a><a href="#sCards">Changed Tools</a>\n'
    '<a href="#sDeep">Deep Matrices</a></nav>\n'
    "</header>\n"
    "<main>\n"
    '<section id="sGate"><h2>Gate &amp; Token Budget Strip</h2>\n'
    '<div id="gate-kpis" class="kpis"></div><div id="gate-alerts" class="alerts"></div>\n'
    "</section>\n"
    '<section id="sGrid"><div class="section-head">\n'
    '<h2>Object &times; Access Mode Zwicky Grid</h2><div id="grid-controls" '
    'class="controls"></div>\n'
    '</div><p id="grid-note" class="hint"></p><div id="grid" class="scroll-x"></div>\n'
    "</section>\n"
    '<section id="sCards"><h2>Changed Tools &amp; Schema Diff Deck</h2>\n'
    '<div id="card-controls" class="controls"></div><div id="cards" class="cards"></div>\n'
    "</section>\n"
    '<details id="sDeep" class="deep-drawer">\n'
    "<summary>Deep Matrices — Shared $defs Contract Matrix (8&times;N) &amp; "
    "Tool&times;Tool Coupling Heatmap (DSM)</summary>\n"
    '<section id="s4"><h2>Shared $defs &times; Tool Matrix</h2>\n'
    '<p class="hint">Blue cells reuse the shared contract; red cells flag ad-hoc '
    "violations.</p>\n"
    '<div id="defs-matrix" class="scroll-x"></div><div id="defs-schemas"></div>\n'
    "</section>\n"
    '<section id="s3"><h2>Tool &times; Tool Coupling DSM Heatmap</h2>\n'
    '<div id="metric-controls" class="controls"></div><div id="dsm-heatmap" '
    'class="scroll-x"></div>\n'
    '<h3>Top Overlapping Tool Pairs</h3><div id="pairs" class="scroll-x"></div>\n'
    "</section></details>\n"
    "</main>\n"
    '<footer class="page-foot">Generated by <code>scripts/render_tool_surface.py</code>\n'
    " · Static schema analysis; semantic overlap is advisory, not an extra gate.</footer>\n"
    '<div id="tooltip" role="tooltip" hidden></div>\n'
    '<script id="grid-data" type="application/json">__REPORT__</script>\n'
    "<script>__SCRIPT__</script>\n"
    "</body>\n"
    "</html>\n"
)


def render_html(report: dict[str, Any]) -> str:
    """Render a standalone, interactive HCI tool-surface visualization page."""
    payload = json.dumps(report, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    script_values = {
        "__OBJECTS__": json.dumps(DOMAIN_OBJECTS),
        "__MODES__": json.dumps(ACCESS_MODES),
        "__SHARED__": json.dumps(SHARED_TYPES),
    }
    script = re.sub(r"__(?:OBJECTS|MODES|SHARED)__", lambda m: script_values[m[0]], _HTML_SCRIPT)
    title = f"{html.escape(str(report['base_label']))} -> {html.escape(str(report['head_label']))}"
    values = {
        "__STYLE__": _HTML_STYLES,
        "__TITLE__": title,
        "__REPORT__": payload,
        "__SCRIPT__": script,
    }
    return re.sub(r"__(?:STYLE|TITLE|REPORT|SCRIPT)__", lambda m: values[m[0]], _HTML_DOCUMENT)


def _load_tools(value: str) -> list[dict[str, Any]]:
    path = pathlib.Path(value)
    if not path.exists() and value == _DEFAULT_SNAPSHOT:
        fallback = _REPO_ROOT / _DEFAULT_SNAPSHOT
        if fallback.exists():
            path = fallback
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        spec = value if ":" in value else f"{value}:tests/unit/snapshots/tool_surface.json"
        result = subprocess.run(
            ["git", "show", spec],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
        if result.returncode != 0:
            stderr = result.stderr.lower()
            if "exists on disk, but not in" in stderr or "does not exist in" in stderr:
                return []
            raise subprocess.CalledProcessError(
                result.returncode, result.args, output=result.stdout, stderr=result.stderr
            )
        data = json.loads(result.stdout)
    if not isinstance(data, list):
        raise ValueError(f"tool surface must be a JSON list: {value}")
    return [tool for tool in data if isinstance(tool, dict)]


def _label(value: str) -> str:
    if ":" in value:
        return value.split(":", 1)[0]
    return pathlib.Path(value).stem or value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a kwin-mcp tool-surface diff")
    parser.add_argument("--base", default="tests/unit/snapshots/tool_surface.json")
    parser.add_argument("--head", default="tests/unit/snapshots/tool_surface.json")
    parser.add_argument("--base-label")
    parser.add_argument("--head-label")
    parser.add_argument("--html-out", type=pathlib.Path)
    parser.add_argument("--markdown-out", type=pathlib.Path)
    parser.add_argument("--json-out", type=pathlib.Path)
    parser.add_argument("--preview-url")
    parser.add_argument("--artifact-url")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    report = compute_diff(
        _load_tools(args.base),
        _load_tools(args.head),
        base_label=args.base_label or _label(args.base),
        head_label=args.head_label or _label(args.head),
    )
    outputs = {
        args.html_out: render_html(report),
        args.markdown_out: render_markdown(
            report, preview_url=args.preview_url, artifact_url=args.artifact_url
        ),
        args.json_out: json.dumps(report, ensure_ascii=False, indent=2) + "\n",
    }
    for path, content in outputs.items():
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    if not any(path is not None for path in outputs):
        sys.stdout.write(
            render_markdown(report, preview_url=args.preview_url, artifact_url=args.artifact_url)
        )
    return 1 if args.check and not report["gates_passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
