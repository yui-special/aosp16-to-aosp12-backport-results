"""Deterministic grouping of hunks from verified dependencies (section 9.2/12.5).

The engine is intentionally simple and reproducible: only dependencies that
carry a verified ``STRONG`` strength couple hunks, groups are the connected
components of that graph, and every promotion or non-promotion is recorded with
its reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable

from .dependency_graph import DependencyGraph
from .schemas import (
    RESOLUTION_VALUES,
    STRENGTH_ORDER,
    canonical_pair,
    evidence_is_locatable,
    hunk_sort_key,
    sort_hunk_ids,
)

__all__ = [
    "GroupingConfig",
    "GroupingEngine",
    "effective_strengths",
    "merge_groups_across_strong_edges",
]


@dataclass
class GroupingConfig:
    """Knobs from ``configs/default.yaml`` (``grouping`` section)."""

    # "global": the model proposes the work packages and the graph only validates /
    # repairs them (default).  "components": the deterministic connected-component
    # algorithm from the implementation document.
    mode: str = "global"
    promote_conditional: bool = True
    promoted_categories: tuple[str, ...] = ("DEFINITION_USE", "INTERFACE_SIGNATURE")
    require_target_absent_for_promotion: bool = True
    require_locatable_evidence_for_promotion: bool = True
    repartition_rounds: int = 2
    # Global-mode guards
    max_group_size: int = 8
    max_split_rounds: int = 2
    max_partition_retries: int = 1
    partition_candidates: int = 3
    # Global-mode prompt budget: how much of each hunk and how many evidence edges
    # the partition call sees.  Fewer/leaner edges keep the model from treating
    # every coupling as "must merge".
    digest_line_limit: int = 4
    max_evidence_edges: int = 60

    @classmethod
    def from_dict(cls, data: dict | None) -> GroupingConfig:
        if not data:
            return cls()
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        payload = {k: v for k, v in data.items() if k in known}
        if "promoted_categories" in payload and isinstance(payload["promoted_categories"], list):
            payload["promoted_categories"] = tuple(payload["promoted_categories"])
        return cls(**payload)


def effective_strengths(
    dependencies: Iterable[dict],
    config: GroupingConfig,
    resolutions: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[dict], list[dict]]:
    """Resolve the strength each dependency actually groups with.

    Returns ``(strength_by_dependency_id, promotions, not_promoted)``.  A caller
    supplied ``resolutions`` entry (``promote``/``keep``/``drop``) wins over the
    automatic policy, which lets a human or a target-version check settle a
    conditional dependency explicitly.
    """
    overrides = dict(resolutions or {})
    records = list(dependencies)
    unknown = sorted(set(overrides) - {str(dep.get("dependency_id")) for dep in records})
    strengths: dict[str, str] = {}
    promotions: list[dict] = []
    not_promoted: list[dict] = []
    for record in records:
        dependency_id = str(record.get("dependency_id"))
        strength = str(record.get("strength"))
        override = overrides.get(dependency_id)
        if override is not None and override not in RESOLUTION_VALUES:
            not_promoted.append(
                {
                    "dependency_id": dependency_id,
                    "reason": f"invalid resolution {override!r}",
                    "strength": strength,
                }
            )
            strengths[dependency_id] = strength
            continue
        if override == "drop":
            strengths[dependency_id] = "NONE"
            promotions.append(
                {
                    "dependency_id": dependency_id,
                    "reason": "dropped by explicit resolution",
                    "resolution": "drop",
                    "from": strength,
                    "to": "NONE",
                }
            )
            continue
        if override == "promote" and strength != "STRONG":
            strengths[dependency_id] = "STRONG"
            promotions.append(
                {
                    "dependency_id": dependency_id,
                    "reason": "promoted by explicit resolution",
                    "resolution": "promote",
                    "from": strength,
                    "to": "STRONG",
                }
            )
            continue
        if (
            strength == "CONDITIONAL"
            and config.promote_conditional
            and str(record.get("category")) in config.promoted_categories
            and (override == "promote" or _promotion_criteria(record, config))
        ):
            strengths[dependency_id] = "STRONG"
            promotions.append(
                {
                    "dependency_id": dependency_id,
                    "reason": "conditional dependency promoted after the target-version check",
                    "resolution": override or "automatic",
                    "from": strength,
                    "to": "STRONG",
                }
            )
            continue
        strengths[dependency_id] = strength
        if strength == "CONDITIONAL":
            reasons = _promotion_blockers(record, config)
            if reasons:
                not_promoted.append(
                    {
                        "dependency_id": dependency_id,
                        "strength": strength,
                        "pair": list(canonical_pair(record.get("source_hunk"), record.get("target_hunk"))),
                        "reasons": reasons,
                    }
                )
    for dependency_id in unknown:
        not_promoted.append(
            {
                "dependency_id": dependency_id,
                "reason": "resolution refers to a dependency that does not exist",
            }
        )
    return strengths, promotions, not_promoted


def _promotion_criteria(record: dict, config: GroupingConfig) -> bool:
    if record.get("needs_more_evidence"):
        return False
    if config.require_target_absent_for_promotion and record.get("target_version_status") != "absent":
        return False
    needs_evidence = config.require_locatable_evidence_for_promotion
    return not needs_evidence or _has_locatable_evidence(record)


def _has_locatable_evidence(record: dict) -> bool:
    return any(evidence_is_locatable(item) for item in record.get("evidence") or [])


def _promotion_blockers(record: dict, config: GroupingConfig) -> list[str]:
    reasons: list[str] = []
    if not config.promote_conditional:
        reasons.append("conditional promotion disabled by configuration")
    if str(record.get("category")) not in config.promoted_categories:
        reasons.append(f"category {record.get('category')} is not eligible for promotion")
    if record.get("needs_more_evidence"):
        reasons.append("evidence is still pending")
    if config.require_target_absent_for_promotion and record.get("target_version_status") != "absent":
        reasons.append(
            f"target_version_status={record.get('target_version_status')} does not show a missing prerequisite"
        )
    if config.require_locatable_evidence_for_promotion and not _has_locatable_evidence(record):
        reasons.append("no locatable evidence")
    return reasons


class GroupingEngine:
    """Build groups from hunk ids and validated dependency records."""

    def __init__(self, config: GroupingConfig | dict | None = None) -> None:
        self.config = (
            config if isinstance(config, GroupingConfig) else GroupingConfig.from_dict(config or {})
        )

    def group(
        self,
        hunk_ids: list[str],
        dependencies: list[dict],
        *,
        resolutions: dict[str, str] | None = None,
    ) -> dict:
        ordered_hunks = sort_hunk_ids(hunk_ids)
        strengths, promotions, not_promoted = effective_strengths(
            dependencies, self.config, resolutions
        )
        by_id = {str(record.get("dependency_id")): record for record in dependencies}

        def resolver(record: dict) -> str:
            return strengths.get(str(record.get("dependency_id")), str(record.get("strength")))

        graph = DependencyGraph(ordered_hunks, dependencies, strength_resolver=resolver)
        components = graph.components()
        group_of: dict[str, int] = {}
        for index, component in enumerate(components, start=1):
            for hunk_id in component:
                group_of[hunk_id] = index

        groups: list[dict] = []
        for index, component in enumerate(components, start=1):
            group_id = f"G{index}"
            relevant = [
                edge.dependency_id
                for edge in graph.edges
                if edge.pair[0] in component and edge.pair[1] in component
            ]
            other = [
                str(record.get("dependency_id"))
                for record in dependencies
                if str(record.get("dependency_id")) not in relevant
                and set(canonical_pair(record.get("source_hunk"), record.get("target_hunk")))
                <= set(component)
                and str(record.get("strength")) != "NONE"
            ]
            external: list[str] = []
            for record in dependencies:
                if str(record.get("strength")) == "NONE":
                    continue
                source = str(record.get("source_hunk"))
                target = str(record.get("target_hunk"))
                if source in component and target in component:
                    continue
                if source in component or target in component:
                    dependency_id = str(record.get("dependency_id"))
                    if dependency_id not in external:
                        external.append(dependency_id)
            groups.append(
                {
                    "group_id": group_id,
                    "hunk_ids": list(component),
                    "group_reason": self._reason(
                        component, relevant, by_id, other, external, not_promoted
                    ),
                    "dependency_ids": sorted(relevant, key=_dependency_sort_key_index),
                    "other_dependency_ids": sorted(other, key=_dependency_sort_key_index),
                    "external_dependency_ids": sorted(external, key=_dependency_sort_key_index),
                }
            )

        cross_group = [edge.to_dict() for edge in graph.cross_group_edges(group_of)]
        questions: list[dict] = []
        for item in not_promoted:
            if not item.get("pair"):
                continue
            record = by_id.get(item["dependency_id"]) or {}
            if record.get("needs_more_evidence"):
                questions.append(
                    {
                        "question": (
                            f"Conditional dependency {item['dependency_id']} between "
                            f"{item['pair'][0]} and {item['pair'][1]} was not promoted: "
                            f"{'; '.join(item.get('reasons') or [])}"
                        ),
                        "hunk_ids": list(item["pair"]),
                        "reason": "conditional dependency left unresolved",
                        "needs_more_evidence": True,
                    }
                )
        return {
            "groups": groups,
            "cross_group_dependencies": cross_group,
            "unresolved_questions": questions,
            "metadata": {
                "hunk_count": len(ordered_hunks),
                "group_count": len(groups),
                "group_sizes": [len(group["hunk_ids"]) for group in groups],
                "strong_edge_count": len(graph.edges),
                "promotions": promotions,
                "not_promoted": not_promoted,
                "ignored_edges": graph.ignored_edges,
                "graph": graph.to_dict(),
                "degree_stats": graph.degree_stats(),
                "effective_strengths": strengths,
                "weak_dependencies_not_merged": [
                    str(record.get("dependency_id"))
                    for record in dependencies
                    if str(strengths.get(str(record.get("dependency_id")))) in ("WEAK", "NONE")
                ],
            },
        }

    @staticmethod
    def _reason(
        component: list[str],
        dependency_ids: list[str],
        by_id: dict[str, dict],
        other_ids: list[str],
        external_ids: list[str],
        not_promoted: list[dict],
    ) -> str:
        blockers = {
            str(item.get("dependency_id")): "; ".join(item.get("reasons") or [])
            for item in not_promoted
        }
        if len(component) == 1:
            parts = [
                f"No strong dependency connects {component[0]} to another hunk, so it forms "
                "its own group."
            ]
            for dependency_id in sorted(external_ids, key=_dependency_sort_key_index):
                record = by_id.get(dependency_id) or {}
                reason = blockers.get(dependency_id) or "it is not a verified strong dependency"
                parts.append(
                    f"{dependency_id} ({record.get('category')} "
                    f"{record.get('source_hunk')}<->{record.get('target_hunk')}, "
                    f"{record.get('strength')}) also links this hunk, but it was not promoted to a "
                    f"grouping edge: {reason}."
                )
            return " ".join(parts)
        linked = [by_id[dependency_id] for dependency_id in dependency_ids if dependency_id in by_id]
        if not linked:
            return f"{', '.join(component)} are grouped by verified strong dependencies."
        parts = []
        for record in sorted(linked, key=lambda item: str(item.get("dependency_id"))):
            summary = str(record.get("reasoning_summary") or "").strip().rstrip(".")
            detail = f": {summary}" if summary else ""
            parts.append(
                f"{record.get('dependency_id')} ({record.get('category')} "
                f"{record.get('source_hunk')}->{record.get('target_hunk')}){detail}"
            )
        reason = (
            f"{', '.join(component)} must be ported together because of verified STRONG "
            f"dependency(ies): {'; '.join(parts)}"
        )
        conditional = [
            dependency_id
            for dependency_id in other_ids + external_ids
            if str((by_id.get(dependency_id) or {}).get("strength")) == "CONDITIONAL"
        ]
        if conditional:
            reason += (
                ". Conditional dependency(ies) " + ", ".join(conditional) + " were left out of the "
                "grouping edges (see not_promoted for the reasons)"
            )
        return reason + "."


def _dependency_sort_key_index(dependency_id: str) -> int:
    text = str(dependency_id)
    return int(text[1:]) if text[1:].isdigit() else 1 << 30


def merge_groups_across_strong_edges(
    groups: list[dict], dependencies: list[dict], *, max_rounds: int = 2
) -> tuple[list[dict], list[dict]]:
    """Repair a partition that splits a strong dependency (section 10.2).

    Returns ``(groups, repairs)``; the repair merges the offending groups and
    keeps every hunk exactly once.
    """
    repairs: list[dict] = []
    strong_pairs = [
        canonical_pair(record.get("source_hunk"), record.get("target_hunk"))
        for record in dependencies
        if str(record.get("strength")) == "STRONG"
    ]
    current = [dict(group) for group in groups]
    for round_index in range(1, max(1, max_rounds) + 1):
        group_of: dict[str, str] = {}
        for group in current:
            for hunk_id in group.get("hunk_ids") or []:
                group_of[hunk_id] = group["group_id"]
        offenders = [
            (pair, group_of.get(pair[0]), group_of.get(pair[1]))
            for pair in strong_pairs
            if pair[0] in group_of and pair[1] in group_of and group_of[pair[0]] != group_of[pair[1]]
        ]
        if not offenders:
            return current, repairs
        to_merge: list[str] = []
        for pair, left, right in offenders:
            repairs.append(
                {
                    "round": round_index,
                    "reason": "strong dependency split across groups",
                    "pair": list(pair),
                    "merged_groups": [left, right],
                }
            )
            for group_id in (left, right):
                if group_id not in to_merge:
                    to_merge.append(group_id)
        merged_hunks = sort_hunk_ids(
            hunk_id
            for group in current
            if group["group_id"] in to_merge
            for hunk_id in group.get("hunk_ids") or []
        )
        survivor = next(group for group in current if group["group_id"] == to_merge[0])
        survivor = dict(survivor)
        survivor["hunk_ids"] = merged_hunks
        merged_dependency_ids: list[str] = []
        for group in current:
            if group["group_id"] not in to_merge:
                continue
            for dependency_id in group.get("dependency_ids") or []:
                if str(dependency_id) not in merged_dependency_ids:
                    merged_dependency_ids.append(str(dependency_id))
        merged_dependency_ids.sort(key=_dependency_sort_key_index)
        survivor["dependency_ids"] = merged_dependency_ids
        survivor["group_reason"] = (
            "Merged by the partition repair step because STRONG dependencies crossed group "
            f"boundaries ({', '.join(to_merge)})."
        )
        survivor["repaired"] = True
        current = [group for group in current if group["group_id"] not in to_merge]
        current.append(survivor)
        current = _renumber_groups(current)
    return current, repairs


def _renumber_groups(groups: list[dict]) -> list[dict]:
    ordered = sorted(groups, key=lambda group: [hunk_sort_key(h) for h in group.get("hunk_ids") or []])
    renamed: list[dict] = []
    for index, group in enumerate(ordered, start=1):
        item = dict(group)
        item["group_id"] = f"G{index}"
        item["hunk_ids"] = sort_hunk_ids(item.get("hunk_ids") or [])
        renamed.append(item)
    return renamed


def strongest_strength(values: Iterable[str]) -> str:
    """Helper used by reporting code: the strongest of the given strengths."""
    best = "NONE"
    for value in values:
        if STRENGTH_ORDER.get(str(value), 99) < STRENGTH_ORDER.get(best, 99):
            best = str(value)
    return best
