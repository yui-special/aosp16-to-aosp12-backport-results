"""End-to-end behaviour for the five documented scenarios (section 15.1)."""

from __future__ import annotations

import json

import pytest

from conftest import SOURCE_REVISION, TARGET_REVISION
from helpers import fixture_path
from patch_hunk_agent.pipeline import PipelineInputs
from patch_hunk_agent.schemas import check_analysis_result, check_grouping_document


def _run(pipeline, name, *, with_context=True, **kwargs):
    inputs = PipelineInputs(
        patch_path=str(fixture_path(name)),
        patch_id=name,
        source_revision=SOURCE_REVISION if with_context else None,
        target_revision=TARGET_REVISION if with_context else None,
        **kwargs,
    )
    return pipeline.run(inputs)


def test_test_a_definition_use_is_grouped(patched_pipeline):
    result = _run(patched_pipeline, "simple_dependency")
    document = result["grouping_document"]
    assert document["status"] == "complete"
    assert check_grouping_document(document) == []
    assert check_analysis_result(result["analysis"]) == []
    assert [group["hunk_ids"] for group in document["groups"]] == [["H1", "H2"]]
    dependency = result["dependency_validation"]["dependencies"][0]
    assert dependency["category"] == "DEFINITION_USE"
    assert dependency["strength"] == "STRONG"
    assert dependency["target_version_status"] == "absent"
    assert dependency["evidence"][0]["symbol"] == "clamp_value"
    assert document["validation"]["strong_dependencies_within_groups"] is True


def test_test_b_interface_signature_is_grouped(patched_pipeline):
    result = _run(patched_pipeline, "signature_dependency")
    document = result["grouping_document"]
    assert document["status"] == "complete"
    assert [group["hunk_ids"] for group in document["groups"]] == [["H1", "H2"]]
    dependency = result["dependency_validation"]["dependencies"][0]
    assert dependency["category"] == "INTERFACE_SIGNATURE"
    assert dependency["strength"] == "STRONG"
    assert dependency["separation_risk"]
    evidence_text = " ".join(item["description"] for item in dependency["evidence"])
    assert "process" in evidence_text


def test_test_c_unrelated_hunks_are_not_merged(patched_pipeline):
    result = _run(patched_pipeline, "independent_hunks")
    document = result["grouping_document"]
    assert document["status"] == "complete"
    assert [group["hunk_ids"] for group in document["groups"]] == [["H1"], ["H2"]]
    assert result["dependency_validation"]["dependencies"] == []
    assert result["hunks"][0]["file_path"] == result["hunks"][1]["file_path"]
    assert document["metadata"]["grouping"]["strong_edge_count"] == 0


def test_test_d_target_already_satisfies_is_not_force_merged(patched_pipeline):
    result = _run(patched_pipeline, "target_already_satisfies")
    document = result["grouping_document"]
    dependency = result["dependency_validation"]["dependencies"][0]
    assert dependency["target_version_status"] == "present"
    assert dependency["strength"] == "CONDITIONAL"
    assert [group["hunk_ids"] for group in document["groups"]] == [["H1"], ["H2"]]
    assert "target_version_status=present" in document["groups"][0]["group_reason"]
    not_promoted = document["metadata"]["grouping"]["not_promoted"]
    assert any(
        "does not show a missing prerequisite" in reason
        for item in not_promoted
        for reason in item.get("reasons", [])
    )
    assert not document["unresolved_questions"]


def test_test_e_missing_evidence_yields_partial_and_questions(contextless_pipeline):
    result = _run(contextless_pipeline, "insufficient_evidence", with_context=False)
    document = result["grouping_document"]
    assert document["status"] == "partial"
    assert document["unresolved_questions"]
    assert all(
        record["strength"] != "STRONG" for record in result["dependency_validation"]["dependencies"]
    )
    assert all(record["target_version_status"] == "unknown" for record in result["analysis"]["dependencies"])
    assert document["validation"]["unresolved_dependencies_reported"] is True
    question = document["unresolved_questions"][0]
    assert question["needs_more_evidence"] is True
    assert question["affected_groups"]
    assert result["contexts"][0]["retrieval_status"] == "unavailable"


@pytest.mark.parametrize(
    "name,with_context",
    [
        ("simple_dependency", True),
        ("signature_dependency", True),
        ("independent_hunks", True),
        ("target_already_satisfies", True),
        ("insufficient_evidence", False),
    ],
)
def test_every_hunk_is_assigned_exactly_once(patched_pipeline, name, with_context):
    result = _run(patched_pipeline, name, with_context=with_context)
    assigned = [
        hunk_id
        for group in result["grouping_document"]["groups"]
        for hunk_id in group["hunk_ids"]
    ]
    expected = [hunk["hunk_id"] for hunk in result["hunks"]]
    assert sorted(assigned) == sorted(expected)
    assert len(assigned) == len(set(assigned))
    assert result["grouping_document"]["validation"]["all_hunks_assigned_once"] is True


def test_repeated_runs_are_deterministic(patched_pipeline):
    def stable_view(document: dict) -> str:
        return json.dumps(
            {
                key: document[key]
                for key in (
                    "patch_id",
                    "status",
                    "groups",
                    "cross_group_dependencies",
                    "unresolved_questions",
                    "validation",
                )
            },
            sort_keys=True,
            ensure_ascii=False,
        )

    payloads = {
        stable_view(_run(patched_pipeline, "signature_dependency")["grouping_document"])
        for _ in range(3)
    }
    assert len(payloads) == 1


def test_run_records_timing_tokens_and_events(patched_pipeline):
    result = _run(patched_pipeline, "simple_dependency")
    run = result["run"]
    assert run["duration_s"] >= 0
    assert run["tokens"]["calls"] >= 1
    events = {event["event"] for event in run["events"]}
    assert {
        "run_started",
        "patch_parsed",
        "evidence_extracted",
        "dependencies_validated",
        "run_finished",
    } <= events
    assert run["config"]["provider"] == "mock-heuristic"


def test_patch_and_source_tree_are_never_modified(patched_pipeline, source_tree, target_tree):
    patch_before = fixture_path("simple_dependency").read_bytes()
    source_before = (source_tree / "src" / "example.c").read_bytes()
    target_before = (target_tree / "src" / "example.c").read_bytes()
    _run(patched_pipeline, "simple_dependency")
    assert fixture_path("simple_dependency").read_bytes() == patch_before
    assert (source_tree / "src" / "example.c").read_bytes() == source_before
    assert (target_tree / "src" / "example.c").read_bytes() == target_before


def test_resolution_can_promote_a_conditional_dependency(patched_pipeline):
    result = _run(
        patched_pipeline, "target_already_satisfies", resolutions={"D1": "promote"}
    )
    document = result["grouping_document"]
    assert [group["hunk_ids"] for group in document["groups"]] == [["H1", "H2"]]
    assert document["metadata"]["grouping"]["promotions"][0]["resolution"] == "promote"


def test_tasks_handed_to_the_backport_agent(patched_pipeline):
    result = _run(patched_pipeline, "simple_dependency")
    tasks = result["tasks"]
    assert tasks["schema"] == "portgpt.group_task/v1"
    assert tasks["group_count"] == 1
    group = tasks["groups"][0]
    assert group["must_port_together"] is True
    assert group["hunk_ids"] == ["H1", "H2"]
    assert group["dependencies"][0]["category"] == "DEFINITION_USE"
    assert group["attention_points"]
    assert group["hunks"][0]["diff_text"].startswith("@@")
    assert group["hunks"][0]["target_context"]["available"] is True
    assert any("not verified backport results" in caveat for caveat in tasks["caveats"])
    assert tasks["traceability_template"]["group_ids"] == ["G1"]


def test_complete_example_patch_splits_into_two_dependency_groups(project_root):
    from conftest import build_test_pipeline

    pipeline = build_test_pipeline(
        source_dir=project_root / "examples" / "source",
        target_dir=project_root / "examples" / "target",
    )
    result = pipeline.run(
        PipelineInputs(
            patch_path=str(project_root / "examples" / "input" / "sample.diff"),
            patch_id="sample",
            source_revision=SOURCE_REVISION,
            target_revision=TARGET_REVISION,
        )
    )
    document = result["grouping_document"]
    assert document["status"] == "complete"
    assert [group["hunk_ids"] for group in document["groups"]] == [
        ["H1", "H2"],
        ["H3", "H4"],
    ]
    categories = {record["category"] for record in result["dependency_validation"]["dependencies"]}
    assert categories == {"DEFINITION_USE", "INTERFACE_SIGNATURE"}
