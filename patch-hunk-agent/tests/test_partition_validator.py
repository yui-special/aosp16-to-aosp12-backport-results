"""The partition validator must catch structural and graph violations."""

from __future__ import annotations

from helpers import dependency
from patch_hunk_agent.partition_validator import PartitionValidator


def _group(group_id: str, hunk_ids: list[str], dependency_ids: list[str] | None = None) -> dict:
    return {
        "group_id": group_id,
        "hunk_ids": hunk_ids,
        "group_reason": "test",
        "dependency_ids": dependency_ids or [],
    }


def test_sound_partition_passes_every_check():
    groups = {"groups": [_group("G1", ["H1", "H2"], ["D1"]), _group("G2", ["H3"])]}
    report = PartitionValidator().validate(
        ["H1", "H2", "H3"], groups, [dependency("H1", "H2")], analysis_status="complete"
    )
    assert report["validation"] == {
        "all_hunks_assigned_once": True,
        "strong_dependencies_within_groups": True,
        "unresolved_dependencies_reported": True,
    }
    assert report["status"] == "complete"
    assert report["errors"] == [] and report["cross_group_dependencies"] == []


def test_strong_dependency_across_groups_fails_validation():
    groups = {"groups": [_group("G1", ["H1"]), _group("G2", ["H2"])]}
    report = PartitionValidator().validate(
        ["H1", "H2"], groups, [dependency("H1", "H2")], analysis_status="complete"
    )
    assert report["status"] == "failed"
    assert report["validation"]["strong_dependencies_within_groups"] is False
    assert report["cross_group_dependencies"][0]["pair"] == ["H1", "H2"]


def test_missing_hunk_is_an_error():
    groups = {"groups": [_group("G1", ["H1"])]}
    report = PartitionValidator().validate(["H1", "H2"], groups, [], analysis_status="complete")
    assert report["status"] == "failed"
    assert report["validation"]["all_hunks_assigned_once"] is False
    assert any(error.get("hunk_ids") == ["H2"] for error in report["errors"])


def test_duplicate_assignment_is_an_error():
    groups = {"groups": [_group("G1", ["H1"]), _group("G2", ["H1"])]}
    report = PartitionValidator().validate(["H1"], groups, [], analysis_status="complete")
    assert report["status"] == "failed"
    assert any("more than one group" in error["reason"] for error in report["errors"])


def test_fictional_hunk_is_an_error():
    groups = {"groups": [_group("G1", ["H1", "H9"])]}
    report = PartitionValidator().validate(["H1"], groups, [], analysis_status="complete")
    assert report["status"] == "failed"
    assert any("not part of the patch" in error["reason"] for error in report["errors"])


def test_empty_group_is_an_error():
    groups = {"groups": [_group("G1", [])]}
    report = PartitionValidator().validate(["H1"], groups, [], analysis_status="complete")
    assert report["status"] == "failed"


def test_invalid_group_id_is_an_error():
    groups = {"groups": [_group("group-one", ["H1"])]}
    report = PartitionValidator().validate(["H1"], groups, [], analysis_status="complete")
    assert any("invalid group_id" in error["reason"] for error in report["errors"])


def test_unknown_dependency_reference_is_a_warning():
    groups = {"groups": [_group("G1", ["H1", "H2"], ["D9"])]}
    report = PartitionValidator().validate(
        ["H1", "H2"], groups, [dependency("H1", "H2")], analysis_status="complete"
    )
    assert report["status"] == "complete"
    assert report["warnings"][0]["reason"] == "group references an unknown dependency id"


def test_unresolved_questions_make_the_partition_partial_and_mark_groups():
    groups = {"groups": [_group("G1", ["H1"]), _group("G2", ["H2"])]}
    questions = [
        {
            "question": "is the prerequisite already present?",
            "hunk_ids": ["H2"],
            "reason": "unknown target state",
            "needs_more_evidence": True,
        }
    ]
    report = PartitionValidator().validate(
        ["H1", "H2"], groups, [], unresolved_questions=questions, analysis_status="partial"
    )
    assert report["status"] == "partial"
    assert report["validation"]["unresolved_dependencies_reported"] is True
    marked = [group for group in report["groups"] if group.get("has_unresolved_questions")]
    assert [group["group_id"] for group in marked] == ["G2"]
    assert report["unresolved_questions"][0]["affected_groups"] == ["G2"]


def test_grouping_is_accepted_as_a_bare_list():
    report = PartitionValidator().validate(
        ["H1"], [_group("G1", ["H1"])], [], analysis_status="complete"
    )
    assert report["status"] == "complete"


def test_missing_analysis_status_is_partial_by_default():
    report = PartitionValidator().validate(["H1"], [_group("G1", ["H1"])], [])
    assert report["status"] == "partial"
    assert report["checks"]["analysis_status"] is None


def test_group_id_order_is_normalised_numerically():
    groups = {"groups": [_group("G10", ["H2"]), _group("G2", ["H1"])]}
    report = PartitionValidator().validate(["H1", "H2"], groups, [], analysis_status="complete")
    assert report["status"] == "complete"
    assert [len(group["hunk_ids"]) for group in report["groups"]] == [1, 1]
