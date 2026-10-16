"""Independent validation of dependency records (section 12.4/18.1).

The validator never trusts the analyzer: it re-checks schema, graph sanity and
evidence quality, and it records every change it makes.  A record is only
allowed to keep ``STRONG`` when it carries concrete, locatable evidence that the
target version does not already satisfy the prerequisite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from collections.abc import Iterable

from .schemas import (
    STRENGTH_ORDER,
    canonical_pair,
    check_dependency,
    evidence_is_locatable,
    hunk_sort_key,
    normalize_dependency,
)

__all__ = ["ValidatorConfig", "DependencyValidator"]


@dataclass
class ValidatorConfig:
    """Knobs from ``configs/default.yaml`` (``validator`` section)."""

    require_evidence_for_strong: bool = True
    require_separation_risk_for_strong: bool = True
    downgrade_strong_when_target_present: bool = True
    downgrade_strong_when_pending_evidence: bool = True
    cross_check_context: bool = True
    weak_when_no_evidence: bool = True

    @classmethod
    def from_dict(cls, data: dict | None) -> ValidatorConfig:
        if not data:
            return cls()
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class DependencyValidator:
    """Structural and evidential checks on dependency records."""

    def __init__(self, config: ValidatorConfig | dict | None = None) -> None:
        self.config = (
            config if isinstance(config, ValidatorConfig) else ValidatorConfig.from_dict(config or {})
        )

    def validate(
        self,
        dependencies: Iterable[dict] | None,
        hunk_ids: list[str],
        *,
        contexts: Any = None,
    ) -> dict:
        known = [str(hunk_id) for hunk_id in hunk_ids]
        known_set = set(known)
        context_map = _context_map(contexts)
        rejected: list[dict] = []
        warnings: list[dict] = []
        questions: list[dict] = []
        accepted: list[dict] = []

        for index, record in enumerate(dependencies or []):
            problems = check_dependency(record, known_hunks=known)
            if problems:
                rejected.append(
                    {"index": index, "reason": "; ".join(problems), "record": record}
                )
                continue
            if record["source_hunk"] == record["target_hunk"]:
                rejected.append(
                    {"index": index, "reason": "self dependency", "record": record}
                )
                continue
            accepted.append(normalize_dependency(record))

        merged, merge_warnings = self._merge_duplicates(accepted)
        warnings.extend(merge_warnings)

        final: list[dict] = []
        for record in merged:
            adjusted, record_warnings, record_questions = self._apply_rules(
                record, known_set, context_map
            )
            warnings.extend(record_warnings)
            questions.extend(record_questions)
            final.append(adjusted)

        final.sort(
            key=lambda item: (
                hunk_sort_key(item["source_hunk"]),
                hunk_sort_key(item["target_hunk"]),
                item["category"],
            )
        )
        for position, record in enumerate(final, start=1):
            record["dependency_id"] = f"D{position}"

        return {
            "dependencies": final,
            "rejected": rejected,
            "warnings": warnings,
            "unresolved_questions": _dedupe_questions(questions),
            "stats": _stats(final, rejected, warnings),
        }

    # -- internals -------------------------------------------------------- #

    def _merge_duplicates(self, records: list[dict]) -> tuple[list[dict], list[dict]]:
        warnings: list[dict] = []
        slots: dict[tuple[str, str, str], dict] = {}
        order: list[tuple[str, str, str]] = []
        for record in records:
            pair = canonical_pair(record["source_hunk"], record["target_hunk"])
            key = (pair[0], pair[1], str(record["category"]))
            existing = slots.get(key)
            if existing is None:
                slots[key] = record
                order.append(key)
                continue
            keep_new = STRENGTH_ORDER.get(str(record["strength"]), 99) < STRENGTH_ORDER.get(
                str(existing["strength"]), 99
            )
            survivor, dropped = (record, existing) if keep_new else (existing, record)
            merged_evidence = _merge_evidence(survivor.get("evidence"), dropped.get("evidence"))
            survivor = dict(survivor)
            survivor["evidence"] = merged_evidence
            survivor["needs_more_evidence"] = bool(
                survivor.get("needs_more_evidence")
            ) or bool(dropped.get("needs_more_evidence"))
            if survivor.get("strength") != dropped.get("strength"):
                survivor["strength"] = _weaker(survivor.get("strength"), dropped.get("strength"))
            warnings.append(
                {
                    "reason": "duplicate_dependency_merged",
                    "pair": list(pair),
                    "category": record["category"],
                    "kept_strength": survivor.get("strength"),
                    "dropped_strength": dropped.get("strength"),
                }
            )
            slots[key] = survivor
        return [slots[key] for key in order], warnings

    def _apply_rules(
        self, record: dict, known_set: set[str], context_map: dict[str, dict]
    ) -> tuple[dict, list[dict], list[dict]]:
        warnings: list[dict] = []
        questions: list[dict] = []
        record = dict(record)
        notes: list[str] = list(record.get("notes") or [])
        evidence = record.get("evidence") or []
        locatable = [item for item in evidence if evidence_is_locatable(item)]
        strength = str(record.get("strength"))

        if strength == "STRONG":
            if self.config.require_evidence_for_strong and not evidence:
                strength = "WEAK" if self.config.weak_when_no_evidence else "CONDITIONAL"
                notes.append("no evidence supplied for STRONG; downgraded")
                warnings.append(_downgrade(record, "missing_evidence", strength))
            elif self.config.require_evidence_for_strong and not locatable:
                strength = "CONDITIONAL"
                notes.append("evidence is not locatable for STRONG; downgraded to CONDITIONAL")
                warnings.append(_downgrade(record, "evidence_not_locatable", strength))
            if (
                strength == "STRONG"
                and self.config.require_separation_risk_for_strong
                and not str(record.get("separation_risk") or "").strip()
            ):
                strength = "CONDITIONAL"
                notes.append("no separation risk described; downgraded to CONDITIONAL")
                warnings.append(_downgrade(record, "missing_separation_risk", strength))
            if (
                strength == "STRONG"
                and self.config.downgrade_strong_when_pending_evidence
                and record.get("needs_more_evidence")
            ):
                strength = "CONDITIONAL"
                notes.append("evidence still pending; downgraded to CONDITIONAL")
                warnings.append(_downgrade(record, "pending_evidence", strength))
                questions.append(
                    {
                        "question": (
                            "Does the target version already provide what "
                            f"{record.get('target_hunk')} needs from {record.get('source_hunk')}?"
                        ),
                        "hunk_ids": [record.get("source_hunk"), record.get("target_hunk")],
                        "reason": "dependency strength depends on evidence that was not retrieved",
                        "needs_more_evidence": True,
                    }
                )
            if (
                strength == "STRONG"
                and self.config.downgrade_strong_when_target_present
                and record.get("target_version_status") == "present"
            ):
                strength = "CONDITIONAL"
                notes.append("target version already satisfies the prerequisite; not grouped")
                warnings.append(_downgrade(record, "target_already_satisfies", strength))

        if record.get("target_version_status") == "unknown" and record.get("needs_more_evidence"):
            questions.append(
                {
                    "question": (
                        "Target version state is unknown for the symbols involved in this "
                        "dependency; is coordinated handling still required?"
                    ),
                    "hunk_ids": [record.get("source_hunk"), record.get("target_hunk")],
                    "reason": "target_version_status=unknown",
                    "needs_more_evidence": True,
                }
            )

        if self.config.cross_check_context and context_map:
            warnings.extend(_context_disagreements(record, context_map, known_set))

        record["strength"] = strength
        record["notes"] = notes
        record["evidence"] = evidence
        return record, warnings, questions


def _weaker(left: Any, right: Any) -> str:
    left_rank = STRENGTH_ORDER.get(str(left), 99)
    right_rank = STRENGTH_ORDER.get(str(right), 99)
    return str(left) if left_rank >= right_rank else str(right)


def _downgrade(record: dict, reason: str, new_strength: str) -> dict:
    return {
        "reason": reason,
        "pair": [record.get("source_hunk"), record.get("target_hunk")],
        "category": record.get("category"),
        "new_strength": new_strength,
    }


def _merge_evidence(left: Any, right: Any) -> list[dict]:
    combined: list[dict] = []
    seen: set[tuple] = set()
    for item in list(left or []) + list(right or []):
        if not isinstance(item, dict):
            continue
        key = (item.get("symbol"), item.get("file_path"), item.get("line"), item.get("description"))
        if key in seen:
            continue
        seen.add(key)
        combined.append(item)
    return combined


def _context_map(contexts: Any) -> dict[str, dict]:
    if not contexts:
        return {}
    if isinstance(contexts, dict):
        if "hunk_id" in contexts:
            return {str(contexts["hunk_id"]): contexts}
        return {str(key): value for key, value in contexts.items()}
    return {str(item.get("hunk_id")): item for item in contexts if item.get("hunk_id")}


def _context_disagreements(
    record: dict, context_map: dict[str, dict], known_set: set[str]
) -> list[dict]:
    """Flag cases where the record contradicts retrieved code evidence."""
    warnings: list[dict] = []
    status = record.get("target_version_status")
    symbols = [
        str(item.get("symbol")).strip()
        for item in record.get("evidence") or []
        if str(item.get("symbol") or "").strip()
    ]
    for hunk_id in (record.get("source_hunk"), record.get("target_hunk")):
        if hunk_id not in known_set:
            continue
        context = context_map.get(str(hunk_id)) or {}
        target_status = context.get("target_symbol_status") or {}
        for symbol in symbols:
            observed = target_status.get(symbol)
            if not observed or observed == status:
                continue
            if {observed, status} <= {"present", "absent"}:
                warnings.append(
                    {
                        "reason": "record_contradicts_retrieved_context",
                        "pair": [record.get("source_hunk"), record.get("target_hunk")],
                        "symbol": symbol,
                        "record_status": status,
                        "observed_status": observed,
                        "hunk_id": hunk_id,
                        "detail": (
                            "the retrieved target code suggests a different target-version state "
                            "than the dependency record claims"
                        ),
                    }
                )
    return warnings


def _dedupe_questions(questions: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple] = set()
    for question in questions:
        key = (str(question.get("question")), tuple(question.get("hunk_ids") or []))
        if key in seen:
            continue
        seen.add(key)
        out.append(question)
    return out


def _stats(dependencies: list[dict], rejected: list[dict], warnings: list[dict]) -> dict:
    by_strength: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for record in dependencies:
        by_strength[str(record.get("strength"))] = by_strength.get(str(record.get("strength")), 0) + 1
        by_category[str(record.get("category"))] = by_category.get(str(record.get("category")), 0) + 1
    return {
        "dependency_count": len(dependencies),
        "by_strength": by_strength,
        "by_category": by_category,
        "rejected_count": len(rejected),
        "warning_count": len(warnings),
        "downgrade_count": len([w for w in warnings if w.get("new_strength")]),
    }
