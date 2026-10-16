"""Adapter that turns a grouping result into downstream backport tasks (section 13).

The adapter deliberately describes the grouping as a *hypothesis to verify while
backporting*, never as a completed or verified backport.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import ANALYSIS_VERSION, PACKAGE_VERSION
from .utils.json_utils import save_json
from .utils.logging_utils import utc_now_iso

__all__ = ["AdapterConfig", "PortGPTAdapter", "TASK_SCHEMA"]

TASK_SCHEMA = "portgpt.group_task/v1"

CAVEATS = [
    "Dependency records are LLM-proposed and only structurally validated.",
    "They are not verified backport results and must not be reported as such.",
    "STRONG dependencies mean the hunks are intended to be ported together; they do "
    "not prove that the port is correct.",
    "Unresolved questions are listed per group and may indicate missing context.",
]


@dataclass
class AdapterConfig:
    """Knobs controlling how much context is handed to the downstream agent."""

    include_context: bool = True
    context_chars: int = 2000
    include_diff_text: bool = True
    include_dependencies: bool = True

    @classmethod
    def from_dict(cls, data: dict | None) -> AdapterConfig:
        if not data:
            return cls()
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class PortGPTAdapter:
    """Builds machine-readable per-group tasks with full traceability."""

    def __init__(self, config: AdapterConfig | dict | None = None) -> None:
        self.config = (
            config if isinstance(config, AdapterConfig) else AdapterConfig.from_dict(config or {})
        )

    def build_tasks(
        self,
        grouping_document: dict,
        hunks: list[dict],
        *,
        contexts: list[dict] | dict | None = None,
        analysis: dict | None = None,
        source_revision: str | None = None,
        target_revision: str | None = None,
        patch_id: str | None = None,
    ) -> dict:
        context_map = _context_map(contexts)
        hunk_map = {str(hunk.get("hunk_id")): hunk for hunk in hunks}
        dependencies = list((analysis or {}).get("dependencies") or [])
        dependency_map = {str(record.get("dependency_id")): record for record in dependencies}
        questions = list(grouping_document.get("unresolved_questions") or [])
        patch = patch_id or grouping_document.get("patch_id") or "patch_001"

        tasks: list[dict] = []
        for group in grouping_document.get("groups") or []:
            group_id = str(group.get("group_id"))
            hunk_ids = [str(hunk_id) for hunk_id in group.get("hunk_ids") or []]
            related = [
                dependency_map[dependency_id]
                for dependency_id in group.get("dependency_ids") or []
                if dependency_id in dependency_map
            ]
            group_questions = [
                question
                for question in questions
                if set(question.get("hunk_ids") or []) & set(hunk_ids)
            ]
            group_hunks = [
                self._render_hunk(hunk_map[hunk_id], context_map.get(hunk_id))
                for hunk_id in hunk_ids
                if hunk_id in hunk_map
            ]
            missing = [hunk_id for hunk_id in hunk_ids if hunk_id not in hunk_map]
            tasks.append(
                {
                    "group_id": group_id,
                    "hunk_ids": hunk_ids,
                    "must_port_together": len(hunk_ids) > 1,
                    "group_reason": group.get("group_reason", ""),
                    "dependencies": [_render_dependency(record) for record in related]
                    if self.config.include_dependencies
                    else [],
                    "attention_points": _attention_points(related),
                    "unresolved_questions": group_questions,
                    "hunks": group_hunks,
                    "missing_hunks": missing,
                }
            )

        return {
            "schema": TASK_SCHEMA,
            "agent_version": PACKAGE_VERSION,
            "analysis_version": ANALYSIS_VERSION,
            "generated_at": utc_now_iso(),
            "patch_id": patch,
            "source_revision": source_revision,
            "target_revision": target_revision,
            "grouping_status": grouping_document.get("status"),
            "validation": grouping_document.get("validation", {}),
            "caveats": CAVEATS,
            "group_count": len(tasks),
            "groups": tasks,
            "traceability_template": {
                "patch_id": patch,
                "agent_version": PACKAGE_VERSION,
                "analysis_version": ANALYSIS_VERSION,
                "source_revision": source_revision,
                "target_revision": target_revision,
                "group_ids": [task["group_id"] for task in tasks],
                "hunk_ids": [hunk_id for task in tasks for hunk_id in task["hunk_ids"]],
            },
        }

    def write_tasks(self, payload: dict, path: str | Path) -> Path:
        return save_json(path, payload)

    def build_traceability_record(
        self,
        *,
        patch_id: str,
        group_id: str,
        hunk_ids: list[str],
        portgpt_input: dict | None = None,
        generated_diff: str | None = None,
        build_result: dict | None = None,
        test_result: dict | None = None,
        failure_reason: str | None = None,
        grouping_document: dict | None = None,
        recorded_at: str | None = None,
    ) -> dict:
        """One row of the backport log, keyed back to the original grouping decision."""
        return {
            "schema": "portgpt.traceability/v1",
            "recorded_at": recorded_at or utc_now_iso(),
            "patch_id": patch_id,
            "group_id": group_id,
            "hunk_ids": list(hunk_ids),
            "grouping_status": (grouping_document or {}).get("status"),
            "grouping_validation": (grouping_document or {}).get("validation", {}),
            "portgpt_input": portgpt_input,
            "generated_diff_available": bool(generated_diff),
            "generated_diff_chars": len(generated_diff or ""),
            "build_result": build_result,
            "test_result": test_result,
            "failure_reason": failure_reason,
        }

    def append_traceability_record(self, path: str | Path, record: dict) -> Path:
        import json

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return target

    def _render_hunk(self, hunk: dict, context: dict | None) -> dict:
        item: dict[str, Any] = {
            "hunk_id": hunk.get("hunk_id"),
            "file_path": hunk.get("file_path"),
            "target_file_path": hunk.get("target_file_path"),
            "header": hunk.get("header"),
            "old_start": hunk.get("old_start"),
            "old_count": hunk.get("old_count"),
            "new_start": hunk.get("new_start"),
            "new_count": hunk.get("new_count"),
            "section": hunk.get("section"),
            "added_lines": hunk.get("added_lines") or [],
            "removed_lines": hunk.get("removed_lines") or [],
            "context_lines": hunk.get("context_lines") or [],
        }
        if self.config.include_diff_text:
            item["diff_text"] = hunk.get("diff_text")
        if self.config.include_context and context:
            item["source_context"] = _trim(context.get("source_context"), self.config.context_chars)
            item["target_context"] = _trim(context.get("target_context"), self.config.context_chars)
            item["target_symbol_status"] = context.get("target_symbol_status") or {}
            item["retrieval_status"] = context.get("retrieval_status")
        return item


def _render_dependency(record: dict) -> dict:
    return {
        "dependency_id": record.get("dependency_id"),
        "source_hunk": record.get("source_hunk"),
        "target_hunk": record.get("target_hunk"),
        "category": record.get("category"),
        "strength": record.get("strength"),
        "evidence": record.get("evidence") or [],
        "separation_risk": record.get("separation_risk"),
        "target_version_status": record.get("target_version_status"),
        "reasoning_summary": record.get("reasoning_summary"),
        "needs_more_evidence": record.get("needs_more_evidence"),
        "origin": record.get("origin"),
    }


_CATEGORY_GUIDANCE = {
    "DEFINITION_USE": "port the definition and its use together",
    "INTERFACE_SIGNATURE": "update every caller and the definition in the same step",
    "CALL_GRAPH": "keep caller and callee behaviour consistent",
    "SHARED_STATE": "keep initialisation, use and release of the state consistent",
    "CONTROL_FLOW": "keep the related branches and cleanup paths consistent",
    "SEMANTIC_INVARIANT": "preserve the invariant that both hunks maintain",
    "CONTEXT_DEPENDENCY": "keep the dependent structural context intact",
}


def _attention_points(dependencies: list[dict]) -> list[str]:
    points: list[str] = []
    for record in sorted(dependencies, key=lambda item: str(item.get("dependency_id"))):
        symbols = [
            str(item.get("symbol"))
            for item in record.get("evidence") or []
            if str(item.get("symbol") or "").strip()
        ]
        unique_symbols: list[str] = []
        for symbol in symbols:
            if symbol not in unique_symbols:
                unique_symbols.append(symbol)
        guidance = _CATEGORY_GUIDANCE.get(str(record.get("category")), "handle these hunks together")
        target = ", ".join(unique_symbols) if unique_symbols else "the shared code"
        points.append(
            f"{record.get('dependency_id')} {record.get('strength')} "
            f"{record.get('category')} ({record.get('source_hunk')}<->{record.get('target_hunk')}): "
            f"{guidance} for {target}."
        )
    return points


def _context_map(contexts: Any) -> dict[str, dict]:
    if not contexts:
        return {}
    if isinstance(contexts, dict):
        if "hunk_id" in contexts:
            return {str(contexts["hunk_id"]): contexts}
        return {str(key): value for key, value in contexts.items()}
    return {str(item.get("hunk_id")): item for item in contexts if item.get("hunk_id")}


def _trim(block: Any, limit: int) -> dict | None:
    if not isinstance(block, dict):
        return None
    trimmed = dict(block)
    code = trimmed.get("code")
    if isinstance(code, str) and len(code) > limit:
        trimmed["code"] = code[:limit] + "\n...<truncated>"
    return trimmed
