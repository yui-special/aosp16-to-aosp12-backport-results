"""Retrieval must be read-only, bounded and honest about missing data."""

from __future__ import annotations

from pathlib import Path


from helpers import fixture_hunks
from patch_hunk_agent.context_provider import (
    CodeContextProvider,
    GitRepoAccess,
    LocalTreeAccess,
    NullAccess,
)
from patch_hunk_agent.schemas import check_context_record

SOURCE_REVISION = "SRC"
TARGET_REVISION = "TGT"


def _provider(source_tree: Path, target_tree: Path) -> CodeContextProvider:
    return CodeContextProvider(
        source_access=LocalTreeAccess({SOURCE_REVISION: str(source_tree)}),
        target_access=LocalTreeAccess({TARGET_REVISION: str(target_tree)}),
    )


def test_local_tree_access_reads_and_reports_missing(source_tree, target_tree):
    access = LocalTreeAccess({SOURCE_REVISION: str(source_tree)})
    assert access.available() is True
    assert "existing_helper" in (access.read_file(SOURCE_REVISION, "src/example.c") or "")
    assert access.read_file(SOURCE_REVISION, "src/nope.c") is None
    assert access.read_file("UNKNOWN", "src/example.c") is None


def test_local_tree_access_refuses_path_traversal(source_tree):
    access = LocalTreeAccess({SOURCE_REVISION: str(source_tree)})
    assert access.read_file(SOURCE_REVISION, "../../../etc/passwd") is None
    assert access.read_file(SOURCE_REVISION, "/etc/passwd") is None
    assert access._resolve(Path(source_tree), "../target/src/guard.c") is None


def test_context_record_shape_and_status(source_tree, target_tree):
    provider = _provider(source_tree, target_tree)
    hunks = fixture_hunks("simple_dependency")
    record = provider.get_context(hunks[0], SOURCE_REVISION, TARGET_REVISION)
    assert check_context_record(record) == []
    assert record["retrieval_status"] == "complete"
    assert record["source_context"]["origin"] == "repository"
    assert record["target_context"]["available"] is True
    assert record["target_symbol_status"]["clamp_value"] == "absent"
    assert any(query["status"] == "ok" for query in record["queries"])


def test_source_block_contains_the_enclosing_function(source_tree, target_tree):
    provider = _provider(source_tree, target_tree)
    hunks = fixture_hunks("simple_dependency")
    record = provider.get_context(hunks[0], SOURCE_REVISION, TARGET_REVISION)
    assert "existing_helper" in record["source_context"]["code"]
    assert record["source_context"]["scope_function"] == "existing_helper"
    assert record["source_context"]["line_start"] <= 6


def test_signature_symbol_is_absent_even_though_a_definition_exists(source_tree, target_tree):
    provider = _provider(source_tree, target_tree)
    hunks = fixture_hunks("signature_dependency")
    record = provider.get_context(hunks[1], SOURCE_REVISION, TARGET_REVISION)
    assert record["target_symbol_status"]["process"] == "absent"
    assert "int process(int x)" in record["target_definition_text"]["process"]
    evidence = [
        item
        for item in record["symbol_evidence"]
        if item["symbol"] == "process" and item["side"] == "target"
    ]
    assert evidence and any(item["kind"] == "definition" for item in evidence)
    assert all(item["source"] == "repository" for item in evidence)


def test_target_already_defines_the_symbol(source_tree, target_tree):
    provider = _provider(source_tree, target_tree)
    hunks = fixture_hunks("target_already_satisfies")
    record = provider.get_context(hunks[0], SOURCE_REVISION, TARGET_REVISION)
    assert record["target_symbol_status"]["guard_limit"] == "present"


def test_no_access_means_unavailable_and_unknown():
    provider = CodeContextProvider(source_access=NullAccess(), target_access=NullAccess())
    hunks = fixture_hunks("insufficient_evidence")
    record = provider.get_context(hunks[0], None, None)
    assert record["retrieval_status"] == "unavailable"
    assert set(record["target_symbol_status"].values()) == {"unknown"}
    assert record["source_context"]["available"] is True
    assert record["source_context"]["origin"] == "diff_hunk"
    assert any("could not be determined" in error for error in record["errors"])


def test_symbol_query_budget_is_enforced(source_tree, target_tree):
    provider = CodeContextProvider(
        source_access=LocalTreeAccess({SOURCE_REVISION: str(source_tree)}),
        target_access=LocalTreeAccess({TARGET_REVISION: str(target_tree)}),
        config={"max_symbol_queries": 2, "allow_search": False},
    )
    hunks = fixture_hunks("simple_dependency")
    record = provider.get_context(hunks[1], SOURCE_REVISION, TARGET_REVISION)
    assert len(record["target_symbol_status"]) <= 2
    assert any(query["status"] == "skipped" for query in record["queries"])


def test_disabled_context_short_circuits(source_tree, target_tree):
    provider = CodeContextProvider(
        source_access=LocalTreeAccess({SOURCE_REVISION: str(source_tree)}),
        target_access=LocalTreeAccess({TARGET_REVISION: str(target_tree)}),
        config={"enabled": False},
    )
    record = provider.get_context(fixture_hunks("simple_dependency")[0], SOURCE_REVISION, TARGET_REVISION)
    assert record["retrieval_status"] == "unavailable"
    assert record["symbol_evidence"] == []


def test_additional_evidence_requires_a_target(source_tree, target_tree):
    provider = _provider(source_tree, target_tree)
    rejected = provider.additional_evidence({})
    assert rejected["status"] == "rejected"
    found = provider.additional_evidence(
        {"symbol": "clamp_value", "side": "target", "revision": TARGET_REVISION}
    )
    assert found["status"] == "ok"
    assert found["matches"] == []
    present = provider.additional_evidence(
        {"symbol": "existing_helper", "side": "target", "revision": TARGET_REVISION}
    )
    assert present["matches"] and present["matches"][0]["kind"] == "definition"
    file_result = provider.additional_evidence(
        {"file_path": "src/example.c", "side": "source", "revision": SOURCE_REVISION}
    )
    assert file_result["status"] == "ok" and "existing_helper" in file_result["code"]


def test_git_access_without_a_repository_is_unavailable(tmp_path):
    access = GitRepoAccess(tmp_path)
    assert access.available() is False
    assert access.read_file("HEAD", "src/example.c") is None
    hits, error = access.symbol_hits("HEAD", "anything")
    assert hits == [] and error


def test_cache_is_used_for_repeated_symbol_queries(source_tree, target_tree):
    source = LocalTreeAccess({SOURCE_REVISION: str(source_tree)})
    provider = CodeContextProvider(source_access=source, target_access=source)
    hunks = fixture_hunks("simple_dependency")
    provider.get_contexts(hunks, SOURCE_REVISION, SOURCE_REVISION)
    first_cache_size = len(source._cache)
    assert first_cache_size > 0
    provider.get_contexts(hunks, SOURCE_REVISION, SOURCE_REVISION)
    assert len(source._cache) == first_cache_size
