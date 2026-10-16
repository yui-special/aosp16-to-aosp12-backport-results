"""Data contracts for the hunk grouping agent.

The implementation document fixes the shape of the interchange JSON.  Every
structure that crosses a module boundary is validated here so that assumptions
never leak into the graph or grouping logic.
"""

from __future__ import annotations

import re
from typing import Any
from collections.abc import Iterable

from . import ANALYSIS_VERSION, SchemaError

__all__ = [
    "CATEGORIES",
    "STRENGTHS",
    "TARGET_STATUS",
    "RETRIEVAL_STATUS",
    "ANALYSIS_STATUS",
    "GROUPING_STATUS",
    "RESOLUTION_VALUES",
    "STRENGTH_ORDER",
    "HUNK_ID_RE",
    "GROUP_ID_RE",
    "is_hunk_id",
    "hunk_sort_key",
    "sort_hunk_ids",
    "canonical_pair",
    "check_hunk",
    "normalize_hunk",
    "check_dependency",
    "normalize_dependency",
    "require_dependency",
    "check_evidence_item",
    "normalize_evidence_item",
    "evidence_is_locatable",
    "check_analysis_result",
    "check_grouping_document",
    "check_context_record",
    "dedupe",
    "new_analysis_result",
    "new_grouping_document",
]

CATEGORIES: tuple[str, ...] = (
    "DEFINITION_USE",
    "INTERFACE_SIGNATURE",
    "CALL_GRAPH",
    "SHARED_STATE",
    "CONTROL_FLOW",
    "SEMANTIC_INVARIANT",
    "CONTEXT_DEPENDENCY",
)

STRENGTHS: tuple[str, ...] = ("STRONG", "CONDITIONAL", "WEAK", "NONE")
STRENGTH_ORDER: dict[str, int] = {name: rank for rank, name in enumerate(STRENGTHS)}

TARGET_STATUS: tuple[str, ...] = ("present", "absent", "partially_present", "unknown")
RETRIEVAL_STATUS: tuple[str, ...] = ("complete", "partial", "unavailable", "failed")
ANALYSIS_STATUS: tuple[str, ...] = ("complete", "partial", "failed")
GROUPING_STATUS: tuple[str, ...] = ("complete", "partial", "failed")
RESOLUTION_VALUES: tuple[str, ...] = ("promote", "keep", "drop")

HUNK_ID_RE = re.compile(r"^H[1-9][0-9]*$")
GROUP_ID_RE = re.compile(r"^G[1-9][0-9]*$")
DEPENDENCY_ID_RE = re.compile(r"^D[1-9][0-9]*$")


def is_hunk_id(value: Any) -> bool:
    return isinstance(value, str) and bool(HUNK_ID_RE.match(value))


def hunk_sort_key(hunk_id: str) -> tuple[int, str]:
    """Numeric-aware ordering so ``H2`` sorts before ``H10``."""
    text = str(hunk_id)
    match = HUNK_ID_RE.match(text)
    if not match:
        return (1 << 30, text)
    return (int(text[1:]), text)


def sort_hunk_ids(hunk_ids: Iterable[str]) -> list[str]:
    return sorted({str(h) for h in hunk_ids}, key=hunk_sort_key)


def canonical_pair(a: str, b: str) -> tuple[str, str]:
    """Unordered endpoint pair in a deterministic order."""
    left, right = sorted((str(a), str(b)), key=hunk_sort_key)
    return left, right


def dedupe(values: Iterable[Any]) -> list[Any]:
    """Order-preserving de-duplication."""
    seen: set[str] = set()
    out: list[Any] = []
    for value in values:
        marker = value if isinstance(value, str) else repr(value)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(value)
    return out


HUNK_REQUIRED_FIELDS: tuple[str, ...] = (
    "hunk_id",
    "file_path",
    "old_start",
    "old_count",
    "new_start",
    "new_count",
    "header",
    "diff_text",
)

_HUNK_LIST_FIELDS = ("added_lines", "removed_lines", "context_lines", "symbols", "evidence")


def check_hunk(hunk: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(hunk, dict):
        return [f"hunk must be a mapping, got {type(hunk).__name__}"]
    for field in HUNK_REQUIRED_FIELDS:
        if field not in hunk:
            problems.append(f"missing required field {field!r}")
    if "hunk_id" in hunk and not is_hunk_id(hunk["hunk_id"]):
        problems.append(f"invalid hunk_id {hunk.get('hunk_id')!r} (expected H<number>)")
    for field in ("old_start", "old_count", "new_start", "new_count"):
        value = hunk.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            problems.append(f"{field} must be a non-negative integer, got {value!r}")
    for field in _HUNK_LIST_FIELDS:
        if field in hunk and not isinstance(hunk[field], list):
            problems.append(f"{field} must be a list")
    if "file_path" in hunk and not isinstance(hunk["file_path"], str):
        problems.append("file_path must be a string")
    if "line_map" in hunk and not isinstance(hunk["line_map"], list):
        problems.append("line_map must be a list")
    return problems


def normalize_hunk(hunk: dict) -> dict:
    """Fill optional hunk fields with documented defaults."""
    out = dict(hunk)
    out.setdefault("old_path", out.get("file_path", ""))
    out.setdefault("new_path", out.get("target_file_path", out.get("file_path", "")))
    out.setdefault("section", "")
    out.setdefault("file_change_type", "modify")
    out.setdefault("parse_warnings", [])
    for field in _HUNK_LIST_FIELDS:
        out.setdefault(field, [])
    out.setdefault("line_map", [])
    return out


_EVIDENCE_TEXT_FIELDS = ("file_path", "symbol", "description")


def check_evidence_item(item: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(item, dict):
        return [f"evidence item must be a mapping, got {type(item).__name__}"]
    if not any(str(item.get(field) or "").strip() for field in _EVIDENCE_TEXT_FIELDS):
        problems.append("evidence item needs at least one of file_path/symbol/description")
    for field in ("line", "line_start", "line_end"):
        if field in item and item[field] is not None:
            value = item[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                problems.append(f"evidence.{field} must be a non-negative integer, got {value!r}")
    return problems


def normalize_evidence_item(item: dict) -> dict:
    out: dict[str, Any] = {
        "file_path": item.get("file_path") or "",
        "symbol": item.get("symbol") or "",
        "description": item.get("description") or "",
    }
    for extra in ("line", "line_start", "line_end", "revision", "hunk_id", "quote", "source"):
        if item.get(extra) is not None:
            out[extra] = item[extra]
    return out


def evidence_is_locatable(item: dict) -> bool:
    """True when the evidence points at something a reviewer can look up."""
    if not isinstance(item, dict):
        return False
    has_symbol = bool(str(item.get("symbol") or "").strip())
    has_file = bool(str(item.get("file_path") or "").strip())
    has_line = item.get("line") is not None or item.get("line_start") is not None
    has_quote = bool(str(item.get("quote") or "").strip())
    return bool(has_symbol or has_quote or (has_file and has_line))


DEPENDENCY_REQUIRED_FIELDS: tuple[str, ...] = (
    "source_hunk",
    "target_hunk",
    "category",
    "strength",
)


def check_dependency(dep: Any, *, known_hunks: Iterable[str] | None = None) -> list[str]:
    problems: list[str] = []
    if not isinstance(dep, dict):
        return [f"dependency must be a mapping, got {type(dep).__name__}"]
    for field in DEPENDENCY_REQUIRED_FIELDS:
        if field not in dep:
            problems.append(f"missing required field {field!r}")
    for field in ("source_hunk", "target_hunk"):
        if field in dep and not is_hunk_id(dep[field]):
            problems.append(f"{field} must look like H<number>, got {dep.get(field)!r}")
    if "category" in dep and dep["category"] not in CATEGORIES:
        problems.append(f"unknown category {dep.get('category')!r}")
    if "strength" in dep and dep["strength"] not in STRENGTHS:
        problems.append(f"unknown strength {dep.get('strength')!r}")
    status = dep.get("target_version_status", "unknown")
    if status not in TARGET_STATUS:
        problems.append(f"unknown target_version_status {status!r}")
    if "evidence" in dep:
        if not isinstance(dep["evidence"], list):
            problems.append("evidence must be a list")
        else:
            for index, item in enumerate(dep["evidence"]):
                for problem in check_evidence_item(item):
                    problems.append(f"evidence[{index}]: {problem}")
    if "needs_more_evidence" in dep and not isinstance(dep["needs_more_evidence"], bool):
        problems.append("needs_more_evidence must be a boolean")
    if known_hunks is not None:
        known = {str(h) for h in known_hunks}
        for field in ("source_hunk", "target_hunk"):
            value = dep.get(field)
            if is_hunk_id(value) and value not in known:
                problems.append(f"{field} {value!r} is not part of this patch")
    return problems


def normalize_dependency(dep: dict) -> dict:
    """Canonicalise a dependency record: fill defaults, drop unknown extras."""
    evidence = [
        normalize_evidence_item(item) for item in dep.get("evidence") or [] if isinstance(item, dict)
    ]
    out: dict[str, Any] = {
        "dependency_id": dep.get("dependency_id") or None,
        "source_hunk": dep.get("source_hunk"),
        "target_hunk": dep.get("target_hunk"),
        "category": dep.get("category"),
        "strength": dep.get("strength"),
        "evidence": evidence,
        "separation_risk": str(dep.get("separation_risk") or ""),
        "target_version_status": dep.get("target_version_status") or "unknown",
        "reasoning_summary": str(dep.get("reasoning_summary") or ""),
        "needs_more_evidence": bool(dep.get("needs_more_evidence", False)),
    }
    if dep.get("origin"):
        out["origin"] = dep["origin"]
    if dep.get("extra_evidence_rounds") is not None:
        out["extra_evidence_rounds"] = dep["extra_evidence_rounds"]
    if dep.get("notes"):
        out["notes"] = list(dep["notes"])
    return out


def require_dependency(dep: dict, *, known_hunks: Iterable[str] | None = None) -> dict:
    problems = check_dependency(dep, known_hunks=known_hunks)
    if problems:
        raise SchemaError("invalid dependency record: " + "; ".join(problems))
    return normalize_dependency(dep)


def check_analysis_result(doc: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(doc, dict):
        return [f"analysis result must be a mapping, got {type(doc).__name__}"]
    for field in ("patch_id", "status", "hunk_ids", "dependencies"):
        if field not in doc:
            problems.append(f"missing required field {field!r}")
    if doc.get("status") not in ANALYSIS_STATUS:
        problems.append(f"invalid status {doc.get('status')!r}")
    hunk_ids = doc.get("hunk_ids")
    known: list[str] | None = None
    if not isinstance(hunk_ids, list) or not hunk_ids:
        problems.append("hunk_ids must be a non-empty list")
    else:
        known = [str(h) for h in hunk_ids]
        if len(set(known)) != len(known):
            problems.append("hunk_ids contains duplicates")
        for hunk_id in known:
            if not is_hunk_id(hunk_id):
                problems.append(f"hunk_ids contains invalid id {hunk_id!r}")
    dependencies = doc.get("dependencies")
    if dependencies is None:
        pass
    elif not isinstance(dependencies, list):
        problems.append("dependencies must be a list")
    else:
        for index, dep in enumerate(dependencies):
            for problem in check_dependency(dep, known_hunks=known):
                problems.append(f"dependencies[{index}]: {problem}")
    if "unresolved_questions" in doc and not isinstance(doc["unresolved_questions"], list):
        problems.append("unresolved_questions must be a list")
    return problems


def check_grouping_document(doc: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(doc, dict):
        return [f"grouping document must be a mapping, got {type(doc).__name__}"]
    for field in ("patch_id", "status", "groups"):
        if field not in doc:
            problems.append(f"missing required field {field!r}")
    if doc.get("status") not in GROUPING_STATUS:
        problems.append(f"invalid status {doc.get('status')!r}")
    groups = doc.get("groups")
    if groups is None or not isinstance(groups, list):
        problems.append("groups must be a list")
    else:
        seen_ids: set[str] = set()
        seen_hunks: list[str] = []
        for index, group in enumerate(groups):
            if not isinstance(group, dict):
                problems.append(f"groups[{index}] must be a mapping")
                continue
            group_id = group.get("group_id")
            if not isinstance(group_id, str) or not GROUP_ID_RE.match(group_id):
                problems.append(f"groups[{index}].group_id must look like G<number>")
            elif group_id in seen_ids:
                problems.append(f"duplicate group_id {group_id}")
            else:
                seen_ids.add(group_id)
            hunk_ids = group.get("hunk_ids")
            if not isinstance(hunk_ids, list) or not hunk_ids:
                problems.append(f"groups[{index}].hunk_ids must be a non-empty list")
            else:
                for hunk_id in hunk_ids:
                    if not is_hunk_id(hunk_id):
                        problems.append(f"groups[{index}] has invalid hunk id {hunk_id!r}")
                    seen_hunks.append(hunk_id)
                if len(set(hunk_ids)) != len(hunk_ids):
                    problems.append(f"groups[{index}].hunk_ids contains duplicates")
            if "dependency_ids" in group and not isinstance(group["dependency_ids"], list):
                problems.append(f"groups[{index}].dependency_ids must be a list")
        if len(set(seen_hunks)) != len(seen_hunks):
            problems.append("a hunk is assigned to more than one group")
    if "cross_group_dependencies" in doc and not isinstance(doc["cross_group_dependencies"], list):
        problems.append("cross_group_dependencies must be a list")
    if "unresolved_questions" in doc and not isinstance(doc["unresolved_questions"], list):
        problems.append("unresolved_questions must be a list")
    return problems


def check_context_record(record: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(record, dict):
        return [f"context record must be a mapping, got {type(record).__name__}"]
    if not is_hunk_id(record.get("hunk_id")):
        problems.append("context record needs a valid hunk_id")
    if record.get("retrieval_status") not in RETRIEVAL_STATUS:
        problems.append(f"invalid retrieval_status {record.get('retrieval_status')!r}")
    for side in ("source_context", "target_context"):
        block = record.get(side)
        if not isinstance(block, dict):
            problems.append(f"{side} must be a mapping with an 'available' flag")
            continue
        if not isinstance(block.get("available"), bool):
            problems.append(f"{side}.available must be a boolean")
        if block.get("available") and not isinstance(block.get("code", ""), str):
            problems.append(f"{side}.code must be a string")
    if not isinstance(record.get("symbol_evidence", []), list):
        problems.append("symbol_evidence must be a list")
    return problems


def check_grouping_against_analysis(grouping: dict, analysis: dict) -> list[str]:
    """Cross-document consistency check used by the partition validator."""
    problems: list[str] = []
    if grouping.get("patch_id") != analysis.get("patch_id"):
        problems.append(
            f"patch_id mismatch: grouping={grouping.get('patch_id')!r} analysis={analysis.get('patch_id')!r}"
        )
    grouping_hunks = [h for group in grouping.get("groups") or [] for h in group.get("hunk_ids") or []]
    analysis_hunks = [str(h) for h in analysis.get("hunk_ids") or []]
    if set(grouping_hunks) != set(analysis_hunks):
        problems.append("grouping hunk set differs from analysis hunk set")
    return problems


def new_analysis_result(
    patch_id: str,
    hunk_ids: list[str],
    dependencies: list[dict],
    *,
    status: str = "complete",
    unresolved_questions: list[dict] | None = None,
    metadata: dict | None = None,
    rejected_records: list[dict] | None = None,
    call_records: list[dict] | None = None,
) -> dict:
    meta: dict[str, Any] = {"analysis_version": ANALYSIS_VERSION}
    meta.update(metadata or {})
    return {
        "patch_id": patch_id,
        "status": status,
        "hunk_ids": list(hunk_ids),
        "dependencies": dependencies,
        "unresolved_questions": unresolved_questions or [],
        "metadata": meta,
        "rejected_records": rejected_records or [],
        "call_records": call_records or [],
    }


def new_grouping_document(
    patch_id: str,
    status: str,
    groups: list[dict],
    *,
    cross_group_dependencies: list[dict] | None = None,
    unresolved_questions: list[dict] | None = None,
    validation: dict | None = None,
    metadata: dict | None = None,
) -> dict:
    return {
        "patch_id": patch_id,
        "status": status,
        "groups": groups,
        "cross_group_dependencies": cross_group_dependencies or [],
        "unresolved_questions": unresolved_questions or [],
        "validation": validation
        or {
            "all_hunks_assigned_once": False,
            "strong_dependencies_within_groups": False,
            "unresolved_dependencies_reported": False,
        },
        "metadata": {"analysis_version": ANALYSIS_VERSION, **(metadata or {})},
    }
