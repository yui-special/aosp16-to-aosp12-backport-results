"""Grouping must be deterministic, minimal and fully explained."""

from __future__ import annotations

from helpers import as_json, dependency
from patch_hunk_agent.grouping_engine import (
    GroupingConfig,
    GroupingEngine,
    effective_strengths,
    merge_groups_across_strong_edges,
)
from patch_hunk_agent.schemas import check_grouping_document


def _groups(engine: GroupingEngine, hunk_ids, records, **kwargs) -> list[list[str]]:
    result = engine.group(hunk_ids, records, **kwargs)
    return [group["hunk_ids"] for group in result["groups"]]


def test_strong_edges_are_transitively_merged():
    engine = GroupingEngine()
    records = [dependency("H1", "H2"), dependency("H2", "H3", dependency_id="D2")]
    assert _groups(engine, ["H1", "H2", "H3"], records) == [["H1", "H2", "H3"]]


def test_unrelated_hunks_stay_separate_even_in_one_file():
    engine = GroupingEngine()
    result = engine.group(["H1", "H2"], [])
    assert [group["hunk_ids"] for group in result["groups"]] == [["H1"], ["H2"]]
    assert "No strong dependency" in result["groups"][0]["group_reason"]


def test_weak_dependencies_never_merge():
    engine = GroupingEngine()
    records = [dependency("H1", "H2", strength="WEAK")]
    result = engine.group(["H1", "H2"], records)
    assert [group["hunk_ids"] for group in result["groups"]] == [["H1"], ["H2"]]
    assert result["metadata"]["weak_dependencies_not_merged"] == ["D1"]


def test_grouping_is_deterministic_across_input_order():
    records = [
        dependency("H1", "H2", dependency_id="D1"),
        dependency("H4", "H5", dependency_id="D2", category="SHARED_STATE"),
        dependency("H2", "H3", dependency_id="D3"),
    ]
    engine = GroupingEngine()
    first = engine.group(["H1", "H2", "H3", "H4", "H5"], records)
    shuffled_hunks = ["H5", "H3", "H1", "H4", "H2"]
    shuffled_records = [records[2], records[0], records[1]]
    second = engine.group(shuffled_hunks, shuffled_records)
    assert as_json(first["groups"]) == as_json(second["groups"])


def test_isolated_hunks_still_receive_a_group():
    engine = GroupingEngine()
    result = engine.group(["H3", "H1", "H2"], [dependency("H1", "H2")])
    assert [group["group_id"] for group in result["groups"]] == ["G1", "G2"]
    assert [group["hunk_ids"] for group in result["groups"]] == [["H1", "H2"], ["H3"]]


def test_unknown_hunk_ids_in_dependencies_are_reported():
    engine = GroupingEngine()
    result = engine.group(["H1"], [dependency("H1", "H9")])
    assert result["metadata"]["ignored_edges"][0]["reason"] == "unknown hunk id"
    assert [group["hunk_ids"] for group in result["groups"]] == [["H1"]]


def test_conditional_promotion_requires_an_absent_prerequisite():
    engine = GroupingEngine()
    promotable = dependency("H1", "H2", strength="CONDITIONAL", target_version_status="absent")
    promoted = engine.group(["H1", "H2"], [promotable])
    assert [group["hunk_ids"] for group in promoted["groups"]] == [["H1", "H2"]]
    assert promoted["metadata"]["promotions"][0]["to"] == "STRONG"

    blocked = engine.group(
        ["H1", "H2"],
        [dependency("H1", "H2", strength="CONDITIONAL", target_version_status="present")],
    )
    assert [group["hunk_ids"] for group in blocked["groups"]] == [["H1"], ["H2"]]
    reasons = blocked["metadata"]["not_promoted"][0]["reasons"]
    assert any("does not show a missing prerequisite" in reason for reason in reasons)


def test_conditional_with_pending_evidence_is_not_promoted_and_asks_a_question():
    engine = GroupingEngine()
    record = dependency(
        "H1", "H2", strength="CONDITIONAL", needs_more_evidence=True, target_version_status="unknown"
    )
    result = engine.group(["H1", "H2"], [record])
    assert [group["hunk_ids"] for group in result["groups"]] == [["H1"], ["H2"]]
    assert result["unresolved_questions"]
    assert "evidence is still pending" in result["unresolved_questions"][0]["question"]


def test_promotion_can_be_disabled_by_config():
    engine = GroupingEngine(GroupingConfig(promote_conditional=False))
    record = dependency("H1", "H2", strength="CONDITIONAL")
    result = engine.group(["H1", "H2"], [record])
    assert [group["hunk_ids"] for group in result["groups"]] == [["H1"], ["H2"]]


def test_explicit_resolutions_override_the_policy():
    record = dependency("H1", "H2", strength="CONDITIONAL", target_version_status="present")
    engine = GroupingEngine()
    promoted = engine.group(["H1", "H2"], [record], resolutions={"D1": "promote"})
    assert [group["hunk_ids"] for group in promoted["groups"]] == [["H1", "H2"]]
    assert promoted["metadata"]["promotions"][0]["resolution"] == "promote"

    dropped = engine.group(["H1", "H2"], [record], resolutions={"D1": "drop"})
    assert [group["hunk_ids"] for group in dropped["groups"]] == [["H1"], ["H2"]]
    assert dropped["metadata"]["promotions"][0]["resolution"] == "drop"


def test_invalid_resolution_is_reported():
    engine = GroupingEngine()
    record = dependency("H1", "H2", strength="CONDITIONAL")
    result = engine.group(["H1", "H2"], [record], resolutions={"D1": "explode"})
    assert "invalid resolution" in result["metadata"]["not_promoted"][0]["reason"]
    assert [group["hunk_ids"] for group in result["groups"]] == [["H1"], ["H2"]]


def test_resolution_for_unknown_dependency_is_reported():
    engine = GroupingEngine()
    result = engine.group(["H1"], [], resolutions={"D9": "promote"})
    assert "does not exist" in result["metadata"]["not_promoted"][0]["reason"]


def test_singleton_reason_mentions_unpromoted_conditional_dependency():
    engine = GroupingEngine()
    record = dependency("H1", "H2", strength="CONDITIONAL", target_version_status="present")
    result = engine.group(["H1", "H2"], [record])
    assert "CONDITIONAL" in result["groups"][0]["group_reason"]
    assert result["groups"][0]["external_dependency_ids"] == ["D1"]


def test_effective_strengths_returns_unresolved_ids():
    records = [dependency("H1", "H2", dependency_id="D1")]
    strengths, promotions, not_promoted = effective_strengths(
        records, GroupingConfig(), {"D2": "promote"}
    )
    assert strengths == {"D1": "STRONG"}
    assert promotions == []
    assert not_promoted[0]["dependency_id"] == "D2"


def test_merge_repairs_a_split_strong_dependency():
    groups = [
        {"group_id": "G1", "hunk_ids": ["H1"], "dependency_ids": [], "group_reason": ""},
        {"group_id": "G2", "hunk_ids": ["H2"], "dependency_ids": ["D1"], "group_reason": ""},
        {"group_id": "G3", "hunk_ids": ["H3"], "dependency_ids": [], "group_reason": ""},
    ]
    records = [dependency("H1", "H2", dependency_id="D1")]
    repaired, repairs = merge_groups_across_strong_edges(groups, records)
    assert repairs and repairs[0]["pair"] == ["H1", "H2"]
    assert [group["hunk_ids"] for group in repaired] == [["H1", "H2"], ["H3"]]
    assert [group["group_id"] for group in repaired] == ["G1", "G2"]
    assert repaired[0]["dependency_ids"] == ["D1"]
    assert repaired[0]["repaired"] is True


def test_merge_is_a_no_op_when_the_partition_is_already_sound():
    groups = [{"group_id": "G1", "hunk_ids": ["H1", "H2"], "dependency_ids": ["D1"]}]
    repaired, repairs = merge_groups_across_strong_edges(
        groups, [dependency("H1", "H2", dependency_id="D1")]
    )
    assert repairs == []
    assert repaired == groups


def test_grouping_output_is_schema_valid_with_metadata():
    engine = GroupingEngine()
    result = engine.group(["H1", "H2"], [dependency("H1", "H2")])
    document = {
        "patch_id": "p1",
        "status": "complete",
        "groups": result["groups"],
    }
    assert check_grouping_document(document) == []
