"""Machine evidence extraction: candidates only, never final decisions."""

from __future__ import annotations

from helpers import fixture_contexts, fixture_hunks
from patch_hunk_agent.evidence_extractor import (
    classify_definition,
    enrich_hunks,
    extract,
    extract_symbols,
    function_scope,
    parameter_text,
    signature_changes,
    strip_strings_and_comments,
    touched_functions,
)


def _names(hunk: dict, role: str) -> set[str]:
    return {entry["name"] for entry in hunk["symbols"] if entry["role"] == role}


def test_function_definition_is_extracted():
    hunks = fixture_hunks("simple_dependency")
    definitions = [entry for entry in hunks[0]["symbols"] if entry["role"] == "definition"]
    assert [entry["name"] for entry in definitions] == ["clamp_value"]
    assert definitions[0]["kind"] == "function"
    assert definitions[0]["line"] == 8


def test_call_reference_is_extracted_with_use_kind():
    hunks = fixture_hunks("simple_dependency")
    calls = [
        entry
        for entry in hunks[1]["symbols"]
        if entry["role"] == "reference" and entry["use_kind"] == "call"
    ]
    assert "clamp_value" in {entry["name"] for entry in calls}


def test_signature_change_is_detected():
    hunks = fixture_hunks("signature_dependency")
    changes = signature_changes(hunks[1])
    assert len(changes) == 1
    change = changes[0]
    assert change["name"] == "process"
    assert change["before_params"] == "(int x)"
    assert change["after_params"] == "(int x, int flags)"
    assert change["before_line"] == 20 and change["after_line"] == 20


def test_statement_lines_are_not_definitions():
    """Regression: ``return limit;`` used to be read as a variable definition."""
    assert classify_definition("    return limit;") is None
    assert classify_definition("    if (value > limit) {") is None
    assert classify_definition("    log_debug(\"x\", y);") is None
    assert classify_definition("static int clamp_value(int value)") == ("function", "clamp_value")
    assert classify_definition("#define MAX_RETRY 3") == ("macro", "MAX_RETRY")
    assert classify_definition("struct session_table {") == ("type", "session_table")


def test_strings_and_comments_are_not_scanned():
    code, in_block = strip_strings_and_comments('    helper("call_me()"); // other()')
    assert "call_me" not in code and "other" not in code
    code, in_block = strip_strings_and_comments("    /* start")
    assert in_block is True
    code, in_block = strip_strings_and_comments("    end */ helper();", in_block)
    assert in_block is False and "helper" in code


def test_include_lines_do_not_create_references():
    hunk = {
        "hunk_id": "H1",
        "file_path": "src/guard.c",
        "added_lines": ['#include "guard.h"'],
        "context_lines": [],
        "removed_lines": [],
        "line_map": [{"kind": "add", "new_line": 1, "old_line": None, "text": '#include "guard.h"'}],
    }
    names = {entry["name"] for entry in extract_symbols(hunk)}
    assert names == set()


def test_parameter_text_and_function_scope():
    lines = [
        "static int outer(int a)",
        "{",
        "    if (a) {",
        "        return inner(a);",
        "    }",
        "    return a;",
        "}",
    ]
    scope = function_scope(lines, 3)
    assert scope is not None
    assert scope["name"] == "outer"
    assert scope["start_line"] == 1 and scope["end_line"] == 7
    assert parameter_text("int process(int x, int flags)") == "(int x, int flags)"


def test_touched_functions_prefers_the_section_header():
    hunks = fixture_hunks("simple_dependency")
    assert touched_functions(hunks[0]) == ["existing_helper"]
    assert touched_functions({"section": "", "hunk_id": "H1"}) == []


def test_candidate_pair_for_definition_use():
    hunks = fixture_hunks("simple_dependency")
    candidates = extract(hunks)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["pair"] == ["H1", "H2"]
    assert candidate["signal_names"] == ["definition_use"]
    assert candidate["shared_symbols"] == ["clamp_value"]
    assert candidate["machine_hint"] == "CONDITIONAL_HINT"
    assert candidate["machine_evidence"][0]["symbol"] == "clamp_value"


def test_independent_hunks_produce_no_signal():
    hunks = fixture_hunks("independent_hunks")
    candidates = extract(hunks)
    assert len(candidates) == 1
    assert candidates[0]["signals"] == []
    assert candidates[0]["machine_hint"] == "NONE"
    assert candidates[0]["same_file"] is True


def test_target_version_state_downgrades_and_upgrades_hints():
    hunks = fixture_hunks("simple_dependency")
    candidates = extract(hunks, fixture_contexts(hunks, with_access=False))
    assert candidates[0]["machine_hint"] == "CONDITIONAL_HINT"

    hunks = fixture_hunks("simple_dependency")
    candidates = extract(hunks, fixture_contexts(hunks, with_access=True))
    assert candidates[0]["machine_hint"] == "STRONG_HINT"


def test_signature_change_outranks_definition_use():
    hunks = fixture_hunks("signature_dependency")
    contexts = fixture_contexts(hunks)
    candidates = extract(hunks, contexts)
    candidate = candidates[0]
    assert "signature_change" in candidate["signal_names"]
    assert candidate["machine_hint"] == "STRONG_HINT"
    signature_signal = [s for s in candidate["signals"] if s["signal"] == "signature_change"][0]
    assert signature_signal["target_symbol_status"] == "absent"
    assert signature_signal["before_params"] == "(int x)"


def test_shared_write_signal_for_synthetic_pair():
    hunks = [
        {
            "hunk_id": "H1",
            "file_path": "a.c",
            "added_lines": ["    shared_counter += 1;"],
            "context_lines": [],
            "removed_lines": [],
            "line_map": [
                {"kind": "add", "new_line": 5, "old_line": None, "text": "    shared_counter += 1;"}
            ],
        },
        {
            "hunk_id": "H2",
            "file_path": "b.c",
            "added_lines": ["    shared_counter = 0;"],
            "context_lines": [],
            "removed_lines": [],
            "line_map": [
                {"kind": "add", "new_line": 9, "old_line": None, "text": "    shared_counter = 0;"}
            ],
        },
    ]
    prepared = enrich_hunks(hunks)
    candidates = extract(prepared)
    assert candidates[0]["signal_names"] == ["shared_write"]
    assert candidates[0]["machine_hint"] == "CONDITIONAL_HINT"


def test_enrich_hunks_does_not_mutate_input():
    hunks = fixture_hunks("simple_dependency")
    without = [dict(hunk) for hunk in hunks]
    for hunk in without:
        hunk.pop("symbols")
    enriched = enrich_hunks(without)
    assert all("symbols" not in hunk for hunk in without)
    assert enriched[0]["symbols"]
