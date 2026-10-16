"""CLI contract: documented commands, exit codes and JSON output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from patch_hunk_agent.cli import main


@pytest.fixture
def config_path(project_root: Path) -> str:
    return str(project_root / "configs" / "default.yaml")


def test_group_writes_a_valid_grouping_document(
    tmp_path, project_root, fixtures_dir, config_path, capsys
):
    output = tmp_path / "grouping.json"
    exit_code = main(
        [
            "group",
            "--patch",
            str(fixtures_dir / "simple_dependency.diff"),
            "--patch-id",
            "cli_case",
            "--source-revision",
            "SRC",
            "--target-revision",
            "TGT",
            "--source-tree",
            str(project_root / "tests" / "fixtures" / "trees" / "source"),
            "--target-tree",
            str(project_root / "tests" / "fixtures" / "trees" / "target"),
            "--config",
            config_path,
            "--provider",
            "mock-heuristic",
            # The offline mock answers the dependency schema, so pin the documented
            # deterministic algorithm; global mode needs a model that can propose
            # work packages (see tests/test_global_grouper.py).
            "--grouping-mode",
            "components",
            "--output",
            str(output),
        ]
    )
    assert exit_code == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["patch_id"] == "cli_case"
    assert document["status"] == "complete"
    assert [group["hunk_ids"] for group in document["groups"]] == [["H1", "H2"]]
    stdout = capsys.readouterr().out
    assert "cli_case" in stdout and "G1: H1, H2" in stdout


def test_group_can_emit_tasks_and_run_records(tmp_path, project_root, fixtures_dir, config_path):
    tasks = tmp_path / "tasks.json"
    analysis = tmp_path / "analysis.json"
    record = tmp_path / "run.jsonl"
    exit_code = main(
        [
            "group",
            "--patch",
            str(fixtures_dir / "signature_dependency.diff"),
            "--source-revision",
            "SRC",
            "--target-revision",
            "TGT",
            "--source-tree",
            str(project_root / "tests" / "fixtures" / "trees" / "source"),
            "--target-tree",
            str(project_root / "tests" / "fixtures" / "trees" / "target"),
            "--config",
            config_path,
            "--provider",
            "mock-heuristic",
            "--output",
            str(tmp_path / "grouping.json"),
            "--tasks-output",
            str(tasks),
            "--analysis-output",
            str(analysis),
            "--record",
            str(record),
        ]
    )
    assert exit_code == 0
    assert json.loads(tasks.read_text(encoding="utf-8"))["group_count"] == 1
    assert json.loads(analysis.read_text(encoding="utf-8"))["status"] == "complete"
    events = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    assert any(event["event"] == "run_finished" for event in events)


def test_group_prints_full_json_on_request(tmp_path, project_root, fixtures_dir, config_path, capsys):
    exit_code = main(
        [
            "group",
            "--patch",
            str(fixtures_dir / "independent_hunks.diff"),
            "--source-revision",
            "SRC",
            "--target-revision",
            "TGT",
            "--source-tree",
            str(project_root / "tests" / "fixtures" / "trees" / "source"),
            "--target-tree",
            str(project_root / "tests" / "fixtures" / "trees" / "target"),
            "--config",
            config_path,
            "--provider",
            "mock-heuristic",
            "--output",
            str(tmp_path / "grouping.json"),
            "--print-json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["grouping_document"]["groups"][0]["hunk_ids"] == ["H1"]


def test_inspect_lists_hunks_and_evidence(fixtures_dir, config_path, capsys):
    exit_code = main(
        [
            "inspect",
            "--patch",
            str(fixtures_dir / "simple_dependency.diff"),
            "--config",
            config_path,
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["patch"]["hunk_count"] == 2
    assert payload["hunks"][0]["symbols"][0]["name"] == "clamp_value"


def test_inspect_reports_retrieval_status(fixtures_dir, project_root, config_path, capsys):
    exit_code = main(
        [
            "inspect",
            "--patch",
            str(fixtures_dir / "simple_dependency.diff"),
            "--source-tree",
            str(project_root / "tests" / "fixtures" / "trees" / "source"),
            "--target-tree",
            str(project_root / "tests" / "fixtures" / "trees" / "target"),
            "--config",
            config_path,
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["retrieval"][0]["target_symbol_status"]["clamp_value"] == "absent"
    assert payload["candidate_pairs"][0]["machine_hint"] == "STRONG_HINT"


def test_validate_accepts_a_sound_grouping(tmp_path, fixtures_dir, config_path):
    grouping = tmp_path / "grouping.json"
    analysis = tmp_path / "analysis.json"
    main(
        [
            "group",
            "--patch",
            str(fixtures_dir / "simple_dependency.diff"),
            "--config",
            config_path,
            "--provider",
            "mock-heuristic",
            "--no-context",
            "--output",
            str(grouping),
            "--analysis-output",
            str(analysis),
        ]
    )
    report_path = tmp_path / "validation.json"
    exit_code = main(
        [
            "validate",
            "--grouping",
            str(grouping),
            "--analysis",
            str(analysis),
            "--config",
            config_path,
            "--output",
            str(report_path),
        ]
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["validation"]["all_hunks_assigned_once"] is True


def test_validate_rejects_a_split_strong_dependency(tmp_path, fixtures_dir, config_path, capsys):
    grouping = {
        "patch_id": "tampered",
        "status": "complete",
        "groups": [
            {"group_id": "G1", "hunk_ids": ["H1"], "dependency_ids": [], "group_reason": ""},
            {"group_id": "G2", "hunk_ids": ["H2"], "dependency_ids": ["D1"], "group_reason": ""},
        ],
        "dependencies": [
            {
                "dependency_id": "D1",
                "source_hunk": "H1",
                "target_hunk": "H2",
                "category": "DEFINITION_USE",
                "strength": "STRONG",
                "evidence": [{"file_path": "src/example.c", "symbol": "clamp_value", "line": 8}],
                "separation_risk": "unresolved call",
                "target_version_status": "absent",
                "reasoning_summary": "x",
                "needs_more_evidence": False,
            }
        ],
    }
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(grouping), encoding="utf-8")
    exit_code = main(["validate", "--grouping", str(path), "--config", config_path])
    assert exit_code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert report["validation"]["strong_dependencies_within_groups"] is False


def test_evaluate_scores_a_dataset(tmp_path, capsys):
    dataset = {
        "cases": [
            {
                "patch_id": "p1",
                "hunks": ["H1", "H2"],
                "reference_dependencies": [
                    {"source_hunk": "H1", "target_hunk": "H2", "category": "DEFINITION_USE", "strength": "STRONG"}
                ],
                "reference_groups": [["H1", "H2"]],
                "outcome": {
                    "backport_success": True,
                    "compile_pass": True,
                    "test_pass": True,
                    "tokens": 1200,
                    "retries": 0,
                    "runtime_s": 12.5,
                },
            }
        ]
    }
    predictions = {
        "predictions": [
            {
                "patch_id": "p1",
                "analysis": {
                    "dependencies": [
                        {
                            "source_hunk": "H1",
                            "target_hunk": "H2",
                            "category": "DEFINITION_USE",
                            "strength": "STRONG",
                        }
                    ]
                },
                "grouping": {"groups": [{"group_id": "G1", "hunk_ids": ["H1", "H2"]}]},
                "grouping_document": {"status": "complete"},
            }
        ]
    }
    dataset_path = tmp_path / "dataset.json"
    predictions_path = tmp_path / "predictions.json"
    output_path = tmp_path / "report.json"
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")
    predictions_path.write_text(json.dumps(predictions), encoding="utf-8")
    exit_code = main(
        [
            "evaluate",
            "--dataset",
            str(dataset_path),
            "--predictions",
            str(predictions_path),
            "--output",
            str(output_path),
        ]
    )
    assert exit_code == 0
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["aggregate"]["dependency_f1"] == 1.0
    assert report["aggregate"]["backport_success_rate"] == 1.0
    assert report["aggregate"]["average_tokens"] == 1200.0
    assert "dependency_precision" in capsys.readouterr().out


def test_demo_runs_offline_and_writes_outputs(tmp_path, project_root):
    exit_code = main(
        [
            "demo",
            "--output",
            str(tmp_path),
            "--fixtures",
            str(project_root / "tests" / "fixtures"),
            "--config",
            str(project_root / "configs" / "default.yaml"),
        ]
    )
    assert exit_code == 0
    summary = json.loads((tmp_path / "demo_summary.json").read_text(encoding="utf-8"))
    assert len(summary["cases"]) == 5
    by_id = {case["patch_id"]: case for case in summary["cases"]}
    assert by_id["simple_dependency"]["groups"] == [{"group_id": "G1", "hunk_ids": ["H1", "H2"]}]
    assert by_id["independent_hunks"]["groups"] == [
        {"group_id": "G1", "hunk_ids": ["H1"]},
        {"group_id": "G2", "hunk_ids": ["H2"]},
    ]
    assert (tmp_path / "insufficient_evidence.grouping.json").is_file()


def test_providers_command_lists_providers(project_root, capsys):
    assert main(["providers"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["default"] == "mock-heuristic"
    assert set(payload["providers"]) >= {"mock", "mock-heuristic", "openai"}


def test_missing_patch_file_is_reported_as_an_error(tmp_path, config_path, capsys):
    exit_code = main(
        ["group", "--patch", str(tmp_path / "missing.diff"), "--config", config_path]
    )
    assert exit_code == 2
    assert "error" in capsys.readouterr().err


def test_invalid_resolution_syntax_is_rejected(tmp_path, fixtures_dir, config_path, capsys):
    exit_code = main(
        [
            "group",
            "--patch",
            str(fixtures_dir / "simple_dependency.diff"),
            "--config",
            config_path,
            "--resolution",
            "nonsense",
        ]
    )
    assert exit_code == 2
    assert "invalid --resolution" in capsys.readouterr().err
