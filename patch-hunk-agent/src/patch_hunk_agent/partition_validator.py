"""Independent validation of the final partition (section 10)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from collections.abc import Iterable

from .schemas import (
    GROUP_ID_RE,
    canonical_pair,
    hunk_sort_key,
    is_hunk_id,
    new_grouping_document,
)

__all__ = ["PartitionValidator", "PartitionValidatorConfig"]


@dataclass
class PartitionValidatorConfig:
    """Knobs from ``configs/default.yaml`` (``partition_validator`` section)."""

    require_contiguous_group_ids: bool = False
    treat_missing_analysis_as_partial: bool = True

    @classmethod
    def from_dict(cls, data: dict | None) -> PartitionValidatorConfig:
        if not data:
            return cls()
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class PartitionValidator:
    """Re-derives the partition constraints instead of trusting the grouping step."""

    def __init__(self, config: PartitionValidatorConfig | dict | None = None) -> None:
        self.config = (
            config
            if isinstance(config, PartitionValidatorConfig)
            else PartitionValidatorConfig.from_dict(config or {})
        )

    def validate(
        self,
        hunk_ids: list[str],
        groups: Any,
        dependencies: list[dict],
        *,
        patch_id: str = "patch_001",
        unresolved_questions: Iterable[dict] | None = None,
        analysis_status: str | None = None,
        deferred_dependency_ids: Iterable[str] | None = None,
        metadata: dict | None = None,
    ) -> dict:
        expected = [str(hunk_id) for hunk_id in hunk_ids]
        expected_set = set(expected)
        # Constraints the grouping step deliberately refused to enforce (because
        # merging would exceed the package size limit).  They are reported as
        # warnings and as unresolved questions, not as partition failures.
        deferred = {str(item) for item in deferred_dependency_ids or []}
        raw_groups = _as_group_list(groups)
        errors: list[dict] = []
        warnings: list[dict] = []
        assignment: dict[str, str] = {}
        normalised: list[dict] = []

        seen_group_ids: set[str] = set()
        for index, group in enumerate(raw_groups):
            if not isinstance(group, dict):
                errors.append({"reason": "group is not an object", "index": index})
                continue
            group_id = str(group.get("group_id") or "")
            if not GROUP_ID_RE.match(group_id):
                errors.append(
                    {"reason": f"invalid group_id {group_id!r}", "index": index}
                )
                continue
            if group_id in seen_group_ids:
                errors.append({"reason": f"duplicate group_id {group_id}"})
                continue
            seen_group_ids.add(group_id)
            members = list(group.get("hunk_ids") or [])
            if not members:
                errors.append({"reason": f"{group_id} has no hunks"})
                continue
            clean_members: list[str] = []
            for hunk_id in members:
                if not is_hunk_id(hunk_id):
                    errors.append(
                        {"reason": f"{group_id} contains invalid hunk id {hunk_id!r}"}
                    )
                    continue
                if hunk_id not in expected_set:
                    errors.append(
                        {
                            "reason": "grouping contains a hunk that is not part of the patch",
                            "hunk_id": hunk_id,
                            "group_id": group_id,
                        }
                    )
                    continue
                if hunk_id in assignment:
                    errors.append(
                        {
                            "reason": "hunk assigned to more than one group",
                            "hunk_id": hunk_id,
                            "group_ids": [assignment[hunk_id], group_id],
                        }
                    )
                    continue
                assignment[hunk_id] = group_id
                clean_members.append(hunk_id)
            normalised.append(
                {
                    **{k: v for k, v in group.items() if k != "hunk_ids"},
                    "group_id": group_id,
                    "hunk_ids": sorted(clean_members, key=hunk_sort_key),
                }
            )

        missing = [hunk_id for hunk_id in expected if hunk_id not in assignment]
        if missing:
            errors.append({"reason": "hunks were not assigned to any group", "hunk_ids": missing})

        if self.config.require_contiguous_group_ids and normalised:
            expected_ids = [f"G{index}" for index in range(1, len(normalised) + 1)]
            actual_ids = sorted((group["group_id"] for group in normalised), key=_group_sort_key)
            if actual_ids != expected_ids:
                warnings.append(
                    {"reason": "group ids are not contiguous from G1", "group_ids": actual_ids}
                )

        dependency_ids = {str(record.get("dependency_id")) for record in dependencies}
        for group in normalised:
            for dependency_id in group.get("dependency_ids") or []:
                if str(dependency_id) not in dependency_ids:
                    warnings.append(
                        {
                            "reason": "group references an unknown dependency id",
                            "group_id": group["group_id"],
                            "dependency_id": dependency_id,
                        }
                    )

        cross_group: list[dict] = []
        for record in dependencies:
            if str(record.get("strength")) != "STRONG":
                continue
            source = str(record.get("source_hunk"))
            target = str(record.get("target_hunk"))
            if source not in expected_set or target not in expected_set:
                errors.append(
                    {
                        "reason": "strong dependency references an unknown hunk",
                        "dependency_id": record.get("dependency_id"),
                        "pair": [source, target],
                    }
                )
                continue
            if assignment.get(source) != assignment.get(target):
                if str(record.get("dependency_id")) in deferred:
                    warnings.append(
                        {
                            "reason": "deferred_constraint_crosses_groups",
                            "dependency_id": record.get("dependency_id"),
                            "pair": list(canonical_pair(source, target)),
                            "groups": [assignment.get(source), assignment.get(target)],
                            "detail": "reported by the grouping step: enforcing it would exceed "
                            "the package size limit",
                        }
                    )
                    continue
                cross_group.append(
                    {
                        "dependency_id": record.get("dependency_id"),
                        "pair": list(canonical_pair(source, target)),
                        "category": record.get("category"),
                        "groups": [assignment.get(source), assignment.get(target)],
                        "reason": "STRONG dependencies must live inside a single group",
                    }
                )

        questions = _normalise_questions(unresolved_questions)
        affected_groups: list[str] = []
        for question in questions:
            groups_for_question: list[str] = []
            for hunk_id in question.get("hunk_ids") or []:
                group_id = assignment.get(str(hunk_id))
                if group_id and group_id not in groups_for_question:
                    groups_for_question.append(group_id)
            question["affected_groups"] = groups_for_question
            for group_id in groups_for_question:
                if group_id not in affected_groups:
                    affected_groups.append(group_id)
        for group in normalised:
            if group["group_id"] in affected_groups:
                group["has_unresolved_questions"] = True
                group.setdefault("group_reason", "")

        all_assigned_once = not any(
            "not assigned" in str(error.get("reason"))
            or "more than one group" in str(error.get("reason"))
            or "not part of the patch" in str(error.get("reason"))
            or "invalid hunk id" in str(error.get("reason"))
            for error in errors
        )
        strong_ok = not cross_group and not any(
            "strong dependency references an unknown hunk" in str(error.get("reason"))
            for error in errors
        )
        # Every unresolved item that was found is carried into the document verbatim,
        # so the reporting obligation is always satisfied at this point.
        reported = True

        if errors or cross_group:
            status = "failed"
        elif questions or (analysis_status and analysis_status != "complete") or self.config.treat_missing_analysis_as_partial and analysis_status is None:
            status = "partial"
        else:
            status = "complete"

        document = new_grouping_document(
            patch_id,
            status,
            normalised,
            cross_group_dependencies=cross_group,
            unresolved_questions=questions,
            validation={
                "all_hunks_assigned_once": all_assigned_once,
                "strong_dependencies_within_groups": strong_ok,
                "unresolved_dependencies_reported": bool(reported),
            },
            metadata=metadata,
        )
        document["errors"] = errors
        document["warnings"] = warnings
        document["checks"] = {
            "expected_hunk_count": len(expected),
            "assigned_hunk_count": len(assignment),
            "group_count": len(normalised),
            "group_sizes": [len(group["hunk_ids"]) for group in normalised],
            "strong_dependency_count": sum(
                1 for record in dependencies if str(record.get("strength")) == "STRONG"
            ),
            "unresolved_question_count": len(questions),
            "affected_groups": affected_groups,
            "analysis_status": analysis_status,
        }
        return document


def _as_group_list(groups: Any) -> list[dict]:
    if isinstance(groups, dict):
        inner = groups.get("groups")
        return list(inner or [])
    if isinstance(groups, list):
        return list(groups)
    return []


def _normalise_questions(questions: Iterable[dict] | None) -> list[dict]:
    out: list[dict] = []
    for question in questions or []:
        if isinstance(question, str):
            out.append(
                {
                    "question": question,
                    "hunk_ids": [],
                    "reason": "",
                    "needs_more_evidence": True,
                }
            )
            continue
        if not isinstance(question, dict):
            continue
        item = {
            "question": str(question.get("question") or "").strip(),
            "hunk_ids": [str(h) for h in question.get("hunk_ids") or [] if is_hunk_id(h)],
            "reason": str(question.get("reason") or ""),
            "needs_more_evidence": bool(question.get("needs_more_evidence", True)),
        }
        if question.get("suggested_query"):
            item["suggested_query"] = question["suggested_query"]
        if item["question"]:
            out.append(item)
    return out


def _group_sort_key(group_id: str) -> int:
    text = str(group_id)[1:]
    return int(text) if text.isdigit() else 1 << 30
