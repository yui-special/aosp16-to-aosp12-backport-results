"""Evaluation metrics for grouping quality and backport outcomes (section 16.2)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Iterable

from .schemas import canonical_pair
from .utils.json_utils import load_json, save_json

__all__ = ["EvalConfig", "Evaluator"]


@dataclass
class EvalConfig:
    """Knobs controlling what counts as a success."""

    treat_partial_as_success: bool = False
    strong_strengths: tuple[str, ...] = ("STRONG",)

    @classmethod
    def from_dict(cls, data: dict | None) -> EvalConfig:
        if not data:
            return cls()
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        payload = {k: v for k, v in data.items() if k in known}
        if "strong_strengths" in payload and isinstance(payload["strong_strengths"], list):
            payload["strong_strengths"] = tuple(payload["strong_strengths"])
        return cls(**payload)


class Evaluator:
    """Computes the metrics required by the evaluation plan."""

    def __init__(self, config: EvalConfig | dict | None = None) -> None:
        self.config = config if isinstance(config, EvalConfig) else EvalConfig.from_dict(config or {})

    # -- dependency metrics ------------------------------------------------ #

    def dependency_metrics(
        self, predicted: Iterable[dict], reference: Iterable[dict]
    ) -> dict:
        predicted_pairs = {canonical_pair(r.get("source_hunk"), r.get("target_hunk")) for r in predicted}
        reference_pairs = {canonical_pair(r.get("source_hunk"), r.get("target_hunk")) for r in reference}
        predicted_keyed = {
            (*canonical_pair(r.get("source_hunk"), r.get("target_hunk")), str(r.get("category")))
            for r in predicted
        }
        reference_keyed = {
            (*canonical_pair(r.get("source_hunk"), r.get("target_hunk")), str(r.get("category")))
            for r in reference
        }

        true_positive = len(predicted_pairs & reference_pairs)
        pair_precision = _ratio(true_positive, len(predicted_pairs))
        pair_recall = _ratio(true_positive, len(reference_pairs))
        category_precision = _ratio(len(predicted_keyed & reference_keyed), len(predicted_keyed))
        category_recall = _ratio(len(predicted_keyed & reference_keyed), len(reference_keyed))

        reference_strong = {
            canonical_pair(r.get("source_hunk"), r.get("target_hunk"))
            for r in reference
            if str(r.get("strength") or "").upper() in self.config.strong_strengths
        }
        predicted_strong = {
            canonical_pair(r.get("source_hunk"), r.get("target_hunk"))
            for r in predicted
            if str(r.get("strength") or "").upper() in self.config.strong_strengths
        }
        missed_strong = reference_strong - predicted_strong

        return {
            "dependency_precision": pair_precision,
            "dependency_recall": pair_recall,
            "dependency_f1": _f1(pair_precision, pair_recall),
            "category_precision": category_precision,
            "category_recall": category_recall,
            "category_f1": _f1(category_precision, category_recall),
            "predicted_pairs": len(predicted_pairs),
            "reference_pairs": len(reference_pairs),
            "true_positives": true_positive,
            "false_positives": sorted("|".join(pair) for pair in predicted_pairs - reference_pairs),
            "false_negatives": sorted("|".join(pair) for pair in reference_pairs - predicted_pairs),
            "strong_miss_rate": _ratio(len(missed_strong), len(reference_strong)),
            "missed_strong_pairs": sorted("|".join(pair) for pair in missed_strong),
        }

    # -- grouping metrics -------------------------------------------------- #

    def grouping_metrics(
        self, groups: Iterable[Iterable[str]], reference_groups: Iterable[Iterable[str]]
    ) -> dict:
        group_list = [list(group) for group in (groups or [])]
        reference_list = [list(group) for group in (reference_groups or [])]
        predicted_membership = _membership(group_list)
        reference_membership = _membership(reference_list)

        predicted_pairs = _merged_pairs(group_list)
        reference_pairs = _merged_pairs(reference_list)
        correct = predicted_pairs & reference_pairs
        over_merged = predicted_pairs - reference_pairs
        under_merged = reference_pairs - predicted_pairs

        precision = _ratio(len(correct), len(predicted_pairs))
        recall = _ratio(len(correct), len(reference_pairs))
        return {
            "group_count": len(group_list),
            "reference_group_count": len(reference_list),
            "co_membership_precision": precision,
            "co_membership_recall": recall,
            "over_grouping_rate": _ratio(len(over_merged), len(predicted_pairs)),
            "under_grouping_rate": _ratio(len(under_merged), len(reference_pairs)),
            "over_merged_pairs": sorted("|".join(pair) for pair in over_merged),
            "under_merged_pairs": sorted("|".join(pair) for pair in under_merged),
            "predicted_membership": predicted_membership,
            "reference_membership": reference_membership,
        }

    # -- outcome metrics --------------------------------------------------- #

    def backport_metrics(self, records: Iterable[dict]) -> dict:
        rows = list(records)
        totals = {
            "task_count": len(rows),
            "backport_success_rate": _ratio(
                sum(1 for row in rows if _is_success(row.get("backport_success"))), len(rows)
            ),
            "compile_pass_rate": _ratio(
                sum(1 for row in rows if _is_success(row.get("compile_pass"))), len(rows)
            ),
            "test_pass_rate": _ratio(
                sum(1 for row in rows if _is_success(row.get("test_pass"))), len(rows)
            ),
            "average_tokens": _mean([row.get("tokens") for row in rows]),
            "average_retries": _mean([row.get("retries") for row in rows]),
            "average_runtime_s": _mean([row.get("runtime_s") for row in rows]),
        }
        return totals

    # -- dataset level ----------------------------------------------------- #

    def evaluate_patch(self, case: dict, prediction: dict) -> dict:
        predicted_dependencies = (prediction.get("analysis") or {}).get("dependencies")
        if predicted_dependencies is None:
            predicted_dependencies = (prediction.get("dependencies") or [])
        reference_dependencies = case.get("reference_dependencies") or []

        groups = (prediction.get("grouping") or {}).get("groups") or prediction.get("groups") or []
        group_hunks = [group.get("hunk_ids") if isinstance(group, dict) else group for group in groups]
        reference_groups = case.get("reference_groups") or []

        outcome = prediction.get("outcome") or case.get("outcome") or {}
        metrics = {
            "patch_id": case.get("patch_id") or prediction.get("patch_id"),
            "dependency": self.dependency_metrics(predicted_dependencies, reference_dependencies),
            "grouping": self.grouping_metrics(group_hunks, reference_groups)
            if reference_groups
            else None,
            "outcome": self.backport_metrics([outcome]) if outcome else None,
            "prediction_status": (prediction.get("grouping_document") or {}).get("status")
            or (prediction.get("grouping") or {}).get("status"),
        }
        return metrics

    def evaluate_dataset(self, dataset: dict | list, predictions: dict | list) -> dict:
        cases = dataset.get("cases") if isinstance(dataset, dict) else dataset
        prediction_rows = predictions.get("predictions") if isinstance(predictions, dict) else predictions
        by_patch = {
            str(row.get("patch_id")): row
            for row in (prediction_rows or [])
            if isinstance(row, dict)
        }
        per_patch: list[dict] = []
        missing: list[str] = []
        for case in cases or []:
            patch_id = str(case.get("patch_id"))
            prediction = by_patch.get(patch_id)
            if prediction is None:
                missing.append(patch_id)
                continue
            per_patch.append(self.evaluate_patch(case, prediction))

        aggregate = self._aggregate(per_patch)
        aggregate["patch_count"] = len(per_patch)
        aggregate["missing_predictions"] = missing
        return {"per_patch": per_patch, "aggregate": aggregate}

    def _aggregate(self, per_patch: list[dict]) -> dict:
        def collect(section: str, key: str) -> list[float]:
            values: list[float] = []
            for row in per_patch:
                block = row.get(section)
                if isinstance(block, dict) and isinstance(block.get(key), (int, float)):
                    values.append(float(block[key]))
            return values

        return {
            "dependency_precision": _mean(collect("dependency", "dependency_precision")),
            "dependency_recall": _mean(collect("dependency", "dependency_recall")),
            "dependency_f1": _mean(collect("dependency", "dependency_f1")),
            "strong_miss_rate": _mean(collect("dependency", "strong_miss_rate")),
            "over_grouping_rate": _mean(collect("grouping", "over_grouping_rate")),
            "co_membership_recall": _mean(collect("grouping", "co_membership_recall")),
            "backport_success_rate": _mean(collect("outcome", "backport_success_rate")),
            "compile_pass_rate": _mean(collect("outcome", "compile_pass_rate")),
            "test_pass_rate": _mean(collect("outcome", "test_pass_rate")),
            "average_tokens": _mean(collect("outcome", "average_tokens")),
            "average_retries": _mean(collect("outcome", "average_retries")),
            "average_runtime_s": _mean(collect("outcome", "average_runtime_s")),
        }

    # -- files ------------------------------------------------------------- #

    def run_from_files(
        self, dataset_path: str | Path, predictions_path: str | Path, output_path: str | Path | None = None
    ) -> dict:
        dataset = load_json(dataset_path)
        predictions = load_json(predictions_path)
        report = self.evaluate_dataset(dataset, predictions)
        if output_path:
            save_json(output_path, report)
        return report


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


def _f1(precision: float, recall: float) -> float:
    if precision + recall <= 0:
        return 0.0
    return round(2 * precision * recall / (precision + recall), 6)


def _mean(values: Iterable[Any]) -> float | None:
    numbers = [float(value) for value in values if isinstance(value, (int, float))]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 6)


def _membership(groups: Iterable[Iterable[str]]) -> list[list[str]]:
    return [sorted(group) for group in groups or []]


def _merged_pairs(groups: Iterable[Iterable[str]]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for group in groups or []:
        members = sorted(str(member) for member in group)
        for index, left in enumerate(members):
            for right in members[index + 1 :]:
                pairs.add((left, right))
    return pairs


def _is_success(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "pass", "passed", "success", "ok", "1")
    if isinstance(value, (int, float)):
        return bool(value)
    return False
