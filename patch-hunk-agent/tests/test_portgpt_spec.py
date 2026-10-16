"""Bridge to PortGPT's hunk-group spec: alignment, coverage, round trips."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from helpers import fixture_hunks, fixture_path, dependency
from conftest import ROOT
from patch_hunk_agent.portgpt_spec import (
    DEFAULT_SPEC_FILENAME,
    PortGPTBridgeError,
    build_group_spec,
    build_merged_hunks,
    check_alignment,
    check_spec_coverage,
    dump_merged_hunks,
    grouping_document_from_spec,
    merged_hunk_text,
    load_group_spec,
    load_portgpt_splitter,
    split_patch_with_portgpt,
    write_group_spec,
)

FAKE_SPLITTER = '''
import re


def split_patch_indexed(patch, flag_commit=True):
    lines = patch.splitlines()
    first = None
    for index, line in enumerate(lines):
        if line.startswith("--- a/") or line.startswith("--- /dev/null"):
            first = index
            break
    if first is None:
        return "", []
    message = "\\n".join(lines[: max(first - 2, 0)]) if flag_commit else ""
    hunks = []
    head = []
    current = None
    for line in lines[first:]:
        if line.startswith("--- a/") or line.startswith("--- /dev/null"):
            head = [line]
            continue
        if line.startswith("+++ "):
            head.append(line)
            continue
        if line.startswith("@@"):
            current = {"head": list(head), "body": [line]}
            hunks.append(current)
            continue
        if current is not None and line[:1] in (" ", "+", "-", "\\\\"):
            current["body"].append(line)
    out = []
    for index, hunk in enumerate(hunks):
        path = "(new file)"
        for line in hunk["head"]:
            if line.startswith("--- a/"):
                path = line[len("--- a/"):]
        out.append({"idx": index, "file": path, "text": "\\n".join(hunk["head"] + hunk["body"])})
    return message, out
'''

FAKE_UTILS_HEADER = "import definitely_not_installed_module_xyz\n"


@pytest.fixture
def fake_portgpt(tmp_path: Path) -> Path:
    """A minimal PortGPT-shaped tree whose utils import fails (forces extraction)."""
    root = tmp_path / "portgpt"
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "utils.py").write_text(FAKE_UTILS_HEADER + FAKE_SPLITTER, encoding="utf-8")
    return root


def _portgpt_hunks(patch_text: str) -> list[dict]:
    """PortGPT-shaped hunk records built from this agent's own inventory."""
    from patch_hunk_agent.patch_parser import PatchParser

    document = PatchParser().parse_document(patch_text)
    records = []
    for index, hunk in enumerate(document.hunks):
        records.append(
            {
                "idx": index,
                "file": hunk.file_path,
                "text": "\n".join([f"--- a/{hunk.file_path}", f"+++ b/{hunk.target_file_path}", hunk.diff_text]),
            }
        )
    return records


def test_alignment_passes_for_the_same_inventory():
    patch_text = fixture_path("simple_dependency").read_text(encoding="utf-8")
    hunks = fixture_hunks("simple_dependency")
    report = check_alignment(hunks, _portgpt_hunks(patch_text))
    assert report["aligned"] is True
    assert report["problems"] == []
    assert report["index_of"] == {"H1": 0, "H2": 1}


def test_alignment_reports_header_file_and_body_differences():
    patch_text = fixture_path("simple_dependency").read_text(encoding="utf-8")
    hunks = fixture_hunks("simple_dependency")
    theirs = _portgpt_hunks(patch_text)
    theirs[0]["file"] = "src/elsewhere.c"
    theirs[1]["text"] = theirs[1]["text"].replace("@@ -10,5 +18,6 @@", "@@ -99,5 +99,6 @@")
    theirs[1]["text"] = theirs[1]["text"] + "\ncontext line lost"
    report = check_alignment(hunks, theirs)
    assert report["aligned"] is False
    fields = {problem["field"] for problem in report["problems"]}
    assert {"file_path", "header", "body_line_count"} <= fields


def test_alignment_reports_count_mismatch_and_extra_hunks():
    hunks = fixture_hunks("simple_dependency")
    report = check_alignment(hunks, [])
    assert report["aligned"] is False
    assert report["problems"][0]["field"] == "hunk_count"
    assert sum(1 for problem in report["problems"] if problem["field"] == "extra_hunk") == 2


def test_spec_coverage_accepts_a_full_partition():
    spec = {"groups": [{"id": 1, "hunks": [0, 2]}, {"id": 2, "hunks": [1]}]}
    report = check_spec_coverage(spec, 3)
    assert report["valid"] is True
    assert report["assigned_count"] == 3


def test_spec_coverage_rejects_bad_specs():
    spec = {
        "groups": [
            {"id": 1, "hunks": [0, 0, 5, -1, "H2"]},
            {"id": 2, "hunks": []},
            "not-a-group",
        ]
    }
    report = check_spec_coverage(spec, 3)
    reasons = {problem["reason"] for problem in report["problems"]}
    assert report["valid"] is False
    assert "group is not an object" in reasons
    assert "group has no hunks" in reasons
    assert "hunk index is not an integer" in reasons
    assert "hunk index out of range" in reasons
    assert report["uncovered_hunks"] == [1, 2]


def test_spec_coverage_flags_duplicate_assignment():
    spec = {"groups": [{"id": 1, "hunks": [0]}, {"id": 2, "hunks": [0, 1]}]}
    report = check_spec_coverage(spec, 2)
    assert any(problem["reason"] == "hunk assigned to more than one group" for problem in report["problems"])


def test_build_group_spec_matches_portgpt_contract():
    hunks = fixture_hunks("simple_dependency")
    grouping = {
        "patch_id": "simple_dependency",
        "status": "complete",
        "groups": [
            {"group_id": "G1", "hunk_ids": ["H1", "H2"], "dependency_ids": ["D1"]},
        ],
    }
    spec = build_group_spec(
        grouping,
        hunks,
        dependencies=[dependency("H1", "H2")],
        patch_id="simple_dependency",
        model="unit-model",
        language="zh",
    )
    assert list(spec) [:2] == ["groups", "dependencies"]
    group = spec["groups"][0]
    assert group["id"] == 1
    assert group["hunks"] == [0, 1]
    assert group["hunk_ids"] == ["H1", "H2"]
    assert group["name"] and group["concern"] and group["why_together"]
    assert "clamp_value" in group["why_together"] or "helper" in group["why_together"]
    record = spec["dependencies"][0]
    assert record["from_hunk"] == 0 and record["to_hunk"] == 1
    assert record["category"] == "DEFINITION_USE"
    assert record["why"]
    assert spec["_patch"] == "simple_dependency"
    assert spec["_model"] == "unit-model"
    assert spec["notes"]
    assert DEFAULT_SPEC_FILENAME == "hunk_groups.json"


def test_build_group_spec_honours_an_explicit_index_map():
    hunks = fixture_hunks("simple_dependency")
    grouping = {"patch_id": "p", "groups": [{"group_id": "G1", "hunk_ids": ["H2", "H1"]}]}
    spec = build_group_spec(
        grouping, hunks, index_of={"H1": 7, "H2": 3}, patch_id="p", language="en"
    )
    assert spec["groups"][0]["hunks"] == [3, 7]


def test_build_group_spec_keeps_imported_wording():
    hunks = fixture_hunks("simple_dependency")
    imported = {
        "patch_id": "p",
        "groups": [
            {
                "group_id": "G1",
                "hunk_ids": ["H1", "H2"],
                "source_name": "原名称",
                "source_concern": "原关注点",
                "source_why": "原理由",
                "source_group_id": 4,
            }
        ],
        "source_dependencies": [],
    }
    spec = build_group_spec(imported, hunks, patch_id="p")
    assert spec["groups"][0]["name"] == "原名称"
    assert spec["groups"][0]["concern"] == "原关注点"
    assert spec["groups"][0]["why_together"] == "原理由"
    assert spec["groups"][0]["id"] == 1  # PortGPT ids stay 1..N


def test_spec_import_export_round_trip_is_lossless():
    hunks = fixture_hunks("simple_dependency")
    spec = {
        "groups": [
            {"id": 1, "name": "组一", "hunks": [0, 1], "concern": "关注", "why_together": "必须一起"}
        ],
        "dependencies": [{"from_hunk": 0, "to_hunk": 1, "why": "定义与使用"}],
        "_model": "some-model",
        "_patch": "p1",
    }
    imported = grouping_document_from_spec(spec, hunks)
    assert imported["groups"][0]["hunk_ids"] == ["H1", "H2"]
    assert imported["source_dependencies"][0]["why"] == "定义与使用"
    exported = build_group_spec(imported, hunks, patch_id="p1")
    assert exported["groups"][0]["name"] == "组一"
    assert exported["groups"][0]["hunks"] == [0, 1]
    assert exported["dependencies"][0]["why"] == "定义与使用"
    assert exported["dependencies"][0]["from_hunk"] == 0


def test_import_ignores_dependency_entries_with_unknown_indices():
    hunks = fixture_hunks("simple_dependency")
    spec = {"groups": [{"id": 1, "hunks": [0, 1]}], "dependencies": [{"from_hunk": 0, "to_hunk": 99}]}
    imported = grouping_document_from_spec(spec, hunks)
    assert imported["source_dependencies"] == []


def test_write_group_spec_round_trips_through_json(tmp_path):
    spec = {"groups": [{"id": 1, "name": "中文名", "hunks": [0]}], "notes": "备注"}
    path = write_group_spec(spec, tmp_path / "nested" / DEFAULT_SPEC_FILENAME)
    assert load_group_spec(path) == spec
    assert json.loads(path.read_text(encoding="utf-8"))["groups"][0]["name"] == "中文名"


def test_load_group_spec_rejects_other_json(tmp_path):
    path = tmp_path / "not-a-spec.json"
    path.write_text('{"dependencies": []}', encoding="utf-8")
    with pytest.raises(PortGPTBridgeError):
        load_group_spec(path)


def test_splitter_is_extracted_when_portgpt_imports_are_unavailable(fake_portgpt):
    info, message, hunks = split_patch_with_portgpt(
        fake_portgpt, fixture_path("simple_dependency").read_text(encoding="utf-8")
    )
    assert info.source == "extracted"
    assert info.origin.endswith("utils.py")
    assert len(hunks) == 2
    assert hunks[0]["file"] == "src/example.c"
    assert hunks[0]["text"].startswith("--- a/src/example.c")
    assert message == ""


def _two_group_spec() -> dict:
    return {
        "groups": [
            {"id": 7, "name": "第二组", "hunks": [1], "concern": "c2", "why_together": "w2"},
            {"id": 3, "name": "第一组", "hunks": [0, 1], "concern": "c1", "why_together": "w1"},
        ],
        "dependencies": [{"from_hunk": 0, "to_hunk": 1, "why": "d"}],
    }


def test_build_merged_hunks_concatenates_and_orders_by_min_member():
    hunks = fixture_hunks("simple_dependency")
    merged = build_merged_hunks(_two_group_spec(), hunks)
    # ordered by smallest member index, like PortGPT's group_patch
    assert [group["hunks"] for group in merged] == [[0, 1], [1]]
    assert merged[0]["gid"] == 3 and merged[0]["name"] == "第一组"
    assert merged[0]["concern"] == "c1" and merged[0]["why"] == "w1"
    first_text = merged_hunk_text(hunks[0])
    second_text = merged_hunk_text(hunks[1])
    assert merged[0]["text"] == first_text + "\n" + second_text + "\n"
    assert merged[1]["text"] == second_text + "\n"
    # files are listed per member hunk (duplicates included), matching PortGPT
    assert merged[0]["files"] == ["src/example.c", "src/example.c"]


def test_build_merged_hunks_prefers_portgpt_text():
    hunks = fixture_hunks("simple_dependency")
    portgpt_hunks = [
        {"idx": 0, "file": "src/example.c", "text": "AUTHORITATIVE-0"},
        {"idx": 1, "file": "src/example.c", "text": "AUTHORITATIVE-1"},
    ]
    merged = build_merged_hunks(_two_group_spec(), hunks, portgpt_hunks=portgpt_hunks)
    assert merged[0]["text"] == "AUTHORITATIVE-0\nAUTHORITATIVE-1\n"


def test_merged_hunk_text_rebuilds_markers():
    hunk = fixture_hunks("simple_dependency")[1]
    text = merged_hunk_text(hunk)
    lines = text.split("\n")
    assert lines[0] == "--- a/src/example.c"
    assert lines[1] == "+++ b/src/example.c"
    assert lines[2] == hunk["header"]
    assert f"+{hunk['added_lines'][0]}" in lines
    assert any(line.startswith(" ") for line in lines[3:])


def test_dump_merged_hunks_layout_matches_portgpt(tmp_path):
    hunks = fixture_hunks("simple_dependency")
    merged = build_merged_hunks(_two_group_spec(), hunks)
    original, ported = dump_merged_hunks(merged, tmp_path / "merged_hunks")
    assert original.name == "original" and ported.name == "ported"
    assert ported.is_dir()
    files = sorted(path.name for path in original.glob("*.patch"))
    assert files == ["merged-hunk-00.patch", "merged-hunk-01.patch"]
    assert (original / "merged-hunk-00.patch").read_text(encoding="utf-8") == merged[0]["text"]
    index = json.loads((original / "index.json").read_text(encoding="utf-8"))
    assert [item["file"] for item in index] == files
    assert index[0]["gid"] == 3 and index[0]["hunks"] == [0, 1]
    assert set(index[0]) == {"file", "gid", "name", "hunks", "concern", "why", "files"}
    summary = (tmp_path / "merged_hunks" / "index.md").read_text(encoding="utf-8")
    assert "merged hunk 0" in summary and "第一组" in summary
    assert "原始 hunk 编号：0, 1" in summary


def test_dump_merged_hunks_removes_stale_files_but_keeps_others(tmp_path):
    hunks = fixture_hunks("simple_dependency")
    target = tmp_path / "merged"
    (target / "original").mkdir(parents=True)
    (target / "original" / "notes.txt").write_text("keep me", encoding="utf-8")

    two_groups = build_merged_hunks(
        {"groups": [{"id": 1, "hunks": [0]}, {"id": 2, "hunks": [1]}]}, hunks
    )
    first_original, _ = dump_merged_hunks(two_groups, target)
    assert [path.name for path in first_original.glob("*.patch")] == [
        "merged-hunk-00.patch",
        "merged-hunk-01.patch",
    ]

    one_group = build_merged_hunks({"groups": [{"id": 1, "hunks": [0, 1]}]}, hunks)
    second_original, _ = dump_merged_hunks(one_group, target)

    # A leftover file would be read by PortGPT as an extra session input.
    assert [path.name for path in second_original.glob("*.patch")] == ["merged-hunk-00.patch"]
    assert json.loads((second_original / "index.json").read_text(encoding="utf-8"))[0]["hunks"] == [0, 1]
    assert (second_original / "notes.txt").read_text(encoding="utf-8") == "keep me"


def test_cli_writes_merged_hunks(fake_portgpt, tmp_path, capsys):
    from patch_hunk_agent.cli import main

    grouping = {
        "patch_id": "simple_dependency",
        "status": "complete",
        "groups": [{"group_id": "G1", "hunk_ids": ["H1", "H2"], "dependency_ids": ["D1"]}],
    }
    grouping_path = tmp_path / "grouping.json"
    grouping_path.write_text(json.dumps(grouping), encoding="utf-8")
    merged_dir = tmp_path / "merged"
    exit_code = main(
        [
            "portgpt-spec",
            "--patch",
            str(fixture_path("simple_dependency")),
            "--portgpt-root",
            str(fake_portgpt),
            "--grouping",
            str(grouping_path),
            "--output",
            str(tmp_path / "hunk_groups.json"),
            "--merged-hunks-dir",
            str(merged_dir),
            "--strict",
        ]
    )
    assert exit_code == 0
    assert (merged_dir / "original" / "merged-hunk-00.patch").is_file()
    assert "merged hunks" in capsys.readouterr().out


def test_missing_portgpt_tree_is_reported(tmp_path):
    with pytest.raises(PortGPTBridgeError):
        load_portgpt_splitter(tmp_path)


def test_fake_tree_alignment_matches_this_parser(fake_portgpt):
    patch_text = fixture_path("simple_dependency").read_text(encoding="utf-8")
    info, _message, hunks = split_patch_with_portgpt(fake_portgpt, patch_text)
    report = check_alignment(fixture_hunks("simple_dependency"), hunks)
    assert report["aligned"] is True


def test_cli_checks_an_existing_spec(fake_portgpt, tmp_path, capsys):
    from patch_hunk_agent.cli import main

    spec_path = tmp_path / "hunk_groups.json"
    spec_path.write_text(
        json.dumps({"groups": [{"id": 1, "hunks": [0], "name": "g1"}, {"id": 2, "hunks": [1]}]}),
        encoding="utf-8",
    )
    exit_code = main(
        [
            "portgpt-spec",
            "--patch",
            str(fixture_path("simple_dependency")),
            "--portgpt-root",
            str(fake_portgpt),
            "--check-spec",
            str(spec_path),
            "--strict",
        ]
    )
    assert exit_code == 0
    assert "covered   : 2/2" in capsys.readouterr().out


def test_cli_fails_a_strict_spec_check_on_a_gap(fake_portgpt, tmp_path, capsys):
    from patch_hunk_agent.cli import main

    spec_path = tmp_path / "hunk_groups.json"
    spec_path.write_text(json.dumps({"groups": [{"id": 1, "hunks": [0]}]}), encoding="utf-8")
    exit_code = main(
        [
            "portgpt-spec",
            "--patch",
            str(fixture_path("simple_dependency")),
            "--portgpt-root",
            str(fake_portgpt),
            "--check-spec",
            str(spec_path),
            "--strict",
        ]
    )
    assert exit_code == 1
    assert "uncovered hunk indices: [1]" in capsys.readouterr().out


def test_cli_builds_a_spec_from_a_grouping_document(fake_portgpt, tmp_path, capsys):
    from patch_hunk_agent.cli import main

    grouping = {
        "patch_id": "simple_dependency",
        "status": "complete",
        "groups": [{"group_id": "G1", "hunk_ids": ["H1", "H2"], "dependency_ids": ["D1"]}],
    }
    grouping_path = tmp_path / "grouping.json"
    grouping_path.write_text(json.dumps(grouping), encoding="utf-8")
    output = tmp_path / "hunk_groups.json"
    exit_code = main(
        [
            "portgpt-spec",
            "--patch",
            str(fixture_path("simple_dependency")),
            "--portgpt-root",
            str(fake_portgpt),
            "--grouping",
            str(grouping_path),
            "--output",
            str(output),
            "--language",
            "en",
            "--strict",
        ]
    )
    assert exit_code == 0
    spec = json.loads(output.read_text(encoding="utf-8"))
    assert spec["groups"][0]["hunks"] == [0, 1]
    assert spec["_patch"] == "simple_dependency"
    assert "spec coverage : 2/2" in capsys.readouterr().out


def test_cli_imports_a_spec_back_into_a_grouping(fake_portgpt, tmp_path, capsys):
    from patch_hunk_agent.cli import main

    spec_path = tmp_path / "hunk_groups.json"
    spec_path.write_text(
        json.dumps(
            {
                "groups": [{"id": 1, "name": "组一", "hunks": [0, 1], "concern": "c", "why_together": "w"}],
                "dependencies": [{"from_hunk": 0, "to_hunk": 1, "why": "定义与使用"}],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "imported.grouping.json"
    exit_code = main(
        [
            "portgpt-spec",
            "--patch",
            str(fixture_path("simple_dependency")),
            "--portgpt-root",
            str(fake_portgpt),
            "--from-spec",
            str(spec_path),
            "--grouping-output",
            str(output),
            "--strict",
        ]
    )
    assert exit_code == 0
    imported = json.loads(output.read_text(encoding="utf-8"))
    assert imported["groups"][0]["hunk_ids"] == ["H1", "H2"]
    assert imported["source_dependencies"][0]["why"] == "定义与使用"

    # A second pass through the CLI must keep the imported dependencies, and the
    # spec must credit the model that produced the grouping (not this exporter).
    spec_out = tmp_path / "round_trip.hunk_groups.json"
    code = main(
        [
            "portgpt-spec",
            "--patch",
            str(fixture_path("simple_dependency")),
            "--portgpt-root",
            str(fake_portgpt),
            "--grouping",
            str(output),
            "--output",
            str(spec_out),
            "--strict",
        ]
    )
    assert code == 0
    rebuilt = json.loads(spec_out.read_text(encoding="utf-8"))
    assert rebuilt["dependencies"][0]["why"] == "定义与使用"
    assert rebuilt["dependencies"][0]["from_hunk"] == 0
    assert rebuilt["groups"][0]["name"] == "组一"


def _discover_real_tree() -> tuple[Path | None, Path | None, list[str]]:
    """Locate a real PortGPT checkout and patch without failing silently.

    ``PORTGPT_ROOT`` / ``PORTGPT_PATCH`` win when set - and when they are set but
    invalid that is a hard error, because an explicit request must never turn into
    a skipped test.  Without them a few conventional locations are probed (parent
    directory of the repository first, so a copied checkout keeps working).
    """
    notes: list[str] = []
    parents = [ROOT, ROOT.parent]
    tree_candidates = [ROOT.parent / "portgpt-src-modified", ROOT / "portgpt-src-modified"]
    tree_candidates += [parent / "backport_patch" / "portgpt-src-modified" for parent in parents]
    tree_candidates.append(Path(r"C:\Users\wangnc\Desktop\backport_patch\portgpt-src-modified"))
    patch_candidates = [ROOT.parent / "48649-AOSP16-fix.patch", ROOT / "48649-AOSP16-fix.patch"]
    patch_candidates += [parent / "backport_patch" / "48649-AOSP16-fix.patch" for parent in parents]
    patch_candidates.append(
        Path(r"C:\Users\wangnc\Desktop\backport_patch\CVE-2025-48649-gpt5.5\1-AOSP16-fix.patch")
    )
    patch_candidates.append(Path(r"C:\Users\wangnc\Desktop\backport_patch\48649-AOSP16-fix.patch"))

    env_tree = os.environ.get("PORTGPT_ROOT")
    env_patch = os.environ.get("PORTGPT_PATCH")
    if env_tree:
        candidate = Path(env_tree)
        if not (candidate / "tools" / "utils.py").is_file():
            raise AssertionError(
                f"PORTGPT_ROOT={env_tree} does not point at a PortGPT checkout "
                "(tools/utils.py missing); refusing to skip silently"
            )
        tree = candidate
    else:
        tree = next((path for path in tree_candidates if (path / "tools" / "utils.py").is_file()), None)
        if tree is None:
            notes.append(
                "no PortGPT checkout found (set PORTGPT_ROOT to exercise the alignment and "
                "byte-parity tests): " + ", ".join(str(path) for path in tree_candidates[:4])
            )
    if env_patch:
        candidate = Path(env_patch)
        if not candidate.is_file():
            raise AssertionError(f"PORTGPT_PATCH={env_patch} does not exist; refusing to skip silently")
        patch = candidate
    else:
        patch = next((path for path in patch_candidates if path.is_file()), None)
        if patch is None:
            notes.append("no reference patch found (set PORTGPT_PATCH)")
    return tree, patch, notes


REAL_TREE, REAL_PATCH, DISCOVERY_NOTES = _discover_real_tree()


def test_real_tree_availability_is_reported():
    """A missing checkout must be visible in the run, not a silent skip."""
    if DISCOVERY_NOTES:
        import warnings

        warnings.warn(
            "real-PortGPT integration tests are SKIPPED -> " + "; ".join(DISCOVERY_NOTES),
            UserWarning,
            stacklevel=2,
        )
    else:
        assert REAL_TREE is not None and REAL_PATCH is not None


@pytest.mark.skipif(
    not (REAL_TREE.is_dir() and REAL_PATCH.is_file()),
    reason="set PORTGPT_ROOT and PORTGPT_PATCH to run against a real PortGPT checkout",
)
def test_real_portgpt_tree_round_trip():
    from patch_hunk_agent.patch_parser import PatchParser

    patch_text = REAL_PATCH.read_text(encoding="utf-8", errors="replace")
    hunks = PatchParser().parse_document(patch_text, patch_id=REAL_PATCH.stem).hunk_dicts()
    info, _message, portgpt_hunks = split_patch_with_portgpt(REAL_TREE, patch_text)
    assert portgpt_hunks, "PortGPT splitter returned no hunks"
    report = check_alignment(hunks, portgpt_hunks)
    assert report["aligned"] is True, report["problems"]

    grouping = {
        "patch_id": REAL_PATCH.stem,
        "status": "complete",
        "groups": [
            {"group_id": "G1", "hunk_ids": ["H1", "H2"]},
            {"group_id": "G2", "hunk_ids": [f"H{i}" for i in range(3, len(hunks) + 1)]},
        ],
    }
    spec = build_group_spec(
        grouping,
        hunks,
        index_of=report["index_of"],
        patch_id=REAL_PATCH.stem,
        language="zh",
    )
    coverage = check_spec_coverage(spec, len(portgpt_hunks))
    assert coverage["valid"] is True
    assert spec["groups"][0]["hunks"] == [0, 1]
    assert info.source in ("import", "extracted")


def test_real_portgpt_consumer_reassembles_the_hunks():
    """Use PortGPT's own group_patch to prove merged hunks survive the bridge."""
    if not (REAL_TREE.is_dir() and REAL_PATCH.is_file()):
        pytest.skip("set PORTGPT_ROOT and PORTGPT_PATCH to run against a real PortGPT checkout")
    import ast
    import logging
    import re as _re

    utils_path = REAL_TREE / "tools" / "utils.py"
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(utils_path.read_text(encoding="utf-8"), filename=str(utils_path))
    wanted = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in {"split_patch_indexed", "group_patch"}
    ]
    if len(wanted) < 2:
        pytest.skip("group_patch not found in the PortGPT checkout")
    namespace = {
        "__name__": "portgpt_consumer",
        "logger": logging.getLogger("portgpt.consumer"),
        "re": _re,
        "json": json,
        "os": os,
    }
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        compiled = compile(ast.Module(body=wanted, type_ignores=[]), "portgpt_consumer", "exec")
    exec(compiled, namespace)

    patch_text = REAL_PATCH.read_text(encoding="utf-8", errors="replace")
    hunks = fixture_hunks_for_text(patch_text)
    _message, portgpt_hunks = namespace["split_patch_indexed"](patch_text, True)
    alignment = check_alignment(hunks, portgpt_hunks)
    grouping = {
        "patch_id": "real",
        "groups": [
            {"group_id": f"G{i}", "hunk_ids": [hunk["hunk_id"]]} for i, hunk in enumerate(hunks, 1)
        ],
    }
    spec = build_group_spec(
        grouping, hunks, index_of=alignment["index_of"], patch_id="real", language="zh"
    )
    _message2, merged = namespace["group_patch"](patch_text, spec, True)
    by_index = {hunk["idx"]: hunk for hunk in portgpt_hunks}
    expected = {
        tuple(sorted(group["hunks"])): "\n".join(by_index[i]["text"] for i in sorted(group["hunks"]))
        + "\n"
        for group in spec["groups"]
    }
    assert {tuple(item["hunks"]): item["text"] for item in merged} == expected


def fixture_hunks_for_text(patch_text: str) -> list[dict]:
    """Helper used by the real-tree parity test."""
    from patch_hunk_agent.evidence_extractor import enrich_hunks
    from patch_hunk_agent.patch_parser import PatchParser

    return enrich_hunks(PatchParser().parse_document(patch_text, patch_id="real").hunk_dicts())


def test_coordination_directives_reach_portgpts_prompt_channel():
    """`why` is what PortGPT's describe_group() renders, so directives must land there."""
    hunks = fixture_hunks("simple_dependency")
    spec = {
        "groups": [
            {
                "id": 1,
                "group_id": "G1",
                "name": "a",
                "hunks": [0],
                "concern": "c",
                "why_together": "reason",
            },
            {
                "id": 2,
                "group_id": "G2",
                "name": "b",
                "hunks": [1],
                "concern": "c",
                "why_together": "reason",
            },
        ]
    }
    merged = build_merged_hunks(
        spec,
        hunks,
        order=["G2", "G1"],
        coordination={"G1": ["DEFERRED_CONSTRAINT D1 prefers G2 first"]},
    )
    assert [group["group_id"] for group in merged] == ["G2", "G1"]
    assert merged[1]["why"].endswith("DEFERRED_CONSTRAINT D1 prefers G2 first")
    assert merged[1]["coordination"] == ["DEFERRED_CONSTRAINT D1 prefers G2 first"]
    assert merged[0]["why"] == "reason"


@pytest.mark.skipif(
    not (REAL_TREE.is_dir() and REAL_PATCH.is_file()),
    reason="set PORTGPT_ROOT and PORTGPT_PATCH to run against a real PortGPT checkout",
)
def test_merged_hunk_files_are_byte_identical_to_portgpts_own_dump(tmp_path):
    """The strongest integration claim: same grouping in, same bytes out."""
    import ast
    import logging
    import re as _re
    import tempfile
    import warnings

    utils_path = REAL_TREE / "tools" / "utils.py"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(utils_path.read_text(encoding="utf-8"), filename=str(utils_path))
    wanted = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"split_patch_indexed", "group_patch", "dump_merged_hunks"}
    ]
    if len(wanted) < 3:
        pytest.skip("PortGPT checkout lacks the grouping helpers")
    namespace = {
        "__name__": "portgpt_producer",
        "logger": logging.getLogger("portgpt.producer"),
        "re": _re,
        "json": json,
        "os": os,
    }
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        compiled = compile(
            ast.Module(body=wanted, type_ignores=[]), "portgpt_producer", "exec"
        )
    exec(compiled, namespace)

    patch_text = REAL_PATCH.read_text(encoding="utf-8", errors="replace")
    hunks = fixture_hunks_for_text(patch_text)
    alignment = check_alignment(hunks, namespace["split_patch_indexed"](patch_text, True)[1])
    assert alignment["aligned"] is True, alignment["problems"]

    sibling_spec = REAL_PATCH.parent / DEFAULT_SPEC_FILENAME
    if sibling_spec.is_file():
        spec = load_group_spec(sibling_spec)  # the reviewed grouping, if it is there
    else:
        spec = build_group_spec(
            {
                "patch_id": "real",
                "groups": [
                    {
                        "group_id": "G1",
                        "hunk_ids": [hunk["hunk_id"] for hunk in hunks[:2]],
                    },
                    {
                        "group_id": "G2",
                        "hunk_ids": [hunk["hunk_id"] for hunk in hunks[2:]],
                    },
                ],
            },
            hunks,
            index_of=alignment["index_of"],
            patch_id="real",
            language="zh",
        )

    mine_dir = tmp_path / "mine"
    my_original, _my_ported = dump_merged_hunks(
        build_merged_hunks(
            spec,
            hunks,
            portgpt_hunks=namespace["split_patch_indexed"](patch_text, True)[1],
            index_of=alignment["index_of"],
        ),
        mine_dir,
    )
    theirs_root = Path(tempfile.mkdtemp(prefix="pha-pg-parity-"))
    their_original = Path(
        namespace["dump_merged_hunks"](
            namespace["group_patch"](patch_text, spec, True)[1], str(theirs_root)
        )[0]
    )

    my_files = sorted(path.name for path in my_original.glob("*.patch"))
    their_files = sorted(path.name for path in their_original.glob("*.patch"))
    assert my_files == their_files and my_files
    for name in my_files:
        assert (my_original / name).read_bytes() == (their_original / name).read_bytes(), name
    assert (my_original / "index.json").read_bytes() == (their_original / "index.json").read_bytes()
    assert (mine_dir / "index.md").read_bytes() == (theirs_root / "index.md").read_bytes()
