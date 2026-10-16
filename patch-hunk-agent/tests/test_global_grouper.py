"""Model-proposed work packages, validated and repaired by the graph machinery."""

from __future__ import annotations

from helpers import as_json, dependency, fixture_path, fixture_hunks
from patch_hunk_agent.global_grouper import GlobalGrouper, GlobalGroupingConfig
from patch_hunk_agent.grouping_engine import GroupingEngine
from patch_hunk_agent.llm import MockLLMClient


def _grouper(responses, **config) -> GlobalGrouper:
    # Unit tests drive the mechanics with scripted responses, so keep a single
    # partition candidate here; the best-of-N selector has its own test below.
    base = {
        "max_group_size": 4,
        "max_split_rounds": 1,
        "max_partition_retries": 1,
        "partition_candidates": 1,
    }
    base.update(config)
    engine = GroupingEngine()
    return GlobalGrouper(
        MockLLMClient(responses=responses),
        config=GlobalGroupingConfig.from_dict(base),
        engine=engine.group,
    )


def _partition(*groups, name="pkg", why="w", verify="v") -> dict:
    return {
        "groups": [
            {
                "name": f"{name}{index}",
                "hunk_ids": list(members),
                "why": why,
                "independent_verification": verify,
                "context_hunks": [],
            }
            for index, members in enumerate(groups, start=1)
        ]
    }


def test_model_partition_is_taken_as_is():
    hunks = fixture_hunks("simple_dependency")
    grouper = _grouper([{"groups": [
        {"name": "定义", "hunk_ids": ["H1"], "why": "decl", "independent_verification": "compile"},
        {"name": "使用", "hunk_ids": ["H2"], "why": "call", "independent_verification": "test"},
    ]}])
    result = grouper.group(["H1", "H2"], [], hunks=hunks)
    assert [group["hunk_ids"] for group in result["groups"]] == [["H1"], ["H2"]]
    assert result["groups"][0]["name"] == "定义"
    assert result["metadata"]["grouping_mode"] == "global"
    assert result["metadata"]["constraint_edge_count"] == 0


def test_strong_constraint_crossing_groups_merges_only_that_pair():
    hunks = fixture_hunks("simple_dependency")
    records = [
        dependency("H1", "H2", dependency_id="D1"),
        dependency("H2", "H3", dependency_id="D2"),
    ]
    hunks = hunks + [
        {
            "hunk_id": "H3",
            "file_path": "src/other.c",
            "added_lines": ["+    other();"],
            "removed_lines": [],
            "context_lines": [],
            "line_map": [{"kind": "add", "new_line": 3, "old_line": None, "text": "    other();"}],
        }
    ]
    grouper = _grouper([{"groups": [
        {"name": "a", "hunk_ids": ["H1"]},
        {"name": "b", "hunk_ids": ["H2"]},
        {"name": "c", "hunk_ids": ["H3"]},
    ]}])
    result = grouper.group(["H1", "H2", "H3"], records, hunks=hunks)
    # D1 merged H1+H2; D2 now links that package to H3, so no constraint crosses groups.
    assert [group["hunk_ids"] for group in result["groups"]] == [["H1", "H2", "H3"]]
    assert result["cross_group_dependencies"] == []


def test_constraint_repair_is_not_transitive_beyond_direct_edges():
    hunks = fixture_hunks("simple_dependency")
    records = [dependency("H1", "H2", dependency_id="D1", strength="WEAK")]
    grouper = _grouper([_partition(["H1"], ["H2"])])
    result = grouper.group(["H1", "H2"], records, hunks=hunks)
    # A WEAK edge is not a constraint: the model's split survives.
    assert [group["hunk_ids"] for group in result["groups"]] == [["H1"], ["H2"]]


def test_uncovered_hunks_become_their_own_group():
    hunks = fixture_hunks("simple_dependency")
    grouper = _grouper([_partition(["H1"])])
    result = grouper.group(["H1", "H2"], [], hunks=hunks)
    assert [group["hunk_ids"] for group in result["groups"]] == [["H1"], ["H2"]]
    assert any("H2 was not assigned" in problem for problem in result["metadata"]["partition_problems"])


def test_unknown_and_duplicate_hunk_ids_are_reported_not_trusted():
    hunks = fixture_hunks("simple_dependency")
    grouper = _grouper([{"groups": [
        {"name": "a", "hunk_ids": ["H1", "H1", "H99"]},
        {"name": "b", "hunk_ids": ["H2"]},
    ]}])
    result = grouper.group(["H1", "H2"], [], hunks=hunks)
    problems = " ".join(result["metadata"]["partition_problems"])
    assert "unknown hunk id 'H99'" in problems
    assert "H1 appears in more than one group" in problems
    assert [group["hunk_ids"] for group in result["groups"]] == [["H1"], ["H2"]]


def test_oversized_group_is_split_by_the_model():
    hunks = fixture_hunks("simple_dependency")
    big = [dict(hunk) for hunk in hunks]
    for index in range(3, 7):
        big.append(
            {
                "hunk_id": f"H{index}",
                "file_path": f"src/f{index}.c",
                "added_lines": [f"+    call{index}();"],
                "removed_lines": [],
                "context_lines": [],
                "line_map": [
                    {"kind": "add", "new_line": 1, "old_line": None, "text": f"    call{index}();"}
                ],
            }
        )
    ids = [hunk["hunk_id"] for hunk in big]
    grouper = _grouper(
        [
            {"groups": [{"name": "everything", "hunk_ids": ids}]},
            {"groups": [
                {"name": "half1", "hunk_ids": ids[:3]},
                {"name": "half2", "hunk_ids": ids[3:]},
            ]},
        ],
        max_group_size=3,
    )
    result = grouper.group(ids, [], hunks=big)
    assert [len(group["hunk_ids"]) for group in result["groups"]] == [3, 3]
    assert result["metadata"]["splits"]
    assert all(group.get("split_from") for group in result["groups"])


def test_unusable_model_output_falls_back_to_components():
    hunks = fixture_hunks("simple_dependency")
    records = [dependency("H1", "H2", dependency_id="D1")]
    grouper = _grouper(["not json at all", "still not json"])
    result = grouper.group(["H1", "H2"], records, hunks=hunks)
    assert result["metadata"]["grouping_mode"] == "components_fallback"
    assert result["metadata"]["fallback_reason"]
    assert [group["hunk_ids"] for group in result["groups"]] == [["H1", "H2"]]
    assert any("unusable" in question["question"] for question in result["unresolved_questions"])


def test_partition_is_deterministic_for_the_same_input():
    hunks = fixture_hunks("simple_dependency")
    response = _partition(["H1"], ["H2"])
    outputs = set()
    for _ in range(3):
        result = _grouper([response]).group(["H1", "H2"], [], hunks=hunks)
        outputs.add(as_json([group["hunk_ids"] for group in result["groups"]]))
    assert len(outputs) == 1
    assert outputs.pop() == as_json([["H1"], ["H2"]])


def test_context_hunks_are_reported_without_merging():
    hunks = fixture_hunks("simple_dependency")
    grouper = _grouper([{"groups": [
        {"name": "a", "hunk_ids": ["H1"], "why": "w", "context_hunks": ["H2", "H99"]},
        {"name": "b", "hunk_ids": ["H2"], "why": "w"},
    ]}])
    result = grouper.group(["H1", "H2"], [], hunks=hunks)
    assert result["groups"][0]["context_hunks"] == ["H2"]
    assert [group["hunk_ids"] for group in result["groups"]] == [["H1"], ["H2"]]


def test_execution_plan_orders_and_coordinates_packages():
    hunks = fixture_hunks("signature_dependency")  # H1 calls process(), H2 changes its signature
    hunks.append(
        {
            "hunk_id": "H3",
            "file_path": "src/caller.c",
            "added_lines": ["+    process(value, 0);"],
            "removed_lines": [],
            "context_lines": [],
            "line_map": [
                {"kind": "add", "new_line": 9, "old_line": None, "text": "    process(value, 0);"}
            ],
        }
    )
    records = [dependency("H2", "H1", dependency_id="D1", category="INTERFACE_SIGNATURE")]
    grouper = _grouper([{"groups": [
        {"name": "caller side", "hunk_ids": ["H1", "H3"], "why": "call sites"},
        {"name": "declaration side", "hunk_ids": ["H2"], "why": "signature"},
    ]}], max_group_size=2)
    result = grouper.group(["H1", "H2", "H3"], records, hunks=hunks)
    plan = result["metadata"]["execution_plan"]
    # The merge is refused (3 hunks > budget 2), so the declaring package keeps its
    # own place and must be ported first.
    assert [group["hunk_ids"] for group in result["groups"]] == [["H1", "H3"], ["H2"]]
    assert plan["order"] == ["G2", "G1"]
    assert plan["directives"]["G1"][0] == "PORT_ORDER 2/2"
    assert plan["directives"]["G2"][0] == "PORT_ORDER 1/2"
    assert plan["anchor_evidence"] == "source_side_proxy"
    assert plan["requires_target_confirmation"] is True


def test_shared_target_region_and_joint_verification_are_reported():
    hunks = fixture_hunks("simple_dependency")  # both hunks live in src/example.c
    grouper = _grouper([_partition(["H1"], ["H2"])])
    result = grouper.group(["H1", "H2"], [], hunks=hunks)
    plan = result["metadata"]["execution_plan"]
    assert plan["shared_target_regions"], plan
    region = plan["shared_target_regions"][0]
    assert region["file"] == "src/example.c"
    assert region["level"] in ("method", "file")
    assert region["requires_target_confirmation"] is True
    assert plan["joint_verification"][0]["kind"] == "shared_target_region"
    assert any(
        directive.startswith("SHARED_TARGET_") for directive in plan["directives"]["G1"]
    )


def test_deferred_constraint_becomes_an_executable_directive():
    hunks = fixture_hunks("simple_dependency")
    hunks.append(
        {
            "hunk_id": "H3",
            "file_path": "src/other.c",
            "added_lines": ["+    other();"],
            "removed_lines": [],
            "context_lines": [],
            "line_map": [{"kind": "add", "new_line": 1, "old_line": None, "text": "    other();"}],
        }
    )
    # Budget 2 + a strong edge from the 2-hunk package to H3: the merge is refused.
    records = [dependency("H2", "H3", dependency_id="D1", category="INTERFACE_SIGNATURE")]
    grouper = _grouper([_partition(["H1", "H2"], ["H3"])], max_group_size=2)
    result = grouper.group(["H1", "H2", "H3"], records, hunks=hunks)
    deferred = result["metadata"]["deferred_constraints"]
    assert deferred and deferred[0]["dependency_id"] == "D1"
    assert any(
        directive.startswith("DEFERRED_CONSTRAINT D1")
        for directives in result["metadata"]["execution_plan"]["directives"].values()
        for directive in directives
    )
    assert result["metadata"]["execution_plan"]["joint_verification"]
    satisfaction = result["metadata"]["constraint_satisfaction"]
    assert satisfaction["active_constraints"] == 1
    assert satisfaction["deferred_reported"] == 1
    assert satisfaction["satisfaction_rate"] == 0.0


def test_partition_request_carries_no_reference_grouping():
    """Generation-time input must be the patch alone.

    A reviewed grouping may be used to *score* a run afterwards, but it must never
    reach the model: the request may only contain the patch's own hunks, the
    evidence derived from them and the target-side information.
    """
    import json

    captured: list[dict] = []

    def responder(payload):
        captured.append(payload)
        return {"groups": [{"name": "all", "hunk_ids": [h["hunk_id"] for h in hunks]}]}

    hunks = fixture_hunks("simple_dependency")
    grouper = GlobalGrouper(
        MockLLMClient(responder=responder),
        config=GlobalGroupingConfig.from_dict({"partition_candidates": 1}),
        engine=GroupingEngine().group,
    )
    grouper.group(["H1", "H2"], [dependency("H1", "H2", dependency_id="D1")], hunks=hunks)

    assert captured, "the partition request was never built"
    payload = captured[0]
    assert set(payload) == {
        "task",
        "instructions",
        "output_schema",
        "hunks",
        "strong_dependency_evidence",
        "conditional_dependency_evidence",
        "notes",
    }
    blob = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("reference_group", "reference_dependenc", "human_reference", "M000", "hunk_groups.json"):
        assert forbidden not in blob, forbidden
    # The request may only mention hunks that exist in the patch under analysis.
    assert {item["hunk_id"] for item in payload["hunks"]} == {"H1", "H2"}


def test_pipeline_uses_global_mode_by_default(project_root, source_tree, target_tree):
    """End-to-end: the model's partition wins, constraints are only a guard."""
    from conftest import SOURCE_REVISION, TARGET_REVISION, build_test_pipeline
    from patch_hunk_agent.pipeline import PipelineInputs

    def responder(payload):
        task = payload.get("task")
        if task == "global_work_package_partition":
            return {"groups": [
                {"name": "helper + its caller", "hunk_ids": ["H1", "H2"], "why": "definition and use"},
            ]}
        return {"dependencies": [
            {
                "source_hunk": "H1",
                "target_hunk": "H2",
                "category": "DEFINITION_USE",
                "strength": "STRONG",
                "evidence": [{"file_path": "src/example.c", "symbol": "clamp_value", "line": 8}],
                "separation_risk": "call would be unresolved",
                "target_version_status": "absent",
                "reasoning_summary": "H1 defines clamp_value used by H2",
                "needs_more_evidence": False,
            }
        ]}

    pipeline = build_test_pipeline(
        source_dir=source_tree, target_dir=target_tree, llm=MockLLMClient(responder=responder)
    )
    pipeline.config.grouping.mode = "global"
    result = pipeline.run(
        PipelineInputs(
            patch_path=str(fixture_path("simple_dependency")),
            patch_id="global_mode",
            source_revision=SOURCE_REVISION,
            target_revision=TARGET_REVISION,
        )
    )
    document = result["grouping_document"]
    assert document["status"] == "complete"
    assert [group["hunk_ids"] for group in document["groups"]] == [["H1", "H2"]]
    assert result["grouping"]["metadata"]["grouping_mode"] == "global"
    assert document["validation"]["strong_dependencies_within_groups"] is True
