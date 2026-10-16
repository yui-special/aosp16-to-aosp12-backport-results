"""Evaluation metrics for dependency identification, grouping and outcomes."""

from __future__ import annotations

from patch_hunk_agent.evaluator import Evaluator


def _dep(source, target, category="DEFINITION_USE", strength="STRONG"):
    return {"source_hunk": source, "target_hunk": target, "category": category, "strength": strength}


def test_dependency_metrics_perfect_match():
    reference = [_dep("H1", "H2"), _dep("H3", "H4", category="SHARED_STATE")]
    predicted = [_dep("H2", "H1"), _dep("H4", "H3", category="SHARED_STATE")]
    metrics = Evaluator().dependency_metrics(predicted, reference)
    assert metrics["dependency_precision"] == 1.0
    assert metrics["dependency_recall"] == 1.0
    assert metrics["dependency_f1"] == 1.0
    assert metrics["strong_miss_rate"] == 0.0


def test_dependency_metrics_with_false_positives_and_misses():
    reference = [_dep("H1", "H2"), _dep("H3", "H4")]
    predicted = [_dep("H1", "H2"), _dep("H5", "H6")]
    metrics = Evaluator().dependency_metrics(predicted, reference)
    assert metrics["dependency_precision"] == 0.5
    assert metrics["dependency_recall"] == 0.5
    assert metrics["false_positives"] == ["H5|H6"]
    assert metrics["false_negatives"] == ["H3|H4"]


def test_strong_miss_rate_counts_missed_strong_edges():
    reference = [_dep("H1", "H2"), _dep("H3", "H4")]
    predicted = [_dep("H1", "H2"), _dep("H3", "H4", strength="WEAK")]
    metrics = Evaluator().dependency_metrics(predicted, reference)
    assert metrics["strong_miss_rate"] == 0.5
    assert metrics["missed_strong_pairs"] == ["H3|H4"]


def test_categories_are_scored_separately():
    reference = [_dep("H1", "H2", category="DEFINITION_USE")]
    predicted = [_dep("H1", "H2", category="SHARED_STATE")]
    metrics = Evaluator().dependency_metrics(predicted, reference)
    assert metrics["dependency_recall"] == 1.0
    assert metrics["category_recall"] == 0.0


def test_grouping_metrics_detect_over_grouping():
    predicted = [["H1", "H2", "H3"]]
    reference = [["H1", "H2"], ["H3"]]
    metrics = Evaluator().grouping_metrics(predicted, reference)
    # two of the three merged pairs are unnecessary
    assert metrics["over_grouping_rate"] == 0.666667
    assert metrics["under_grouping_rate"] == 0.0
    assert metrics["over_merged_pairs"] == ["H1|H3", "H2|H3"]


def test_grouping_metrics_detect_under_grouping():
    predicted = [["H1"], ["H2"]]
    reference = [["H1", "H2"]]
    metrics = Evaluator().grouping_metrics(predicted, reference)
    assert metrics["over_grouping_rate"] == 0.0
    assert metrics["under_grouping_rate"] == 1.0
    assert metrics["co_membership_recall"] == 0.0


def test_backport_metrics_and_averages():
    records = [
        {
            "backport_success": True,
            "compile_pass": True,
            "test_pass": False,
            "tokens": 1000,
            "retries": 1,
            "runtime_s": 10,
        },
        {
            "backport_success": False,
            "compile_pass": True,
            "test_pass": True,
            "tokens": 2000,
            "retries": 3,
            "runtime_s": 30,
        },
    ]
    metrics = Evaluator().backport_metrics(records)
    assert metrics["backport_success_rate"] == 0.5
    assert metrics["compile_pass_rate"] == 1.0
    assert metrics["test_pass_rate"] == 0.5
    assert metrics["average_tokens"] == 1500.0
    assert metrics["average_retries"] == 2.0
    assert metrics["average_runtime_s"] == 20.0


def test_dataset_evaluation_aggregates_and_reports_missing_predictions():
    dataset = {
        "cases": [
            {
                "patch_id": "p1",
                "reference_dependencies": [_dep("H1", "H2")],
                "reference_groups": [["H1", "H2"]],
                "outcome": {"backport_success": True, "compile_pass": True, "test_pass": True},
            },
            {"patch_id": "p2", "reference_dependencies": [], "reference_groups": [["H1"], ["H2"]]},
        ]
    }
    predictions = [
        {
            "patch_id": "p1",
            "analysis": {"dependencies": [_dep("H1", "H2")]},
            "grouping": {"groups": [{"group_id": "G1", "hunk_ids": ["H1", "H2"]}]},
        }
    ]
    report = Evaluator().evaluate_dataset(dataset, predictions)
    assert report["aggregate"]["patch_count"] == 1
    assert report["aggregate"]["missing_predictions"] == ["p2"]
    assert report["aggregate"]["dependency_f1"] == 1.0
    assert report["aggregate"]["over_grouping_rate"] == 0.0
    assert report["aggregate"]["backport_success_rate"] == 1.0


def test_run_from_files_writes_the_report(tmp_path):
    dataset_path = tmp_path / "dataset.json"
    predictions_path = tmp_path / "predictions.json"
    output_path = tmp_path / "report.json"
    dataset_path.write_text('{"cases": []}', encoding="utf-8")
    predictions_path.write_text('{"predictions": []}', encoding="utf-8")
    report = Evaluator().run_from_files(dataset_path, predictions_path, output_path)
    assert report["aggregate"]["patch_count"] == 0
    assert output_path.is_file()
