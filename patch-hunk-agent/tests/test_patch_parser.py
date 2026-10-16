"""Parser behaviour, including the cases that silently break naive parsers."""

from __future__ import annotations

import pytest

from helpers import fixture_path, parse_fixture
from patch_hunk_agent import PatchParseError
from patch_hunk_agent.patch_parser import PatchParser
from patch_hunk_agent.schemas import check_hunk

FIXTURES = [
    "simple_dependency",
    "signature_dependency",
    "independent_hunks",
    "target_already_satisfies",
    "insufficient_evidence",
]


@pytest.mark.parametrize("name", FIXTURES)
def test_every_fixture_parses_with_stable_ids(name):
    document = parse_fixture(name)
    assert document.hunk_ids() == [f"H{i}" for i in range(1, len(document.hunks) + 1)]
    assert document.warnings == []
    for hunk in document.hunks:
        assert check_hunk(hunk.to_dict()) == []
        assert hunk.parse_warnings == []
        assert hunk.diff_text.startswith("@@")


def test_parsing_twice_is_identical():
    first = parse_fixture("simple_dependency")
    second = parse_fixture("simple_dependency")
    assert first.hunk_dicts() == second.hunk_dicts()


def test_hunk_body_is_split_correctly():
    document = parse_fixture("simple_dependency")
    first, second = document.hunks
    assert first.added_lines[0] == "static int clamp_value(int value, int limit)"
    assert first.removed_lines == []
    assert "}" in first.context_lines
    assert second.added_lines == ["    result = clamp_value(result, 100);"]
    assert first.new_start == 6 and first.new_count == 11
    assert second.old_start == 10 and second.new_start == 18


def test_line_map_points_at_real_lines():
    document = parse_fixture("simple_dependency")
    first = document.hunks[0]
    added = [entry for entry in first.line_map if entry["kind"] == "add"]
    assert added[0]["new_line"] == 8
    assert added[0]["old_line"] is None
    context = [entry for entry in first.line_map if entry["kind"] == "context"]
    assert context[0]["old_line"] == 6 and context[0]["new_line"] == 6


def test_multiple_files_and_order():
    document = parse_fixture("signature_dependency")
    assert [change.file_path for change in document.files] == [
        "src/caller.c",
        "src/process.c",
    ]
    assert document.hunks[0].file_path == "src/caller.c"
    assert document.hunks[1].file_path == "src/process.c"
    assert document.files_touched() == ["src/caller.c", "src/process.c"]


def test_removed_line_starting_with_dashes_is_not_a_file_header():
    """A removed line ``-- legacy_call();`` renders as ``--- legacy_call();``.

    That is textually identical to a file header, so the hunk must be delimited by
    the declared line counts rather than by lookahead.
    """
    diff = (
        "diff --git a/x.c b/x.c\n"
        "--- a/x.c\n"
        "+++ b/x.c\n"
        "@@ -1,4 +1,4 @@\n"
        " int main(void)\n"
        " {\n"
        "--- legacy_call();\n"
        "+++ legacy_call(0);\n"
        " }\n"
    )
    document = PatchParser().parse_document(diff)
    assert len(document.hunks) == 1
    hunk = document.hunks[0]
    assert hunk.removed_lines == ["-- legacy_call();"]
    assert hunk.added_lines == ["++ legacy_call(0);"]
    assert hunk.parse_warnings == []
    assert document.files_touched() == ["x.c"]


def test_missing_newline_marker_is_preserved():
    diff = (
        "--- a/x.c\n"
        "+++ b/x.c\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "\\ No newline at end of file\n"
        "+new\n"
        "\\ No newline at end of file\n"
    )
    document = PatchParser().parse_document(diff)
    hunk = document.hunks[0]
    assert hunk.removed_lines == ["old"] and hunk.added_lines == ["new"]
    assert hunk.parse_warnings == []
    assert hunk.diff_text.count("\\ No newline at end of file") == 2


def test_crlf_input_is_tolerated():
    diff = (
        "--- a/x.c\r\n"
        "+++ b/x.c\r\n"
        "@@ -1,2 +1,2 @@\r\n"
        " keep\r\n"
        "-old\r\n"
        "+new\r\n"
    )
    document = PatchParser().parse_document(diff)
    hunk = document.hunks[0]
    assert hunk.removed_lines == ["old"] and hunk.added_lines == ["new"]
    assert hunk.parse_warnings == []


def test_new_file_without_counts_header():
    diff = (
        "diff --git a/new.c b/new.c\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new.c\n"
        "@@ -0,0 +1,3 @@\n"
        "+int add(int a, int b)\n"
        "+{\n"
        "+    return a + b;\n"
    )
    document = PatchParser().parse_document(diff)
    hunk = document.hunks[0]
    assert document.files[0].change_type == "add"
    assert hunk.file_path == "new.c" and hunk.target_file_path == "new.c"
    assert len(hunk.added_lines) == 3
    assert hunk.parse_warnings == []


def test_single_line_counts_default_to_one():
    document = PatchParser().parse_document("--- a/x.c\n+++ b/x.c\n@@ -5 +5 @@\n-old\n+new\n")
    assert document.hunks[0].old_count == 1 and document.hunks[0].new_count == 1


def test_leading_commit_message_is_ignored():
    text = (
        "commit 1234567890abcdef\n"
        "Subject: fix a thing\n"
        "\n"
        + fixture_path("simple_dependency").read_text(encoding="utf-8")
    )
    document = PatchParser().parse_document(text)
    assert len(document.hunks) == 2
    assert any("non-diff line" in warning for warning in document.warnings)


def test_empty_patch_is_rejected():
    with pytest.raises(PatchParseError):
        PatchParser().parse_document("   \n\n")


def test_text_without_hunks_is_rejected():
    with pytest.raises(PatchParseError):
        PatchParser().parse_document("just some prose\nwith no diff at all\n")


def test_hunk_header_before_file_header_is_rejected():
    with pytest.raises(PatchParseError) as excinfo:
        PatchParser().parse_document("@@ -1 +1 @@\n-a\n+b\n")
    assert "before any file header" in str(excinfo.value)


def test_malformed_hunk_header_is_rejected():
    with pytest.raises(PatchParseError) as excinfo:
        PatchParser().parse_document("--- a/x.c\n+++ b/x.c\n@@ -1,2 +1,2\n-a\n+b\n")
    assert "malformed hunk header" in str(excinfo.value)


def test_count_mismatch_is_reported_and_hunk_is_kept():
    diff = (
        "--- a/x.c\n"
        "+++ b/x.c\n"
        "@@ -1,9 +1,9 @@\n"
        "-only\n"
        "+replacement\n"
        "@@ -20,2 +20,2 @@\n"
        "-second\n"
        "+second-new\n"
    )
    document = PatchParser().parse_document(diff)
    assert len(document.hunks) == 2
    assert document.hunks[0].parse_warnings
    assert "do not match" in document.hunks[0].parse_warnings[0]
    parser = PatchParser(require_complete_counts=True)
    with pytest.raises(PatchParseError) as excinfo:
        parser.parse_document(diff)
    assert "H1" in str(excinfo.value)


def test_max_hunks_guard_refuses_silent_truncation():
    text = "\n".join(
        "--- a/x.c\n+++ b/x.c\n@@ -1 +1 @@\n-a\n+b" for _ in range(3)
    )
    with pytest.raises(PatchParseError):
        PatchParser(max_hunks=2).parse_document(text)


def test_hunk_id_ordinals_are_not_reused_after_bad_hunk():
    diff = (
        "--- a/x.c\n"
        "+++ b/x.c\n"
        "@@ -1,1 +1,1 @@\n"
        "-a\n"
        "+b\n"
        "--- a/y.c\n"
        "+++ b/y.c\n"
        "@@ -3,1 +3,1 @@\n"
        "-c\n"
        "+d\n"
    )
    document = PatchParser().parse_document(diff)
    assert document.hunk_ids() == ["H1", "H2"]
    assert document.hunks[1].file_path == "y.c"
