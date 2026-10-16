#!/usr/bin/env python
"""Score one pipeline result against a reference dataset (grouping quality).

Used by ``results/cve-2025-48649/run_agent.ps1`` after a real run:

    python scripts/score_grouping.py \
        --dataset results/cve-2025-48649/dataset.json \
        --result  results/cve-2025-48649/result.agent.json \
        --predictions-out results/cve-2025-48649/predictions_agent.json \
        --output results/cve-2025-48649/evaluation_agent.json

The result file is this agent's ``--print-json`` output (it contains the grouping
document plus the validated dependencies), so no LLM call is needed here.
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, help="reference dataset (reference_groups)")
    parser.add_argument("--result", required=True, help="pipeline result JSON from --print-json")
    parser.add_argument("--predictions-out", default=None, help="where to write the prediction file")
    parser.add_argument("--output", default=None, help="where to write the evaluation report")
    parser.add_argument("--patch-id", default=None, help="override the patch id used for matching")
    return parser


def prediction_from_result(result: dict, patch_id: str | None = None) -> dict:
    document = result.get("grouping_document") or {}
    dependencies = (result.get("dependency_validation") or {}).get("dependencies") or []
    resolved = patch_id or result.get("patch_id") or document.get("patch_id") or "patch_001"
    return {
        "predictions": [
            {
                "patch_id": resolved,
                "analysis": {"dependencies": dependencies},
                "grouping": {"groups": document.get("groups") or []},
                "grouping_document": {"status": document.get("status")},
            }
        ]
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dataset = load_json(args.dataset)
    result = load_json(args.result)
    prediction = prediction_from_result(result, args.patch_id)
    if args.predictions_out:
        save_json(args.predictions_out, prediction)
    report = Evaluator().evaluate_dataset(dataset, prediction)
    if args.output:
        save_json(args.output, report)
    print(json.dumps(report["aggregate"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
