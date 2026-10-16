"""Analyzer behaviour: parsing, repair, rejection and bounded retries."""

from __future__ import annotations

import json

import pytest

from helpers import fixture_contexts, fixture_hunks
from patch_hunk_agent.dependency_analyzer import AnalyzerConfig, DependencyAnalyzer
from patch_hunk_agent.evidence_extractor import extract
from patch_hunk_agent.llm import HeuristicMockClient, MockLLMClient

VALID_RESPONSE = {
    "status": "complete",
    "dependencies": [
        {
            "source_hunk": "H1",
            "target_hunk": "H2",
            "category": "DEFINITION_USE",
            "strength": "STRONG",
            "evidence": [
                {"file_path": "src/example.c", "symbol": "clamp_value", "line": 8, "description": "d"}
            ],
            "separation_risk": "the call stays unresolved",
            "target_version_status": "absent",
            "reasoning_summary": "target lacks clamp_value",
            "needs_more_evidence": False,
        }
    ],
    "unresolved_questions": [],
}


def _analyze(hunks, responses=None, responder=None, **config):
    client = MockLLMClient(responses=responses, responder=responder)
    analyzer = DependencyAnalyzer(client, config=AnalyzerConfig(**config) if config else None)
    return analyzer.analyze(
        hunks, extract(hunks), patch_id="p1", source_revision="SRC", target_revision="TGT"
    ), client


def test_plain_json_response_is_accepted():
    hunks = fixture_hunks("simple_dependency")
    result, client = _analyze(hunks, responses=[json.dumps(VALID_RESPONSE)])
    assert result["status"] == "complete"
    assert len(result["dependencies"]) == 1
    assert result["dependencies"][0]["dependency_id"] == "D1"
    assert result["dependencies"][0]["category"] == "DEFINITION_USE"
    assert len(client.calls) == 1
    assert check_analysis(result) == []


def test_markdown_fenced_json_is_repaired():
    hunks = fixture_hunks("simple_dependency")
    result, _ = _analyze(hunks, responses=["```json\n" + json.dumps(VALID_RESPONSE) + "\n```"])
    assert len(result["dependencies"]) == 1
    assert "stripped_markdown_fence" in result["call_records"][0]["repairs"]


def test_bare_list_response_is_accepted():
    hunks = fixture_hunks("simple_dependency")
    result, _ = _analyze(hunks, responses=[json.dumps(VALID_RESPONSE["dependencies"])])
    assert len(result["dependencies"]) == 1


def test_unknown_hunk_id_is_rejected_not_silently_dropped():
    hunks = fixture_hunks("simple_dependency")
    bad = json.loads(json.dumps(VALID_RESPONSE))
    bad["dependencies"][0]["target_hunk"] = "H99"
    result, _ = _analyze(hunks, responses=[json.dumps(bad), json.dumps(VALID_RESPONSE)])
    assert result["rejected_records"]
    assert "not part of this patch" in result["rejected_records"][0]["reason"]
    assert len(result["dependencies"]) == 1  # recovered on the repair attempt


def test_invalid_strength_is_rejected():
    hunks = fixture_hunks("simple_dependency")
    bad = json.loads(json.dumps(VALID_RESPONSE))
    bad["dependencies"][0]["strength"] = "MAYBE"
    result, client = _analyze(hunks, responses=[json.dumps(bad), json.dumps(bad)])
    assert result["dependencies"] == []
    assert len(result["rejected_records"]) >= 1
    assert result["status"] == "partial"
    # max_format_retries=2 -> one attempt plus two bounded repair attempts.
    assert len(client.calls) == 3


def test_retries_are_bounded():
    hunks = fixture_hunks("simple_dependency")
    result, client = _analyze(
        hunks, responses=["not json at all"] * 5, max_format_retries=2
    )
    assert len(client.calls) == 3
    assert len(result["call_records"]) == 3
    assert result["status"] in ("partial", "failed")
    assert result["unresolved_questions"]


def test_hunks_without_candidates_still_report_status():
    hunks = fixture_hunks("simple_dependency")
    analyzer = DependencyAnalyzer(MockLLMClient(responses=[]))
    result = analyzer.analyze(hunks, [])
    assert result["dependencies"] == []
    assert result["status"] == "complete"
    assert result["metadata"]["candidate_pair_count"] == 0


def test_heuristic_provider_matches_machine_evidence():
    hunks = fixture_hunks("simple_dependency")
    contexts = fixture_contexts(hunks)
    analyzer = DependencyAnalyzer(HeuristicMockClient())
    result = analyzer.analyze(hunks, extract(hunks, contexts), contexts=contexts)
    record = result["dependencies"][0]
    assert record["strength"] == "STRONG"
    assert record["origin"] == "heuristic_mock"
    assert record["evidence"][0]["symbol"] == "clamp_value"
    assert record["target_version_status"] == "absent"


def test_evidence_round_asks_the_provider_and_records_it():
    hunks = fixture_hunks("insufficient_evidence")
    calls: list[dict] = []

    class RecordingProvider:
        def additional_evidence(self, request):
            calls.append(request)
            return {"status": "ok", "matches": [], "request": request}

    analyzer = DependencyAnalyzer(
        HeuristicMockClient(), context_provider=RecordingProvider(), config={"max_evidence_rounds": 1}
    )
    result = analyzer.analyze(hunks, extract(hunks, None), patch_id="p")
    assert calls and calls[0]["symbol"] == "feature_limit"
    assert result["unresolved_questions"]
    assert any(record.get("extra_evidence_rounds") == 1 for record in result["dependencies"])


def test_extra_evidence_round_can_be_disabled():
    hunks = fixture_hunks("insufficient_evidence")
    calls: list[dict] = []

    class RecordingProvider:
        def additional_evidence(self, request):  # pragma: no cover - must not be called
            calls.append(request)
            return {"status": "ok", "matches": []}

    analyzer = DependencyAnalyzer(
        HeuristicMockClient(), context_provider=RecordingProvider(), config={"max_evidence_rounds": 0}
    )
    analyzer.analyze(hunks, extract(hunks, None), patch_id="p")
    assert calls == []


def test_existing_analysis_is_reused_and_validated():
    hunks = fixture_hunks("simple_dependency")
    seeded = [
        {
            "dependency_id": "D7",
            "source_hunk": "H1",
            "target_hunk": "H2",
            "category": "DEFINITION_USE",
            "strength": "STRONG",
            "evidence": [{"file_path": "src/example.c", "symbol": "clamp_value", "line": 8}],
            "separation_risk": "risk",
            "target_version_status": "absent",
            "reasoning_summary": "from a previous run",
            "needs_more_evidence": False,
        },
        {"source_hunk": "H1", "target_hunk": "H99", "category": "DEFINITION_USE", "strength": "WEAK"},
    ]
    analyzer = DependencyAnalyzer(HeuristicMockClient(), config={"max_evidence_rounds": 0})
    result = analyzer.analyze(
        hunks, [], patch_id="p", existing_analysis=seeded
    )
    assert len(result["dependencies"]) == 1
    assert result["dependencies"][0]["origin"] == "existing_analysis"
    assert result["rejected_records"][0]["source"] == "existing_analysis"


def test_pair_budget_is_reported_when_candidates_exceed_it():
    hunks = [
        {
            "hunk_id": f"H{index}",
            "file_path": "src/x.c",
            "added_lines": [f"    shared_symbol = {index};"],
            "context_lines": [],
            "removed_lines": [],
            "line_map": [
                {
                    "kind": "add",
                    "new_line": index,
                    "old_line": None,
                    "text": f"    shared_symbol = {index};",
                }
            ],
        }
        for index in range(1, 6)
    ]
    analyzer = DependencyAnalyzer(
        MockLLMClient(responses=[]), config={"max_candidate_pairs": 2, "max_evidence_rounds": 0}
    )
    result = analyzer.analyze(hunks, extract(hunks), patch_id="p")
    assert result["metadata"]["skipped_pair_count"] == 8
    assert any("budget" in question["reason"] for question in result["unresolved_questions"])


def test_metadata_records_model_usage_and_timing():
    hunks = fixture_hunks("simple_dependency")
    result, _ = _analyze(hunks, responses=[json.dumps(VALID_RESPONSE)])
    assert result["metadata"]["model"] == "mock-llm"
    assert result["metadata"]["usage"]["calls"] == 1
    assert result["metadata"]["duration_s"] >= 0
    assert result["metadata"]["analysis_version"] == "1.0"


def test_empty_hunk_list_is_rejected():
    from patch_hunk_agent import AnalysisError

    analyzer = DependencyAnalyzer(MockLLMClient(responses=[]))
    with pytest.raises(AnalysisError):
        analyzer.analyze([], [])


def check_analysis(result: dict) -> list[str]:
    from patch_hunk_agent.schemas import check_analysis_result

    return check_analysis_result(result)
