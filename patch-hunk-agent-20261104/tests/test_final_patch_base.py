"""Full-patch validation must never stack a candidate on cumulative changes."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

from git import Repo

SRC = (Path(__file__).resolve().parents[1] / 'data_ncwang/backport_patch/tools/patch-backporting/src')
sys.path.insert(0, str(SRC))
from agent.invoke_llm import rebuild_from_candidate
from tools.project import Project


class FinalPatchBaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = Repo.init(self.root)
        with self.repo.config_writer() as config:
            config.set_value('user', 'name', 'test')
            config.set_value('user', 'email', 'test@example.invalid')
        self.source = self.root / 'Example.java'
        self.source.write_text('class Example {\n}\n')
        self.repo.index.add(['Example.java'])
        self.repo.index.commit('base')
        self.base = self.repo.head.commit.hexsha
        self.repo.git.checkout('-b', 'cumulative')
        self.source.write_text('class Example {\n    void repaired() { call(1); }\n}\n')
        self.repo.index.add(['Example.java'])
        self.repo.index.commit('group')
        self.branch_head = self.repo.head.commit.hexsha
        self.full = self.repo.git.diff(self.base, 'cumulative')
        self.source.write_text(self.source.read_text().replace('call(1)', 'call(2)'))
        self.fragment = self.repo.git.diff('cumulative')
        self.repo.git.reset('--hard')
        (self.root / 'build.sh').write_text(
            'python3 - <<\'CHECK\'\nfrom pathlib import Path\n'
            'assert Path("Example.java").read_text().count("void repaired()") == 1\n'
            'with Path("build-calls.log").open("a") as f: f.write("build\\n")\nCHECK\n')
        data = SimpleNamespace(project_url='local-test', project_dir=str(self.root),
                               error_message='', new_patch_parent=self.base,
                               target_release='cumulative')
        self.project = Project(data)
        self.project.full_patch_base = self.base
        self.project.all_hunks_applied_succeeded = True

    def test_full_patch_uses_original_base_even_when_model_supplies_cumulative_ref(self):
        self.project._validate('cumulative', self.full)  # GitPython diff has no final newline.
        self.assertTrue(self.project.poc_succeeded)
        self.assertEqual(self.source.read_text().count('void repaired()'), 1)
        self.assertEqual(self.repo.head.commit.hexsha, self.base)
        self.assertEqual(self.repo.git.rev_parse('cumulative'), self.branch_head)

    def test_delta_on_cumulative_branch_is_rejected_without_context_rewriting(self):
        self.project.context_mismatch_times = 100
        result = self.project._validate('cumulative', self.fragment)
        self.assertIn('APPLY_FAILED', result)
        self.assertFalse(self.project.poc_succeeded)
        self.assertEqual(self.source.read_text(), 'class Example {\n}\n')
        self.assertFalse((self.root / 'build-calls.log').exists())

    def test_failed_candidate_does_not_inherit_previous_validation_success(self):
        self.project._validate('cumulative', self.full)
        self.assertTrue(self.project.poc_succeeded)
        result = self.project._validate('cumulative', self.fragment)
        self.assertIn('APPLY_FAILED', result)
        self.assertFalse(self.project.compile_succeeded)
        self.assertFalse(self.project.testcase_succeeded)
        self.assertFalse(self.project.poc_succeeded)
        self.assertEqual((self.root / 'build-calls.log').read_text(), 'build\n')

    def test_missing_original_base_does_not_fall_back_to_target_branch(self):
        self.project.full_patch_base = 'nonexistent-original-base'
        result = self.project._validate('cumulative', self.full)
        self.assertIn('VALIDATION_BASE_ERROR', result)
        self.assertFalse(self.project.compile_succeeded)
        self.assertFalse((self.root / 'build-calls.log').exists())

    def test_new_files_are_in_checkpoint_and_removed_before_next_attempt(self):
        new_file = ('diff --git a/Extra.java b/Extra.java\nnew file mode 100644\n'
                    '--- /dev/null\n+++ b/Extra.java\n@@ -0,0 +1 @@\n+class Extra {}\n')
        self.project._validate('cumulative', self.full + '\n' + new_file)
        self.assertTrue(self.project.poc_succeeded)
        self.project.checkpoint_dir = str(self.root / 'checkpoints')
        self.project.persist_validated_patch(self.full + '\n' + new_file, 'cumulative')
        checkpoint = (self.root / 'checkpoints/latest-validated.patch').read_text()
        self.assertIn('+++ b/Extra.java', checkpoint)
        self.project._validate('cumulative', self.full)
        self.assertTrue(self.project.poc_succeeded)
        self.assertFalse((self.root / 'Extra.java').exists())
        self.assertEqual((self.root / 'build-calls.log').read_text(), 'build\nbuild\n')

    def test_rebuild_does_not_apply_a_fragment_to_the_cumulative_branch(self):
        result = rebuild_from_candidate(self.project, self.base, 'cumulative', self.fragment)
        self.assertIsNone(result)
        self.assertEqual(self.repo.head.commit.hexsha, self.branch_head)
        self.assertIn('call(1)', self.source.read_text())
        self.assertFalse(self.repo.git.diff())

    def test_full_candidate_rebuild_is_repeatable_and_keeps_final_newline(self):
        for _ in range(2):
            result = rebuild_from_candidate(self.project, self.base, 'cumulative', self.full)
            self.assertIsNotNone(result)
            self.assertTrue(result.endswith('\n'))
            self.assertEqual(result.count('+    void repaired()'), 1)
            self.assertEqual(self.repo.head.commit.hexsha, self.branch_head)


if __name__ == '__main__':
    unittest.main()
