"""Adapter output must be traceable and must not claim verified results."""

from __future__ import annotations

import json

from helpers import dependency
from patch_hunk_agent.portgpt_adapter import PortGPTAdapter


def _grouping() -> dict:
    return {
        "patch_id": "p1",
        "status": "partial",
        "groups": [
            {
                "group_id": "G1",
                "hunk_ids": ["H1", "H2"],
                "group_reason": "linked by D1",
                "dependency_ids": ["D1"],
            },
            {"group_id": "G2", "hunk_ids": ["H3"], "group_reason": "isolated", "dependency_ids": []},
        ],
        "unresolved_questions": [
            {"question": "is the target ready?", "hunk_ids": ["H1"], "reason": "unknown"}
        ],
        "validation": {
            "all_hunks_assigned_once": True,
            "strong_dependencies_within_groups": True,
            "unresolved_dependencies_reported": True,
        },
    }


def _hunks() -> list[dict]:
    return [
        {
            "hunk_id": hunk_id,
            "file_path": f"src/f{hunk_id}.c",
            "target_file_path": f"src/f{hunk_id}.c",
            "header": "@@ -1,1 +1,1 @@",
            "old_start": 1,
            "old_count": 1,
            "new_start": 1,
            "new_count": 1,
            "section": "",
            "added_lines": ["+new"],
            "removed_lines": [],
            "context_lines": [],
            "diff_text": "@@ -1,1 +1,1 @@\n-old\n+new",
        }
        for hunk_id in ("H1", "H2", "H3")
    ]


def test_tasks_carry_hunks_dependencies_and_caveats():
    payload = PortGPTAdapter().build_tasks(
        _grouping(),
        _hunks(),
        analysis={"dependencies": [dependency("H1", "H2")]},
        source_revision="SRC",
        target_revision="TGT",
    )
    assert payload["schema"] == "portgpt.group_task/v1"
    assert payload["grouping_status"] == "partial"
    assert any("not verified backport results" in caveat for caveat in payload["caveats"])
    first, second = payload["groups"]
    assert first["must_port_together"] is True
    assert second["must_port_together"] is False
    assert first["dependencies"][0]["dependency_id"] == "D1"
    assert first["attention_points"][0].startswith("D1 STRONG DEFINITION_USE")
    assert first["unresolved_questions"]
    assert second["unresolved_questions"] == []
    assert first["hunks"][0]["diff_text"].startswith("@@")


def test_missing_hunks_are_reported_not_silently_skipped():
    payload = PortGPTAdapter().build_tasks(_grouping(), _hunks()[:2])
    assert payload["groups"][1]["missing_hunks"] == ["H3"]


def test_context_is_included_and_trimmed():
    contexts = [
        {
            "hunk_id": "H1",
            "retrieval_status": "complete",
            "source_context": {"available": True, "code": "x" * 5000},
            "target_context": {"available": True, "code": "y"},
            "target_symbol_status": {"clamp_value": "absent"},
        }
    ]
    payload = PortGPTAdapter().build_tasks(_grouping(), _hunks(), contexts=contexts)
    hunk = payload["groups"][0]["hunks"][0]
    assert hunk["retrieval_status"] == "complete"
    assert len(hunk["source_context"]["code"]) < 5000
    assert hunk["target_symbol_status"] == {"clamp_value": "absent"}


def test_context_can_be_omitted():
    payload = PortGPTAdapter({"include_context": False}).build_tasks(_grouping(), _hunks())
    assert "source_context" not in payload["groups"][0]["hunks"][0]


def test_traceability_record_keeps_group_decisions(tmp_path):
    adapter = PortGPTAdapter()
    record = adapter.build_traceability_record(
        patch_id="p1",
        group_id="G1",
        hunk_ids=["H1", "H2"],
        portgpt_input={"group_id": "G1"},
        generated_diff="--- a/x\n+++ b/x\n",
        build_result={"ok": True},
        test_result={"passed": 3, "failed": 0},
        grouping_document=_grouping(),
    )
    assert record["schema"] == "portgpt.traceability/v1"
    assert record["generated_diff_available"] is True
    assert record["grouping_validation"]["all_hunks_assigned_once"] is True
    path = adapter.append_traceability_record(tmp_path / "trace.jsonl", record)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["group_id"] == "G1"


def test_write_tasks_creates_parent_directories(tmp_path):
    adapter = PortGPTAdapter()
    payload = adapter.build_tasks(_grouping(), _hunks())
    path = adapter.write_tasks(payload, tmp_path / "nested" / "tasks.json")
    assert json.loads(path.read_text(encoding="utf-8"))["group_count"] == 2
