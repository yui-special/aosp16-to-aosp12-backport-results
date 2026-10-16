#!/usr/bin/env python
"""Compare baseline and treatment predictions against a labelled dataset.

Example:

    python scripts/run_evaluation.py \
        --dataset datasets/patches.json \
        --baseline predictions/baseline.json \
        --treatment predictions/grouped.json \
        --output results/evaluation.json

The dataset and prediction formats are documented in README.md ("Evaluation").
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from patch_hunk_agent.evaluator import Evaluator  # noqa: E402
from patch_hunk_agent.utils.json_utils import load_json, save_json  # noqa: E402

METRICS = (
    "dependency_precision",
    "dependency_recall",
    "dependency_f1",
    "strong_miss_rate",
    "over_grouping_rate",
    "co_membership_recall",
    "backport_success_rate",
    "compile_pass_rate",
    "test_pass_rate",
    "average_tokens",
    "average_retries",
    "average_runtime_s",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, help="labelled reference dataset")
    parser.add_argument("--baseline", default=None, help="predictions from the baseline system")
    parser.add_argument("--treatment", default=None, help="predictions from the grouped system")
    parser.add_argument("--predictions", default=None, help="alias for --treatment")
    parser.add_argument("--output", default=None, help="where to write the JSON report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    treatment_path = args.treatment or args.predictions
    if not args.baseline and not treatment_path:
        print("error: pass --treatment (or --predictions) and/or --baseline", file=sys.stderr)
        return 2

    dataset = load_json(args.dataset)
    evaluator = Evaluator()
    report: dict = {"dataset": str(args.dataset), "runs": {}}
    for label, path in (("baseline", args.baseline), ("treatment", treatment_path)):
        if not path:
            continue
        predictions = load_json(path)
        result = evaluator.evaluate_dataset(dataset, predictions)
        report["runs"][label] = result

    if len(report["runs"]) == 2:
        report["comparison"] = _compare(
            report["runs"]["baseline"]["aggregate"], report["runs"]["treatment"]["aggregate"]
        )

    if args.output:
        save_json(args.output, report)
    print(json.dumps(_summary(report), ensure_ascii=False, indent=2))
    return 0


def _compare(baseline: dict, treatment: dict) -> dict:
    comparison: dict[str, dict] = {}
    for metric in METRICS:
        left, right = baseline.get(metric), treatment.get(metric)
        if left is None or right is None:
            continue
        comparison[metric] = {
            "baseline": left,
            "treatment": right,
            "delta": round(float(right) - float(left), 6),
        }
    return comparison


def _summary(report: dict) -> dict:
    summary = {"dataset": report["dataset"], "aggregates": {}}
    for label, result in report["runs"].items():
        summary["aggregates"][label] = result["aggregate"]
    if "comparison" in report:
        summary["comparison"] = report["comparison"]
    return summary


if __name__ == "__main__":
    raise SystemExit(main())
