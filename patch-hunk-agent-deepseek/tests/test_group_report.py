import io
import json
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


SRC = (Path(__file__).resolve().parents[1] / "data_ncwang/backport_patch/tools/"
       "patch-backporting/src")
sys.path.insert(0, str(SRC))

from tools.group_report import GroupComparison


def group(number):
    return {
        "gid": "group-%d" % number,
        "name": "组 %d" % number,
        "hunks": [number * 2, number * 2 + 1],
        "files": ["input-%d.java" % number],
        "text": "-old_%d\n+new_%d\n" % (number, number),
        "concern": "共享调用链",
        "hints": "原始摘要：保留校验行为。",
    }


class GroupComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.stream = io.StringIO()
        self.logger = logging.Logger("group-report-test", level=logging.INFO)
        self.logger.addHandler(logging.StreamHandler(self.stream))

    def read_report(self):
        return (self.directory / "group-comparison.md").read_text()

    def test_original_ids_and_inline_patches_are_kept_without_metadata(self):
        original = group(7)
        original["text"] = "+// ```example```\n"
        report = GroupComparison(self.directory, [(7, original)], self.logger)
        original["hunks"].clear()
        report.record(7, "+discarded_candidate")
        report.record(7, "+retained_candidate")
        text = self.read_report()
        self.assertIn("Group 07 — hunks [14, 15]", text)
        self.assertIn("````diff\n+// ```example```\n````", text)
        self.assertIn("+retained_candidate", text)
        for omitted in ("discarded_candidate", "共享调用链", "原始摘要", "input-7.java"):
            self.assertNotIn(omitted, text)

    def test_old_report_is_replaced_without_reusing_patch_files(self):
        (self.directory / "merged-hunk-01.patch").write_text("+stale_patch")
        report = GroupComparison(self.directory, [(0, group(0)), (4, group(4))], self.logger)
        report.record(0, "+old_output")
        GroupComparison(self.directory, [(2, group(2))], self.logger)
        text = self.read_report()
        self.assertIn("Group 02 — hunks [4, 5]", text)
        self.assertIn("尚未完成", text)
        self.assertNotIn("stale_patch", text)
        self.assertNotIn("old_output", text)
        self.assertNotIn("Group 04", text)

    def test_write_failure_preserves_existing_file_and_cleans_temporary_file(self):
        (self.directory / "group-comparison.md").write_text("previous report")
        with patch("tools.group_report.os.replace", side_effect=OSError("disk full")):
            GroupComparison(self.directory, [(0, group(0))], self.logger)
        self.assertEqual(self.read_report(), "previous report")
        self.assertIn("Failed to write group comparison", self.stream.getvalue())
        self.assertFalse(list(self.directory.glob(".group-comparison-*")))

    def test_backport_pairs_direct_skipped_failed_groups_and_preserves_status_files(self):
        from agent import invoke_llm as runtime

        groups = [group(i) for i in range(3)]
        for i, item in enumerate(groups):
            item["hunks"] = [i]
        project = SimpleNamespace(
            repo=MagicMock(), dir=str(self.directory), succeeded_patches=[], round_succeeded=False,
            _get_patch=lambda _: "upstream fixture",
        )

        def apply(ref, text, revise):
            project.round_succeeded = text == groups[0]["text"]
            if project.round_succeeded:
                project.succeeded_patches.append("+direct_actual_output\n")
            return "apply result"

        def invoke(payload, callbacks):
            project.round_succeeded = payload["group_ids"] == [1]

        project._apply_hunk = apply
        executor = SimpleNamespace(invoke=invoke)
        data = SimpleNamespace(new_patch="source", new_patch_parent="parent",
                               target_release="target", project_url="local-fixture",
                               patch_dataset_dir=str(self.directory) + "/")
        hunks = [{"idx": i, "file": g["files"][0], "text": g["text"]}
                 for i, g in enumerate(groups)]
        with patch.object(runtime, "logger", self.logger), \
                patch.object(runtime, "group_patch", return_value=("", groups)), \
                patch.object(runtime, "split_patch_indexed", return_value=("", hunks)), \
                patch.object(runtime, "CoverageReviewer", side_effect=StopIteration('report boundary')):
            with self.assertRaises(StopIteration):
                runtime.do_backport(executor, project, data, None,
                                    str(self.directory / "runtime.log"))
        report_path = self.directory / "merged_hunks/ported/group-comparison.md"
        text = report_path.read_text()
        for i in range(3):
            self.assertIn("Group %02d — hunks [%d]" % (i, i), text)
        self.assertIn("+direct_actual_output", text)
        self.assertIn("无需移植", text)
        self.assertIn("未产出补丁", text)
        statuses = [json.loads((report_path.parent / ("merged-hunk-%02d.status.json" % i)).read_text())
                    for i in range(3)]
        self.assertTrue(statuses[0]["round_succeeded"])
        self.assertTrue(statuses[1]["need_not_ported"])
        self.assertFalse(statuses[2]["round_succeeded"])
        self.assertEqual(self.stream.getvalue().count("[GROUP COMPARISON]"), 3)

    def test_cumulative_report_uses_actual_commit_not_revision_history(self):
        from git import Repo
        from agent import invoke_llm as runtime

        repo = Repo.init(self.directory / "target")
        with repo.config_writer() as config:
            config.set_value("user", "name", "test")
            config.set_value("user", "email", "test@example.invalid")
        source = Path(repo.working_tree_dir) / "Example.java"
        source.write_text("class Example {\n}\n")
        repo.index.add(["Example.java"])
        repo.index.commit("base")
        base = repo.head.commit.hexsha
        repo.git.checkout("-b", "cumulative")
        original = group(0)
        original["hunks"] = [0]
        original["text"] = "+void repaired() {}\n"
        project = SimpleNamespace(repo=repo, dir=repo.working_tree_dir, succeeded_patches=[], round_succeeded=False,
                                  _get_patch=lambda _: "upstream fixture",
                                  _apply_hunk=lambda *args: "context mismatch")

        def invoke(payload, callbacks):
            source.write_text("class Example {\n    void repaired() {}\n}\n")
            project.succeeded_patches.extend(["+discarded_revision\n", "+candidate_history\n"])
            project.round_succeeded = True

        data = SimpleNamespace(new_patch="source", new_patch_parent="parent",
                               target_release="cumulative", project_url="local-fixture",
                               cumulative=True, cum_base=base,
                               patch_dataset_dir=str(self.directory) + "/")
        hunks = [{"idx": 0, "file": "Example.java", "text": original["text"]}]
        with patch.object(runtime, "logger", self.logger), \
                patch.object(runtime, "group_patch", return_value=("", [original])), \
                patch.object(runtime, "split_patch_indexed", return_value=("", hunks)), \
                patch.object(runtime, "CoverageReviewer", side_effect=StopIteration('report boundary')), \
                patch.dict("os.environ", {"PG_COMPILE_PER_GROUP": "0"}):
            with self.assertRaises(StopIteration):
                runtime.do_backport(SimpleNamespace(invoke=invoke), project, data, None,
                                    str(self.directory / "runtime.log"))
        report_path = self.directory / "merged_hunks/ported/group-comparison.md"
        after = report_path.read_text().split("### 移植后", 1)[1]
        self.assertEqual(after.count("void repaired()"), 1)
        self.assertIn(repo.git.diff(base, "cumulative"), after)
        self.assertNotIn("discarded_revision", after)
        self.assertNotIn("candidate_history", after)
        self.assertNotEqual(repo.head.commit.hexsha, base)


if __name__ == "__main__":
    unittest.main()
