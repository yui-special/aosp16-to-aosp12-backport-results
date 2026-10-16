"""Harness for the historical regression dataset (section 15.2).

Skipped until real CVE-49649 / H6-H7 cases are recorded under
``tests/regression/cases``.  The harness never invents expectations: a case is
only scored against the reference relations a human reviewed from the real
patch.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import ROOT, SOURCE_REVISION, TARGET_REVISION, build_test_pipeline
from patch_hunk_agent.evaluator import Evaluator
from patch_hunk_agent.pipeline import PipelineInputs
from patch_hunk_agent.schemas import CATEGORIES, STRENGTHS, is_hunk_id

CASES_DIR = ROOT / "tests" / "regression" / "cases"
REQUIRED_FIELDS = ("case_id", "patch_path", "reference_dependencies", "reference_groups")
SKIP_REASON = (
    "no real-patch regression cases recorded yet; "
    "add tests/regression/cases/*.json (see tests/regression/README.md)"
)


def _case_paths() -> list[Path]:
    if not CASES_DIR.is_dir():
        return []
    return sorted(CASES_DIR.glob("*.json"))


def _load_cases() -> list[dict]:
    cases: list[dict] = []
    for path in _case_paths():
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["__path__"] = str(path)
        cases.append(payload)
    return cases


CASES = _load_cases()


def test_case_files_are_well_formed():
    """Schema checks run even when the referenced patches are not present yet."""
    if not CASES:
        pytest.skip(SKIP_REASON)
    for case in CASES:
        for field in REQUIRED_FIELDS:
            assert field in case, f"{case['__path__']}: missing {field}"
        assert str(case["case_id"]).strip()
        for record in case["reference_dependencies"]:
            assert is_hunk_id(record["source_hunk"]), record
            assert is_hunk_id(record["target_hunk"]), record
            assert record["category"] in CATEGORIES, record
            assert record["strength"] in STRENGTHS, record
        for group in case["reference_groups"]:
            assert group and all(is_hunk_id(hunk_id) for hunk_id in group), group


@pytest.mark.skipif(not CASES, reason=SKIP_REASON)
@pytest.mark.parametrize("case", CASES, ids=[str(case.get("case_id")) for case in CASES])
def test_regression_case(case, tmp_path):
    patch_path = ROOT / case["patch_path"]
    if not patch_path.is_file():
        pytest.skip(f"{case['case_id']}: patch file {patch_path} is not present")

    provider = os.environ.get("PATCH_HUNK_AGENT_REGRESSION_PROVIDER", "mock-heuristic")
    llm = _build_llm(provider)
    pipeline = build_test_pipeline(
        source_dir=ROOT / case["source_tree"] if case.get("source_tree") else None,
        target_dir=ROOT / case["target_tree"] if case.get("target_tree") else None,
        llm=llm,
    )
    result = pipeline.run(
        PipelineInputs(
            patch_path=str(patch_path),
            patch_id=str(case["case_id"]),
            source_revision=case.get("source_revision") or SOURCE_REVISION,
            target_revision=case.get("target_revision") or TARGET_REVISION,
            build_tasks=False,
        )
    )

    scope = set(case.get("hunk_scope") or [])
    predicted = [
        record
        for record in result["dependency_validation"]["dependencies"]
        if not scope or {record["source_hunk"], record["target_hunk"]} <= scope
    ]
    metrics = Evaluator().dependency_metrics(predicted, case["reference_dependencies"])
    grouping = Evaluator().grouping_metrics(
        [
            [hunk for hunk in group["hunk_ids"] if not scope or hunk in scope]
            for group in result["grouping_document"]["groups"]
            if not scope or set(group["hunk_ids"]) & scope
        ],
        [group for group in case["reference_groups"] if not scope or set(group) <= scope],
    )

    # Cases that carry dependency-level ground truth are scored on dependency recall;
    # cases that only carry a reviewed grouping are scored on co-membership recall.
    thresholds = {"dependency_recall": 1.0, "co_membership_recall": 0.0, "over_grouping_rate": 0.0}
    thresholds.update(case.get("thresholds") or {})
    if case["reference_dependencies"]:
        assert metrics["dependency_recall"] >= thresholds["dependency_recall"], metrics
    else:
        assert grouping["co_membership_recall"] >= thresholds["co_membership_recall"], grouping
    assert grouping["over_grouping_rate"] <= thresholds["over_grouping_rate"], grouping

    outcome = case.get("outcome") or {}
    if outcome:
        scored = Evaluator().backport_metrics([outcome])
        assert scored["task_count"] == 1


def _build_llm(provider: str):
    from patch_hunk_agent.llm import build_llm_client
    from patch_hunk_agent.config import load_config

    if provider in ("mock", "mock-heuristic"):
        return build_llm_client(provider)
    config = load_config()
    return build_llm_client(provider, provider_config=config.models.get("providers", {}).get(provider, {}))
