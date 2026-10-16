"""Read-only git access, exercised against a throwaway repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from patch_hunk_agent.context_provider import CodeContextProvider, GitRepoAccess
from patch_hunk_agent.utils import git_utils

pytestmark = pytest.mark.skipif(not git_utils.git_available(), reason="git is not installed")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "tests@example.invalid")
    _git(work, "config", "user.name", "Tests")
    (work / "src").mkdir()
    (work / "src" / "example.c").write_text(
        "#include <stdio.h>\n\nstatic int existing_helper(int x)\n{\n    return x + 1;\n}\n",
        encoding="utf-8",
    )
    _git(work, "add", ".")
    _git(work, "commit", "-m", "initial")
    (work / "src" / "example.c").write_text(
        "#include <stdio.h>\n\nstatic int existing_helper(int x)\n{\n    return x + 2;\n}\n",
        encoding="utf-8",
    )
    _git(work, "commit", "-am", "bump helper")
    return work


@pytest.fixture
def revisions(repo: Path) -> tuple[str, str]:
    head = git_utils.run_git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    parent = git_utils.run_git(["rev-parse", "HEAD~1"], cwd=repo).stdout.strip()
    return parent, head


def test_repository_detection(repo: Path, tmp_path: Path):
    assert git_utils.is_git_repo(repo) is True
    assert git_utils.is_git_repo(tmp_path / "nope") is False


def test_rev_parse_and_show(repo: Path, revisions):
    first, second = revisions
    assert git_utils.git_rev_parse(repo, first) == first
    assert git_utils.git_rev_parse(repo, "does-not-exist") is None
    assert "return x + 1;" in (git_utils.git_show(repo, first, "src/example.c") or "")
    assert "return x + 2;" in (git_utils.git_show(repo, second, "src/example.c") or "")
    assert git_utils.git_show(repo, second, "src/missing.c") is None
    assert git_utils.git_file_exists(repo, second, "src/example.c") is True
    assert git_utils.git_file_exists(repo, second, "src/missing.c") is False


def test_grep_finds_matches_and_reports_no_match(repo: Path, revisions):
    first, _ = revisions
    matches, error = git_utils.git_grep(repo, first, r"\bexisting_helper\b")
    assert error is None
    assert matches and matches[0]["file_path"] == "src/example.c"
    assert matches[0]["line"] == 3
    empty, empty_error = git_utils.git_grep(repo, first, r"\bnot_present_anywhere\b")
    assert empty == [] and empty_error is None


def test_non_read_only_subcommands_are_refused(repo: Path):
    with pytest.raises(ValueError):
        git_utils.run_git(["commit", "-m", "nope"], cwd=repo)
    with pytest.raises(ValueError):
        git_utils.run_git(["push"], cwd=repo)


def test_missing_executable_is_reported(tmp_path: Path):
    result = git_utils.run_git(["rev-parse", "HEAD"], cwd=tmp_path / "not-a-repo")
    assert result.ok is False
    assert result.returncode != 0


def test_git_repo_access_reads_revisions(repo: Path, revisions):
    first, second = revisions
    access = GitRepoAccess(repo)
    assert access.available() is True
    assert access.supports_revision(first) is True
    assert access.supports_revision(None) is False
    assert "return x + 1;" in (access.read_file(first, "src/example.c") or "")
    hits, error = access.symbol_hits(second, "existing_helper", max_results=10)
    assert error is None and hits
    assert access.symbol_hits(None, "existing_helper")[0] == []


def test_context_provider_over_a_git_repository(repo: Path, revisions):
    first, second = revisions
    access = GitRepoAccess(repo)
    provider = CodeContextProvider(source_access=access, target_access=access)
    hunk = {
        "hunk_id": "H1",
        "file_path": "src/example.c",
        "target_file_path": "src/example.c",
        "old_start": 5,
        "old_count": 1,
        "new_start": 5,
        "new_count": 2,
        "section": "existing_helper",
        "added_lines": ["+    return x + 3;"],
        "removed_lines": ["-    return x + 1;"],
        "context_lines": ["{"],
        "symbols": [
            {
                "name": "existing_helper",
                "role": "definition",
                "kind": "function",
                "use_kind": None,
                "line": 3,
                "confidence": "high",
            }
        ],
    }
    record = provider.get_context(hunk, first, second)
    assert record["retrieval_status"] == "complete"
    assert "return x + 1;" in record["source_context"]["code"]
    assert record["target_symbol_status"]["existing_helper"] == "present"
    follow_up = provider.additional_evidence(
        {"symbol": "existing_helper", "side": "target", "revision": second}
    )
    assert follow_up["status"] == "ok" and follow_up["matches"]
