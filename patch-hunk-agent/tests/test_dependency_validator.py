"""Validator rules: evidence-driven downgrades and independent re-checks."""

from __future__ import annotations

from helpers import dependency
from patch_hunk_agent.dependency_validator import DependencyValidator


def _validate(records, hunk_ids=("H1", "H2", "H3"), **config):
    return DependencyValidator(config or None).validate(records, list(hunk_ids))


def test_valid_strong_dependency_is_kept():
    report = _validate([dependency("H1", "H2")])
    assert len(report["dependencies"]) == 1
    assert report["dependencies"][0]["strength"] == "STRONG"
    assert report["rejected"] == []


def test_strong_without_evidence_is_downgraded():
    report = _validate([dependency("H1", "H2", evidence=[])])
    assert report["dependencies"][0]["strength"] == "WEAK"
    assert report["warnings"][0]["reason"] == "missing_evidence"


def test_strong_with_weak_when_no_evidence_disabled_is_conditional():
    report = _validate(
        [dependency("H1", "H2", evidence=[])], weak_when_no_evidence=False
    )
    assert report["dependencies"][0]["strength"] == "CONDITIONAL"


def test_non_locatable_evidence_is_downgraded():
    report = _validate(
        [
            dependency(
                "H1",
                "H2",
                evidence=[{"description": "prose that points at nothing locatable"}],
            )
        ]
    )
    assert report["dependencies"][0]["strength"] == "CONDITIONAL"
    assert report["warnings"][0]["reason"] == "evidence_not_locatable"


def test_missing_separation_risk_is_downgraded():
    report = _validate([dependency("H1", "H2", separation_risk="")])
    assert report["dependencies"][0]["strength"] == "CONDITIONAL"
    assert report["warnings"][0]["reason"] == "missing_separation_risk"


def test_target_present_downgrades_and_records_the_prerequisite():
    report = _validate([dependency("H1", "H2", target_version_status="present")])
    record = report["dependencies"][0]
    assert record["strength"] == "CONDITIONAL"
    assert record["target_version_status"] == "present"
    assert any("already satisfies" in note for note in record["notes"])
    assert report["warnings"][0]["reason"] == "target_already_satisfies"


def test_pending_evidence_downgrades_and_asks_a_question():
    report = _validate([dependency("H1", "H2", needs_more_evidence=True)])
    assert report["dependencies"][0]["strength"] == "CONDITIONAL"
    assert report["warnings"][0]["reason"] == "pending_evidence"
    assert report["unresolved_questions"]


def test_unknown_target_state_alone_does_not_downgrade_strong():
    """A rename of the same symbol is strong regardless of the target version."""
    report = _validate([dependency("H1", "H2", target_version_status="unknown")])
    assert report["dependencies"][0]["strength"] == "STRONG"


def test_unknown_hunk_id_is_rejected():
    report = _validate([dependency("H1", "H9")])
    assert report["dependencies"] == []
    assert "not part of this patch" in report["rejected"][0]["reason"]


def test_self_dependency_is_rejected():
    report = _validate([dependency("H1", "H1")])
    assert report["dependencies"] == []
    assert report["rejected"][0]["reason"] == "self dependency"


def test_duplicate_pairs_are_merged_keeping_the_weaker_strength():
    records = [
        dependency("H1", "H2", strength="STRONG", dependency_id="D1"),
        dependency(
            "H2",
            "H1",
            strength="WEAK",
            dependency_id="D2",
            evidence=[{"file_path": "x.c", "symbol": "other", "line": 3}],
        ),
    ]
    report = _validate(records)
    assert len(report["dependencies"]) == 1
    merged = report["dependencies"][0]
    assert merged["strength"] == "WEAK"
    assert {item["symbol"] for item in merged["evidence"]} == {"helper", "other"}
    assert report["warnings"][0]["reason"] == "duplicate_dependency_merged"


def test_dependency_ids_are_renumbered_deterministically():
    records = [
        dependency("H3", "H1", dependency_id="D9", category="SHARED_STATE"),
        dependency("H1", "H2", dependency_id="D4", category="DEFINITION_USE"),
    ]
    first = _validate(records)
    second = _validate(list(reversed(records)))
    assert [record["dependency_id"] for record in first["dependencies"]] == ["D1", "D2"]
    assert [record["dependency_id"] for record in second["dependencies"]] == ["D1", "D2"]
    assert [record["category"] for record in first["dependencies"]] == [
        record["category"] for record in second["dependencies"]
    ]


def test_context_disagreement_is_reported_as_a_warning():
    contexts = {
        "H2": {
            "hunk_id": "H2",
            "retrieval_status": "complete",
            "target_symbol_status": {"helper": "present"},
        }
    }
    report = DependencyValidator().validate(
        [dependency("H1", "H2", target_version_status="absent")],
        ["H1", "H2"],
        contexts=contexts,
    )
    assert any(
        warning["reason"] == "record_contradicts_retrieved_context"
        for warning in report["warnings"]
    )


def test_stats_summarise_the_outcome():
    report = _validate(
        [
            dependency("H1", "H2"),
            dependency("H2", "H3", category="SHARED_STATE", strength="WEAK", dependency_id="D2"),
        ]
    )
    assert report["stats"]["dependency_count"] == 2
    assert report["stats"]["by_strength"] == {"STRONG": 1, "WEAK": 1}
    assert report["stats"]["by_category"]["SHARED_STATE"] == 1
